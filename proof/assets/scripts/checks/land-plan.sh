#!/usr/bin/env bash
# Check for the `land` step of `mol-proof-plan` — the only thing that puts a
# planning run's output on main. The AGENT prepares the commit (the items and
# questions it wrote, origin's main integrated, the views regenerated); the
# ORCHESTRATOR, here, decides whether it may land, and lands it. The planner
# never pushes, so "the gate passed on this tree" and "this tree is what landed"
# cannot come apart: both are this script, on one commit.
#
# It is `land-on-main.sh` with one difference, and it is not a looser one. A
# packet lands what a reviewer (or the gate) approved, plus its item's
# completion; a planning run has no reviewer at all, so what may land is stated
# as a content rule over paths instead: backlog items nobody is holding, open
# questions, and generated views — `plan_scope.py`, and nothing else. Everything
# that makes a landing safe is the same: fast-forward or refuse, never a force,
# the rig's gate on the integrated tree, and the push here rather than in a
# session.
#
#   land-plan.sh            decide, push, record
#   land-plan.sh --verify   decide only; the planner may run this to read why a
#                           landing was refused. It never pushes.
#
# A run that wrote nothing is a legitimate outcome — every stopped packet needed
# a person, say — and passes without landing anything.
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib.sh"
git() { command timeout 120 git "$@"; }
bd() { command timeout 120 bd "$@"; }
VERIFY_ONLY=0; [ "${1:-}" = "--verify" ] && VERIFY_ONLY=1

ROOT_JSON="$(proof_root_json)"
ROOT_ID="$(proof_root_id "$ROOT_JSON")"
proof_load_worktree "$ROOT_JSON"

[ "$(root_var "$ROOT_JSON" land_plan)" = "true" ] \
  || proof_die "this workflow was not slung as a planning run; nothing here may write to $BASE"

git -C "$WORK_DIR" fetch --quiet origin "$BASE" \
  || proof_die "cannot fetch origin/$BASE from $WORK_DIR (network, or no credentials in the orchestrator's environment)"
UPSTREAM="origin/$BASE"

# Mirrors land-on-main.sh deliberately: the two landing checks share their
# safety, not their code, so a change to one can never silently move the other.
record_landed() {
  bd update "$ROOT_ID" \
    --set-metadata "proof.landed_commit=$HEAD_COMMIT" \
    --set-metadata "proof.landed_tree=$HEAD_TREE" \
    --set-metadata "proof.plan_outcome=landed" >/dev/null 2>&1 \
    || proof_die "landed, but could not record it on $ROOT_ID"
}

republish() {
  local shared; shared="$(root_var "$ROOT_JSON" shared_checkout)"
  [ -n "$shared" ] || return 0
  local state=refused
  if [ "$(git -C "$shared" rev-parse --abbrev-ref HEAD 2>/dev/null)" = "$BASE" ] \
     && git -C "$shared" fetch --quiet origin "$BASE" 2>/dev/null \
     && git -C "$shared" merge --quiet --ff-only "origin/$BASE" >/dev/null 2>&1; then
    state=ok
  fi
  echo "proof-check: shared checkout $shared republish: $state" >&2
  bd update "$ROOT_ID" --set-metadata "proof.republished=$state" >/dev/null 2>&1 \
    || echo "proof-check: could not record shared-checkout republish state ($state)" >&2
}

if git -C "$WORK_DIR" merge-base --is-ancestor HEAD "$UPSTREAM"; then
  if [ "$(metadata_of "$ROOT_JSON" proof.landed_commit)" = "$HEAD_COMMIT" ] \
     || { [ "$(metadata_of "$ROOT_JSON" proof.validated_commit)" = "$HEAD_COMMIT" ] \
          && [ "$(metadata_of "$ROOT_JSON" proof.validated_tree)" = "$HEAD_TREE" ]; }; then
    echo "proof-check: the recorded planning landing is already on $UPSTREAM" >&2
    [ "$VERIFY_ONLY" = 1 ] || { record_landed; republish; }
  else
    echo "proof-check: the planning run added nothing to $BASE" >&2
    if [ "$VERIFY_ONLY" = 0 ]; then
      bd update "$ROOT_ID" --set-metadata "proof.plan_outcome=no-op" >/dev/null 2>&1 \
        || proof_die "could not record the planning no-op outcome"
    fi
  fi
  exit 0
fi

git -C "$WORK_DIR" merge-base --is-ancestor "$UPSTREAM" HEAD \
  || proof_die "$UPSTREAM has moved: merge it into $BRANCH, regenerate the views, validate, commit — then close the step again"

python3 "$HERE/plan_scope.py" "$WORK_DIR" "$UPSTREAM" "$(root_var "$ROOT_JSON" assigned_items)" \
  || proof_die "what would land on $BASE is not planning output (above)"

GATED_COMMIT="$HEAD_COMMIT"
timeout --kill-after=10s 600s bash -c \
  '. "$1/lib.sh"; proof_load_worktree "$2"; proof_run_gates "$2"' \
  _ "$HERE" "$ROOT_JSON" || proof_die "planning gate failed or exceeded 600s; nothing landed"
proof_load_worktree "$ROOT_JSON"
[ "$HEAD_COMMIT" = "$GATED_COMMIT" ] || proof_die "the gate changed HEAD; its result is not evidence for that commit"
proof_stamp_validated "$ROOT_JSON"

if [ "$VERIFY_ONLY" = 1 ]; then
  echo "proof-check: $HEAD_COMMIT may land on $BASE (verify only; nothing pushed)" >&2
  exit 0
fi

# A plain refspec: no '+', no --force. If main moved since the fetch above the
# remote refuses the push, this exits non-zero, and the step is issued again.
git -C "$WORK_DIR" push --quiet origin "HEAD:refs/heads/$BASE" \
  || proof_die "push to $BASE was not confirmed — inspect origin before retrying; no force push is permitted"
git -C "$WORK_DIR" fetch --quiet origin "$BASE"
git -C "$WORK_DIR" merge-base --is-ancestor "$HEAD_COMMIT" "$UPSTREAM" \
  || proof_die "pushed, but $HEAD_COMMIT is not an ancestor of $UPSTREAM"
record_landed
republish
echo "proof-check: landed $HEAD_COMMIT (tree $HEAD_TREE) on $BASE" >&2
