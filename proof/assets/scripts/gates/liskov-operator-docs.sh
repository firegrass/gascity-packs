#!/usr/bin/env bash
# Gate for liskov-operator-docs (the internal operator handbook), run by the
# ORCHESTRATOR in the packet worktree: the repository's own validation, whole,
# which is exactly what its `build` check runs on every pull request.
#
# `pnpm build` runs `prebuild` = `scripts/check-no-cross-deps.mjs`, the
# structural test that keeps this site a sibling of the public docs rather than
# a downstream of it; it is named here as well so a failure says which check
# failed rather than "prebuild". Broken links and broken Markdown links are
# fatal in the config on purpose — a dead link in a runbook is a dead end in an
# incident.
set -euo pipefail
echo "gate: node $(node --version), pnpm $(pnpm --version)" >&2
pnpm install --frozen-lockfile --prefer-offline
node scripts/check-no-cross-deps.mjs
pnpm typecheck
pnpm build
echo "gate: passed" >&2
