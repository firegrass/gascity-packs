#!/usr/bin/env bash
# The gater: a zero-token pool agent that runs one packet's gate, then exits.
#
# A rig's single control dispatcher used to run every gate inline, one at a
# time, and no other packet's control step moved while it did (2026-10-07,
# 2026-10-09; Q-20261007-b7vh). With `gate_lane = "true"` on a rig the packet
# formula gives each implement iteration, and each review-cycle iteration, a
# `gate` step routed here. Gas City's pool claim is atomic, so the rig runs as
# many gates at once as this agent's `max_active_sessions` allows, and the
# implement and review checks only read the record.
#
# A review-cycle gate runs beside the review, on the tree the reviewer reads.
# When that tree has already passed a gate (the root's orchestrator-written
# `proof.validated_tree`, as on a first review), it records a pass without
# building again.
#
# One process is one turn, like upstream's agent-script workers: claim one
# routed bead, gate it, close it, exit. The reconciler starts another session
# while gate work remains.
#
# What it writes is evidence for exactly one tree: `proof.gate_tree` is the tree
# that was gated, and the check accepts the result only for the tree at HEAD
# when it runs. It closes the bead `gc.outcome=pass` whatever the gate said — a
# failed step would be read as the iteration failing for another reason — and
# the check decides.
set -uo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CHECKS="$HERE/checks"
. "$CHECKS/lib.sh"

say() { echo "gater: $*" >&2; }

claim="$(gc hook --claim --drain-ack --json 2>/dev/null)" || { say "no claim (hook exit $?)"; exit 0; }
action="$(printf '%s' "$claim" | jq -r '.action // empty' 2>/dev/null)"
bead="$(printf '%s' "$claim" | jq -r '.bead_id // empty' 2>/dev/null)"
if [ "$action" != "work" ] || [ -z "$bead" ]; then
  say "nothing to gate (${action:-no action})"
  exit 0
fi
say "claimed $bead"

LOG="$(mktemp "${TMPDIR:-/tmp}/proof-gater.XXXXXX")"
trap 'rm -f "$LOG"' EXIT

# The gate runs in the same narrow environment the control dispatcher's check
# sandbox gave it (internal/convergence/condition.go): HOME is the city, PATH is
# the tools' directories plus the system ones, nothing else inherited — and then
# the rig's operator-configured gate_path and gate_home, exactly as
# proof_run_gates applies them. A session's own environment never reaches it.
sandbox_path() {
  local dirs="" t d
  for t in "bd" gc dolt jq; do
    d="$(dirname -- "$(command -v "$t" 2>/dev/null || echo /usr/bin/$t)")"
    case ":$dirs:" in *":$d:"*) ;; *) dirs="${dirs:+$dirs:}$d" ;; esac
  done
  printf '%s:/usr/local/bin:/usr/bin:/bin' "$dirs"
}

# run_gate <bead> — exit 0 pass, 1 fail; sets TREE and COMMIT when the
# worktree could be read. Runs in a subshell so lib.sh's proof_die only ends it.
TREE=""; COMMIT=""
result="error"
started="$(date +%s)"
if (
  # No `set -e` here: bash ignores it inside a subshell that is an `if`
  # condition, so every step fails the subshell explicitly. proof_die exits.
  export GC_BEAD_ID="$bead"
  ROOT_JSON="$(proof_root_json)" || exit 1
  proof_load_packet "$ROOT_JSON"
  printf '%s %s\n' "$HEAD_TREE" "$HEAD_COMMIT" >"$LOG.head" || exit 1
  if [ "$(metadata_of "$ROOT_JSON" proof.validated_tree)" = "$HEAD_TREE" ]; then
    echo "gater: tree $HEAD_TREE already passed a gate (proof.validated_tree); not gating it again" >&2
    : >"$LOG.reused" || exit 1
    exit 0
  fi
  gate="$(root_var "$ROOT_JSON" gate_command)"
  gate_path="$(root_var "$ROOT_JSON" gate_path)"
  gate_home="$(root_var "$ROOT_JSON" gate_home)"
  if [ -z "$gate" ]; then
    echo "gater: no gate_command configured for this rig; structural checks only" >&2
    exit 0
  fi
  echo "gater: running gate in $WORK_DIR on tree $HEAD_TREE: $gate" >&2
  city="${GC_CITY:-${GC_CITY_PATH:-$(cd -- "$HERE/../../../.." && pwd)}}"
  cd "$WORK_DIR" || exit 1
  gate_timeout="$(root_var "$ROOT_JSON" gate_timeout)"; gate_timeout="${gate_timeout:-90m}"
  [[ "$gate_timeout" =~ ^[1-9][0-9]*[smh]$ ]] || { echo "gater: invalid gate_timeout" >&2; exit 1; }
  timeout --kill-after=30s "$gate_timeout" env -i HOME="${gate_home:-$city}" PATH="${gate_path:+$gate_path:}$(sandbox_path)" \
    TMPDIR="${TMPDIR:-/tmp}" LANG=C.UTF-8 sh -c "$gate"
  gate_rc=$?
  if [ "$gate_rc" -ne 0 ]; then
    echo "gater: command exited $gate_rc (execution deadline $gate_timeout)" >&2
    exit "$gate_rc"
  fi
) >"$LOG" 2>&1; then
  result="pass"
else
  result="fail"
fi
if [ -f "$LOG.head" ]; then
  read -r TREE COMMIT <"$LOG.head"; rm -f "$LOG.head"
else
  # The worktree could not be read (missing, dirty, wrong branch, nothing
  # ahead): the check repeats those structural checks itself and will say why,
  # so record an error rather than a gate verdict.
  result="error"
fi
reused=false
if [ -f "$LOG.reused" ]; then reused=true; rm -f "$LOG.reused"; fi
dur=$(( $(date +%s) - started ))
say "$bead: $result on tree ${TREE:-?} in ${dur}s"
tail -40 "$LOG" >&2

# Metadata values are bounded: the tail the check repeats to the next iteration.
tail_text="$(tail -40 "$LOG" | cut -c1-300)"
lane="$(sed -n 's/^gate-fact: lane=//p' "$LOG" | tail -1)"
scorecard="$(sed -n 's/^scorecard: //p' "$LOG" | paste -sd ';' - | sed 's/;/; /g' | cut -c1-300)"
gc bd update "$bead" \
  --set-metadata "proof.gate_result=$result" \
  --set-metadata "proof.gate_tree=$TREE" \
  --set-metadata "proof.gate_commit=$COMMIT" \
  --set-metadata "proof.gate_session=${GC_SESSION_NAME:-}" \
  --set-metadata "proof.gate_session_id=${GC_SESSION_ID:-}" \
  --set-metadata "proof.gate_seconds=$dur" \
  --set-metadata "proof.gate_reused=$reused" \
  --set-metadata "proof.gate_lane=$lane" \
  --set-metadata "proof.scorecard=$scorecard" \
  --set-metadata "proof.gate_log_tail=$tail_text" >/dev/null \
  || { say "could not record the result on $bead"; exit 1; }
gc bd update "$bead" --set-metadata gc.outcome=pass --set-metadata gc.work_outcome=no-op \
  --status closed >/dev/null \
  || { say "could not close $bead"; exit 1; }
say "closed $bead"
exit 0
