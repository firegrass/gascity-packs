#!/usr/bin/env bash
# Check for the `land` step — and the only thing in this pack that writes to a
# repository's main branch. The AGENT prepares the landing commit (the item
# completed, origin's main integrated, views regenerated); the ORCHESTRATOR,
# here, decides whether that commit may land, and lands it. The agent never
# pushes to main, so "the gate passed on this tree" and "this tree is what
# landed" cannot come apart: both are this script, on one commit.
#
# The landing happens only if ALL hold:
#   1. the packet was approved, and HEAD is built on top of the approved commit
#      (a rebase rewrites reviewed commits, so it is refused);
#   2. HEAD already contains origin's main — the push is a fast-forward or it
#      does not happen. Never forced;
#   3. what would land is what was reviewed, plus the item's completion and
#      regenerated views, and nothing else (landing_scope.py);
#   4. the rig's gate passes on HEAD's tree — the integrated tree, not the one
#      that was reviewed.
#
#   land-on-main.sh            decide, push, record
#   land-on-main.sh --verify   decide only; an agent may run this to read why a
#                              landing was refused. It never pushes.
#
# Re-running after a successful landing is a no-op that passes.
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib.sh"
git() { command timeout 120 git "$@"; }
bd() { command timeout 120 bd "$@"; }
VERIFY_ONLY=0; [ "${1:-}" = "--verify" ] && VERIFY_ONLY=1

ROOT_JSON="$(proof_root_json)"
ROOT_ID="$(proof_root_id "$ROOT_JSON")"
proof_load_worktree "$ROOT_JSON"

[ "$(root_var "$ROOT_JSON" land_main)" = "true" ] \
  || proof_die "this packet was not admitted with landing 'main'; nothing here may write to $BASE"
ITEM_PATH="$(root_var "$ROOT_JSON" item_path)"
[ -n "$ITEM_PATH" ] || proof_die "workflow root carries no item_path variable"

APPROVED_COMMIT="$(metadata_of "$ROOT_JSON" proof.approved_commit)"
APPROVED_TREE="$(metadata_of "$ROOT_JSON" proof.approved_tree)"
[ -n "$APPROVED_COMMIT" ] && [ -n "$APPROVED_TREE" ] \
  || proof_die "no approved commit is recorded on $ROOT_ID; a packet lands only after review"
[ "$(git -C "$WORK_DIR" rev-parse "$APPROVED_COMMIT^{tree}" 2>/dev/null)" = "$APPROVED_TREE" ] \
  || proof_die "recorded approved commit $APPROVED_COMMIT does not have the approved tree $APPROVED_TREE"
git -C "$WORK_DIR" merge-base --is-ancestor "$APPROVED_COMMIT" HEAD \
  || proof_die "HEAD is not built on the approved commit $APPROVED_COMMIT (a rebase or reset rewrote reviewed history); integrate with a merge"

git -C "$WORK_DIR" fetch --quiet origin "$BASE" \
  || proof_die "cannot fetch origin/$BASE from $WORK_DIR (network, or no credentials in the orchestrator's environment)"
UPSTREAM="origin/$BASE"

record_landed() {
  bd update "$ROOT_ID" \
    --set-metadata "proof.landed_commit=$HEAD_COMMIT" \
    --set-metadata "proof.landed_tree=$HEAD_TREE" >/dev/null 2>&1 \
    || proof_die "landed, but could not record it on $ROOT_ID"
}

# Best effort, and only ever a fast-forward: other sessions read files, not
# refs, so a landing that leaves the shared checkout behind is one only this
# packet can see. A dirty file that refuses the fast-forward is another
# session's work; it is left alone and the refusal is recorded.
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
  echo "proof-check: $HEAD_COMMIT is already on $UPSTREAM" >&2
  [ "$VERIFY_ONLY" = 1 ] || { record_landed; republish; }
  exit 0
fi

