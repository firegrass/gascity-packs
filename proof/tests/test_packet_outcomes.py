"""packet_outcomes.py: the per-packet outcome record (ADR-0238 §10, stage 1b)."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'assets/scripts'))
import packet_outcomes as po  # noqa: E402
import timeline  # noqa: E402

PID = 'BKLG-20261015-test'


def bead(bid, created, **md):
    return {'id': bid, 'created_at': created, 'status': 'closed', 'metadata': md}


def gate_lane_beads():
    """A packet re-admitted once: a first bead whose workflow failed, then the
    delivered bead with a gate-lane first iteration, two review rounds and
    sessions for the worker and the reviewer."""
    rows = [
        bead('w-old', '2026-10-15T08:00:00Z', **{'proof.packet_id': PID, 'proof.superseded': True}),
        bead('r-old', '2026-10-15T08:00:05Z', **{'gc.kind': 'workflow', 'gc.var.packet_id': PID, 'gc.outcome': 'fail',
                                                 'gc.formula_name': 'mol-proof-packet'}),
        bead('w-new', '2026-10-15T09:00:00Z', **{'proof.packet_id': PID, 'proof.landing': 'pr',
                                                 'proof.merge_commit': 'abc123', 'proof.main_green': 'def456'}),
        bead('r-new', '2026-10-15T09:00:05Z', **{'gc.kind': 'workflow', 'gc.var.packet_id': PID, 'gc.outcome': 'pass',
                                                 'gc.formula_name': 'mol-proof-packet'}),
        bead('s-lc1', '2026-10-15T09:01:00Z', **{'gc.root_bead_id': 'r-new', 'gc.step_ref': 'mol-proof-packet.load-context.attempt.1',
                                                 'gc.session_id': 'pr-w1', 'gc.outcome': 'fail'}),
        bead('s-lc2', '2026-10-15T09:02:00Z', **{'gc.root_bead_id': 'r-new', 'gc.step_ref': 'mol-proof-packet.load-context.attempt.2',
                                                 'gc.session_id': 'pr-w1', 'gc.outcome': 'pass'}),
        bead('s-impl-ctl', '2026-10-15T09:02:30Z', **{'gc.root_bead_id': 'r-new', 'gc.step_ref': 'mol-proof-packet.implement',
                                                      'gc.max_attempts': 3, 'gc.attempt_log': '[]', 'gc.kind': 'control'}),
        bead('s-impl1', '2026-10-15T09:03:00Z', **{'gc.root_bead_id': 'r-new', 'gc.step_ref': 'implement.iteration.1',
                                                   'gc.session_id': 'pr-w1', 'gc.outcome': 'pass'}),
        bead('s-gate1', '2026-10-15T09:30:00Z', **{'gc.root_bead_id': 'r-new', 'gc.step_ref': 'implement.iteration.1.gate',
                                                   'gc.session_id': 'pr-g1', 'gc.outcome': 'pass', 'proof.gate_result': 'fail'}),
        bead('s-impl2', '2026-10-15T09:40:00Z', **{'gc.root_bead_id': 'r-new', 'gc.step_ref': 'implement.iteration.2',
                                                   'gc.session_id': 'pr-w1', 'gc.outcome': 'pass'}),
        bead('s-rev1', '2026-10-15T10:00:00Z', **{'gc.root_bead_id': 'r-new', 'gc.step_ref': 'review-cycle.iteration.1.review',
                                                  'gc.session_id': 'pr-r1', 'gc.outcome': 'pass',
                                                  'proof.review_verdict': 'iterate', 'proof.review_findings': '3'}),
        bead('s-rev2', '2026-10-15T10:30:00Z', **{'gc.root_bead_id': 'r-new', 'gc.step_ref': 'review-cycle.iteration.2.review',
                                                  'gc.session_id': 'pr-r2', 'gc.outcome': 'pass',
                                                  'proof.review_verdict': 'approve', 'proof.review_findings': '0'}),
    ]
    rows[3]['closed_at'] = '2026-10-15T11:00:00Z'
    return rows


def usage_facts():
    def model(sid, worker, provider, m, inp, out, cost=None, unpriced=False):
        f = {'session_id': sid, 'run_id': sid, 'worker': worker, 'kind': 'model', 'provider': provider,
             'input_tokens': inp, 'output_tokens': out, 'cache_read_tokens': 10, 'idempotency_key': sid + str(inp)}
        if m:
            f['model'] = m
        if unpriced:
            f['unpriced'] = True
        else:
            f['cost_usd_estimate'] = cost
        return f
    return [
        model('pr-w1', 'proof__worker-pr-w1', 'grok', 'grok-4.7', 1000, 200, 0.0032),
        model('pr-w1', 'proof__worker-pr-w1', 'grok', 'grok-4.7', 2000, 100, 0.0046),
        model('pr-r1', 'proof__reviewer-pr-r1', 'claude', 'claude-opus-5-5', 500, 50, 0.003),
        model('pr-r2', 'proof__reviewer-pr-r2', 'codex', None, 500, 50, unpriced=True),
        {'session_id': 'pr-w1', 'run_id': 'pr-w1', 'worker': 'proof__worker-pr-w1', 'kind': 'compute', 'wall_seconds': 1800.0,
         'idempotency_key': 'c1'},
        {'session_id': 'pr-other', 'run_id': 'pr-other', 'worker': 'proof__worker-pr-other', 'kind': 'model', 'provider': 'grok',
         'model': 'grok-4.7', 'input_tokens': 9, 'output_tokens': 9, 'cost_usd_estimate': 99.0, 'idempotency_key': 'x'},
    ]


class TimeboxParsing(unittest.TestCase):
    def test_table(self):
        for text, hours in [('2-3h', 3), ('1h', 1), ('30m', 0.5), ('2 days', 48), ('3-4h active work', 4),
                            ('1–2h', 2), ('45 min', 0.75), ('a day or so', None), (None, None), ('', None), (3, None)]:
            self.assertEqual(po.timebox_hours(text), hours, text)


class Roles(unittest.TestCase):
    def test_from_usage_worker_name_and_step_ref(self):
        self.assertEqual(po.role_of('', 'proof__worker-pr-wisp-ly91ztk'), 'worker')
        self.assertEqual(po.role_of('', 'proof__reviewer-pr-oz37r2'), 'reviewer')
        self.assertEqual(po.role_of('', 'proof__recovery-expert-pr-a1'), 'recovery-expert')
        self.assertEqual(po.role_of('mol-proof-packet.implement.iteration.1.gate'), 'gater')
        self.assertEqual(po.role_of('review-cycle.iteration.2.review'), 'reviewer')
        self.assertEqual(po.role_of('implement.iteration.1'), 'worker')
        self.assertEqual(po.role_of('', 'mayor'), 'unknown')


class Outcome(unittest.TestCase):
    def setUp(self):
        self.beads = gate_lane_beads()
        self.work = next(b for b in self.beads if b['id'] == 'w-new')
        self.item = {'id': PID, 'fields': {'timebox': '2-3h', 'model_floor': 'everyday_powerful'}}
        self.prov = {'engine': 'e' * 40, 'pack_commit': 'f' * 40}

    def facts(self, **kw):
        usage = [f for f in usage_facts() if f['session_id'] in {'pr-w1', 'pr-g1', 'pr-r1', 'pr-r2'}]
        return po.outcome_for('liskov-rs', self.work, self.beads, usage, item=self.item, provenance=self.prov, **kw)

    def test_counts_attempts_iterations_and_reviews(self):
        f = self.facts()
        self.assertEqual((f['attempts'], f['workflow_roots'], f['prior_beads']), (2, 1, 1))
        self.assertEqual((f['load_context_attempts'], f['implement_iterations']), (2, 2))
        self.assertEqual((f['review_rounds'], f['review_findings']), (2, 3))
        self.assertEqual(f['review_verdicts'], {'round_1': 'iterate', 'round_2': 'approve'})
        self.assertIs(f['gate_first_pass'], False)          # the gate lane's first iteration failed
        self.assertEqual(f['state'], 'pass')

    def test_dollars_models_and_time(self):
        f = self.facts()
        self.assertAlmostEqual(f['cost_usd'], 0.0108)
        self.assertEqual(f['cost_by_model'], {'grok-4.7': 0.0078, 'claude-opus-5-5': 0.003})
        self.assertEqual(f['model_by_role'], {'worker': 'grok-4.7', 'reviewer': 'claude-opus-5-5'})
        self.assertEqual(f['provider_by_role'], {'worker': 'grok', 'reviewer': 'claude'})
        self.assertEqual(f['unpriced_facts'], 1)
        self.assertEqual(f['tokens_by_model']['grok-4.7'], 1000 + 200 + 10 + 2000 + 100 + 10)
        self.assertEqual(f['wall_hours'], 2.0)              # 09:00 -> 11:00
        self.assertEqual(f['active_hours'], 0.5)
        self.assertEqual((f['timebox'], f['timebox_hours_max'], f['wall_over_timebox']), ('2-3h', 3, 0.667))
        self.assertEqual(f['model_floor'], 'everyday_powerful')
        self.assertEqual((f['merge_commit'], f['main_green'], f['engine'], f['pack_commit']), ('abc123', 'def456', 'e' * 40, 'f' * 40))
        self.assertEqual(f['gaps'], ['usage-partly-unpriced'])

    def test_record_validates_as_a_timeline_row(self):
        row = timeline.snapshot('outcome', 'liskov-rs', 'w-new', self.facts(), '2026-10-15T12:00:00Z')
        timeline.validate(row)
        self.assertEqual(row['kind'], 'outcome')

    def test_inline_rig_reads_the_control_bead_and_records_gaps(self):
        beads = [b for b in self.beads if b['id'] not in ('s-gate1', 's-impl2', 's-rev1', 's-rev2')]
        f = po.outcome_for('liskov-ui', self.work, beads, [], item={'fields': {'timebox': 'a while'}}, provenance={})
        self.assertIs(f['gate_first_pass'], True)           # empty attempt log, one iteration
        self.assertEqual(f['review_rounds'], 0)
        self.assertIsNone(f['review_findings'])
        self.assertEqual(f['gaps'], ['model-unrecorded', 'timebox-unparsed', 'usage-missing'])
        beads[next(i for i, b in enumerate(beads) if b['id'] == 's-impl-ctl')]['metadata']['gc.attempt_log'] = '["gate failed"]'
        self.assertIs(po.outcome_for('liskov-ui', self.work, beads, [], item=None, provenance={})['gate_first_pass'], False)

    def test_findings_without_a_count_are_a_gap_not_zero(self):
        for b in self.beads:
            b['metadata'].pop('proof.review_findings', None)
        f = self.facts()
        self.assertIsNone(f['review_findings'])
        self.assertIn('review-findings-uncounted', f['gaps'])


class Recording(unittest.TestCase):
    def test_record_appends_once_and_stamps_the_bead(self):
        beads = gate_lane_beads()
        work = next(b for b in beads if b['id'] == 'w-new')
        with tempfile.TemporaryDirectory() as city:
            os.makedirs(os.path.join(city, '.gc'))
            with open(os.path.join(city, '.gc/usage.jsonl'), 'w') as fh:
                fh.write('not json\n')
                for f in usage_facts():
                    fh.write(json.dumps(f) + '\n')
            calls = []
            with patch.object(po.fl, 'sh', lambda argv, cwd=None: calls.append(argv)), \
                    patch.object(po, 'provenance', lambda c: {'engine': None, 'pack_commit': None}), \
                    patch.object(po, 'ledger_row', lambda clone, pid: {'fields': {'timebox': '1h'}}):
                text = po.record(city, 'liskov-rs', work, beads, '/clone')
                again = po.record(city, 'liskov-rs', work, beads, '/clone')
            self.assertIn('outcome  gate first pass False, 2 attempt(s), 2 review round(s), 0.0108 USD', text)
            self.assertEqual(again, '')
            rows = po.rows(city)
            self.assertEqual(len(rows), 1)
            timeline.validate(rows[0])
            self.assertEqual(rows[0]['facts']['packet'], PID)
            self.assertNotIn(99.0, rows[0]['facts']['cost_by_model'].values())   # another session's fact is not ours
            self.assertEqual(len(calls), 1)
            stamps = dict(a.split('=', 1) for a in calls[0][calls[0].index('--set-metadata') + 1::2])
            self.assertEqual(stamps['proof.outcome.gate_first_pass'], 'false')
            self.assertEqual(stamps['proof.outcome.attempts'], '2')
            self.assertEqual(stamps['proof.model'], 'grok-4.7')
            self.assertEqual(stamps['proof.provider'], 'grok')
            self.assertEqual(stamps['proof.outcome.timebox_hours'], '1.0')
            table = po.rollup(rows)
            self.assertEqual(table['grok-4.7']['packets'], 1)
            self.assertEqual(table['grok-4.7']['gate_first_pass'], 0.0)
            self.assertEqual(po.lines(city)[0], 'stage 1b outcomes (1 delivered packet)')
            self.assertEqual(po.lines(city, '2027-01-01T00:00:00Z'), ['stage 1b outcomes: no delivered packet recorded since 2027-01-01T00:00:00Z'])

    def test_no_packet_id_writes_nothing(self):
        with tempfile.TemporaryDirectory() as city:
            self.assertEqual(po.record(city, 'r', {'id': 'x', 'metadata': {}}, [], ''), '')
            self.assertFalse(Path(city, po.ANALYTICS).exists())


if __name__ == '__main__':
    unittest.main()
