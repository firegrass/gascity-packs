#!/usr/bin/env bash
# Check for the `review-cycle` step. The packet advances only if ALL hold:
#   1. the latest review verdict is `approve`;
#   2. the tree the reviewer looked at is the tree now at HEAD — a change made
#      after review is unreviewed, so stale review evidence blocks completion;
#   3. the gates have passed on that same tree (re-run if the tree moved since
#      the implement step's gates — or, with gate_lane on, read from this
#      iteration's `review-gate` step, which the gater ran beside the review).
#
# The review is the last step of every iteration: the worker applies a review's
# findings at the start of the NEXT iteration, so no fix goes unreviewed. When
# the last iteration's review still does not approve, the packet stops with
# `review_budget_exhausted` (or `review_rejected`) and the findings recorded.
set -euo pipefail
. "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

ROOT_JSON="$(proof_root_json)"
ROOT_ID="$(printf '%s\n' "$ROOT_JSON" | jq -r '(if type=="array" then .[0] else . end).id')"
proof_load_packet "$ROOT_JSON"

REVIEWS="$(bd list --all --metadata-field "gc.root_bead_id=$ROOT_ID" --json --limit=0 2>/dev/null || printf '[]')"
LATEST="$(printf '%s\n' "$REVIEWS" | jq -c '
  [ .[] | select((.metadata["proof.review_verdict"] // "") != "")
        | select((.metadata["gc.kind"] // "") as $kind | $kind == "" or $kind == "task") ]
  | sort_by(.closed_at // .updated_at // "") | last // empty')"
[ -n "$LATEST" ] || proof_die "no review verdict recorded under workflow $ROOT_ID"

VERDICT="$(printf '%s\n' "$LATEST" | jq -r '.metadata["proof.review_verdict"]')"
REVIEWED_TREE="$(printf '%s\n' "$LATEST" | jq -r '.metadata["proof.reviewed_tree"] // empty')"

if [ "$VERDICT" != "approve" ]; then
  # The loop's control bead carries its budget; gc ends the loop when the
  # iteration that just closed is number gc.max_attempts. The review is the last
  # step of every iteration (findings are applied at the start of the next), so
  # on the last iteration a non-approving review is final: say why the packet
  # stops, and keep what is still outstanding where a person will look.
  CONTROL="$(printf '%s\n' "$REVIEWS" | jq -c --arg root "$ROOT_ID" '
    [ .[] | select(.status != "closed")
          | select((.metadata["gc.root_bead_id"] // "") == $root and (.metadata["gc.kind"] // "") == "ralph"
                   and (.metadata["gc.step_id"] // "") == "review-cycle") ] | first // empty')"
  CONTROL_ID="$(printf '%s\n' "$CONTROL" | jq -r '.id // empty' 2>/dev/null || true)"
  MAX_ATTEMPTS="$(printf '%s\n' "$CONTROL" | jq -r '.metadata["gc.max_attempts"] // empty' 2>/dev/null || true)"
  ATTEMPT="${GC_ITERATION:-}"
  LATEST_ID="$(printf '%s\n' "$LATEST" | jq -r '.id')"
  if [[ "$ATTEMPT" =~ ^[0-9]+$ ]] && [[ "$MAX_ATTEMPTS" =~ ^[0-9]+$ ]] && [ "$ATTEMPT" -ge "$MAX_ATTEMPTS" ]; then
    REASON=review_budget_exhausted
    [ "$VERDICT" = "reject" ] && REASON=review_rejected
    # The findings are the reviewer's notes: data, stored, never evaluated.
    FINDINGS="$(printf '%s\n' "$LATEST" | jq -r '(.notes // "")[0:4000]')"
    bd update "$LATEST_ID" --set-metadata "gc.failure_reason=$REASON" >/dev/null 2>&1 \
      || echo "proof-check: could not record $REASON on review $LATEST_ID" >&2
    bd update "$ROOT_ID" \
      --set-metadata "proof.review_failure_reason=$REASON" \
      --set-metadata "proof.outstanding_findings_bead=$LATEST_ID" \
      --set-metadata "proof.outstanding_findings=$FINDINGS" >/dev/null 2>&1 \
      || echo "proof-check: could not record the outstanding findings on $ROOT_ID" >&2
    proof_die "$REASON: review $ATTEMPT of $MAX_ATTEMPTS says '$VERDICT'; its findings are outstanding (review $LATEST_ID, recorded on $ROOT_ID)"
  fi
  # A rejection reaches apply-findings next; its explicit hard close ends
  # the loop in the qualified engine. The pack never edits controller budgets.
  proof_die "latest review verdict is '$VERDICT', not approve"
fi
[ -n "$REVIEWED_TREE" ] || proof_die "the approving review recorded no reviewed tree"
[ "$REVIEWED_TREE" = "$HEAD_TREE" ] || proof_die "stale review: reviewed tree $REVIEWED_TREE, HEAD tree is $HEAD_TREE"

VALIDATED_TREE="$(metadata_of "$ROOT_JSON" proof.validated_tree)"
if [ "$VALIDATED_TREE" != "$HEAD_TREE" ]; then
  echo "proof-check: tree moved since the last gate run ($VALIDATED_TREE -> $HEAD_TREE)" >&2
  proof_lane_gate "$ROOT_JSON" review-cycle
  case "$LANE_GATE" in
    pass) ;;
    fail) exit 1 ;;
    *) echo "proof-check: re-running gates" >&2; proof_run_gates "$ROOT_JSON" || exit 1 ;;
  esac
  proof_stamp_validated "$ROOT_JSON"
fi

# The approved COMMIT is recorded beside the tree: a direct landing on main has
# to prove it is built on top of what was reviewed (see land-on-main.sh), and a
# tree hash alone cannot say which history it belongs to.
proof_verify_contract "$ROOT_JSON"
bd update "$ROOT_ID" \
  --set-metadata "proof.approved_tree=$HEAD_TREE" \
  --set-metadata "proof.approved_commit=$HEAD_COMMIT" \
  --set-metadata "proof.approved_by=reviewer" >/dev/null 2>&1 \
  || proof_die "could not record approved tree on $ROOT_ID"
echo "proof-check: approved and gated on tree $HEAD_TREE" >&2
