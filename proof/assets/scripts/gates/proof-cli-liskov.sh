#!/usr/bin/env bash
# Gate for proof-cli-liskov, run by the ORCHESTRATOR in the packet worktree.
#
# This repository has NO pull-request CI: its only workflow publishes to npm on
# a `v*` tag. Its AGENTS.md names this sequence as the gate, and here that is
# literal — nothing else runs on the change before it reaches `main`, which is
# why its policy merges on the gate rather than on a green check.
#
# `pnpm smoke:proof-plugin` is deliberately NOT here: it needs a sibling
# `proof-cli` checkout through PROOF_CLI_ROOT, which a packet worktree has no
# honest way to point at.
set -euo pipefail
echo "gate: node $(node --version), pnpm $(pnpm --version)" >&2
pnpm install --frozen-lockfile --prefer-offline
pnpm typecheck
pnpm test
pnpm build
pnpm pack:dry-run
echo "gate: passed" >&2
