#!/usr/bin/env bash
# Gate for liskov-self-custody-signer, run by the ORCHESTRATOR in the packet
# worktree: CI's `Test and build` job, command for command. This repository
# holds signing code, so nothing here is diff-aware — the whole workspace is
# formatted, linted with warnings denied, built and tested on every packet.
set -euo pipefail
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-8}"   # every rig's workers share this host
rustup component add rustfmt clippy >/dev/null 2>&1 || true
echo "gate: cargo fmt" >&2
cargo fmt --all --check
echo "gate: clippy -D warnings" >&2
cargo clippy --all-targets --all-features --locked -- -D warnings
echo "gate: build" >&2
cargo build --workspace --all-targets --locked
echo "gate: test" >&2
cargo test --workspace --all-features --locked
echo "gate: passed" >&2
