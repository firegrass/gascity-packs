#!/usr/bin/env bash
# Gate for liskov-runtime-images, run by the ORCHESTRATOR in the packet
# worktree. The repository owns its own change classifier, and its AGENTS.md
# names `scripts/validate-change.sh` as the gate: unit tests and script
# compile checks always, and for a MATERIAL change the actual construction of
# each affected rootfs target.
#
# It is given the packet's base so it classifies the packet's diff, not the
# whole tree. A material change on a host with no AArch64 cross-compiler and no
# `liskov-runtime-images-crosscc:local` image exits 2 — the gate fails closed
# and the packet stops, which is correct: an image change nobody built is not
# evidence of anything.
set -euo pipefail
BASE="$(git merge-base origin/main HEAD)"
echo "gate: validate-change.sh against $BASE" >&2
scripts/validate-change.sh "$BASE"
echo "gate: passed" >&2
