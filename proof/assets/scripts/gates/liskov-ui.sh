#!/usr/bin/env bash
# Gate for liskov-ui, run by the ORCHESTRATOR in the packet worktree: the
# repository's own validation, whole — `pnpm typecheck && pnpm build && pnpm test`
# (docs/meta/doctrine/member-repo-validation.md). It is not diff-aware: a
# TypeScript change has no smaller honest unit than the type-checked build.
#
# A packet worktree is new, so dependencies are installed first from the frozen
# lockfile; pnpm's store is shared, so that is a link step, not a download.
# Node comes from asdf, which needs a `.tool-versions` on the path from the
# worktree upward: rig_context.py writes one beside the rigs' CLAUDE.md. Configure
# `gate_path` (asdf shims + pnpm) and `gate_home` for the rig.
set -euo pipefail
echo "gate: node $(node --version), pnpm $(pnpm --version)" >&2
pnpm install --frozen-lockfile --prefer-offline
pnpm typecheck
pnpm build
pnpm test
echo "gate: passed" >&2
