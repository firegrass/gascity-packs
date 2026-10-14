#!/usr/bin/env bash
# Preflight credential check for a live-test packet (ADR-0152).
#
# Refuses BEFORE any production contact when the tester's credential is absent,
# expired, not yet valid, scoped to another app, or wider or narrower than the
# exercise level the packet was admitted at. A credential that expires
# mid-exercise is the one new failure this design could introduce: `settle` would
# still run as a teardown, but against a surface it can no longer reach — so the
# check is bound to the WHOLE exercise deadline, not to a fixed floor.
#
# Usage:
#   livetest-credentials.sh --exercise observe|mutate|spend --app <app> \
#     --max-exercise-seconds <n> [--verb <cmd>]...
#
# Exit 0 = the credential matches the tier. Non-zero = refuse the packet; the
# first stdout line says which condition failed, in the words an operator needs.
#
# This script makes NO mutating call on any path, including every refusal path,
# and it never prints token material: flyctl's own output is redacted before it
# reaches the log.
set -uo pipefail

ME="livetest-credentials"
exercise=""; app=""; max_seconds=""; verbs=()

while [ $# -gt 0 ]; do
  case "$1" in
    --exercise) exercise="${2:?--exercise needs a value}"; shift 2 ;;
    --app)      app="${2:?--app needs a value}"; shift 2 ;;
    --max-exercise-seconds) max_seconds="${2:?--max-exercise-seconds needs a value}"; shift 2 ;;
    --verb)     verbs+=("${2:?--verb needs a value}"); shift 2 ;;
    *) echo "$ME: unknown argument" >&2; exit 64 ;;
  esac
done

case "$exercise" in
  observe|mutate|spend) ;;
  "") echo "$ME: --exercise is required (observe|mutate|spend)"; exit 64 ;;
  *)  echo "$ME: --exercise must be observe, mutate or spend"; exit 64 ;;
esac
[ -n "$app" ] || { echo "$ME: --app is required"; exit 64; }
case "$max_seconds" in
  "") echo "$ME: --max-exercise-seconds is required: the credential is checked against"
      echo "the whole exercise deadline plus its cleanup margin, and an undeclared"
      echo "deadline cannot be checked against anything."; exit 64 ;;
  *[!0-9]*|0) echo "$ME: --max-exercise-seconds must be a positive whole number of seconds,"
      echo "the supplied deadline is invalid."; exit 64 ;;
esac

# A mutate or spend packet must say what it will run before anything is judged:
# the token's allowlist is checked against the packet's commands in both
# directions, and with no commands named there is nothing to check.
if [ "$exercise" != "observe" ] && [ "${#verbs[@]}" -eq 0 ]; then
  echo "$ME: REFUSE — a $exercise packet must name the verbs it will run (--verb),"
  echo "so the token's allowlist can be checked against what the packet needs."
  exit 22
fi

# Redact anything token-shaped before printing a tool's output. The token is a
# secret; a refusal message is not a reason to leak it into a log.
redact() {
  local text="$1"
  if [ -n "${FLY_API_TOKEN:-}" ]; then
    text="${text//"$FLY_API_TOKEN"/<redacted-token>}"
  fi
  printf '%s\n' "$text" | grep -viE '^\s*$' | head -3
}

# 1. The credential must be present, and must be the one the tier supplies.
if [ -z "${FLY_API_TOKEN:-}" ]; then
  echo "$ME: REFUSE — no FLY_API_TOKEN in the environment."
  echo "The tier agent supplies it through the machine-local city config as a rig"
  echo "agent patch (see gascity/city.reference.toml). With none injected the tester"
  echo "has no Fly credential, which is the correct fail-closed default."
  exit 10
fi

# The tier agent declares its own level. A mismatch means the packet was slung at
# the wrong agent, and the credential in scope is not the one the packet assumes.
declared="${LISKOV_LIVETEST_EXERCISE:-}"
if [ -z "$declared" ]; then
  echo "$ME: REFUSE — LISKOV_LIVETEST_EXERCISE is unset, so the running agent does"
  echo "not declare a tier. Sling this packet at proof.tester-$exercise."
  exit 11
fi
if [ "$declared" != "$exercise" ]; then
  echo "$ME: REFUSE — tier mismatch: packet admitted at '$exercise', running agent"
  echo "declares a different tier. The credential in scope is not the one this packet"
  echo "was authorized against. Sling it at proof.tester-$exercise."
  exit 12
fi

# 2. INSPECT the credential, and do it BEFORE any network probe. A probe cannot
#    tell a read-only token from a narrowly-scoped exec token (both refuse an
#    arbitrary command), and a probe that is wrong the other way runs something in
#    production to find out. `tokens debug` decodes the macaroon's caveats — the
#    allowlist, the app scope, the org mask and the validity window — reveals no
#    secret material, and reaches nothing: a credential for the wrong app or a
#    lapsed deadline is refused without the fleet ever hearing from us.
if ! debug="$(timeout 60 flyctl tokens debug -t "$FLY_API_TOKEN" 2>&1)"; then
  echo "$ME: REFUSE — could not decode the token's caveats. flyctl said:"
  redact "$debug"
  exit 14
fi

judge_args=(--exercise "$exercise" --app "$app" --max-exercise-seconds "$max_seconds")
for v in ${verbs+"${verbs[@]}"}; do judge_args+=(--verb "$v"); done

if ! verdict="$(printf '%s' "$debug" | python3 "$(dirname "$0")/livetest_token.py" "${judge_args[@]}" 2>&1)"; then
  printf '%s: %s\n' "$ME" "$verdict"
  exit 20
fi

# 3. Only now, with the caveats judged, confirm the credential is live. This is a
#    read, and it is the cheapest possible one. A revoked token decodes perfectly
#    well, so the inspection above cannot replace it.
if ! probe="$(timeout 60 flyctl orgs list 2>&1)"; then
  echo "$ME: REFUSE — the Fly credential is not usable (revoked, or the API is"
  echo "unreachable). flyctl said:"
  redact "$probe"
  exit 13
fi

printf '%s: %s\n' "$ME" "$verdict"
exit 0
