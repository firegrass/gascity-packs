#!/usr/bin/env bash
# Check for the `implement` step: the packet's change exists, is committed on
# the packet branch, is one the packet was authorized to make, and passes the
# rig's gate command — as run by the orchestrator, on the exact tree that will
# be reviewed.
#
# `design_scope.py` runs BEFORE the gate, because a refusal there costs no
# build: it refuses a diff that changes what a Console operator sees with no
# agreed design behind it (ADR-0150), and a diff that reaches a reserved or
# protected path its scope did not declare, or a review-substring path with no
# reviewer (ADR-0159). What it allows outside the declared scope is recorded on
# the workflow root as `proof.scope_expansion`, for the factory report.
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib.sh"

ROOT_JSON="$(proof_root_json)"
proof_load_packet "$ROOT_JSON"

DESIGN="$(metadata_of "$ROOT_JSON" proof.design_mockup)"
# The repository comes from admission, which resolved it from the rig clone's
# origin. Re-deriving it here from the worktree's remote could fail to a name
# that matches no rule, and this check may not fail open.
REPO="$(metadata_of "$ROOT_JSON" proof.repo)"
# Whether an independent reviewer was drawn is recorded by admission as
# `proof.review`; a root admitted by an older pack carries only the formula
# variable. Both are orchestrator-written.
REVIEW="$(metadata_of "$ROOT_JSON" proof.review)"
if [ -z "$REVIEW" ]; then
  if [ "$(root_var "$ROOT_JSON" review)" = "true" ]; then REVIEW=on; else REVIEW=off; fi
fi
# The rig's review substrings come from the operator's policy file in the city,
# never from the worktree. The city is found as the landing checks find it: a
# check runs sandboxed, and the deployed pack sits at <city>/packs/proof.
CITY="${GC_CITY:-${GC_CITY_PATH:-$(cd -- "$HERE/../../../../.." && pwd)}}"
RIG="$(root_var "$ROOT_JSON" rig_name)"; RIG="${RIG:-$REPO}"
RECOVERY_REVIEW=""
if [ "$(root_var "$ROOT_JSON" review)" != "true" ] \
   && { [ "$(metadata_of "$ROOT_JSON" proof.review_requested)" = "true" ] \
        || [ -n "$(root_var "$ROOT_JSON" recovery_trial)" ]; }; then
  RECOVERY_REVIEW="$(python3 "$HERE/../recovery_review.py" verify "$(proof_root_id "$ROOT_JSON")" \
    --city "$CITY" --rig "$RIG")" || proof_die "same-packet review must approve this exact candidate"
  REVIEW=on
fi
SCOPE_FILE="$(mktemp "${TMPDIR:-/tmp}/proof-scope.XXXXXX")"
REVIEW_FILE="$(mktemp "${TMPDIR:-/tmp}/proof-review.XXXXXX")"
EXPANDED_FILE="$(mktemp "${TMPDIR:-/tmp}/proof-expanded.XXXXXX")"
trap 'rm -f "$SCOPE_FILE" "$REVIEW_FILE" "$EXPANDED_FILE"' EXIT
metadata_of "$ROOT_JSON" proof.write_scope >"$SCOPE_FILE"
python3 - "$CITY/proof/policy.toml" "$RIG" >"$REVIEW_FILE" <<'POLICY' || proof_die "could not read the review substrings from $CITY/proof/policy.toml"
import sys, tomllib
path, rig = sys.argv[1], sys.argv[2]
try:
    policy = tomllib.load(open(path, "rb"))
except FileNotFoundError:
    print("design-scope: no policy file at %s; no review substring applies" % path, file=sys.stderr)
    sys.exit(0)
table = (policy.get("rigs") or {}).get(rig)
if table is None:
    print("design-scope: %s has no [rigs.%s] table; no review substring applies" % (path, rig), file=sys.stderr)
    sys.exit(0)
substrings = table.get("review_if_scope_contains") or []
if not isinstance(substrings, list) or not all(isinstance(s, str) for s in substrings):
    sys.exit("design-scope: [rigs.%s].review_if_scope_contains in %s is not a list of strings" % (rig, path))
print("\n".join(substrings))
POLICY
python3 "$HERE/design_scope.py" \
  "$WORK_DIR" "origin/$BASE" "$REPO" "$DESIGN" "$SCOPE_FILE" "$REVIEW" "$REVIEW_FILE" \
  >"$EXPANDED_FILE" || exit 1
# What the diff touched outside the declared scope, `repo:path` a line, "" when
# nothing: the cost of ADR-0159, measured rather than assumed.
gc bd update "$(proof_root_id "$ROOT_JSON")" \
  --set-metadata "proof.scope_expansion=$(cat "$EXPANDED_FILE")" >/dev/null 2>&1 \
  || proof_die "could not record the scope expansion on the workflow root"

# The gate lane (gate_lane = true on the rig): the gater has already run the
# gate on this iteration's committed tree, outside the control dispatcher. Its
# record counts only for the exact tree at HEAD and only from a gater session;
# Missing or invalid lane evidence fails this check for ordinary iteration
# repair. Only workflows without the lane may run the gate inline.
proof_lane_gate "$ROOT_JSON" implement
case "$LANE_GATE" in
  pass) ;;
  fail) exit 1 ;;
  *) proof_run_gates "$ROOT_JSON" || exit 1 ;;
esac
proof_stamp_validated "$ROOT_JSON"
# The design this tree was built to, recorded where the pull request, the
# reviewer and the daily report can all read it. "" when nothing renders.
if [ -n "$DESIGN" ]; then
  gc bd update "$(proof_root_id "$ROOT_JSON")" \
    --set-metadata "proof.design_built=$DESIGN" >/dev/null 2>&1 \
    || proof_die "could not record the design this tree was built to"
fi
# Review is opt-in. Without it no `review-cycle` step exists to record an
# approval, so the gated tree is the approved tree — and says who approved it,
# so a landing check or a reader never mistakes a gate for a reviewer.
if [ "$(root_var "$ROOT_JSON" review)" != "true" ]; then
  APPROVED_BY=gate
  [ -z "$RECOVERY_REVIEW" ] || APPROVED_BY=reviewer
  gc bd update "$(proof_root_id "$ROOT_JSON")" \
    --set-metadata "proof.approved_tree=$HEAD_TREE" \
    --set-metadata "proof.approved_commit=$HEAD_COMMIT" \
    --set-metadata "proof.approved_by=$APPROVED_BY" >/dev/null 2>&1 \
    || proof_die "could not record the gated tree as approved"
else
  proof_skip_first_apply_findings "$ROOT_JSON"
fi
echo "proof-check: gates passed on commit $HEAD_COMMIT tree $HEAD_TREE" >&2
