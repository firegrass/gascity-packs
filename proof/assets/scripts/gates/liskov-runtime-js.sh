#!/usr/bin/env bash
# Gate for liskov-runtime-js, run by the ORCHESTRATOR in the packet worktree:
# CI's `Typecheck, Test, Build, Pack` job, step for step. The package is
# published from this tree, so the pack dry run is part of the gate rather than
# an extra.
set -euo pipefail
echo "gate: node $(node --version), pnpm $(pnpm --version)" >&2
pnpm install --frozen-lockfile --prefer-offline
pnpm typecheck
pnpm test
pnpm build
pnpm pack:dry-run
echo "gate: passed" >&2
