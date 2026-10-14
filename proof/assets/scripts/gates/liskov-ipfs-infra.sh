#!/usr/bin/env bash
# Gate for liskov-ipfs-infra, run by the ORCHESTRATOR in the packet worktree.
# The repository has NO CI workflow of any kind, so this is the only thing
# between a worker and `main` — which is why its policy merges on the gate.
# `typecheck` and `test` both build first (the workspace packages depend on
# each other's `dist/`), so the order below is the repository's own.
#
# `pnpm synth` (CDK) is not here: it wants AWS context and credentials, which a
# gate must not have.
set -euo pipefail
echo "gate: node $(node --version), pnpm $(pnpm --version)" >&2
pnpm install --frozen-lockfile --prefer-offline
pnpm build
pnpm typecheck
pnpm test
echo "gate: passed" >&2
