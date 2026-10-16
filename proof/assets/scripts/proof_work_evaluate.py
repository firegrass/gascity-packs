#!/usr/bin/env python3
"""Trusted evaluator called by the Go helper inside its native transaction.

stdin/stdout is one JSON request/result. The helper supplies its own clock and
snapshot; a caller cannot supply either. A refusal commits no changes. Policy
is pure: nothing here touches a file, a clock or a network.
"""
import copy
from datetime import timedelta
import json
import math
from pathlib import PurePosixPath
import sys

import proof_work_policy as p
from proof_work_records import (SCHEMA, Corpus, Refused, bead_id, decode, digest,
                                encode, relationships, semantic, validate_corpus)

FACTORY_SEAT_PREFIX = 'gascity/'
READ_ONLY_ACTIONS = ('snapshot', 'history', 'revision')
STATE_ACTIONS = ('activate', 'freeze')
ROW_ACTIONS = ('claim', 'renew', 'release', 'edit', 'answer', 'evidence', 'complete', 'reopen')
# Legacy roots a native author may create under, by record type.
AUTHOR_ROOTS = {'backlog': ('docs/backlog/items/', 'BKLG'), 'epic': ('docs/backlog/epics/', 'EPIC'),
                'question': ('docs/questions/open/', 'Q')}
# The legacy required-key registry (orchestrator docs_model), so a native
# record exports back into a validated Markdown corpus.
BASE_REQUIRED = ('title', 'doc_type', 'status', 'owner', 'created', 'last_reviewed',
                 'source_documents', 'confidence', 'related_repos')
REQUIRED_FIELDS = {
    'backlog': BASE_REQUIRED + ('milestone', 'lane', 'gate', 'priority', 'depends_on', 'parent_epic'),
    'epic': BASE_REQUIRED + ('milestone', 'lane', 'gate', 'priority', 'depends_on', 'epic_id', 'outcome'),
    'question': BASE_REQUIRED,
}
QUESTION_ANSWER_FIELDS = ('asked_of', 'due', 'resolution', 'answered', 'related_backlog')
LIFECYCLE_FIELDS = {'status', 'created', 'doc_type', 'epic_id', 'milestone_id', 'done_at', *p.CLAIM_KEYS}


def is_factory(actor):
    return actor.startswith(FACTORY_SEAT_PREFIX)


def today(now):
    return now.strftime('%Y-%m-%d')


