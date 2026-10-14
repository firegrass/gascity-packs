#!/usr/bin/env bash
# Gate for liskov-secret-broker-js, run by the ORCHESTRATOR in the packet
# worktree. The repository has NO CI workflow, so this is the only thing
# between a worker and `main`.
set -euo pipefail
echo "gate: node $(node --version), pnpm $(pnpm --version)" >&2
pnpm install --frozen-lockfile --prefer-offline
pnpm typecheck
pnpm test
pnpm build
pnpm pack:dry-run
echo "gate: passed" >&2
