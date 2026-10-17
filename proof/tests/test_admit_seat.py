"""The factory's claim seat (ADR-0239 decision 4): one name, read in gc's
order, under `gascity/<factory>/<rig>`; and the one test every pack client
uses to recognise its own claims, including the two-part seat of claims made
before pack 0.44.0."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'assets/scripts'
sys.path.insert(0, str(SCRIPTS))
import admit  # noqa: E402
import proof_work_evaluate as ev  # noqa: E402
import proof_work_records as records  # noqa: E402

CLEAN = {k: v for k, v in os.environ.items() if not k.startswith(('GC_', 'LISKOV_'))}


class FactoryNameTests(unittest.TestCase):
    def name(self, files, env=None):
        with tempfile.TemporaryDirectory() as root:
            city = Path(root) / 'proofcity'
            (city / '.gc').mkdir(parents=True)
            for rel, text in files.items():
                (city / rel).write_text(text)
            with patch.dict(os.environ, {**CLEAN, 'GC_CITY': str(city), **(env or {})}, clear=True):
                return admit.factory_name(), admit.seat('lrs')

    def test_gc_order_env_then_site_binding_then_city_toml_then_basename(self):
        both = {'.gc/site.toml': 'workspace_name = "proof"\n', 'city.toml': '[workspace]\nname = "declared"\n'}
        self.assertEqual(self.name(both, {'GC_CITY_NAME': 'env'}), ('env', 'gascity/env/lrs'))
        self.assertEqual(self.name(both), ('proof', 'gascity/proof/lrs'))
        self.assertEqual(self.name({'city.toml': '[workspace]\nname = "declared"\n'}), ('declared', 'gascity/declared/lrs'))
        self.assertEqual(self.name({'city.toml': '[workspace]\nprovider = "codex"\n'}), ('proofcity', 'gascity/proofcity/lrs'))

    def test_a_name_that_is_not_one_segment_is_refused(self):
        for bad in ('pro/of', 'a b', 'q?', 'x#y', 'p%'):
            with self.assertRaisesRegex(admit.Refused, 'not usable in a URL path'):
                self.name({}, {'GC_CITY_NAME': bad})
            with self.assertRaisesRegex(admit.Refused, 'not usable in a URL path'):
                self.name({'.gc/site.toml': 'workspace_name = "%s"\n' % bad})

    def test_no_city_is_a_refusal_not_a_guess(self):
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        with patch.dict(os.environ, CLEAN, clear=True), tempfile.TemporaryDirectory() as root:
            os.chdir(root)
            with self.assertRaisesRegex(admit.Refused, 'cannot find the city'):
                admit.factory_name()


class SeatTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {**CLEAN, 'GC_CITY_NAME': 'p620'}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_seat_parts(self):
        self.assertEqual(admit.seat('liskov-rs'), 'gascity/p620/liskov-rs')
        self.assertEqual(admit.seat_rig('gascity/p620/liskov-rs'), 'liskov-rs')
        self.assertEqual(admit.seat_factory('gascity/p620/liskov-rs'), 'p620')
        self.assertEqual(admit.seat_rig('gascity/liskov-rs'), 'liskov-rs')     # the pre-0.44.0 form
        self.assertEqual(admit.seat_factory('gascity/liskov-rs'), '')
        for person in ('patrick', 'claude-83o6', '', None, 'gascity/', 'gascity//x', 'gascity/a/b/c'):
            self.assertEqual((admit.seat_rig(person), admit.seat_factory(person)), ('', ''), person)

    def test_held_by_us_recognises_this_factory_and_the_legacy_seat_only(self):
        self.assertTrue(admit.held_by_us('gascity/p620/liskov-rs', 'liskov-rs'))
        self.assertTrue(admit.held_by_us('gascity/liskov-rs', 'liskov-rs'))
        self.assertFalse(admit.held_by_us('gascity/other/liskov-rs', 'liskov-rs'))
        self.assertFalse(admit.held_by_us('gascity/p620/liskov-ui', 'liskov-rs'))
        self.assertFalse(admit.held_by_us('patrick', 'liskov-rs'))
        self.assertFalse(admit.held_by_us(None, 'liskov-rs'))

    def test_the_evaluator_parses_seats_the_same_way(self):
        for value in ('gascity/p620/liskov-rs', 'gascity/liskov-rs', 'patrick', 'gascity//x', 'gascity/a/b/c'):
            self.assertEqual(ev.seat_factory(value) or '', admit.seat_factory(value), value)
            self.assertEqual(ev.seat_rig(value) or '', admit.seat_rig(value), value)

    def test_factory_field_grammar_matches_the_seat_segment(self):
        for good in ('proof', 'p620', 'patrick-laptop', 'dan.ubuntu', 'a_b'):
            self.assertEqual(records.factory_problem(good), '', good)
        for bad in ('', 'a/b', 'a b', '-x', 'a?b', None, ['proof']):
            self.assertTrue(records.factory_problem(bad), repr(bad))


if __name__ == '__main__':
    unittest.main()
