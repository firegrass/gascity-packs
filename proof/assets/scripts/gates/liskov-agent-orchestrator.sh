#!/usr/bin/env bash
# Diff-aware gate for the orchestrator repository, run by the ORCHESTRATOR in the
# packet worktree. The documentation system's validator always; and when the diff
# touches gascity/ — the factory's own trusted code, which the factory may now be
# asked to change — every test the pack has. Without this a worker could land a
# broken dispatcher behind a green docs check.
set -euo pipefail
BASE="$(git merge-base origin/main HEAD)"
if [ -f scripts/resolve_proof_pack.py ]; then
  python3 scripts/resolve_proof_pack.py
fi
# Older in-flight worktrees can precede the receipt rollout. Integrated trees
# carrying the change checker enforce adoption against their actual merge base.
if [ -f gascity/check_shaping_changes.py ]; then
  python3 gascity/check_shaping_changes.py --base "$BASE"
fi
echo "gate: validate_docs" >&2
python3 scripts/validate_docs.py
if git diff --name-only "$BASE" HEAD | grep -q '^gascity/'; then
  echo "gate: gascity/ changed; running the pack's tests" >&2
  gascity/tests/test_checks.sh
  for t in gascity/tests/test_*.py; do
    echo "gate: $t" >&2
    python3 "$t"
  done
  for f in gascity/packs/proof/assets/scripts/*.py gascity/*.py; do python3 -m py_compile "$f"; done
fi
echo "gate: passed" >&2
