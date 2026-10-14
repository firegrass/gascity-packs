#!/usr/bin/env bash
# Gate for liskov-github-actions, run by the ORCHESTRATOR in the packet
# worktree: CI's `build` job, step for step. `check:dist` is the one that
# matters most here — the actions are consumed from the committed `actions/`
# bundle, so a source change whose dist was not rebuilt ships nothing.
set -euo pipefail
echo "gate: node $(node --version), pnpm $(pnpm --version)" >&2
pnpm install --frozen-lockfile --prefer-offline
pnpm typecheck
pnpm test
python3 -m compileall -q actions/cargo-runtime-image-build actions/source-assurance
pnpm check:dist
echo "gate: passed" >&2
