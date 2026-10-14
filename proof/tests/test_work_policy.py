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

    def test_unsupported_completion_and_live_mode_refused(self):
        with self.assertRaisesRegex(Refused, 'unsupported checked action'):
            self.call('complete')
        self.imported['state']['mode'] = 'live'
        with self.assertRaisesRegex(Refused, 'live mutation qualification'):
            self.call('claim')


if __name__ == '__main__':
    unittest.main()
