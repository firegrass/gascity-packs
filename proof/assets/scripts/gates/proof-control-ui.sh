#!/usr/bin/env bash
# Gate for proof-control-ui, run by the ORCHESTRATOR in the packet worktree:
# CI's `build` job, command for command. `pnpm build` type-checks the project
# before Vite builds it; `typecheck` is named separately so a failure says
# which half failed.
#
# Note for whoever changes this rig's policy: CI's other job DEPLOYS this
# service to Fly on every push to `main`, so a merge here is a production
# deploy.
set -euo pipefail
echo "gate: node $(node --version), pnpm $(pnpm --version)" >&2
pnpm install --frozen-lockfile --prefer-offline
pnpm typecheck
pnpm build
echo "gate: passed" >&2
