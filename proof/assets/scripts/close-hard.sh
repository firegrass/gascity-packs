#!/usr/bin/env bash
# Report one terminal task outcome. The qualified Gas City engine propagates
# explicit hard failures through compiled loops and their enclosing scopes.
# Never edit controller budgets or close topology from a worker helper.
# close-hard.sh <bead-id> <failure_reason>
set -euo pipefail

die() { echo "close-hard: $*" >&2; exit 2; }
[ $# -eq 2 ] || die "usage: close-hard.sh <bead-id> <failure_reason>"
BEAD="$1" REASON="$2"
[[ "$BEAD" =~ ^[A-Za-z0-9._-]+$ ]] || die "not a bead id: $BEAD"
[[ "$REASON" =~ ^[A-Za-z0-9_.:-]{1,160}$ ]] || die "a failure_reason is a short token ([A-Za-z0-9_.:-]), not prose: $REASON"
if [ -n "${GC_RIG_ROOT:-}" ]; then cd "$GC_RIG_ROOT"; fi

SELF="$(bd show "$BEAD" --json 2>/dev/null)" || die "cannot read bead $BEAD"
meta() { printf '%s\n' "$SELF" | jq -r --arg k "$1" '(if type=="array" then .[0] else . end) | .metadata[$k] // empty'; }
STATUS="$(printf '%s\n' "$SELF" | jq -r '(if type=="array" then .[0] else . end) | .status // empty')"
[ "$STATUS" != "closed" ] || die "bead $BEAD is already closed"
KIND="$(meta gc.kind)" REF="$(meta gc.step_ref)"
# Only a step is ever closed here. A workflow, scope or spec bead is topology
# that `claim.py` and the watchdog put back unassigned for the control
# dispatcher to close, and a control bead (ralph, retry, check, scope-check, …)
# is the dispatcher's own: closing one hard would abort the packet's iteration,
# or the packet, for a failure that is not its own. A step is `gc.kind` empty or
# `task` (gc 1.4.2 writes a re-issued single-step iteration as `task`).
case "$KIND" in
  ""|task) ;;
  *) die "bead $BEAD is gc.kind=$KIND, the orchestrator's, not a step: never close it — put it back with \`gc bd update $BEAD --assignee \"\" --force\` and claim again" ;;
esac
bd update "$BEAD" \
  --set-metadata gc.outcome=fail \
  --set-metadata gc.failure_class=hard \
  --set-metadata "gc.failure_reason=$REASON" \
  --set-metadata gc.work_outcome=blocked \
  --status closed >/dev/null || die "could not close $BEAD"
echo "close-hard: closed $BEAD hard ($REASON)" >&2
