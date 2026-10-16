"""Which merge conflicts `pr_recovery.py integrate` settles by regeneration."""
from pathlib import Path
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'assets/scripts'
sys.path.insert(0, str(SCRIPTS))
import pr_recovery  # noqa: E402

CENSUS = 'scripts/liskov/services-census.json'
METADATA = 'scripts/liskov/services-cargo-metadata.json'


class RegenerationPlanTests(unittest.TestCase):
    def test_bundle_alone_is_the_bundle_row(self):
        rows, left = pr_recovery.regeneration_plan('liskov-rs', [pr_recovery.BUNDLE])
        self.assertEqual([row[1] for row in rows], [(pr_recovery.BUNDLE,)])
        self.assertEqual(left, [])

    def test_census_and_lock_are_regenerated_and_source_is_left(self):
        rows, left = pr_recovery.regeneration_plan('liskov-rs', [CENSUS, 'Cargo.lock', 'crates/x/src/lib.rs'])
        self.assertEqual([row[1] for row in rows], [(METADATA, CENSUS), ('Cargo.lock',)])
        self.assertEqual(left, ['crates/x/src/lib.rs'])

    def test_a_partial_census_conflict_still_regenerates_the_whole_set(self):
        rows, left = pr_recovery.regeneration_plan('liskov-rs', [METADATA])
        self.assertEqual([row[1] for row in rows], [(METADATA, CENSUS)])
        self.assertEqual(left, [])

    def test_hand_written_registries_are_never_regenerated(self):
        rows, left = pr_recovery.regeneration_plan('liskov-rs', ['scripts/liskov/crate-boundaries.toml',
                                                                 'scripts/liskov/application-persistence-inventory.tsv'])
        self.assertEqual(rows, [])
        self.assertEqual(len(left), 2)

    def test_another_repository_has_no_rows(self):
        rows, left = pr_recovery.regeneration_plan('liskov-ui', ['package-lock.json', CENSUS])
        self.assertEqual(rows, [])
        self.assertEqual(left, ['package-lock.json', CENSUS])

    def test_cargo_lock_takes_main_then_updates_workspace_members(self):
        row = next(r for r in pr_recovery.REGENERABLE if r[1] == ('Cargo.lock',))
        self.assertEqual(row[2], '--theirs')
        self.assertEqual(row[3], (['cargo', 'update', '--workspace'],))


if __name__ == '__main__':
    unittest.main()
