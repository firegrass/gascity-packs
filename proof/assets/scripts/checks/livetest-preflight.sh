#!/usr/bin/env bash
# Check for the live-test `preflight` step (ADR-0151 clause 4): refuse a packet
# before it touches anything. Exit 0 = the run may observe; non-zero = refused,
# and the first line names the condition — credential, target, build, freshness,
# outstanding, spend_fence, exercise_not_implemented, configured_command or run.
# One attempt: a re-run is a second production read the manifest never budgeted.
#
# A thin entrypoint: it resolves GC_BEAD_ID to the workflow root with the shared
# check helper and hands the root's facts, as data, to livetest_runner.py, which
# decides (docs/knowledge/production-validation-observe-contract.md). Nothing
# read from a bead reaches a shell; every value travels as one argv element.
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib.sh"
ROOT_JSON="$(proof_root_json)"
exec python3 "$HERE/../livetest_runner.py" preflight \
  --run-id="$(metadata_of "$ROOT_JSON" proof.live_run_id)" \
  --live-target="$(root_var "$ROOT_JSON" live_target)" \
  --mutate="$(root_var "$ROOT_JSON" mutate)" \
  --spend="$(root_var "$ROOT_JSON" spend)" \
  --spend-cap="$(root_var "$ROOT_JSON" spend_cap)" \
  --preflight-command="$(root_var "$ROOT_JSON" preflight_command)" \
  --settle-command="$(root_var "$ROOT_JSON" settle_command)" \
  --tool-path="$(root_var "$ROOT_JSON" gate_path)"
