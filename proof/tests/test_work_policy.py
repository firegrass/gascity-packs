import copy
import json
from pathlib import Path
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'assets/scripts'
sys.path.insert(0, str(SCRIPTS))
import proof_work_policy as policy
from proof_work_records import SCHEMA, Corpus, Refused, decode, digest, encode, relationships, semantic, validate_corpus
from proof_work_evaluate import evaluate

NOW = '2026-10-14T18:00:00Z'


def fixture():
    common = dict(owner='unassigned', created='2026-01-01', last_reviewed='2026-01-02',
                  source_documents=[], confidence='high', related_repos=[])
    definitions = [
        ('docs/backlog/milestones/test.md', dict(doc_type='milestone', title='Synthetic milestone', status='open',
            milestone_id='test', sequence='0', outcome='Synthetic outcome', lanes=['delivery'])),
        ('docs/backlog/epics/EPIC-20260101-aaaa-test.md', dict(doc_type='epic', title='Synthetic epic',
            status='open', epic_id='EPIC-20260101-aaaa', outcome='Synthetic outcome', milestone='test',
            lane='delivery', gate='false', priority='p0', rank='0', depends_on=[])),
    ]
    for code in ('bbbb', 'cccc'):
        definitions.append((f'docs/backlog/items/BKLG-20260101-{code}-test.md', dict(doc_type='backlog',
            title='Synthetic item', status='open', parent_epic='EPIC-20260101-aaaa', milestone='test',
            lane='delivery', gate='false', priority='p0', rank='0', depends_on=[],
            write_scope=['synthetic:src/shared.py'], validation=['python3 test.py'],
            evidence=[], extra_legacy='')))
    rows = []
    for path, fm in definitions:
        fields = dict(common, **fm)
        body = '\n## Acceptance\n\nKeep the synthetic promise.\n'
        rid = fm.get('milestone_id') or '-'.join(Path(path).name.split('-')[:3])
        rows.append(dict(schema=SCHEMA, id=rid, path=path, fields=fields, body=body,
                         authored_revision=1, operational_revision=0, source_sha='a' * 40,
                         source_text='Synthetic original '+rid, semantic_digest=digest(semantic(fields, body))))
    return rows


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.rows = fixture()
        self.request = dict(action='import', records=self.rows, epoch='test', source_sha='a' * 40,
                            operation_id='import')
        self.imported = evaluate(dict(request=self.request, state={}, issues=[], edges=[], now=NOW))

    def call(self, action, **kwargs):
        return evaluate(dict(request=dict(action=action, epoch='test', actor='seat', operation_id=action,
                                          id='BKLG-20260101-bbbb', **kwargs),
                             state=copy.deepcopy(self.imported['state']), issues=copy.deepcopy(self.imported['writes']),
                             edges=copy.deepcopy(self.imported['edges']), now=NOW))

    def test_lossless_zero_empty_and_creation_dates(self):
        for row in self.rows:
            issue = encode(row)
            self.assertEqual(decode(issue), row)
            self.assertEqual(issue['created_at'], '2026-01-01T00:00:00Z')
        self.assertEqual(encode(self.rows[-1])['priority'], 0)
        self.assertEqual(self.rows[-1]['fields']['extra_legacy'], '')
        self.assertEqual(self.rows[-1]['fields']['evidence'], [])
        self.assertEqual(self.imported['events'], [])

    def test_only_depends_on_blocks(self):
        self.assertTrue(all(edge['type'] == 'relates-to' for edge in self.imported['edges']))
        self.assertEqual(self.imported['result']['frontier']['counts']['ready'], 2)

    def test_claim_renew_keeps_semantic_contract(self):
        claimed = self.call('claim')
        target = next(row for row in claimed['writes'] if row['external_ref'] == 'BKLG-20260101-bbbb')
        self.assertEqual(decode(target)['semantic_digest'], self.rows[-2]['semantic_digest'])
        self.assertEqual(claimed['result']['authored_revision'], 1)
        self.assertTrue(claimed['result']['lease_id'])
        self.imported['state'] = claimed['state']
        updates = {i['id']: i for i in claimed['writes']}
        self.imported['writes'] = [updates.get(i['id'], i) for i in self.imported['writes']]
        renewed = self.call('renew', lease_id=claimed['result']['lease_id'])
        self.assertEqual(renewed['result']['semantic_digest'], claimed['result']['semantic_digest'])
        self.assertEqual(renewed['result']['authored_revision'], 1)
        with self.assertRaisesRegex(Refused, 'lease generation'):
            self.call('release', lease_id='old-generation')
        with self.assertRaisesRegex(Refused, 'scope overlaps'):
            data = dict(request=dict(action='claim', epoch='test', actor='seat2', operation_id='racer', id='BKLG-20260101-cccc'),
                        state=claimed['state'], issues=self.imported['writes'], edges=claimed['edges'], now=NOW)
            evaluate(data)

    def test_derived_paths_route_scope_but_do_not_make_claims_collide(self):
        a = [('liskov-rs', 'crates/a/src/x.rs'), ('liskov-rs', 'scripts/liskov/services-census.json'),
             ('liskov-rs', 'scripts/liskov/services-cargo-metadata.json'), ('liskov-rs', 'Cargo.lock'),
             ('liskov-rs', 'crates/slipway-policy-client-contract/bundle/policy-client-bundle.json')]
        b = [('liskov-rs', 'crates/b/src/y.rs'), ('liskov-rs', 'scripts/liskov/services-census.json'),
             ('liskov-rs', 'scripts/liskov/services-cargo-metadata.json'), ('liskov-rs', 'Cargo.lock'),
             ('liskov-rs', 'crates/slipway-policy-client-contract/bundle/**')]
        self.assertTrue(policy.scopes_overlap(a, b))
        self.assertFalse(policy.claims_collide(a, b))
        # The hand-written registry and inventories still serialize.
        self.assertTrue(policy.claims_collide(a + [('liskov-rs', 'scripts/liskov/crate-boundaries.toml')],
                                              b + [('liskov-rs', 'scripts/liskov/crate-boundaries.toml')]))
        self.assertTrue(policy.claims_collide(a + [('liskov-rs', 'scripts/liskov/application-persistence-inventory.tsv')],
                                              b + [('liskov-rs', 'scripts/liskov/application-persistence-inventory.tsv')]))

    def test_a_merged_claim_no_longer_excludes_its_write_scope(self):
        def merged_into(issues, writes):
            updates = {i['id']: i for i in writes}
            return [updates.get(i['id'], i) for i in issues]

        def row_of(writes, rid):
            return decode(next(w for w in writes if w['external_ref'] == rid))

        claimed = self.call('claim')
        issues = merged_into(self.imported['writes'], claimed['writes'])
        racer = dict(action='claim', epoch='test', actor='seat2', operation_id='racer', id='BKLG-20260101-cccc')
        with self.assertRaisesRegex(Refused, 'scope overlaps live claim: BKLG-20260101-bbbb'):
            evaluate(dict(request=racer, state=claimed['state'], issues=issues, edges=claimed['edges'], now=NOW))
        merged = evaluate(dict(request=dict(action='evidence', epoch='test', actor='seat', operation_id='merged',
                                            id='BKLG-20260101-bbbb', entries=['merged:' + 'c' * 40]),
                               state=claimed['state'], issues=issues, edges=claimed['edges'], now=NOW))
        issues = merged_into(issues, merged['writes'])
        bbbb = row_of(issues, 'BKLG-20260101-bbbb')
        self.assertEqual(bbbb['fields']['status'], 'working')            # the claim itself stays
        self.assertEqual(bbbb['fields']['claimed_by'], 'seat')
        record = type('R', (), {'fm': bbbb['fields']})()
        self.assertEqual(policy.merged_commit(record), 'c' * 40)
        now = policy.parse_timestamp(NOW)
        self.assertEqual(policy.claim(record, now)[0], 'live')
        self.assertFalse(policy.blocks_scope(record, now))
        taken = evaluate(dict(request=racer, state=merged['state'], issues=issues, edges=merged['edges'], now=NOW))
        cccc = row_of(merged_into(issues, taken['writes']), 'BKLG-20260101-cccc')
        self.assertEqual(cccc['fields']['claimed_by'], 'seat2')

    def test_edit_requires_authored_revision_and_keeps_full_revision(self):
        with self.assertRaisesRegex(Refused, 'authored revision'):
            self.call('edit', expected_authored_revision=0, body='changed')
        edited = self.call('edit', expected_authored_revision=1, body='changed')
        self.assertEqual(edited['result']['authored_revision'], 2)
        self.assertEqual(edited['events'][0]['before']['body'], self.rows[-2]['body'])
        self.assertEqual(edited['events'][0]['after']['body'], 'changed')

    def test_malformed_scope_cannot_erase_reservation(self):
        with self.assertRaisesRegex(Refused, 'registered field type'):
            self.call('edit', expected_authored_revision=1, fields={'write_scope': 'synthetic:src/shared.py'})
        self.rows[-1]['fields']['write_scope'] = 'synthetic:src/shared.py'
        self.rows[-1]['semantic_digest'] = digest(semantic(self.rows[-1]['fields'], self.rows[-1]['body']))
        with self.assertRaisesRegex(Refused, 'field must be a list: write_scope'):
            encode(self.rows[-1])

    def test_raw_close_drift_and_missing_epoch_fail_closed(self):
        self.imported['writes'][-1]['status'] = 'closed'
        with self.assertRaisesRegex(Refused, 'drift'):
            self.call('snapshot')
        with self.assertRaisesRegex(Refused, 'epoch'):
            evaluate(dict(request=dict(action='snapshot'), state=self.imported['state'], issues=[], edges=[], now=NOW))

    def test_unversioned_and_corrupt_project_metadata_refused(self):
        issue = encode(self.rows[-1])
        issue['storage_class'] = 'unversioned'
        with self.assertRaisesRegex(Refused, 'retention drift'):
            decode(issue)
        issue['metadata']['proof']['schema'] = 'unknown'
        with self.assertRaisesRegex(Refused, 'unsupported project metadata'):
            decode(issue)

    def test_explicit_fallback_reservation(self):
        for row in self.rows[-2:]:
            row['fields']['write_scope'] = []
            row['semantic_digest'] = digest(semantic(row['fields'], row['body']))
        self.imported = evaluate(dict(request=self.request, state={}, issues=[], edges=[], now=NOW))
        claimed = self.call('claim', repos=['synthetic'])
        updates = {i['id']: i for i in claimed['writes']}
        issues = [updates.get(i['id'], i) for i in self.imported['writes']]
        with self.assertRaisesRegex(Refused, 'scope overlaps'):
            evaluate(dict(request=dict(action='claim', epoch='test', actor='other', operation_id='other',
                id='BKLG-20260101-cccc', repos=['synthetic']), state=claimed['state'], issues=issues,
                edges=claimed['edges'], now=NOW))

    def test_frozen_refuses_and_live_accepts_mutation(self):
        self.imported['state']['mode'] = 'frozen'
        with self.assertRaisesRegex(Refused, 'frozen'):
            self.call('claim')
        self.imported['state']['mode'] = 'live'
        self.assertEqual(self.call('claim')['result']['status'], 'working')