def evaluate(data):
    request, state = data['request'], data['state']
    now = p.parse_timestamp(data['now'])
    if now is None:
        raise Refused('helper clock unavailable')
    action = request.get('action')
    issues = data['issues'] or []
    rows = [r for issue in issues if (r := decode(issue)) is not None]
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
                                native_records=[i for i in issues if i.get('external_ref') in corpus.by_id]))
    if action == 'revision':
        return dict(result=dict(epoch=state['epoch'], mode=state['mode'], revision=state['revision'],
                                source_sha=state.get('source_sha'), generated_at=data['now']))
    if action == 'history':
        if request.get('id') not in corpus.by_id:
            raise Refused('unknown work identity')
        return dict(result=history(request['id'], issues))
    actor = request.get('actor')
    if not isinstance(actor, str) or not actor.strip():
        raise Refused('mutation requires actor')
    mode = state.get('mode')
    if mode == 'frozen':
        raise Refused('authority is frozen; a new epoch needs a fresh import')
    if mode not in ('shadow', 'live'):
        raise Refused('unknown authority mode: ' + str(mode))
    at, op = data['now'], request['operation_id']
    if action in STATE_ACTIONS:
        return mutate_state(action, request, state, rows, actor, at, op)
    if action == 'author':
        return author(request, rows, state, corpus, now, actor, at, op)
    if action not in ROW_ACTIONS:
        raise Refused('unsupported checked action: ' + str(action))
    by_id = {r['id']: r for r in rows}
    rid = request.get('id')
    row = by_id.get(rid)
    if row is None:
        raise Refused('unknown work identity')
    record = corpus.by_id[rid]
    before = copy.deepcopy(row)
    fm = row['fields']
    changed = [row]
    events = []
    authored = False
    event = dict(kind=action)
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
            if p.record_id(other) != rid and p.blocks_scope(other, now) and p.claims_collide(scopes, other_scopes):
                raise Refused('scope overlaps live claim: ' + p.record_id(other))
        row['reserved_scopes'] = [list(scope) for scope in scopes]
        fm.update(status='working', claimed_by=actor, claimed_at=at, claim_expires=expiry(request, now))
        row['lease_id'] = digest({'id': rid, 'operation_id': op})
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
            drop_claim(row)
    elif action == 'edit':
        if request.get('expected_authored_revision') != row['authored_revision']:
            raise Refused('authored revision changed')
        if any(k in fm for k in p.CLAIM_KEYS):
            raise Refused('release retained claim before editing contract')
        apply_field_updates(fm, request.get('fields', {}))
        if 'body' in request:
            row['body'] = request['body']
        authored = True
        event['kind'] = 'revision'
    elif action == 'answer':
        if record.doc_type != 'question':
            raise Refused('only a question can be answered')
        if fm['status'] != 'open':
            raise Refused('question is not open')
        if request.get('expected_authored_revision') != row['authored_revision']:
            raise Refused('authored revision changed')
        if not isinstance(request.get('body'), str) or not request['body'].strip():
            raise Refused('an answer needs a body')
        apply_field_updates(fm, request.get('fields', {}), allow_new=QUESTION_ANSWER_FIELDS)
        fm['status'] = 'answered'
        fm.setdefault('answered', today(now))
        row['path'] = 'docs/questions/answered/' + PurePosixPath(row['path']).name
        row['body'] = request['body']
        authored = True
        event['kind'] = 'answered'
    elif action == 'evidence':
        if record.doc_type != 'backlog' or fm['status'] not in ('working', 'done'):
            raise Refused('evidence attaches to a working or done item')
        entries = request.get('entries')
        if not isinstance(entries, list) or not entries or any(not isinstance(e, str) or not e.strip() for e in entries):
            raise Refused('evidence entries must be a non-empty list of strings')
        receipts = request.get('receipts')
        if receipts is not None and not isinstance(receipts, dict):
            raise Refused('evidence receipts must be an object')
        existing = fm.get('evidence', [])
        if not isinstance(existing, list):
            raise Refused('field must be a list: evidence')
        fm['evidence'] = existing + [e for i, e in enumerate(entries) if e not in existing and e not in entries[:i]]
        event['receipts'] = receipts
    elif action == 'complete':
        complete(request, row, record, corpus, by_id, now, actor, at, op, changed, events, event)
    elif action == 'reopen':
        if is_factory(actor):
            raise Refused('a factory seat cannot reopen work')
        if record.doc_type not in ('backlog', 'epic') or fm['status'] not in ('done', 'superseded'):
            raise Refused('reopen requires a done or superseded item')
        reason = request.get('reason')
        if not isinstance(reason, str) or not reason.strip():
            raise Refused('reopen requires a reason')
        reopen_row(row, now)
        event['reason'] = reason
        parent = corpus.epic(fm.get('parent_epic'))
        if record.doc_type == 'backlog' and parent is not None and parent.status in ('done', 'superseded'):
            parent_row = by_id[p.record_id(parent)]
            parent_before = copy.deepcopy(parent_row)
            reopen_row(parent_row, now)
            changed.append(parent_row)
            events.append(dict(id=parent_row['id'], kind='reopened', reason=reason, before=parent_before,
                               after=copy.deepcopy(parent_row), actor=actor, at=at, operation_id=op))
    fm['last_reviewed'] = today(now)
    row['operational_revision'] += 1
    if authored:
        row['authored_revision'] += 1
    row['semantic_digest'] = digest(semantic(fm, row['body']))
    validate_corpus(rows, now)
    state['revision'] += 1
    event['kind'] = {'reopen': 'reopened', 'complete': 'completion'}.get(action, event['kind'])
    events.insert(0, dict(event, id=rid, before=before, after=copy.deepcopy(row), actor=actor, at=at, operation_id=op))
    # Every changed parent contract is retained as an immutable full revision too.
    covered = {e['id'] for e in events}
    events += [dict(id=r['id'], kind='revision', after=copy.deepcopy(r), actor=actor, at=at, operation_id=op)
               for r in changed[1:] if r['id'] not in covered]
    return dict(state=state, writes=[encode(r) for r in changed], edges=relationships(rows),
                events=stamp(events, state), result=row_result(row, state, op))


