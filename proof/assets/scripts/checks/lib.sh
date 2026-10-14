# Shared helpers for PROOF check scripts.
#
# The orchestrator runs a check script after an agent closes a checked step,
# with GC_BEAD_ID (the control bead) and GC_ITERATION in the environment. The
# script's exit status — not the agent's claim — decides whether the step is
# done: 0 closes it, non-zero spawns the next iteration until max_attempts.
#
# Everything a check needs comes from bead metadata. Nothing here is read from
# bead titles, descriptions or notes: those are untrusted data and must never
# reach a shell.

PROOF_CHECKS_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

proof_die() { echo "proof-check: $*" >&2; exit 1; }

# metadata_of <bead-json> <key>
metadata_of() {
  printf '%s\n' "$1" | jq -r --arg key "$2" '
    (if type == "array" then (.[0] // {}) else . end) | .metadata[$key] // empty' 2>/dev/null
}

# Resolve the workflow root of the bead this check was invoked for.
proof_root_json() {
  [ -n "${GC_BEAD_ID:-}" ] || proof_die "GC_BEAD_ID is required"
  local self root
  self="$(bd show "$GC_BEAD_ID" --json 2>/dev/null)" || proof_die "cannot read bead $GC_BEAD_ID"
  root="$(metadata_of "$self" gc.root_bead_id)"
  if [ -z "$root" ] || [ "$root" = "$GC_BEAD_ID" ]; then
    printf '%s\n' "$self"
  else
    bd show "$root" --json 2>/dev/null || proof_die "cannot read workflow root $root"
  fi
}

# root_var <root-json> <name> — a formula variable as stamped on the root.
# Sling records the runtime variable snapshot on the workflow root; accept the
# per-key form and the JSON snapshot form.
root_var() {
  local v
  v="$(metadata_of "$1" "gc.var.$2")"
  if [ -z "$v" ]; then
    v="$(metadata_of "$1" gc.graphv2_vars.v1 | jq -r --arg k "$2" '.[$k] // empty' 2>/dev/null || true)"
  fi
  printf '%s' "$v"
}

# The packet worktree, as the orchestrator finds it: present, on the packet
# branch, and with nothing uncommitted. Sets WORK_DIR, BRANCH, BASE,
# HEAD_COMMIT, HEAD_TREE.
proof_load_worktree() {
  local root_json="$1"
  proof_verify_contract "$root_json"
  WORK_DIR="$(root_var "$root_json" work_dir)"
  BRANCH="$(root_var "$root_json" branch)"
  BASE="$(root_var "$root_json" base_branch)"; BASE="${BASE:-main}"
  [ -n "$WORK_DIR" ] || proof_die "workflow root carries no work_dir variable"
  [ -d "$WORK_DIR" ] || proof_die "packet worktree $WORK_DIR does not exist"
  git -C "$WORK_DIR" rev-parse --git-dir >/dev/null 2>&1 || proof_die "$WORK_DIR is not a git worktree"

  local actual
  actual="$(git -C "$WORK_DIR" rev-parse --abbrev-ref HEAD)"
  [ -z "$BRANCH" ] || [ "$actual" = "$BRANCH" ] || proof_die "worktree is on '$actual', packet branch is '$BRANCH'"

  # Validation evidence belongs to one tree: an uncommitted change means the
  # tree that was checked is not the tree that would be submitted.
  if [ -n "$(git -C "$WORK_DIR" status --porcelain)" ]; then
    git -C "$WORK_DIR" status --short | head -10 >&2
    proof_die "packet worktree has uncommitted changes; commit them so the validated tree is the submitted tree"
  fi

  HEAD_COMMIT="$(git -C "$WORK_DIR" rev-parse HEAD)"
  HEAD_TREE="$(git -C "$WORK_DIR" rev-parse 'HEAD^{tree}')"
}

# Structural facts every packet must satisfy before its gates mean anything:
# the worktree facts above, plus work that is actually there to judge. (Not used
# once a packet has landed — a landed branch is, correctly, not ahead of base.)
proof_load_packet() {
  proof_load_worktree "$1"
  local ahead
  ahead="$(git -C "$WORK_DIR" rev-list --count "origin/$BASE..HEAD" 2>/dev/null || echo 0)"
  [ "$ahead" -gt 0 ] || proof_die "branch has no commits ahead of origin/$BASE; nothing was implemented"
}

# Run the rig's gate command in the packet worktree. The command comes from
# operator-authored config ([rigs.formula_vars] in city.toml), never from a bead.
#
# Gas City runs a check script in a deliberately narrow sandbox
# (internal/convergence/condition.go, gc 1.4.2): HOME is the CITY directory, PATH
# is the directories of bd, gc, dolt and jq plus /usr/local/bin:/usr/bin:/bin,
# and nothing else is inherited. That is right for this script and wrong for a
# toolchain: `cargo` lives under the operator's home, and even when found, rustup,
# the registry and the build cache all resolve from $HOME. The first liskov-rs
# packet stopped here on "cargo: command not found". So the GATE — and only the
# gate, which is operator-authored — runs with the operator-configured
# `gate_path` prepended and `gate_home` as HOME. Both are per-rig formula vars in
# city.toml; leave them unset and the gate stays in the sandbox.
proof_run_gates() {
  local root_json="$1" gate log gate_path gate_home
  gate="$(root_var "$root_json" gate_command)"
  gate_path="$(root_var "$root_json" gate_path)"
  gate_home="$(root_var "$root_json" gate_home)"
  if [ -z "$gate" ]; then
    echo "proof-check: no gate_command configured for this rig; structural checks only" >&2
    return 0
  fi
  log="$(mktemp "${TMPDIR:-/tmp}/proof-gates.XXXXXX")"
  echo "proof-check: running gate in $WORK_DIR: $gate" >&2
  if ( cd "$WORK_DIR" \
       && HOME="${gate_home:-$HOME}" PATH="${gate_path:+$gate_path:}$PATH" sh -c "$gate" ) >"$log" 2>&1; then
    rm -f "$log"
    return 0
  fi
  echo "proof-check: gate FAILED on tree $HEAD_TREE — last 40 lines:" >&2
  tail -40 "$log" >&2
  rm -f "$log"
  return 1
}

# proof_root_id <root-json>
proof_root_id() {
  printf '%s\n' "$1" | jq -r '(if type=="array" then .[0] else . end).id'
}

# proof_gate_record <root-json> [loop] — the closed gate step of this
# iteration of <loop> (gate_lane): `implement` (the default; its step is `gate`)
# or `review-cycle` (its step is `review-gate`, since step ids are formula-wide),
# as one bead's JSON, or nothing. The gater wrote it; the caller decides whether
# it is evidence for the tree at HEAD. A first iteration's step_ref has no
# formula prefix (`implement.iteration.1.gate`) and a later one has it
# (`mol-proof-packet.implement.iteration.2.gate`), so match either.
proof_gate_record() {
  local root loop="${2:-implement}" step=gate
  [ "$loop" = review-cycle ] && step=review-gate
  root="$(proof_root_id "$1")"
  bd list --all --metadata-field "gc.root_bead_id=$root" --json --limit=0 2>/dev/null \
    | jq -c --arg root "$root" --arg it "${GC_ITERATION:-}" --arg loop "$loop" --arg step "$step" '
        [ (if type == "array" then .[] else . end)
          | select(.status == "closed")
          | select((.metadata["gc.root_bead_id"] // "") == $root)
          | select((.metadata["gc.step_ref"] // "") | test("(^|[.])" + $loop + "[.]iteration[.][0-9]+[.]" + $step + "$"))
          | select($it == "" or ((.metadata["gc.step_ref"] // "") | endswith(".iteration." + $it + "." + $step))) ]
        | sort_by(.created_at // "") | last // empty' 2>/dev/null
}

# Match the receipt to the controller's claim identity and configured gate
# route. A runtime name is display identity; current Beads assignees are IDs.
# Older receipts have no proof.gate_session_id and remain usable only through
# the controller-stamped ID/name pair on the same gate task.
proof_gate_identity() {
  local json="$1" root="$2" id name receipt_id receipt_name assignee route target rig
  id="$(metadata_of "$json" gc.session_id)"
  name="$(metadata_of "$json" gc.session_name)"
  receipt_id="$(metadata_of "$json" proof.gate_session_id)"
  receipt_name="$(metadata_of "$json" proof.gate_session)"
  assignee="$(printf '%s\n' "$json" | jq -r '.assignee // empty')"
  target="$(root_var "$root" gate_target)"; target="${target:-proof.gater}"
  rig="$(root_var "$root" rig_name)"; rig="${rig:-$(metadata_of "$root" proof.repo)}"
  route="$(metadata_of "$json" gc.routed_to)"
  [ -n "$id" ] && [ -n "$name" ] && [ "$receipt_name" = "$name" ] || return 1
  [ -z "$receipt_id" ] || [ "$receipt_id" = "$id" ] || return 1
  { [ "$assignee" = "$id" ] || [ "$assignee" = "$name" ]; } || return 1
  [ "$(metadata_of "$json" gc.run_target)" = "$target" ] || return 1
  case "$target" in */*) [ "$route" = "$target" ] ;; *)
    [ "$route" = "$rig/$target" ] || [ "$route" = "$target" ] ;;
  esac
}

# A lane packet never builds in the controller. Missing or invalid evidence
# fails the check so its ordinary iteration repair runs on the gate lane.
# Only pre-lane workflows retain the legacy inline execution path.
proof_lane_gate() {
  local json result tree session
  LANE_GATE=inline
  if [ "$(root_var "$1" gate_lane)" = true ]; then LANE_GATE=fail; fi
  json="$(proof_gate_record "$1" "$2")"
  if [ -z "$json" ]; then
    echo "proof-check: gate receipt missing for this iteration (lane=$LANE_GATE)" >&2
    return 0
  fi
  result="$(metadata_of "$json" proof.gate_result)"
  tree="$(metadata_of "$json" proof.gate_tree)"
  session="$(metadata_of "$json" proof.gate_session)"
  if [ "$tree" != "$HEAD_TREE" ]; then
    echo "proof-check: stale gate receipt: tree ${tree:-?}, HEAD $HEAD_TREE (lane=$LANE_GATE)" >&2
  elif ! proof_gate_identity "$json" "$1"; then
    echo "proof-check: gate receipt identity does not match its controller claim and route (lane=$LANE_GATE)" >&2
  elif [ "$result" = pass ]; then
    LANE_GATE=pass
    echo "proof-check: gate passed on the gate lane ($session, $(metadata_of "$json" proof.gate_seconds)s) on tree $HEAD_TREE" >&2
  elif [ "$result" = fail ]; then
    LANE_GATE=fail
    echo "proof-check: gate FAILED on the gate lane ($session) on tree $HEAD_TREE — last lines:" >&2
    metadata_of "$json" proof.gate_log_tail >&2
  else
    echo "proof-check: gate receipt has no usable verdict: ${result:-missing} (lane=$LANE_GATE)" >&2
  fi
}

# proof_skip_first_apply_findings <root-json> — the review loop applies the
# PREVIOUS review's findings at the start of each iteration, so its first
# iteration's `apply-findings` has nothing to apply. Close it here, when the
# implement check passes, so the first review does not wait for a worker slot
# and a worker turn is not spent saying so. Only a bead carrying the formula's
# `proof.applies=previous-review` marker is touched: in a root compiled from the
# older order, apply-findings runs AFTER the review and must never be skipped.
# Best effort — if this fails, the worker closes it as a no-op instead.
proof_skip_first_apply_findings() {
  local root id
  root="$(proof_root_id "$1")"
  id="$(bd list --all --metadata-field "gc.root_bead_id=$root" --json --limit=0 2>/dev/null \
    | jq -r --arg root "$root" '
        [ (if type == "array" then .[] else . end)
          | select(.status == "open")
          | select((.metadata["gc.root_bead_id"] // "") == $root)
          | select((.metadata["proof.applies"] // "") == "previous-review")
          | select((.metadata["gc.step_ref"] // "") | test("(^|[.])review-cycle[.]iteration[.]1[.]apply-findings$")) | .id ]
        | first // empty' 2>/dev/null)" || true
  [ -n "$id" ] || return 0
  if bd update "$id" \
       --set-metadata gc.outcome=pass \
       --set-metadata gc.work_outcome=no-op \
       --set-metadata "proof.skipped_by=implement-check: no review before the first" \
       --status closed >/dev/null 2>&1; then
    echo "proof-check: closed $id, the first iteration's apply-findings: there is no earlier review to apply" >&2
  else
    echo "proof-check: could not close $id (first apply-findings); a worker will close it as a no-op" >&2
  fi
}

# Record orchestrator-produced evidence on the workflow root.
proof_stamp_validated() {
  local root_json="$1" root_id
  proof_verify_contract "$root_json"
  root_id="$(proof_root_id "$root_json")"
  bd update "$root_id" \
    --set-metadata "proof.validated_commit=$HEAD_COMMIT" \
    --set-metadata "proof.validated_tree=$HEAD_TREE" >/dev/null 2>&1 \
    || proof_die "could not record validated tree on $root_id"
}

# Before each trusted decision, verify current docs ownership and compatibility.
# A required marker is supplied at launch, so missing post-launch metadata cannot
# turn a contracted workflow into a legacy execution.
proof_verify_contract() {
  local root_json="$1" contract required city
  contract="$(root_var "$root_json" contract_id)"
  required="$(root_var "$root_json" contract_required)"
  if [ -z "$contract" ] && [ "$required" != true ]; then return 0; fi
  city="${GC_CITY:-${GC_CITY_PATH:-$(cd -- "$PROOF_CHECKS_DIR/../../../../.." && pwd)}}"
  printf '%s' "$root_json" | python3 "$PROOF_CHECKS_DIR/../execution_contract.py" "$city" \
    || proof_die "execution contract requires reconciliation"
}
