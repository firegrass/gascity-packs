#!/usr/bin/env python3
"""Trusted evaluator called by the Go helper inside its native transaction.

stdin/stdout is one JSON request/result. The helper supplies its own clock and
snapshot; a caller cannot supply either. A refusal commits no changes.
"""
import copy
from datetime import timedelta
import json
import math
import sys

import proof_work_policy as p
from proof_work_records import (SCHEMA, Corpus, Refused, bead_id, decode, digest,
                                encode, relationships, semantic, validate_corpus)


def evaluate(data):
    request, state = data['request'], data['state']
    now = p.parse_timestamp(data['now'])
    if now is None:
        raise Refused('helper clock unavailable')
    action = request.get('action')
    rows = [r for issue in (data['issues'] or []) if (r := decode(issue)) is not None]
    if action == 'import':
        if state or rows:
            raise Refused('import requires an empty dedicated workspace')
        rows = request['records']
        corpus = validate_corpus(rows, now)
        source = request.get('source_sha')
        if any(r['source_sha'] != source for r in rows):
            raise Refused('mixed import revisions')
        state = dict(schema=SCHEMA, epoch=request['epoch'], mode='shadow', revision=0, source_sha=source,
                     reservations_reconciled=request.get('reservations_reconciled', False),
                     reservation_digest=request.get('reservation_digest'),
                     project_ids=sorted(row['id'] for row in rows))
        return dict(state=state, writes=[encode(r) for r in rows], edges=relationships(rows),
                    result=dict(records=len(rows), source_sha=source, mode='shadow',
                                frontier=p.frontier(corpus, now)), events=[])
    if not state or state.get('schema') != SCHEMA or state.get('epoch') != request.get('epoch'):
        raise Refused('missing or mismatched authority epoch')
    if sorted(row['id'] for row in rows) != state.get('project_ids'):
        raise Refused('project inventory drift (missing, added or moved to wisp tier)')
    corpus = validate_corpus(rows, now)
    actual_edges = {(e['issue_id'], e['depends_on_id'], e['type']) for e in (data['edges'] or [])}
    expected_edges = {(e['issue_id'], e['depends_on_id'], e['type']) for e in relationships(rows)}
    if actual_edges != expected_edges:
        raise Refused('native relationship/PROOF drift')
    if action == 'snapshot':
        return dict(result=dict(schema=SCHEMA, **{k: v for k, v in state.items() if k != 'schema'},
                                generated_at=data['now'], records=rows, frontier=p.frontier(corpus, now),
                                native_records=[i for i in (data['issues'] or []) if i.get('external_ref') in corpus.by_id]))
    # Live writes remain impossible until the complete retention/client/cutover
    # qualification is installed. Shadow mutations exercise the exact policies.
    if state.get('mode') != 'shadow':
        raise Refused('live mutation qualification not installed')
    by_id = {r['id']: r for r in rows}
    rid = request.get('id')
    row = by_id.get(rid)
    if row is None:
        raise Refused('unknown work identity')
    record = corpus.by_id[rid]
    before = copy.deepcopy(row)
    fm = row['fields']
    actor = request.get('actor')
    if not isinstance(actor, str) or not actor.strip():
        raise Refused('mutation requires actor')
    changed = [row]
    authored = False
    if action == 'claim':
        if record.doc_type != 'backlog' or not p.is_ready(corpus, record, now):
            raise Refused('work is not ready')
        repos = request.get('repos', [])
        if not isinstance(repos, list) or any(not isinstance(repo, str) or not repo for repo in repos):
            raise Refused('explicit repository reservations must be a list')
        scopes = p.write_scopes(record) or [(repo, '') for repo in repos]
        for other in corpus.items:
            other_row = by_id[p.record_id(other)]
            other_scopes = p.write_scopes(other) or [tuple(scope) for scope in other_row.get('reserved_scopes', [])]
            if p.record_id(other) != rid and p.claim(other, now)[0] == 'live' and p.claims_collide(scopes, other_scopes):
                raise Refused('scope overlaps live claim: ' + p.record_id(other))
        row['reserved_scopes'] = [list(scope) for scope in scopes]
        fm.update(status='working', claimed_by=actor, claimed_at=data['now'], claim_expires=expiry(request, now))
        row['lease_id'] = digest({'id': rid, 'operation_id': request['operation_id']})
        epic = corpus.epic(fm.get('parent_epic'))
        if epic is not None and epic.status == 'open':
            parent = by_id[p.record_id(epic)]
            parent['fields']['status'] = 'working'
            if parent['fields'].get('owner') in (None, '', 'unassigned'):
                parent['fields']['owner'] = actor
            parent['semantic_digest'] = digest(semantic(parent['fields'], parent['body']))
            parent['authored_revision'] += 1
            changed.append(parent)
    elif action in ('renew', 'release'):
        check_lease(row, request, now, require_live=action == 'renew')
        if action == 'renew':
            fm['claim_expires'] = expiry(request, now)
        else:
            fm['status'] = 'open'
            for key in p.CLAIM_KEYS:
                fm.pop(key, None)
            row.pop('lease_id', None)
            row.pop('reserved_scopes', None)
    elif action == 'edit':
        if request.get('expected_authored_revision') != row['authored_revision']:
            raise Refused('authored revision changed')
        if any(k in fm for k in p.CLAIM_KEYS):
            raise Refused('release retained claim before editing contract')
        updates = request.get('fields', {})
        forbidden = {'status', 'created', 'doc_type', 'epic_id', 'milestone_id', 'done_at', *p.CLAIM_KEYS}
        if not isinstance(updates, dict) or forbidden & updates.keys():
            raise Refused('edit cannot change lifecycle or identity')
        if set(updates) - set(fm):
            raise Refused('new fields require schema registration')
        if any(type(value) is not type(fm[key]) for key, value in updates.items()):
            raise Refused('edit cannot change a registered field type')
        fm.update(updates)
        if 'body' in request:
            row['body'] = request['body']
        authored = True
    else:
        raise Refused('unsupported checked action: ' + str(action))
    fm['last_reviewed'] = now.strftime('%Y-%m-%d')
    row['operational_revision'] += 1
    if authored:
        row['authored_revision'] += 1
    row['semantic_digest'] = digest(semantic(fm, row['body']))
    validate_corpus(rows, now)
    state['revision'] += 1
    events = [dict(kind='revision' if authored else action, before=before, after=copy.deepcopy(row),
                   actor=actor, at=data['now'], operation_id=request['operation_id'])]
    # Every changed parent contract is retained as an immutable full revision too.
    events += [dict(kind='revision', after=copy.deepcopy(r), actor=actor, at=data['now'],
                    operation_id=request['operation_id']) for r in changed[1:]]
    result = {key: row[key] for key in ('id', 'authored_revision', 'operational_revision', 'semantic_digest')}
    result.update(bead_id=bead_id(rid), status=fm['status'], lease_id=row.get('lease_id'),
                  epoch=state['epoch'], ledger_revision=state['revision'], operation_id=request['operation_id'])
    return dict(state=state, writes=[encode(r) for r in changed], edges=relationships(rows),
                events=events, result=result)


def check_lease(row, request, now, require_live):
    if row['fields']['status'] != 'working' or row['fields'].get('claimed_by') != request.get('actor'):
        raise Refused('claim belongs to another actor')
    if not request.get('lease_id') or row.get('lease_id') != request['lease_id']:
        raise Refused('lease generation changed')
    record = Corpus([row]).by_id[row['id']]
    if require_live and p.claim(record, now)[0] != 'live':
        raise Refused('lease expired')


def expiry(request, now):
    hours = request.get('hours', 6)
    if type(hours) not in (int, float) or not math.isfinite(hours) or not 0 < hours <= 168:
        raise Refused('lease hours must be finite and in (0, 168]')
    return p.format_timestamp(now + timedelta(hours=hours))


if __name__ == '__main__':
    try:
        print(json.dumps(evaluate(json.load(sys.stdin)), sort_keys=True))
    except (Refused, KeyError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