def complete(request, row, record, corpus, by_id, now, actor, at, op, changed, events, event):
    fm = row['fields']
    if record.doc_type not in ('backlog', 'question'):
        raise Refused('complete targets an item; an epic closes with its last child')
    if fm['status'] != 'working':
        raise Refused('completion requires working status')
    check_lease(row, request, now, require_live=True)
    if request.get('expected_semantic_digest') != row['semantic_digest']:
        raise Refused('semantic contract changed since the attempt was bound')
    resolution = request.get('resolution')
    if resolution not in ('done', 'superseded'):
        raise Refused('resolution must be done or superseded')
    factory = is_factory(actor)
    if resolution == 'superseded' and factory:
        raise Refused('only a person may supersede work')
    evidence = request.get('evidence', {})
    if not isinstance(evidence, dict):
        raise Refused('completion evidence must be an object')
    for key in ('pull_request', 'merge_sha', 'main_sha', 'approved_tree'):
        if key in evidence and (not isinstance(evidence[key], str) or not evidence[key].strip()):
            raise Refused('completion evidence ' + key + ' must be a non-empty string')
    attempts = evidence.get('attempts', [])
    if not isinstance(attempts, list) or any(not isinstance(a, dict) or not isinstance(a.get('bead_id'), str)
                                              or not isinstance(a.get('rig'), str) for a in attempts):
        raise Refused('completion attempts must list {bead_id, rig}')
    acceptance = evidence.get('acceptance')
    if acceptance is not None and not isinstance(acceptance, dict):
        raise Refused('completion acceptance must be an object')
    if resolution == 'done':
        merged = 'main_sha' in evidence and ('pull_request' in evidence or 'merge_sha' in evidence)
        if factory and not merged:
            raise Refused('a factory seat completes only with a merged pull request (or merge sha) and a green main sha')
        if not factory and not merged and not acceptance:
            raise Refused('completion needs merged evidence or acceptance')
    notes = request.get('notes')
    if notes is not None and not isinstance(notes, str):
        raise Refused('completion notes must be a string')
    close_epic = request.get('close_epic', False)
    if close_epic not in (True, False):
        raise Refused('close_epic must be a boolean')
    if close_epic and factory:
        raise Refused('a factory seat can never close an epic')
    epic = corpus.epic(fm.get('parent_epic')) if record.doc_type == 'backlog' else None
    if epic is not None and epic.status in p.ACTIVE_STATUSES:
        siblings = [c for c in corpus.children(p.record_id(epic)) if p.record_id(c) != row['id']]
        if not siblings and not close_epic:
            raise Refused('last active child of ' + p.record_id(epic) + ': a person must complete with close_epic')
        if siblings and close_epic:
            raise Refused('close_epic requires this to be the last active child of ' + p.record_id(epic))
    elif close_epic:
        raise Refused('close_epic needs an active parent epic')
    fm['status'] = resolution
    fm['done_at'] = today(now)
    drop_claim(row)
    if 'pull_request' in evidence:
        current = fm.get('evidence', [])
        if not isinstance(current, list):
            raise Refused('field must be a list: evidence')
        if evidence['pull_request'] not in current:
            fm['evidence'] = current + [evidence['pull_request']]
    event.update(resolution=resolution, evidence=evidence, notes=notes)
    if epic is not None and close_epic:
        parent = by_id[p.record_id(epic)]
        parent_before = copy.deepcopy(parent)
        parent['fields'].update(status='done', done_at=today(now), last_reviewed=today(now))
        parent['operational_revision'] += 1
        parent['semantic_digest'] = digest(semantic(parent['fields'], parent['body']))
        changed.append(parent)
        events.append(dict(id=parent['id'], kind='completion', resolution='done', notes=notes,
                           evidence=dict(closed_with=row['id']), before=parent_before,
                           after=copy.deepcopy(parent), actor=actor, at=at, operation_id=op))


def reopen_row(row, now):
    fm = row['fields']
    fm['status'] = 'open'
    fm.pop('done_at', None)
    fm['last_reviewed'] = today(now)
    row['operational_revision'] += 1
    row['semantic_digest'] = digest(semantic(fm, row['body']))


def author(request, rows, state, corpus, now, actor, at, op):
    records = request.get('records') if 'records' in request else [request.get('record')]
    if not isinstance(records, list) or not records or any(not isinstance(r, dict) for r in records):
        raise Refused('author needs a record')
    by_id = {r['id']: r for r in rows}
    created = []
    for record in records:
        rid, path, fields, body = record.get('id'), record.get('path'), record.get('fields'), record.get('body', '')
        if not isinstance(fields, dict) or not isinstance(body, str) or not isinstance(path, str):
            raise Refused('author record needs path, fields and body')
        doc_type = fields.get('doc_type')
        if doc_type not in AUTHOR_ROOTS:
            raise Refused('author creates a backlog item, epic or question')
        root, prefix = AUTHOR_ROOTS[doc_type]
        name = PurePosixPath(path).name
        if (not path.startswith(root) or path != root + name or not name.endswith('.md')
                or not isinstance(rid, str) or p.work_id_from_filename(name) != rid or not rid.startswith(prefix + '-')):
            raise Refused('path must be ' + root + '<id>-<slug>.md with a ' + prefix + ' id')
        if rid in by_id or any(r['id'] == rid for r in created):
            raise Refused('identity already present: ' + rid)
        if fields.get('status') != 'open':
            raise Refused('a new record is authored open')
        fields = dict(fields)
        fields.setdefault('created', today(now))
        fields.setdefault('last_reviewed', today(now))
        missing = [key for key in REQUIRED_FIELDS[doc_type] if key not in fields]
        if missing:
            raise Refused('missing required scalar field: ' + ', '.join(missing))
        if doc_type == 'epic' and fields.get('epic_id') != rid:
            raise Refused('epic_id must equal the record id')
        if any(key in fields for key in p.CLAIM_KEYS) or 'done_at' in fields:
            raise Refused('a new record carries no claim or completion')
        row = dict(schema=SCHEMA, id=rid, path=path, fields=fields, body=body, authored_revision=1,
                   operational_revision=0, semantic_digest=digest(semantic(fields, body)),
                   source_sha=None, source_text='')
        created.append(row)
    all_rows = rows + created
    validate_corpus(all_rows, now)
    state['project_ids'] = sorted(r['id'] for r in all_rows)
    state['revision'] += 1
    events = [dict(id=r['id'], kind='authored', after=copy.deepcopy(r), actor=actor, at=at, operation_id=op)
              for r in created]
    result = row_result(created[0], state, op)
    if 'records' in request:
        result['ids'] = [r['id'] for r in created]
    return dict(state=state, writes=[encode(r) for r in created], edges=relationships(all_rows),
                events=stamp(events, state), result=result)


