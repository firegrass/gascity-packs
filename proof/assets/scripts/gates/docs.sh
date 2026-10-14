#!/usr/bin/env bash
# Gate for `docs` (the public Liskov/Baran Docusaurus site), run by the
# ORCHESTRATOR in the packet worktree: the repository's own validation, whole
# (its AGENTS.md). The repository has NO pull-request GitHub Actions workflow —
# Vercel builds the preview — so this and the Vercel build are the only two
# things between a worker and a production docs deploy.
#
# `pnpm build` runs `prebuild` = `check:liskov`, which pins the released
# contract against the fixtures; it is named here as well so a failure says
# which check failed rather than "prebuild".
set -euo pipefail
echo "gate: node $(node --version), pnpm $(pnpm --version)" >&2
pnpm install --frozen-lockfile --prefer-offline
pnpm check:liskov
pnpm typecheck
pnpm typecheck:liskov-examples
pnpm build
echo "gate: passed" >&2
