#!/usr/bin/env bash
# Gate for liskov-network-prober, run by the ORCHESTRATOR in the packet
# worktree: CI's `test` job, command for command, minus the container build.
#
# This repository fires NO Actions run on a pull request or on a push to main —
# only `workflow_dispatch` reaches it reliably — so a green pull request proves
# nothing here and this gate is the evidence. Its CI `deploy` job deploys to
# Fly, so merging is an operator's decision: the rig lands at `pr` and no
# `merge` policy merges it.
#
# `docker build` is CI's last step. It is skipped here: the daemon is shared
# with everything else on this host and a gate that can exhaust it is a gate
# that takes the whole factory down.
set -euo pipefail
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-8}"   # every rig's workers share this host
echo "gate: cargo fmt" >&2
cargo fmt --all --check
echo "gate: clippy -D warnings" >&2
cargo clippy --all-targets --all-features --locked -- -D warnings
echo "gate: test" >&2
cargo test --all-features --locked
echo "gate: passed" >&2
