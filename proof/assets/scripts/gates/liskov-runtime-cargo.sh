#!/usr/bin/env bash
# Gate for liskov-runtime-cargo, run by the ORCHESTRATOR in the packet worktree:
# CI's `Rust 1.85 gates` job, command for command.
#
# What it deliberately does not do: CI's other two jobs build the aarch64
# musl static helper and exercise it under QEMU/PRoot. This host cannot
# cross-build them honestly (`ring` wants a musl cross-gcc), and the native
# ARM64 runner is the authority for that artifact. So this rig lands at `pr`
# and merges only when all three CI job names are green — the gate is the fast
# half, not the whole.
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
echo "gate: deterministic release contract" >&2
scripts/test-package-release.sh
echo "gate: passed" >&2
