"""Advisory scope closure from repository-owned registries, never permission.

Counts may stay unchanged. These are early inspection hints, not blanket scope
requirements and never authority to raise a ratchet or classify new debt.
"""
import json
from pathlib import Path

# A migration that creates or drops a `liskov_*` table also adds or removes its
# row in the ADR-0015 projection inventory, or `cargo test -p slipway --test
# projection_inventory` refuses the whole tree. BKLG-20261006-hkmb stopped on
# exactly this on 2026-10-09 (`projection_inventory_outside_scope_refuses_nonce_table`):
# its scope held migrations/** and not the inventory file, and the item told the
# worker a different inventory was "not forced".
MIGRATIONS_PROBE = "crates/slipway/migrations/0000_scope_probe.sql"
PROJECTION_INVENTORY = "crates/slipway/tests/projection_inventory/inventory_table.rs"


def inspect(repo, overlaps):
    root = Path(repo)
    hints = []
    if (root / PROJECTION_INVENTORY).is_file() and overlaps(MIGRATIONS_PROBE) \
            and not overlaps(PROJECTION_INVENTORY):
        hints.append("%s is outside write_scope while crates/slipway/migrations/ is inside it. A migration "
                     "that creates or drops a liskov_* table needs its row there "
                     "(every_liskov_table_has_a_declared_and_satisfied_contract, "
                     "crates/slipway/tests/projection_inventory.rs), or the projection_inventory test "
                     "target refuses the tree. If the migration creates or drops a table, add the file to "
                     "the scope and shape the exact row; a column-only migration needs nothing."
                     % PROJECTION_INVENTORY)
    census = "scripts/liskov/policy-dispatch-census-baseline.json"
    ratchet = "scripts/liskov/source-size-ratchets.tsv"
    for registry in (census, ratchet):
        path = root / registry
        if not path.is_file() or overlaps(registry):
            continue
        try:
            if registry == census:
                paths = json.loads(path.read_text())["paths"]
            else:
                paths = [line.split("\t", 1)[0] for line in path.read_text().splitlines()
                         if line.strip() and not line.startswith("#")]
            hits = sorted(p for p in paths if overlaps(p))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            hints.append("cannot inspect %s: %s; check its owner guard before implementation" % (registry, exc))
            continue
        if hits:
            hints.append("%s tracks scoped file(s) %s but is outside write_scope. Check whether the intended "
                         "change alters its counts; if it does, shape the exact companion edit before implementation. "
                         "Do not widen scope or raise a ceiling automatically."
                         % (registry, ", ".join(hits[:4])))
    return hints
