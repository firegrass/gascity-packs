#!/usr/bin/env bash
# Gate for liskov-diagnostic, run by the ORCHESTRATOR in the packet worktree:
# CI's `validate` job, step for step. The bundle smoke runs without external
# telemetry, as in CI.
#
# Note for whoever changes this rig's policy: a push to `main` in this
# repository dispatches live Acurast probe workflows, which cost money. That is
# a reason to be careful about what may merge here, not a reason to weaken this
# gate.
set -euo pipefail
echo "gate: node $(node --version), pnpm $(pnpm --version)" >&2
pnpm install --frozen-lockfile --prefer-offline
pnpm typecheck
pnpm test
pnpm build
pnpm smoke:bundle
echo "gate: passed" >&2
