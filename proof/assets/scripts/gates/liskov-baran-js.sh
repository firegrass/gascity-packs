#!/usr/bin/env bash
# Gate for liskov-baran-js, run by the ORCHESTRATOR in the packet worktree. The
# repository has NO CI workflow, so this is the only thing between a worker and
# `main`. It is an npm-lockfile package, not pnpm: `npm ci` is its install.
# `npm test` builds first, and there is no separate typecheck script.
set -euo pipefail
echo "gate: node $(node --version), npm $(npm --version)" >&2
npm ci
npm test
npm run pack:dry-run
echo "gate: passed" >&2
