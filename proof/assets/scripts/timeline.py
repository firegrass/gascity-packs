#!/usr/bin/env python3
"""Sanitized receipt snapshots and a disposable analytical projection.

No work-state writes, model calls, mail, or authority inferred from this database.
Each snapshot is immutable; the latest collection of each source identity wins.
Rebuilding is atomic and preserves prior snapshots, including failures and gaps.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

SCHEMA = 'proof.timeline.v1'
KINDS = {'attempt', 'gate', 'recovery', 'workflow', 'ci', 'collection', 'outcome'}
IDENTITY = re.compile(r'[A-Za-z0-9_./:@+\-]{1,240}\Z')
FIELDS = {
    'attempt': {'packet', 'group', 'workflow', 'state', 'superseded', 'admission_pending',
        'contract', 'recipe', 'engine', 'provider', 'model', 'phase', 'created_at', 'closed_at',
        'completed', 'landed', 'acceptance', 'input_tokens', 'output_tokens', 'cache_read_tokens',
        'cache_write_tokens', 'usage_coverage', 'usage_source', 'interventions', 'intervention_coverage',
        'pr', 'commit', 'merge_commit', 'main_green', 'gaps'},
    'gate': {'packet', 'group', 'attempt', 'workflow', 'phase', 'result', 'tree', 'seconds',
             'reused', 'created_at', 'closed_at', 'gaps'},
    'recovery': {'packet', 'group', 'attempt', 'workflow', 'phase', 'state', 'created_at',
                 'closed_at', 'gaps'},
    'workflow': {'packet', 'group', 'attempt', 'workflow', 'phase', 'state', 'superseded', 'created_at', 'closed_at', 'gaps'},
    'ci': {'packet', 'group', 'attempt', 'workflow', 'phase', 'state', 'commit', 'event', 'created_at', 'closed_at', 'seconds', 'gaps'},
    'collection': {'attempts', 'workflows', 'gates', 'recoveries', 'ci_runs', 'completed_packets', 'delivered_packets',
                   'input_tokens', 'output_tokens', 'usage_coverage', 'gaps'},
    # One per delivered packet (packet_outcomes.py, ADR-0238 §10): the facts a
    # model-tier judgement reads, written once at completion.
    'outcome': {'packet', 'workflow', 'state', 'landing', 'attempts', 'workflow_roots', 'prior_beads',
                'load_context_attempts', 'implement_iterations', 'gate_first_pass', 'review_rounds',
                'review_findings', 'review_verdicts', 'provider_by_role', 'model_by_role', 'sessions',
                'wall_hours', 'active_hours', 'timebox', 'timebox_hours_max', 'wall_over_timebox',
                'cost_usd', 'cost_by_model', 'tokens_by_model', 'unpriced_facts', 'model_floor',
                'landed', 'merge_commit', 'main_green', 'engine', 'pack_commit', 'created_at', 'closed_at', 'gaps'},
}
NUMBERS = {'input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens',
           'interventions', 'seconds', 'attempts', 'workflows', 'gates', 'recoveries', 'ci_runs',
           'completed_packets', 'delivered_packets', 'workflow_roots', 'prior_beads', 'load_context_attempts',
           'implement_iterations', 'review_rounds', 'review_findings', 'sessions', 'wall_hours', 'active_hours',
           'timebox_hours_max', 'wall_over_timebox', 'cost_usd', 'unpriced_facts'}
BOOLEANS = {'superseded', 'admission_pending', 'reused'}
# Recorded as true/false/None: None is "could not tell", never a pass.
NULLABLE_BOOLEANS = {'gate_first_pass'}
# identity -> number, and identity -> identity: the per-model and per-role breakdowns.
NUMBER_MAPS = {'cost_by_model', 'tokens_by_model'}
LABEL_MAPS = {'review_verdicts', 'provider_by_role', 'model_by_role'}


class Refused(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def identity(value):
    # Reject rather than publish arbitrary notes, commands, URLs or credential text.
    return value if isinstance(value, str) and IDENTITY.fullmatch(value) and '://' not in value and '@' not in value else None


def number(value):
    if type(value) in (int, float):
        return value if math.isfinite(value) and value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value):
        return number(float(value) if '.' in value else int(value))
    return None


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            return None
        return dt.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    except ValueError:
        return None


def strict_json(data):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise Refused('duplicate JSON key')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(Refused('nonfinite JSON')))


def snapshot(kind, rig, source, facts, collected_at):
    row = {'schema': SCHEMA, 'kind': kind, 'rig': rig, 'source': source,
           'collected_at': timestamp(collected_at), 'facts': facts}
    row['event_id'] = hashlib.sha256(canonical(row).encode()).hexdigest()
    validate(row)
    return row


def validate(row):
    if not isinstance(row, dict) or set(row) != {'schema', 'kind', 'rig', 'source', 'collected_at', 'facts', 'event_id'}:
        raise Refused('invalid timeline envelope')
    if row['schema'] != SCHEMA or row['kind'] not in KINDS:
        raise Refused('unsupported timeline schema/kind')
    if identity(row['rig']) is None or identity(row['source']) is None or timestamp(row['collected_at']) != row['collected_at'] or row['collected_at'] is None:
        raise Refused('invalid timeline identity/time')
    facts = row['facts']
    optional = {'commit', 'merge_commit', 'main_green'} if row['kind'] in ('attempt', 'outcome') else set()
    if not isinstance(facts, dict) or not FIELDS[row['kind']] - optional <= set(facts) <= FIELDS[row['kind']]:
        raise Refused('invalid or unexpected facts')
    for key, value in facts.items():
        if key == 'gaps':
            if not isinstance(value, list) or value != sorted(set(value)) or any(identity(v) is None for v in value):
                raise Refused('invalid gaps')
        elif key == 'usage_coverage' and row['kind'] == 'collection':
            if not isinstance(value, dict) or any(identity(k) is None or type(v) is not int or v < 0 for k, v in value.items()):
                raise Refused('invalid coverage counts')
        elif key in NUMBERS:
            if value is not None and (type(value) not in (int, float) or number(value) is None):
                raise Refused('invalid numeric fact')
        elif key in BOOLEANS:
            if type(value) is not bool:
                raise Refused('invalid boolean fact')
        elif key in NULLABLE_BOOLEANS:
            if value is not None and type(value) is not bool:
                raise Refused('invalid boolean fact')
        elif key in NUMBER_MAPS:
            if not isinstance(value, dict) or any(identity(k) is None or type(v) not in (int, float) or number(v) is None
                                                  for k, v in value.items()):
                raise Refused('invalid number map')
        elif key in LABEL_MAPS:
            if not isinstance(value, dict) or any(identity(k) is None or identity(v) is None for k, v in value.items()):
                raise Refused('invalid label map')
        elif key.endswith('_at'):
            if value is not None and (timestamp(value) is None or timestamp(value) != value):
                raise Refused('invalid source timestamp')
        elif value is not None and identity(value) is None:
            raise Refused('invalid scalar fact')
    expected = hashlib.sha256(canonical({k: v for k, v in row.items() if k != 'event_id'}).encode()).hexdigest()
    if row['event_id'] != expected:
        raise Refused('timeline content digest mismatch')


def export(rig, packets, beads, usage, collected_at, ci_runs=None):
    """Adapt existing owners; never copy arbitrary metadata or agent prose."""
    rows, by_root = [], {}
    for packet in packets:
        work, root, md = packet['bead'], packet.get('root') or {}, packet['md']
        rm = root.get('metadata') or {}
        if root.get('id'):
            by_root[root['id']] = packet
        u = usage.get(work['id'], {})
        coverage = u.get('coverage', 'unknown')
        if coverage not in {'observed', 'recorded', 'partial', 'unknown'}:
            coverage = 'unknown'
        fields = ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens')
        counts = [number(u.get(k)) for k in fields]
        if coverage == 'unknown' or any(v is None for v in counts):
            counts = [None] * 4
            coverage = 'unknown'
        # tokens.py normalizes both providers to disjoint uncached/cache buckets.
        total_input = sum(counts[:3]) if counts[0] is not None else None
        gaps = ['acceptance-unrecorded', 'phase-usage-unattributed']
        if coverage in {'unknown', 'partial'}:
            gaps.append('usage-' + coverage)
        if not root:
            gaps.append('workflow-missing')
        interventions = md.get('proof.recovery_interventions')
        if isinstance(interventions, str):
            try:
                interventions = strict_json(interventions)
            except (ValueError, TypeError):
                interventions = None
        valid_interventions = (isinstance(interventions, list) and
            all(isinstance(i, dict) and identity(i.get('event')) for i in interventions))
        if not valid_interventions:
            gaps.append('interventions-unrecorded')
        completed = md.get('proof.completed')
        completed = identity(completed) if completed not in (None, '', 'not-ours') else None
        facts = dict(packet=identity(packet['id']), group=identity(md.get('proof.delivery_group')),
            workflow=identity(root.get('id')), state=identity(packet['state']),
            superseded=bool(packet.get('superseded')), admission_pending=bool(packet.get('admission_pending')),
            contract=identity(md.get('proof.contract_id')),
            recipe=identity(md.get('proof.pack_tree')) if md.get('proof.pack_tree') not in (None, '', 'unknown') else identity(md.get('proof.pack_commit')),
            engine=identity(md.get('proof.engine_commit')), provider=identity(md.get('proof.provider')),
            model=identity(md.get('proof.model')), phase=None,
            created_at=timestamp(work.get('created_at')), closed_at=timestamp(root.get('closed_at')),
            completed=completed, landed=identity(rm.get('proof.landed_commit')), acceptance='unknown',
            input_tokens=total_input, output_tokens=counts[3], cache_read_tokens=counts[1], cache_write_tokens=counts[2],
            usage_coverage=coverage, usage_source=identity(u.get('source')),
            interventions=len({i['event'] for i in interventions}) if valid_interventions else None,
            intervention_coverage='recorded' if valid_interventions else 'unknown',
            commit=identity(md.get('proof.commit')), merge_commit=identity(md.get('proof.merge_commit')),
            main_green=identity(md.get('proof.main_green')),
            pr=str(md['proof.pr_number']) if str(md.get('proof.pr_number', '')).isdigit() else None,
            gaps=[])
        for k in ('contract', 'recipe', 'engine', 'provider', 'model'):
            if facts[k] in (None, 'unknown'):
                gaps.append(k + '-unrecorded')
        facts['gaps'] = sorted(set(gaps))
        rows.append(snapshot('attempt', rig, work['id'], facts, collected_at))
    # Unlike the delivery reader, retain every workflow attempt, including a
    # prior root on the same work bead and abandoned/superseded roots.
    for bead in beads:
        md = bead.get('metadata') or {}
        if md.get('gc.kind') != 'workflow' or md.get('gc.formula_name') == 'mol-proof-pr-recovery':
            continue
        pid = md.get('gc.var.packet_id')
        start = timestamp(bead.get('created_at'))
        candidates = [p for p in packets if p['id'] == pid and start is not None
                      and timestamp(p['bead'].get('created_at')) is not None
                      and timestamp(p['bead']['created_at']) <= start]
        candidates.sort(key=lambda p: (timestamp(p['bead']['created_at']), p['bead']['id']))
        packet = by_root.get(bead['id']) or (candidates[-1] if candidates else None)
        if packet:
            by_root[bead['id']] = packet
        facts = dict(packet=identity(pid), group=identity(packet['md'].get('proof.delivery_group')) if packet else None,
            attempt=identity(packet['bead']['id']) if packet else None, workflow=identity(bead['id']),
            phase='workflow', state=identity(md.get('gc.outcome')) or identity(bead.get('status')),
            superseded=md.get('proof.superseded') in (True, 'true'),
            created_at=start, closed_at=timestamp(bead.get('closed_at')),
            gaps=[] if packet else ['attempt-join-missing'])
        rows.append(snapshot('workflow', rig, bead['id'], facts, collected_at))
    for bead in beads:
        md = bead.get('metadata') or {}
        parent = md.get('gc.root_bead_id')
        packet = by_root.get(parent)
        kind = 'gate' if 'proof.gate_result' in md else 'recovery' if (md.get('proof.recovery_parent') or md.get('gc.formula_name') == 'mol-proof-pr-recovery') else None
        if kind is None:
            continue
        if kind == 'recovery':
            parent = md.get('proof.recovery_parent')
            packet = by_root.get(parent)
            if md.get('gc.formula_name') == 'mol-proof-pr-recovery':
                key = md.get('gc.var.pr_recovery_key')
                matches = [p for p in packets if key and p['md'].get('proof.pr_recovery_key') == key]
                packet = matches[0] if len(matches) == 1 else None
                parent = (packet.get('root') or {}).get('id') if packet else None
        gaps = [] if packet else ['attempt-join-missing']
        facts = dict(packet=identity(packet['id']) if packet else None,
            group=identity(packet['md'].get('proof.delivery_group')) if packet else None,
            attempt=identity(packet['bead']['id']) if packet else None,
            workflow=identity(parent), phase=identity(md.get('gc.step_ref')),
            created_at=timestamp(bead.get('created_at')), closed_at=timestamp(bead.get('closed_at')),
            gaps=gaps)
        if kind == 'gate':
            facts.update(result=identity(md.get('proof.gate_result')), tree=identity(md.get('proof.gate_tree')),
                seconds=number(md.get('proof.gate_seconds')), reused=md.get('proof.gate_reused') in (True, 'true'))
            if facts['seconds'] is None:
                facts['gaps'].append('gate-duration-unrecorded')
        else:
            facts.update(state=identity(md.get('gc.outcome')) or identity(bead.get('status')))
        facts['gaps'] = sorted(facts['gaps'])
        rows.append(snapshot(kind, rig, bead['id'], facts, collected_at))
    for ci in ci_runs or []:
        head = identity(ci.get('headSha'))
        matches = [p for p in packets if head and head in {
            p['md'].get('proof.commit'), p['md'].get('proof.merge_commit'), p['md'].get('proof.main_green'),
            (p.get('root', {}).get('metadata') or {}).get('proof.validated_commit'),
            (p.get('root', {}).get('metadata') or {}).get('proof.approved_commit'),
            (p.get('root', {}).get('metadata') or {}).get('proof.landed_commit')}]
        packet = matches[0] if len(matches) == 1 else None
        start, end = timestamp(ci.get('createdAt')), timestamp(ci.get('updatedAt'))
        seconds = number((datetime.fromisoformat(end.replace('Z', '+00:00')) - datetime.fromisoformat(start.replace('Z', '+00:00'))).total_seconds()) if start and end else None
        # GitHub list exposes the latest rerun; prior attempts remain unknown.
        facts = dict(packet=identity(packet['id']) if packet else None,
            group=identity(packet['md'].get('proof.delivery_group')) if packet else None,
            attempt=identity(packet['bead']['id']) if packet else None,
            workflow=None, phase='ci', state=identity(ci.get('conclusion')) or identity(ci.get('status')),
            commit=head, event=identity(ci.get('event')), created_at=start,
            closed_at=end if ci.get('status') == 'completed' else None,
            seconds=seconds if ci.get('status') == 'completed' else None,
            gaps=['ci-rerun-history-unavailable'] + ([] if packet else ['attempt-join-missing']))
        facts['gaps'].sort()
        rows.append(snapshot('ci', rig, 'github:' + str(ci['databaseId']), facts, collected_at))
    unique = {}
    for row in rows:
        key = (row['kind'], row['source'])
        if key in unique and unique[key] != row:
            raise Refused('conflicting source observations during collection')
        unique[key] = row
    rows = list(unique.values())
    attempts = [r['facts'] for r in rows if r['kind'] == 'attempt']
    gaps = ['unassigned-usage-not-collected', 'phase-usage-not-collected']
    if ci_runs is None:
        gaps.append('ci-not-collected')
    elif len(ci_runs) >= 1000:
        gaps.append('ci-collection-may-be-truncated')
    facts = dict(attempts=len(attempts), workflows=sum(r['kind'] == 'workflow' for r in rows), ci_runs=sum(r['kind'] == 'ci' for r in rows), gates=sum(r['kind'] == 'gate' for r in rows),
        recoveries=sum(r['kind'] == 'recovery' for r in rows),
        completed_packets=len({a['packet'] for a in attempts if a['completed']}),
        delivered_packets=len({a['packet'] for a in attempts if a['completed'] or a['landed']}),
        input_tokens=sum(a['input_tokens'] or 0 for a in attempts),
        output_tokens=sum(a['output_tokens'] or 0 for a in attempts),
        usage_coverage=dict(Counter(a['usage_coverage'] for a in attempts)), gaps=sorted(gaps))
    rows.append(snapshot('collection', rig, 'collector', facts, collected_at))
    return rows


def atomic_export(path, rows):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as stream:
            for row in rows:
                validate(row)
                stream.write(canonical(row) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)  # publish once; never erase an older export
        except FileExistsError:
            if path.read_bytes() != Path(temporary).read_bytes():
                raise Refused('export already exists with different observations; use a new output path')
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def rebuild(inputs, database):
    """Rebuild in a sibling DB, then replace. Failed imports leave the old DB intact."""
    database = Path(database)
    if database.resolve() in {Path(p).resolve() for p in inputs}:
        raise Refused('database cannot replace a receipt export')
    fd, temporary = tempfile.mkstemp(dir=database.parent, prefix='.' + database.name)
    os.close(fd)
    db = sqlite3.connect(temporary)
    try:
        db.execute('CREATE TABLE receipts (event_id TEXT PRIMARY KEY, rig TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL, collected_at TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(rig,kind,source,collected_at))')
        for path in inputs:
            with open(path) as stream:
                for line in stream:
                    row = strict_json(line)
                    validate(row)
                    payload = canonical(row)
                    existing = db.execute('SELECT payload FROM receipts WHERE event_id=?', (row['event_id'],)).fetchone()
                    if existing:
                        if existing[0] != payload:
                            raise Refused('conflicting event id')
                        continue
                    try:
                        db.execute('INSERT INTO receipts VALUES (?,?,?,?,?,?)',
                            (row['event_id'], row['rig'], row['kind'], row['source'], row['collected_at'], payload))
                    except sqlite3.IntegrityError as exc:
                        raise Refused('conflicting source snapshots at the same collection time') from exc
        db.execute('CREATE VIEW current_receipts AS SELECT r.* FROM receipts r WHERE NOT EXISTS (SELECT 1 FROM receipts newer WHERE newer.rig=r.rig AND newer.kind=r.kind AND newer.source=r.source AND newer.collected_at>r.collected_at)')
        db.execute('PRAGMA user_version=1')
        db.commit()
        db.close()
        with open(temporary, 'rb') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, database)
        directory = os.open(database.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        db.close()
        if os.path.exists(temporary):
            os.unlink(temporary)


def report(database, packet=None, history=False):
    db = sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        if db.execute('PRAGMA user_version').fetchone()[0] != 1:
            raise Refused('unsupported projection schema')
        rows = [strict_json(r[0]) for r in db.execute('SELECT payload FROM current_receipts ORDER BY rig,kind,source')]
        for row in rows:
            validate(row)
        retained = db.execute('SELECT count(*) FROM receipts').fetchone()[0]
        historical = [strict_json(r[0]) for r in db.execute('SELECT payload FROM receipts ORDER BY collected_at,rig,kind,source')] if history else []
    finally:
        db.close()
    selected = [r for r in rows if packet is None or r['facts'].get('packet') == packet]
    attempts = [r for r in selected if r['kind'] == 'attempt']
    logical = {(r['rig'], r['facts']['group'] or r['facts']['packet']) for r in attempts
               if r['facts']['completed'] or r['facts']['landed']}
    reconciliation = {}
    for collection in [r for r in rows if r['kind'] == 'collection']:
        rig, expected = collection['rig'], collection['facts']
        mine = [r['facts'] for r in rows if r['rig'] == rig and r['kind'] == 'attempt']
        actual = dict(attempts=len(mine),
            workflows=sum(r['rig'] == rig and r['kind'] == 'workflow' for r in rows),
            ci_runs=sum(r['rig'] == rig and r['kind'] == 'ci' for r in rows),
            gates=sum(r['rig'] == rig and r['kind'] == 'gate' for r in rows),
            recoveries=sum(r['rig'] == rig and r['kind'] == 'recovery' for r in rows),
            completed_packets=len({a['packet'] for a in mine if a['completed']}),
            delivered_packets=len({a['packet'] for a in mine if a['completed'] or a['landed']}),
            input_tokens=sum(a['input_tokens'] or 0 for a in mine),
            output_tokens=sum(a['output_tokens'] or 0 for a in mine),
            usage_coverage=dict(Counter(a['usage_coverage'] for a in mine)))
        reconciliation[rig] = {'collected_at': collection['collected_at'], 'matches': all(expected[k] == v for k, v in actual.items()), 'actual': actual}
    return dict(schema=SCHEMA, retained_snapshots=retained, attempts=len(attempts),
        logical_deliveries=len(logical), acceptance_verified=None,
        observed_input_tokens=sum(r['facts']['input_tokens'] or 0 for r in attempts),
        observed_output_tokens=sum(r['facts']['output_tokens'] or 0 for r in attempts),
        usage_coverage=dict(Counter(r['facts']['usage_coverage'] for r in attempts)),
        gaps=dict(Counter(g for r in selected for g in r['facts']['gaps'])),
        reconciliation=reconciliation, timeline=selected,
        history=[r for r in historical if packet is None or r['facts'].get('packet') == packet],
        limits='Analytical observations only. Recorded completion is not acceptance. Token sums are lower bounds; historical absence is unknown. No budget or promotion authority.')


def collect_beads(city, rig):
    import admit
    try:
        out = admit.gc(city, 'bd', '--rig', rig, 'list', '--all', '--json', '--limit=0')
    except admit.Refused as exc:
        raise Refused('bead collection failed; no export published') from exc
    rows = strict_json(out.stdout)
    if (not isinstance(rows, list) or any(not isinstance(b, dict) or identity(b.get('id')) is None
            or not isinstance(b.get('metadata', {}), dict) for b in rows)
            or len({b['id'] for b in rows}) != len(rows)):
        raise Refused('invalid or duplicate bead collection')
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--city')
    sub = parser.add_subparsers(dest='action', required=True)
    collect = sub.add_parser('export')
    collect.add_argument('--rig', action='append', required=True)
    collect.add_argument('--output', required=True)
    collect.add_argument('--ci-since', help='Read up to 1000 GitHub runs since this UTC timestamp')
    build = sub.add_parser('rebuild')
    build.add_argument('--input', action='append', required=True)
    build.add_argument('--database', required=True)
    read = sub.add_parser('report')
    read.add_argument('--database', required=True)
    read.add_argument('--packet')
    read.add_argument('--history', action='store_true', help='Include retained observations, never sum their cumulative usage')
    args = parser.parse_args(argv)
    if args.action == 'export':
        import factory_lib as fl
        import tokens
        city = args.city or fl.city_dir()
        rigs = fl.admit.load_rigs(city)
        at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        rows = []
        for rig in sorted(set(args.rig)):
            clone = fl.admit.owned_clone(rigs, rig)
            beads = collect_beads(city, rig)
            packets = fl.packets_of(beads, superseded=True)
            usage = tokens.usage_by_admission(clone, [p['bead'] for p in packets])
            ci_runs = None
            if args.ci_since:
                if timestamp(args.ci_since) is None:
                    raise Refused('--ci-since must be a timezone-qualified timestamp')
                import ci_minutes
                ci_runs = ci_minutes.fetch_runs(fl.repo_slug(clone), args.ci_since, identities=True)
            rows += export(rig, packets, beads, usage, at, ci_runs)
        atomic_export(args.output, rows)
        print(canonical({'snapshots': len(rows), 'output': args.output}))
    elif args.action == 'rebuild':
        rebuild(args.input, args.database)
        print(canonical({'database': args.database}))
    else:
        print(json.dumps(report(args.database, args.packet, args.history), indent=2, allow_nan=False))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, sqlite3.Error) as exc:
        sys.exit('timeline: ' + str(exc))
