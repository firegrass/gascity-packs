#!/usr/bin/env bash
# Gate for baton, run by the ORCHESTRATOR in the packet worktree: CI's `rust`
# job, minus its five end-to-end suites.
#
# Those suites drive the built binary through a pty and tmux against a fixture
# workspace, and they do not pass on this host — outside gc's sandbox entirely,
# with a full environment, `tests/e2e.py` fails at its "run is unavailable"
# assertion (2026-09-20), and inside the sandbox the pty child dies before the
# first drain (`OSError: [Errno 5]` reading the master). A gate that cannot pass
# is not a gate: it would stop every packet in this rig before a worker's work
# was ever looked at.
#
# They are not lost. baton's policy is `merge = "when-green"` on the `rust`
# check, which runs all five on a GitHub runner, so nothing merges here without
# them — they are simply not the local pre-check. Whoever makes them run on this
# host should put them back.
set -euo pipefail
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-8}"   # every rig's workers share this host
echo "gate: cargo fmt" >&2
cargo fmt --check
echo "gate: clippy -D warnings" >&2
cargo clippy --all-targets --locked -- -D warnings
echo "gate: test" >&2
cargo test --locked
echo "gate: build" >&2
cargo build --locked
echo "gate: passed" >&2