TODAY = NOW[:10]
PR = 'https://github.com/proof-computer/liskov-rs/pull/1'


class Ledger:
    """Applies evaluator output the way the Go helper does, in memory."""
    def __init__(self, rows=None, **import_overrides):
        self.rows = fixture() if rows is None else rows
        self.state, self.issues, self.edges, self.now = {}, {}, [], NOW
        self.do(dict(action='import', records=self.rows, epoch='test', source_sha='a' * 40,
                     operation_id='import', **import_overrides))

    def data(self, request):
        return dict(request=request, state=copy.deepcopy(self.state), issues=copy.deepcopy(list(self.issues.values())),
                    edges=copy.deepcopy(self.edges), now=self.now)

    def do(self, request):
        out = evaluate(self.data(request))
        if 'state' in out:
            self.state = out['state']
            for issue in out['writes']:
                self.issues[issue['id']] = issue
            self.edges = out['edges']
            op = request['operation_id']
            for i, event in enumerate(out['events']):
                self.issues[f'pw-event-{op}-{i}'] = dict(id=f'pw-event-{op}-{i}', metadata=event, storage_class='versioned')
            self.issues[f'pw-op-{op}'] = dict(id=f'pw-op-{op}', metadata=dict(digest='d', result=out['result']),
                                            storage_class='versioned')
        return out

    def act(self, action, actor='gascity/lrs', rid='BKLG-20260101-bbbb', op=None, **kwargs):
        request = dict(action=action, epoch='test', actor=actor, operation_id=op or action + '-' + str(len(self.issues)), **kwargs)
        if rid is not None:
            request['id'] = rid
        return self.do(request)

    def row(self, rid):
        return self.do(dict(action='snapshot', epoch='test'))['result']['records']


