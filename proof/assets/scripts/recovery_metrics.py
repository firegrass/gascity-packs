#!/usr/bin/env python3
"""Outcome and recovery measurements, and explicit operator-intervention receipts.

Reads never mail, dispatch or mutate a packet. `intervention` is the sole write:
record a named operator action on a trial's work bead, once per event id.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import argparse
import json
from statistics import median

import factory_lib as fl
import recovery_review as review
import tokens


def delivered(packet):
    return (packet['md'].get('proof.completed') not in (None, '', 'not-ours')
            or bool((packet.get('root', {}).get('metadata') or {}).get('proof.landed_commit')))


def summarize(packets, beads, usage):
    groups = defaultdict(list)
    for packet in packets:
        groups[packet['md'].get('proof.recovery_trial') or 'baseline'].append(packet)
    steps = defaultdict(list)
    reviews = Counter()
    for bead in beads:
        md = bead.get('metadata') or {}
        steps[md.get('gc.root_bead_id')].append(bead)
        if md.get('proof.review_parent'):
            reviews[md['proof.review_parent']] += 1
    result = {}
    for cohort, mine in sorted(groups.items()):
        items = defaultdict(list)
        repeats, minutes, interventions, intervention_coverage = 0, [], 0, 0
        observed, partial, unknown, output = 0, 0, 0, 0
        for packet in mine:
            items[packet['id']].append(packet)
            root = packet.get('root') or {}
            failures = Counter()
            for step in steps[root['id']] if root.get('id') else []:
                md = step.get('metadata') or {}
                if (step.get('status') == 'closed' and md.get('gc.kind') in (None, '', 'task')
                        and md.get('gc.outcome') == 'fail' and md.get('gc.failure_class') == 'hard'
                        and md.get('gc.failure_reason')):
                    failures[md['gc.failure_reason']] += 1
            repeats += sum(max(0, count - 1) for count in failures.values())
            start, end = tokens.stamp(packet['bead'].get('created_at')), tokens.stamp(root.get('closed_at'))
            if start is not None and end is not None:
                minutes.append((end - start) / 60)
            receipts = packet['md'].get('proof.recovery_interventions')
            if receipts is not None:
                receipts = json.loads(receipts) if isinstance(receipts, str) else receipts
                if not isinstance(receipts, list):
                    raise fl.Refused('invalid intervention receipts on ' + packet['bead']['id'])
                interventions += len(receipts); intervention_coverage += 1
            u = usage.get(packet['bead']['id'], {})
            coverage = u.get('coverage', 'unknown')
            unknown += coverage == 'unknown'; partial += coverage == 'partial'
            observed += coverage in ('observed', 'recorded')
            output += u.get('output_tokens', 0)
        result[cohort] = {
            'admissions': len(mine), 'items': len(items),
            'extra_admissions': len(mine) - len(items),
            'delivered_items': sum(any(delivered(p) for p in attempts) for attempts in items.values()),
            'first_observed_admission_delivered': sum(delivered(min(attempts, key=lambda p: p['bead'].get('created_at', '')))
                                                       for attempts in items.values()),
            'workflow_states': dict(Counter(p['state'] for p in mine)),
            'duplicate_hard_task_failures': repeats,
            'review_requests': sum(reviews[(p.get('root') or {}).get('id')] for p in mine),
            'stall_diagnoses': dict(Counter((b.get('metadata') or {}).get('proof.recovery_tier')
                for b in beads if (b.get('metadata') or {}).get('proof.recovery_parent') in
                {p.get('root', {}).get('id') for p in mine}
                and (b.get('metadata') or {}).get('proof.recovery_tier') in ('0', '1'))),
            'scope_expanded_admissions': sum(bool((p.get('root', {}).get('metadata') or {}).get('proof.scope_expansion')) for p in mine),
            'recorded_operator_interventions': interventions,
            'intervention_receipt_admissions': intervention_coverage,
            'admission_to_workflow_close_median_minutes': round(median(minutes), 1) if minutes else None,
            'observed_output_tokens': output,
            'usage_coverage': {'observed_or_recorded': observed, 'partial': partial, 'unknown': unknown},
            'run_provenance': dict(Counter('engine=%s pack=%s' % (
                p['md'].get('proof.engine_commit', 'unknown'), p['md'].get('proof.pack_tree', 'unknown')) for p in mine)),
        }
    return result


def lines(packets, beads, usage):
    result = []
    for cohort, row in summarize(packets, beads, usage).items():
        result += ['  recovery       %s: %d items, %d admissions, %d delivered, %d extra admissions; '
                   '%d duplicate hard task failures; %d review requests' % (
                       cohort, row['items'], row['admissions'], row['delivered_items'], row['extra_admissions'],
                       row['duplicate_hard_task_failures'], row['review_requests']),
                   '  recovery cost  %d recorded output; usage %s; %d recorded operator interventions '
                   '(%d/%d admissions have receipts); workflow-close median %s min (includes waiting)' % (
                       row['observed_output_tokens'], json.dumps(row['usage_coverage'], sort_keys=True),
                       row['recorded_operator_interventions'], row['intervention_receipt_admissions'],
                       row['admissions'], row['admission_to_workflow_close_median_minutes']),
                   '  stall diagnosis Sol %d; Astra %d (diagnosis usage is not yet separately attributed)' % (
                       row['stall_diagnoses'].get('0', 0), row['stall_diagnoses'].get('1', 0))]
    return result


def append_intervention(receipts, event, reason):
    if any(r.get('event') == event for r in receipts):
        return receipts
    return receipts + [{'event': event, 'reason': reason, 'at': datetime.now(timezone.utc).isoformat()}]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('report', 'intervention'))
    parser.add_argument('--rig', action='append', required=True)
    parser.add_argument('--city')
    parser.add_argument('--days', type=int, default=1)
    parser.add_argument('--bead'); parser.add_argument('--event'); parser.add_argument('--reason')
    args = parser.parse_args()
    city = args.city or fl.city_dir()
    if args.action == 'intervention':
        if len(args.rig) != 1 or not all((args.bead, args.event, args.reason)):
            parser.error('intervention needs one rig, --bead, --event and --reason')
        store = review.Store(city, args.rig[0])
        with review.request_lock(city, args.bead):
            bead = store.show(args.bead); md = bead.get('metadata') or {}
            if not md.get('proof.recovery_trial') or not md.get('proof.packet_id'):
                raise fl.Refused('interventions are recorded on an admitted trial work bead')
            receipts = md.get('proof.recovery_interventions') or []
            if isinstance(receipts, str): receipts = json.loads(receipts)
            value = append_intervention(receipts, args.event, args.reason)
            store.bd('update', args.bead, '--set-metadata', 'proof.recovery_interventions=' + json.dumps(value))
        return
    if args.days <= 0: parser.error('--days must be positive')
    floor = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat().replace('+00:00', 'Z')
    rigs = fl.admit.load_rigs(city)
    output = {}
    for rig in args.rig:
        clone = fl.admit.owned_clone(rigs, rig)
        beads = fl.rig_beads(city, rig)
        all_packets = fl.packets_of(beads, superseded=True)
        usage = tokens.usage_by_admission(clone, [p['bead'] for p in all_packets])
        window = [p for p in all_packets if p['bead'].get('created_at', '') >= floor]
        output[rig] = summarize(window, beads, usage)
    print(json.dumps({'since': floor, 'rigs': output,
                      'limits': 'Admission cohort; workflow elapsed includes waiting. Recorded interventions require operator receipts. '
                                'Missing usage is unknown; completion is not production acceptance.'}, indent=2))


if __name__ == '__main__': main()