def mutate_state(action, request, state, rows, actor, at, op):
    before = copy.deepcopy(state)
    if action == 'activate':
        if state['mode'] != 'shadow':
            raise Refused('activate requires shadow mode')
        if state.get('reservations_reconciled') is not True:
            raise Refused('reservations are not reconciled')
        if request.get('expected_source_sha') != state.get('source_sha'):
            raise Refused('source revision changed since the rehearsal')
        state.update(mode='live', activated_at=at, activated_by=actor)
    else:
        if state['mode'] != 'live':
            raise Refused('freeze requires live mode')
        reason = request.get('reason')
        if not isinstance(reason, str) or not reason.strip():
            raise Refused('freeze requires a reason')
        state.update(mode='frozen', frozen_at=at, frozen_by=actor, freeze_reason=reason)
    state['revision'] += 1
    kind = 'activated' if action == 'activate' else 'frozen'
    event = dict(id=None, kind=kind, before=before, after=copy.deepcopy(state), actor=actor, at=at, operation_id=op)
    if action == 'freeze':
        event['reason'] = request['reason']
    result = dict(id=None, bead_id=None, status=state['mode'], lease_id=None, authored_revision=None,
                  operational_revision=None, semantic_digest=None, epoch=state['epoch'],
                  ledger_revision=state['revision'], operation_id=op, mode=state['mode'])
    return dict(state=state, writes=[], edges=relationships(rows), events=stamp([event], state), result=result)


def history(rid, issues):
    events, receipts = [], []
    for issue in issues:
        ident = issue.get('id', '')
        meta = issue.get('metadata') or {}
        if ident.startswith('pw-event-') and isinstance(meta, dict):
            owner = meta.get('id', (meta.get('after') or {}).get('id') if isinstance(meta.get('after'), dict) else None)
            if owner == rid:
                events.append(dict(meta, id=rid))
        elif ident.startswith('pw-op-') and isinstance(meta, dict) and isinstance(meta.get('result'), dict):
            result = meta['result']
            if result.get('id') == rid or rid in (result.get('ids') or []):
                receipts.append(dict(operation_id=result.get('operation_id'), digest=meta.get('digest'), result=result))
    events.sort(key=lambda e: (str(e.get('at', '')), str(e.get('operation_id', ''))))
    receipts.sort(key=lambda r: (r['result'].get('ledger_revision') or 0, str(r['operation_id'])))
    return dict(id=rid, events=events, receipts=receipts)


def stamp(events, state):
    """Every event also records the ledger revision it committed at, a total
    order finer than the second-resolution helper clock."""
    for event in events:
        event['ledger_revision'] = state['revision']
    return events


def row_result(row, state, op):
    result = {key: row[key] for key in ('id', 'authored_revision', 'operational_revision', 'semantic_digest')}
    result.update(bead_id=bead_id(row['id']), status=row['fields']['status'], lease_id=row.get('lease_id'),
                  epoch=state['epoch'], ledger_revision=state['revision'], operation_id=op)
    return result


def apply_field_updates(fm, updates, allow_new=()):
    if not isinstance(updates, dict) or LIFECYCLE_FIELDS & updates.keys():
        raise Refused('edit cannot change lifecycle or identity')
    if set(updates) - set(fm) - set(allow_new):
        raise Refused('new fields require schema registration')
    if any(key in fm and type(value) is not type(fm[key]) for key, value in updates.items()):
        raise Refused('edit cannot change a registered field type')
    fm.update(updates)


def drop_claim(row):
    for key in p.CLAIM_KEYS:
        row['fields'].pop(key, None)
    row.pop('lease_id', None)
    row.pop('reserved_scopes', None)


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
