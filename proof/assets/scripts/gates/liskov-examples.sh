#!/usr/bin/env bash
# Gate for liskov-examples, run by the ORCHESTRATOR in the packet worktree:
# CI's `validate` job, step for step, including the draft validator's own
# self-test and the generated-manifest check — a manifest regenerated but not
# committed is exactly the kind of drift a worker introduces.
set -euo pipefail
echo "gate: node $(node --version), pnpm $(pnpm --version)" >&2
pnpm install --frozen-lockfile --prefer-offline
python3 scripts/validate_drafts.py
python3 scripts/validate_drafts.py --selftest
pnpm manifests:check
pnpm typecheck
pnpm test
pnpm build
pnpm smoke:bundle
echo "gate: passed" >&2