def rows_by_id(snapshot):
    return {r['id']: r for r in snapshot['result']['records']}


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(reservations_reconciled=True)

    def snapshot(self):
        return rows_by_id(self.ledger.do(dict(action='snapshot', epoch='test')))

    def claim(self, rid='BKLG-20260101-bbbb', actor='gascity/lrs'):
        return self.ledger.act('claim', actor=actor, rid=rid)['result']

    def complete(self, rid='BKLG-20260101-bbbb', actor='gascity/lrs', **overrides):
        row = self.snapshot()[rid]
        if not (row['fields']['status'] == 'working' and row['fields'].get('claimed_by') == actor):
            row = self.claim(rid, actor)
        request = dict(lease_id=row['lease_id'], expected_semantic_digest=row['semantic_digest'],
                       resolution='done', evidence=dict(pull_request=PR, main_sha='b' * 40,
                                                        attempts=[dict(bead_id='lrs-1', rig='lrs')]))
        request.update(overrides)
        return self.ledger.act('complete', actor=actor, rid=rid, **request)

    def test_every_event_carries_id_actor_and_operation(self):
        out = self.complete()
        for event in out['events']:
            self.assertEqual(set(event) >= {'id', 'kind', 'actor', 'at', 'operation_id', 'after'}, True)
        self.assertEqual(out['events'][0]['kind'], 'completion')
        self.assertEqual(out['events'][0]['resolution'], 'done')
        self.assertEqual(out['events'][0]['evidence']['pull_request'], PR)
        self.assertEqual(out['events'][0]['after']['fields']['status'], 'done')
        self.assertEqual(out['events'][0]['after']['fields']['done_at'], TODAY)
        self.assertEqual(out['events'][0]['after']['fields']['evidence'], [PR])
        self.assertNotIn('claimed_by', out['events'][0]['after']['fields'])
        self.assertNotIn('lease_id', out['events'][0]['after'])
        self.assertNotIn('reserved_scopes', out['events'][0]['after'])
        self.assertEqual(out['result']['status'], 'done')
        self.assertIsNone(out['result']['lease_id'])
        self.assertEqual(self.snapshot()['BKLG-20260101-bbbb']['fields']['status'], 'done')

    def test_complete_requires_live_lease_and_bound_contract(self):
        claimed = self.claim()
        base = dict(resolution='done', evidence=dict(pull_request=PR, main_sha='b' * 40))
        with self.assertRaisesRegex(Refused, 'lease generation'):
            self.ledger.act('complete', lease_id='stale', expected_semantic_digest=claimed['semantic_digest'], **base)
        with self.assertRaisesRegex(Refused, 'semantic contract'):
            self.ledger.act('complete', lease_id=claimed['lease_id'], expected_semantic_digest='0' * 64, **base)
        with self.assertRaisesRegex(Refused, 'another actor'):
            self.ledger.act('complete', actor='patrick', lease_id=claimed['lease_id'],
                            expected_semantic_digest=claimed['semantic_digest'], **base)
        self.ledger.now = '2026-10-15T18:00:00Z'
        with self.assertRaisesRegex(Refused, 'lease expired'):
            self.ledger.act('complete', lease_id=claimed['lease_id'], expected_semantic_digest=claimed['semantic_digest'], **base)
        self.ledger.now = NOW
        with self.assertRaisesRegex(Refused, 'working status'):
            self.ledger.act('complete', rid='BKLG-20260101-cccc', lease_id='x', expected_semantic_digest='y', **base)

    def test_factory_completion_needs_merged_evidence_and_cannot_supersede(self):
        with self.assertRaisesRegex(Refused, 'merged pull request'):
            self.complete(evidence=dict(acceptance=dict(ok=True)))
        with self.assertRaisesRegex(Refused, 'merged pull request'):
            self.complete(evidence=dict(pull_request=PR))
        with self.assertRaisesRegex(Refused, 'only a person may supersede'):
            self.complete(resolution='superseded', evidence={})
        with self.assertRaisesRegex(Refused, 'resolution must be'):
            self.complete(resolution='answered')
        out = self.complete(evidence=dict(merge_sha='c' * 40, main_sha='b' * 40))
        self.assertEqual(out['result']['status'], 'done')
        self.assertEqual(out['events'][0]['after']['fields']['evidence'], [])

    def test_person_completes_with_acceptance_or_supersedes(self):
        with self.assertRaisesRegex(Refused, 'merged evidence or acceptance'):
            self.complete(actor='patrick', evidence={})
        out = self.complete(actor='patrick', evidence=dict(acceptance=dict(verdict='accepted')))
        self.assertEqual(out['events'][0]['after']['fields']['evidence'], [])
        out = self.complete(rid='BKLG-20260101-cccc', actor='patrick', resolution='superseded', evidence={},
                            close_epic=True, notes='folded into bbbb')
        self.assertEqual(out['result']['status'], 'superseded')
        self.assertEqual(out['events'][0]['notes'], 'folded into bbbb')

    def test_epic_last_active_child_rule(self):
        self.complete()
        with self.assertRaisesRegex(Refused, 'last active child'):
            self.complete(rid='BKLG-20260101-cccc')
        with self.assertRaisesRegex(Refused, 'factory seat can never close an epic'):
            self.complete(rid='BKLG-20260101-cccc', close_epic=True)
        self.ledger.act('release', rid='BKLG-20260101-cccc', lease_id=self.snapshot()['BKLG-20260101-cccc']['lease_id'])
        out = self.complete(rid='BKLG-20260101-cccc', actor='patrick', close_epic=True,
                            evidence=dict(acceptance=dict(verdict='accepted')))
        kinds = [(e['id'], e['kind']) for e in out['events']]
        self.assertEqual(kinds, [('BKLG-20260101-cccc', 'completion'), ('EPIC-20260101-aaaa', 'completion')])
        rows = self.snapshot()
        self.assertEqual(rows['EPIC-20260101-aaaa']['fields']['status'], 'done')
        self.assertEqual(rows['EPIC-20260101-aaaa']['fields']['done_at'], TODAY)

    def test_close_epic_refused_while_siblings_are_active(self):
        with self.assertRaisesRegex(Refused, 'requires this to be the last active child'):
            self.complete(actor='patrick', close_epic=True, evidence=dict(acceptance=dict(verdict='accepted')))

    def test_reopen_is_person_only_and_keeps_completion_events(self):
        self.complete()
        with self.assertRaisesRegex(Refused, 'factory seat cannot reopen'):
            self.ledger.act('reopen', reason='flaky')
        with self.assertRaisesRegex(Refused, 'requires a reason'):
            self.ledger.act('reopen', actor='patrick', reason='')
        with self.assertRaisesRegex(Refused, 'done or superseded'):
            self.ledger.act('reopen', actor='patrick', rid='BKLG-20260101-cccc', reason='x')
        out = self.ledger.act('reopen', actor='patrick', reason='regressed in prod')
        self.assertEqual(out['events'][0]['kind'], 'reopened')
        self.assertEqual(out['events'][0]['reason'], 'regressed in prod')
        self.assertEqual(out['result']['status'], 'open')
        self.assertNotIn('done_at', out['events'][0]['after']['fields'])
        history = self.ledger.do(dict(action='history', epoch='test', id='BKLG-20260101-bbbb'))['result']
        self.assertEqual([e['kind'] for e in history['events']], ['claim', 'completion', 'reopened'])
        self.assertEqual(history['events'][1]['after']['fields']['status'], 'done')
        self.assertEqual([r['result']['status'] for r in history['receipts']], ['working', 'done', 'open'])

    def test_reopen_of_closed_epic_child_reopens_the_epic(self):
        self.complete()
        self.complete(rid='BKLG-20260101-cccc', actor='patrick', close_epic=True,
                      evidence=dict(acceptance=dict(verdict='accepted')))
        out = self.ledger.act('reopen', actor='patrick', reason='more to do')
        self.assertEqual([(e['id'], e['kind']) for e in out['events']],
                         [('BKLG-20260101-bbbb', 'reopened'), ('EPIC-20260101-aaaa', 'reopened')])
        rows = self.snapshot()
        self.assertEqual(rows['EPIC-20260101-aaaa']['fields']['status'], 'open')
        self.assertNotIn('done_at', rows['EPIC-20260101-aaaa']['fields'])
        epic_history = self.ledger.do(dict(action='history', epoch='test', id='EPIC-20260101-aaaa'))['result']
        self.assertEqual([e['kind'] for e in epic_history['events']], ['revision', 'completion', 'reopened'])

    def test_evidence_appends_dedups_and_carries_receipts(self):
        with self.assertRaisesRegex(Refused, 'working or done'):
            self.ledger.act('evidence', entries=['x'])
        self.claim()
        out = self.ledger.act('evidence', entries=['run-1', 'run-2', 'run-1'], receipts=dict(ci=dict(run=1)))
        self.assertEqual(out['events'][0]['kind'], 'evidence')
        self.assertEqual(out['events'][0]['receipts'], dict(ci=dict(run=1)))
        self.assertEqual(out['events'][0]['after']['fields']['evidence'], ['run-1', 'run-2'])
        self.assertEqual(out['result']['operational_revision'], 2)
        self.assertEqual(out['result']['authored_revision'], 1)
        with self.assertRaisesRegex(Refused, 'non-empty list'):
            self.ledger.act('evidence', entries=[])
        with self.assertRaisesRegex(Refused, 'receipts must be an object'):
            self.ledger.act('evidence', entries=['x'], receipts='no')
        lease = self.snapshot()['BKLG-20260101-bbbb']['lease_id']
        digest_now = self.snapshot()['BKLG-20260101-bbbb']['semantic_digest']
        self.ledger.act('complete', lease_id=lease, expected_semantic_digest=digest_now, resolution='done',
                        evidence=dict(pull_request=PR, main_sha='b' * 40))
        out = self.ledger.act('evidence', entries=['run-2', 'post-merge'])
        self.assertEqual(out['events'][0]['after']['fields']['evidence'], ['run-1', 'run-2', PR, 'post-merge'])
        self.assertEqual(out['events'][0]['after']['fields']['status'], 'done')

    def question(self, status='open', path='docs/questions/open/Q-20260101-qqqq-test.md'):
        fields = dict(doc_type='question', title='Synthetic question', status=status, owner='agent',
                      created='2026-01-01', last_reviewed='2026-01-02', source_documents=[], confidence='high',
                      related_repos=[], asked_of='owner')
        return dict(id='Q-20260101-qqqq', path=path, fields=fields, body='\n## Question\n\nWhy?\n')

    def test_author_and_answer_question(self):
        out = self.ledger.act('author', actor='patrick', rid=None, record=self.question())
        self.assertEqual(out['events'][0]['kind'], 'authored')
        self.assertEqual(out['result']['authored_revision'], 1)
        self.assertEqual(out['result']['operational_revision'], 0)
        self.assertIn('Q-20260101-qqqq', self.ledger.state['project_ids'])
        with self.assertRaisesRegex(Refused, 'only a question'):
            self.ledger.act('answer', actor='patrick', expected_authored_revision=1, body='no')
        with self.assertRaisesRegex(Refused, 'authored revision'):
            self.ledger.act('answer', actor='patrick', rid='Q-20260101-qqqq', expected_authored_revision=2, body='x')
        with self.assertRaisesRegex(Refused, 'needs a body'):
            self.ledger.act('answer', actor='patrick', rid='Q-20260101-qqqq', expected_authored_revision=1, body=' ')
        with self.assertRaisesRegex(Refused, 'lifecycle'):
            self.ledger.act('answer', actor='patrick', rid='Q-20260101-qqqq', expected_authored_revision=1,
                            body='x', fields=dict(status='open'))
        out = self.ledger.act('answer', actor='patrick', rid='Q-20260101-qqqq', expected_authored_revision=1,
                              body='# Decided\n', fields=dict(resolution='decided', related_backlog=['BKLG-20260101-bbbb']))
        self.assertEqual(out['events'][0]['kind'], 'answered')
        self.assertEqual(out['result']['status'], 'answered')
        self.assertEqual(out['result']['authored_revision'], 2)
        row = out['events'][0]['after']
        self.assertEqual(row['path'], 'docs/questions/answered/Q-20260101-qqqq-test.md')
        self.assertEqual(row['fields']['answered'], TODAY)
        self.assertEqual(row['fields']['resolution'], 'decided')
        self.assertEqual(row['body'], '# Decided\n')
        with self.assertRaisesRegex(Refused, 'not open'):
            self.ledger.act('answer', actor='patrick', rid='Q-20260101-qqqq', expected_authored_revision=2, body='x')
        self.assertEqual(self.snapshot()['Q-20260101-qqqq']['fields']['status'], 'answered')

    def item(self, code='dddd', **overrides):
        fields = dict(self.ledger.rows[-1]['fields'])
        fields.pop('created'); fields.pop('last_reviewed')
        fields.update(write_scope=['synthetic:src/other.py'], rank='5', **overrides)
        return dict(id=f'BKLG-20260101-{code}', path=f'docs/backlog/items/BKLG-20260101-{code}-new.md',
                    fields=fields, body='\n## Acceptance\n\nNew.\n')

    def test_author_item_defaults_then_claim(self):
        out = self.ledger.act('author', actor='patrick', rid=None, record=self.item())
        row = out['events'][0]['after']
        self.assertEqual((row['fields']['created'], row['fields']['last_reviewed']), (TODAY, TODAY))
        self.assertEqual(row['source_text'], '')
        self.assertEqual(self.ledger.state['revision'], 1)
        claimed = self.ledger.act('claim', rid='BKLG-20260101-dddd')['result']
        self.assertEqual(claimed['status'], 'working')
        history = self.ledger.do(dict(action='history', epoch='test', id='BKLG-20260101-dddd'))['result']
        self.assertEqual([e['kind'] for e in history['events']], ['authored', 'claim'])
        self.assertEqual(len(history['receipts']), 2)

    def test_author_refusals(self):
        refusals = [
            ('identity already present', self.item('bbbb')),
            ('path must be', dict(self.item(), path='docs/backlog/items/BKLG-20260101-eeee-x.md')),
            ('path must be', dict(self.item(), path='docs/backlog/BKLG-20260101-dddd-x.md')),
            ('path must be', dict(self.item(), id='EPIC-20260101-dddd', path='docs/backlog/items/EPIC-20260101-dddd-x.md')),
            ('authored open', self.item(status='working')),
            ('missing required scalar field: parent_epic', self.item(parent_epic=None)),
            ('unknown prerequisite', self.item(depends_on=['BKLG-20260101-zzzz'])),
            ('invalid active milestone lane', self.item(lane='nope')),
            ('parent_epic references unknown epic', self.item(parent_epic='EPIC-20260101-zzzz')),
            ('backlog item, epic or question', dict(self.item(), fields=dict(self.item()['fields'], doc_type='milestone'))),
            ('no claim or completion', self.item(done_at='2026-01-01')),
        ]
        for pattern, record in refusals:
            if 'parent_epic' in record['fields'] and record['fields']['parent_epic'] is None:
                record['fields'].pop('parent_epic')
            with self.assertRaisesRegex(Refused, pattern, msg=pattern):
                self.ledger.act('author', actor='patrick', rid=None, record=record)
        with self.assertRaisesRegex(Refused, 'dependency cycle'):
            self.ledger.act('author', actor='patrick', rid=None, records=[
                self.item('dddd', depends_on=['BKLG-20260101-eeee']),
                dict(self.item('eeee', depends_on=['BKLG-20260101-dddd']), **{}),
            ])
        self.assertEqual(self.ledger.state['revision'], 0)

    def test_author_epic_with_first_item_atomically(self):
        epic_fields = dict(self.ledger.rows[1]['fields'], epic_id='EPIC-20260101-ffff', rank='1')
        epic = dict(id='EPIC-20260101-ffff', path='docs/backlog/epics/EPIC-20260101-ffff-new.md', fields=epic_fields, body='x')
        with self.assertRaisesRegex(Refused, 'at least one active backlog item'):
            self.ledger.act('author', actor='patrick', rid=None, record=epic)
        bad = dict(epic, fields=dict(epic_fields, epic_id='EPIC-20260101-aaaa'))
        with self.assertRaisesRegex(Refused, 'epic_id must equal'):
            self.ledger.act('author', actor='patrick', rid=None, record=bad)
        out = self.ledger.act('author', actor='patrick', rid=None,
                              records=[epic, self.item('gggg', parent_epic='EPIC-20260101-ffff')])
        self.assertEqual(out['result']['ids'], ['EPIC-20260101-ffff', 'BKLG-20260101-gggg'])
        self.assertEqual([e['kind'] for e in out['events']], ['authored', 'authored'])
        self.assertEqual(len(out['writes']), 2)
        self.assertEqual(self.snapshot()['BKLG-20260101-gggg']['fields']['parent_epic'], 'EPIC-20260101-ffff')

    def test_activate_and_freeze(self):
        with self.assertRaisesRegex(Refused, 'source revision changed'):
            self.ledger.act('activate', actor='patrick', rid=None, expected_source_sha='b' * 40)
        with self.assertRaisesRegex(Refused, 'freeze requires live'):
            self.ledger.act('freeze', actor='patrick', rid=None, reason='x')
        out = self.ledger.act('activate', actor='patrick', rid=None, expected_source_sha='a' * 40)
        self.assertEqual(out['events'][0]['kind'], 'activated')
        self.assertEqual(out['result']['mode'], 'live')
        self.assertEqual(out['result']['ledger_revision'], 1)
        self.assertEqual((self.ledger.state['mode'], self.ledger.state['activated_by']), ('live', 'patrick'))
        self.assertEqual(self.ledger.state['activated_at'], NOW)
        self.assertEqual(self.ledger.edges, self.ledger.do(dict(action='snapshot', epoch='test')) and self.ledger.edges)
        with self.assertRaisesRegex(Refused, 'activate requires shadow'):
            self.ledger.act('activate', actor='patrick', rid=None, expected_source_sha='a' * 40)
        self.assertEqual(self.claim()['status'], 'working')
        revision = self.ledger.do(dict(action='revision', epoch='test'))['result']
        self.assertEqual(revision, dict(epoch='test', mode='live', revision=2, source_sha='a' * 40, generated_at=NOW))
        with self.assertRaisesRegex(Refused, 'freeze requires a reason'):
            self.ledger.act('freeze', actor='patrick', rid=None, reason=' ')
        out = self.ledger.act('freeze', actor='patrick', rid=None, reason='rollback to git')
        self.assertEqual((out['events'][0]['kind'], out['events'][0]['reason']), ('frozen', 'rollback to git'))
        self.assertEqual(self.ledger.state['freeze_reason'], 'rollback to git')
        for action, extra in (('claim', dict(rid='BKLG-20260101-cccc')), ('activate', dict(rid=None, expected_source_sha='a' * 40)),
                              ('freeze', dict(rid=None, reason='again')), ('author', dict(rid=None, record=self.item()))):
            with self.assertRaisesRegex(Refused, 'frozen'):
                self.ledger.act(action, actor='patrick', **extra)
        snapshot = self.ledger.do(dict(action='snapshot', epoch='test'))['result']
        self.assertEqual(snapshot['mode'], 'frozen')
        self.assertEqual(self.ledger.do(dict(action='revision', epoch='test'))['result']['mode'], 'frozen')

    def test_activate_requires_reconciled_reservations(self):
        ledger = Ledger()
        with self.assertRaisesRegex(Refused, 'not reconciled'):
            ledger.act('activate', actor='patrick', rid=None, expected_source_sha='a' * 40)

    def test_history_refuses_unknown_and_orders_events(self):
        with self.assertRaisesRegex(Refused, 'unknown work identity'):
            self.ledger.do(dict(action='history', epoch='test', id='BKLG-20260101-zzzz'))
        claimed = self.ledger.act('claim', op='claim-4')['result']
        self.ledger.now = '2026-10-14T18:00:01Z'
        self.ledger.act('renew', lease_id=claimed['lease_id'], op='renew-b')
        self.ledger.act('evidence', entries=['e'], op='a-later-op')
        history = self.ledger.do(dict(action='history', epoch='test', id='BKLG-20260101-bbbb'))['result']
        self.assertEqual([(e['at'], e['operation_id'], e['kind']) for e in history['events']],
                         [(NOW, 'claim-4', 'claim'), ('2026-10-14T18:00:01Z', 'a-later-op', 'evidence'),
                          ('2026-10-14T18:00:01Z', 'renew-b', 'renew')])
        self.assertTrue(all(e['id'] == 'BKLG-20260101-bbbb' for e in history['events']))
        self.assertEqual([r['operation_id'] for r in history['receipts']], ['claim-4', 'renew-b', 'a-later-op'])
        epic = self.ledger.do(dict(action='history', epoch='test', id='EPIC-20260101-aaaa'))['result']
        self.assertEqual([e['kind'] for e in epic['events']], ['revision'])
        self.assertEqual(epic['receipts'], [])

    def test_read_only_actions_need_no_actor_and_ignore_mode(self):
        self.ledger.state['mode'] = 'frozen'
        self.assertEqual(self.ledger.do(dict(action='revision', epoch='test'))['result']['mode'], 'frozen')
        self.assertEqual(self.ledger.do(dict(action='history', epoch='test', id='BKLG-20260101-bbbb'))['result']['events'], [])
        with self.assertRaisesRegex(Refused, 'epoch'):
            self.ledger.do(dict(action='revision', epoch='other'))

    def test_existing_refusals_survive(self):
        with self.assertRaisesRegex(Refused, 'requires actor'):
            self.ledger.do(dict(action='claim', epoch='test', id='BKLG-20260101-bbbb', operation_id='x', actor=''))
        with self.assertRaisesRegex(Refused, 'unknown work identity'):
            self.ledger.act('claim', rid='BKLG-20260101-zzzz')
        with self.assertRaisesRegex(Refused, 'unsupported checked action'):
            self.ledger.act('close')
        self.claim()
        with self.assertRaisesRegex(Refused, 'work is not ready'):
            self.claim()
        with self.assertRaisesRegex(Refused, 'retained claim'):
            self.ledger.act('edit', expected_authored_revision=1, body='x')
        with self.assertRaisesRegex(Refused, 'schema registration'):
            self.ledger.act('edit', rid='BKLG-20260101-cccc', expected_authored_revision=1, fields=dict(brand_new='x'))


if __name__ == '__main__':
    unittest.main()
