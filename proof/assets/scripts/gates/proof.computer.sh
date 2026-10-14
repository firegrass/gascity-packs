#!/usr/bin/env bash
# Gate for proof.computer (the marketing site), run by the ORCHESTRATOR in the
# packet worktree. The repository has no GitHub Actions workflow at all; Vercel
# builds it through `scripts/build-vercel.sh`, which downloads the pinned Zola
# release, verifies its digest and generates `public/`. Running that exact
# script is therefore the strongest honest gate available, and it needs no Zola
# on this host.
#
# The Autumn pricing sync ships its own test; run it when scripts/ changed.
set -euo pipefail
BASE="$(git merge-base origin/main HEAD)"
if git diff --name-only "$BASE" HEAD | grep -q '^scripts/'; then
  echo "gate: scripts/ changed; running the pricing sync test" >&2
  python3 scripts/test-sync-autumn-pricing.py
fi
echo "gate: zola build through the Vercel build script" >&2
sh scripts/build-vercel.sh
echo "gate: passed" >&2