# Another writer can advance main during validation.
# Retry only a proven forward advance, not arbitrary transport/server failures.
# Three pushes and a shared gate budget bound the work in one check; every
# changed tree goes through the same refresh, scope and gate guards. The budget
# is cumulative but sized so every attempt can run a full gate: a gate that
# touches gascity/ runs every pack test (390 s on a quiet host, 550 s under
# load, 2026-10-09), and a 600-second total let a single main advance stop the
# landing in its second gate (vq87, juv8, xft6). 900 s per attempt keeps ~60%
# headroom over the loaded full gate; the total (2700 s) leaves 900 s of the
# land step's 60m check timeout (mol-proof-packet.toml) for the bounded git/bd
# calls and integration around the gates.
MAX_PUSH_ATTEMPTS=3
GATE_SECONDS_PER_ATTEMPT=900
GATE_BUDGET_TOTAL=$((MAX_PUSH_ATTEMPTS * GATE_SECONDS_PER_ATTEMPT))
GATE_BUDGET=$GATE_BUDGET_TOTAL
for ((attempt=1; attempt<=MAX_PUSH_ATTEMPTS; attempt++)); do
  echo "proof-check: landing attempt $attempt/$MAX_PUSH_ATTEMPTS" >&2
  UPSTREAM_COMMIT="$(git -C "$WORK_DIR" rev-parse "$UPSTREAM")"
  if ! git -C "$WORK_DIR" merge-base --is-ancestor "$UPSTREAM_COMMIT" HEAD; then
    [ "$VERIFY_ONLY" = 0 ] || proof_die "$UPSTREAM has moved; the checked landing will integrate it in the packet worktree"
    python3 "$HERE/landing_refresh.py" "$WORK_DIR" "$APPROVED_COMMIT" "$UPSTREAM_COMMIT" "$ITEM_PATH" \
      || proof_die "main integration needs intervention; reviewed content and the original candidate are preserved"
    proof_load_worktree "$ROOT_JSON"
  fi

  python3 "$HERE/landing_scope.py" "$WORK_DIR" "$APPROVED_COMMIT" "$UPSTREAM_COMMIT" "$ITEM_PATH" \
    || proof_die "what would land on $BASE is not what was reviewed (above)"

  GATED_COMMIT="$HEAD_COMMIT"
  [ "$GATE_BUDGET" -gt 0 ] || proof_die "landing's cumulative ${GATE_BUDGET_TOTAL}s gate budget is exhausted; publication stopped"
  gate_started=$SECONDS
  timeout --kill-after=10s "${GATE_BUDGET}s" bash -c \
    '. "$1/lib.sh"; proof_load_worktree "$2"; proof_run_gates "$2"' \
    _ "$HERE" "$ROOT_JSON" || proof_die "landing gate failed or exceeded the remaining ${GATE_BUDGET}s budget; publication stopped"
  GATE_BUDGET=$((GATE_BUDGET - (SECONDS - gate_started)))
  proof_load_worktree "$ROOT_JSON"
  [ "$HEAD_COMMIT" = "$GATED_COMMIT" ] || proof_die "the gate changed HEAD; its result is not evidence for that commit"
  proof_stamp_validated "$ROOT_JSON"

  if [ "$VERIFY_ONLY" = 1 ]; then
    echo "proof-check: $HEAD_COMMIT may land on $BASE (verify only; nothing pushed)" >&2
    exit 0
  fi

  # A retry cannot inherit approval or a gate/target configuration that an
  # operator changed during this check. Validation stamps themselves may move.
  CURRENT_ROOT="$(proof_root_json)"
  [ "$(proof_root_id "$CURRENT_ROOT")" = "$ROOT_ID" ] \
    || proof_die "workflow root changed during landing; inspect before retrying"
  for key in proof.approved_commit proof.approved_tree proof.approved_by; do
    [ "$(metadata_of "$CURRENT_ROOT" "$key")" = "$(metadata_of "$ROOT_JSON" "$key")" ] \
      || proof_die "workflow approval changed during landing; inspect before retrying"
  done
  for key in work_dir branch base_branch item_path land_main review gate_command gate_path gate_home shared_checkout; do
    [ "$(root_var "$CURRENT_ROOT" "$key")" = "$(root_var "$ROOT_JSON" "$key")" ] \
      || proof_die "workflow authorization or gate configuration changed during landing; inspect before retrying"
  done

  # Publish the exact gated commit, even if an unexpected hook moves local HEAD.
  pushed=0
  if git -C "$WORK_DIR" push --quiet origin "$GATED_COMMIT:refs/heads/$BASE"; then pushed=1; fi
  git -C "$WORK_DIR" fetch --quiet origin "$BASE" \
    || proof_die "push to $BASE was not confirmed; cannot refresh origin to reconcile it"
  AFTER_PUSH="$(git -C "$WORK_DIR" rev-parse "$UPSTREAM")"
  if git -C "$WORK_DIR" merge-base --is-ancestor "$GATED_COMMIT" "$AFTER_PUSH"; then
    [ "$pushed" = 1 ] || echo "proof-check: reconciled an ambiguous push; the gated commit is already on $UPSTREAM" >&2
    record_landed
    republish
    echo "proof-check: landed $HEAD_COMMIT (tree $HEAD_TREE) on $BASE after $attempt attempt(s)" >&2
    exit 0
  fi
  [ "$pushed" = 0 ] || proof_die "push succeeded but the gated commit is absent from $UPSTREAM; inspect before retrying"
  [ "$AFTER_PUSH" != "$UPSTREAM_COMMIT" ] \
    || proof_die "push to $BASE was not confirmed and upstream did not advance; inspect before retrying"
  git -C "$WORK_DIR" merge-base --is-ancestor "$UPSTREAM_COMMIT" "$AFTER_PUSH" \
    || proof_die "$UPSTREAM history was rewritten; automatic retry is refused"
  if git -C "$WORK_DIR" merge-base --is-ancestor "$AFTER_PUSH" "$GATED_COMMIT"; then
    proof_die "push failed although the gated candidate contains upstream; main movement does not explain the refusal"
  fi
  proof_load_worktree "$ROOT_JSON"
  [ "$HEAD_COMMIT" = "$GATED_COMMIT" ] || proof_die "candidate changed during publication; automatic retry is refused"
  [ "$attempt" -lt "$MAX_PUSH_ATTEMPTS" ] \
    || proof_die "main kept advancing; exhausted $MAX_PUSH_ATTEMPTS checked push attempts; landing not confirmed"
  echo "proof-check: confirmed main advance $UPSTREAM_COMMIT -> $AFTER_PUSH; re-integrating and revalidating before retry" >&2
done
