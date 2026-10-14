#!/usr/bin/env bash
# Gate for proof-control, run by the ORCHESTRATOR in the packet worktree: CI's
# `build` job, command for command.
#
# Note for whoever changes this rig's policy: CI's other job DEPLOYS this
# service to Fly on every push to `main`, so a merge here is a production
# deploy. That is why the rig lands at `pr` and merges only when `build` is
# green.
set -euo pipefail
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-8}"   # every rig's workers share this host
echo "gate: cargo fmt" >&2
cargo fmt --all -- --check
echo "gate: clippy -D warnings" >&2
cargo clippy --all-targets --locked -- -D warnings
echo "gate: test" >&2
cargo test --locked
echo "gate: passed" >&2
