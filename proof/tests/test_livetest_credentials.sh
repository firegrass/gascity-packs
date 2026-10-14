#!/usr/bin/env bash
# Tests for checks/livetest-credentials.sh and checks/livetest_token.py (ADR-0152).
#
# EVERY case here runs offline. `flyctl` is stubbed on PATH for the whole file,
# so no case reaches the network, contacts production or issues a credential —
# including the invalid-token case, which used to call the real `flyctl orgs
# list` and therefore depended on the operator's ambient Fly config.
#
# The CAPABILITY cases — the ones that prove a tier's boundary actually holds
# against the real fleet — need real Fly tokens and real network, so they are
# opt-in and run the judge directly against decoded caveats:
#
#   LIVETEST_CRED_TOKENS=1 \
#   LIVETEST_RO_TOKEN_FILE=/path/to/readonly \
#   LIVETEST_EXEC_TOKEN_FILE=/path/to/machine-exec \
#   LIVETEST_APP=liskov-api \
#   LIVETEST_EXEC_ALLOWED="/usr/local/bin/liskov_ops policy-rollback-floor-census" \
#     gascity/tests/test_livetest_credentials.sh
#
# Every capability probe is read-only, and for the negative cases a SUCCESS is
# the failure. None of them mutates anything, on any path.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK="$HERE/assets/scripts/checks/livetest-credentials.sh"
JUDGE="$HERE/assets/scripts/checks/livetest_token.py"
pass=0; fail=0

ok()  { pass=$((pass+1)); printf '  ok   %s\n' "$1"; }
bad() { fail=$((fail+1)); printf '  FAIL %s\n' "$1"; [ $# -gt 1 ] && printf '       %s\n' "$2"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
NOW=1800000000
APP_ID=24628470           # liskov-api, as the trusted descriptor carries it
OTHER_APP_ID=99999999

# --- the flyctl stub -------------------------------------------------------
# Two subcommands are reached by the check. Both are driven entirely by env, so
# a case declares the credential it is presenting rather than owning one.
mkdir -p "$TMP/bin"
cat > "$TMP/bin/flyctl" <<'STUB'
#!/usr/bin/env bash
case "${1:-} ${2:-}" in
  "tokens debug")
    if [ "${STUB_DEBUG_RC:-0}" != 0 ]; then
      # flyctl echoes the token it was handed back at you on some failures;
      # that is exactly what the check must redact.
      echo "Error: could not decode token ${FLY_API_TOKEN:-}"
      exit "${STUB_DEBUG_RC}"
    fi
    cat "${STUB_DEBUG_FILE:?stub: STUB_DEBUG_FILE unset}"
    ;;
  "orgs list")
    if [ "${STUB_ORGS_RC:-0}" != 0 ]; then
      echo "Error: token revoked (${FLY_API_TOKEN:-})"
      exit "${STUB_ORGS_RC}"
    fi
    echo "proof-computer"
    ;;
  *) echo "stub flyctl: unexpected command: $*" >&2; exit 97 ;;
esac
STUB
chmod +x "$TMP/bin/flyctl"
STUB_PATH="$TMP/bin:$PATH"

expect_rc() { # desc want_rc env... -- args...
  local desc="$1" want="$2"; shift 2
  local -a envs=() args=()
  while [ $# -gt 0 ] && [ "$1" != "--" ]; do envs+=("$1"); shift; done; shift || true
  args=("$@")
  local out rc
  out="$(env "PATH=$STUB_PATH" "${envs[@]}" "$CHECK" "${args[@]}" 2>&1)"; rc=$?
  if [ "$rc" -eq "$want" ]; then ok "$desc"; else bad "$desc" "want rc=$want got rc=$rc: $(printf '%s' "$out" | head -1)"; fi
}

judge() { # desc want_rc json_file -- judge args...
  local desc="$1" want="$2" file="$3"; shift 3; shift || true
  local out rc
  out="$(python3 "$JUDGE" "$@" < "$file" 2>&1)"; rc=$?
  if [ "$rc" -eq "$want" ]; then ok "$desc"; else bad "$desc" "want rc=$want got rc=$rc: $(printf '%s' "$out" | head -2 | tail -1)"; fi
}

# --- fixtures --------------------------------------------------------------
# The shapes are the ones measured against the live fleet on 2026-09-21 and
# recorded in docs/raw/2026-09-21-live-test-credential-tier-measurements.md.
mkfixtures() { # <dir> <now> <expiry-offset>
python3 - "$1" "$2" "$3" "$APP_ID" "$OTHER_APP_ID" <<'P'
import copy, json, sys
d, now, span, app, other = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5]
VERB = ["/usr/local/bin/liskov_ops", "policy-rollback-floor-census"]
ARMING = ["/usr/local/bin/liskov_ops", "platform-coverage-funding", "budget-activate"]

def window(nb=None, na=None):
    return {"type": "ValidityWindow",
            "body": {"not_before": now - 60 if nb is None else nb,
                     "not_after": now + span if na is None else na}}

def commands(*argvs):
    return {"type": "IfPresent",
            "body": {"ifs": [{"type": "Commands",
                              "body": [{"args": list(a)} for a in argvs]}],
                     "else": "r"}}

def apps(app_id):
    return {"type": "IfPresent",
            "body": {"ifs": [{"type": "Apps", "body": {"apps": {app_id: "rwcdC"}}},
                             {"type": "FeatureSet",
                              "body": {"features": {"builder": "rwcdC", "wg": "rwcdC"}}}],
                     "else": "r"}}

def token(*caveats):
    return [{"location": "https://api.fly.io/v1",
             "caveats": [{"type": "Organization", "body": {"id": 1649235, "mask": "rwcdC"}}]
                        + list(caveats)},
            {"location": "https://api.fly.io/aaa/v1", "caveats": [window()]}]

def write(name, doc):
    json.dump(doc, open("%s/%s.json" % (d, name), "w"))

readonly = [{"location": "https://api.fly.io/v1", "caveats": [
                {"type": "Organization", "body": {"id": 1649235, "mask": "rwcdC"}},
                {"type": "Organization", "body": {"id": 1649235, "mask": "r"}}]},
            {"location": "https://api.fly.io/aaa/v1", "caveats": [window()]}]
write("readonly", readonly)
write("exec", token(apps(app), commands(VERB)))

# wrong app: a perfectly valid single-app token, for something else
write("exec_wrongapp", token(apps(other), commands(VERB)))
# flyctl's default when neither -C nor -p is passed: every command allowed
write("exec_noallow", token(apps(app)))
# an allowlist that is a bare binary prefix — it covers the requested read and
# every other verb with it
write("exec_broad", token(apps(app), commands(["/usr/local/bin/liskov_ops"])))
# shelling out: refused server side by Fly, and refused here before we ask
write("exec_shell", token(apps(app), commands(["/bin/sh", "-c"])))
# an arming verb in a mutate credential
write("exec_arming", token(apps(app), commands(VERB, ARMING)))

# validity windows
twenty_year = token(apps(app), commands(VERB))
twenty_year[1]["caveats"] = [window(na=now + 175200 * 3600)]
write("exec_20yr", twenty_year)
noexp = token(apps(app), commands(VERB)); noexp[1]["caveats"] = []
write("exec_noexp", noexp)
expired = token(apps(app), commands(VERB))
expired[1]["caveats"] = [window(na=now - 600)]
write("exec_expired", expired)
ro_future = copy.deepcopy(readonly)
ro_future[1]["caveats"] = [window(nb=now + 3600, na=now + 86400)]
write("readonly_future", ro_future)
# an hour of life left: enough for the 30-minute floor, and not enough for a
# 59-minute exercise plus its cleanup margin
ro_hour = copy.deepcopy(readonly)
ro_hour[1]["caveats"] = [window(na=now + 3600)]
write("readonly_hour", ro_hour)

# ambiguous structures: each must refuse rather than flatten into a union
write("exec_two_commands", token(apps(app), commands(VERB), commands(ARMING)))
write("exec_two_apps", token(apps(app), apps(other), commands(VERB)))
nested = token(apps(app))
nested[0]["caveats"].append({"type": "IfPresent", "body": {
    "ifs": [{"type": "IfPresent", "body": {
        "ifs": [{"type": "Commands", "body": [{"args": VERB}]}], "else": "r"}}],
    "else": "r"}})
write("exec_nested", nested)
wide_else = token(apps(app))
wide_else[0]["caveats"].append({"type": "IfPresent", "body": {
    "ifs": [{"type": "Commands", "body": [{"args": VERB}]}], "else": "rwcdC"}})
write("exec_wide_else", wide_else)
unknown = token(apps(app), commands(VERB))
unknown[0]["caveats"].append({"type": "Machines", "body": {"machines": {"abc": "rwcdC"}}})
write("exec_unknown", unknown)
P
}
mkfixtures "$TMP" "$NOW" 86400

echo "argument validation"
expect_rc "missing --exercise refuses"        64 X=1 -- --app liskov-api --max-exercise-seconds 120
expect_rc "bad --exercise refuses"            64 X=1 -- --exercise sudo --app liskov-api --max-exercise-seconds 120
expect_rc "missing --app refuses"             64 X=1 -- --exercise observe --max-exercise-seconds 120
expect_rc "missing exercise deadline refuses" 64 X=1 -- --exercise observe --app liskov-api
expect_rc "non-numeric deadline refuses"      64 X=1 -- --exercise observe --app liskov-api --max-exercise-seconds soon
expect_rc "mutate without --verb refuses"     22 X=1 -- --exercise mutate --app liskov-api --max-exercise-seconds 120

echo "fail-closed defaults"
expect_rc "no FLY_API_TOKEN refuses"          10 FLY_API_TOKEN= LISKOV_LIVETEST_EXERCISE=observe -- --exercise observe --app liskov-api --max-exercise-seconds 120
expect_rc "no declared tier refuses"          11 FLY_API_TOKEN=x LISKOV_LIVETEST_EXERCISE= -- --exercise observe --app liskov-api --max-exercise-seconds 120
expect_rc "tier mismatch refuses"             12 FLY_API_TOKEN=x LISKOV_LIVETEST_EXERCISE=observe --verb=/bin/true -- --exercise spend --app liskov-api --max-exercise-seconds 120 --verb /bin/true

echo "the wrapper, with flyctl stubbed (no network on any path)"
# fixtures for the wrapper, which uses the real clock
WNOW="$(date +%s)"; mkdir -p "$TMP/live"; mkfixtures "$TMP/live" "$WNOW" 86400
expect_rc "a readonly token passes observe preflight" 0 \
  FLY_API_TOKEN=tok-abc LISKOV_LIVETEST_EXERCISE=observe "STUB_DEBUG_FILE=$TMP/live/readonly.json" \
  -- --exercise observe --app liskov-api --max-exercise-seconds 3600
expect_rc "undecodable caveats refuse before any probe" 14 \
  FLY_API_TOKEN=tok-abc LISKOV_LIVETEST_EXERCISE=observe STUB_DEBUG_RC=1 \
  -- --exercise observe --app liskov-api --max-exercise-seconds 3600
expect_rc "a revoked but decodable token refuses" 13 \
  FLY_API_TOKEN=tok-abc LISKOV_LIVETEST_EXERCISE=observe "STUB_DEBUG_FILE=$TMP/live/readonly.json" STUB_ORGS_RC=1 \
  -- --exercise observe --app liskov-api --max-exercise-seconds 3600
expect_rc "an exec token presented as observe refuses" 20 \
  FLY_API_TOKEN=tok-abc LISKOV_LIVETEST_EXERCISE=observe "STUB_DEBUG_FILE=$TMP/live/exec.json" \
  -- --exercise observe --app liskov-api --max-exercise-seconds 3600
expect_rc "an unknown target refuses (absent descriptor)" 20 \
  FLY_API_TOKEN=tok-abc LISKOV_LIVETEST_EXERCISE=observe "STUB_DEBUG_FILE=$TMP/live/readonly.json" \
  -- --exercise observe --app liskov-console --max-exercise-seconds 3600

# The caveats must be judged BEFORE the liveness probe: a packet refused on its
# caveats must never have touched the network. The stub fails `orgs list`
# loudly, and the wrong-app refusal must still be the one reported.
out="$(env "PATH=$STUB_PATH" FLY_API_TOKEN=tok-abc LISKOV_LIVETEST_EXERCISE=mutate \
  "STUB_DEBUG_FILE=$TMP/live/exec_wrongapp.json" STUB_ORGS_RC=1 \
  "$CHECK" --exercise mutate --app liskov-api --max-exercise-seconds 3600 \
  --verb "/usr/local/bin/liskov_ops policy-rollback-floor-census" 2>&1)"
rc=$?
if [ "$rc" -eq 20 ] && printf '%s' "$out" | grep -q "wrong app"; then
  ok "caveats are judged before the network probe"
else
  bad "caveats are judged before the network probe" "rc=$rc: $(printf '%s' "$out" | head -2 | tail -1)"
fi

# A refusal must not leak the credential into the log.
out="$(env "PATH=$STUB_PATH" FLY_API_TOKEN=fo1-secret-value LISKOV_LIVETEST_EXERCISE=observe \
  STUB_DEBUG_RC=1 "$CHECK" --exercise observe --app liskov-api --max-exercise-seconds 3600 2>&1)"
if printf '%s' "$out" | grep -q 'fo1-secret-value'; then
  bad "a refusal never prints the token" "the token appeared in the output"
elif printf '%s' "$out" | grep -q '<redacted-token>'; then
  ok "a refusal never prints the token"
else
  bad "a refusal never prints the token" "no redaction marker in: $(printf '%s' "$out" | head -2 | tail -1)"
fi

echo "caveat inspection: tier shape"
judge "readonly is a valid observe credential"     0 "$TMP/readonly.json" -- --exercise observe --app liskov-api --max-exercise-seconds 120 --now $NOW
judge "readonly is NOT a mutate credential"        2 "$TMP/readonly.json" -- --exercise mutate --app liskov-api --max-exercise-seconds 120 --verb "/bin/true" --now $NOW
judge "exec token is NOT an observe credential"    2 "$TMP/exec.json"     -- --exercise observe --app liskov-api --max-exercise-seconds 120 --now $NOW
judge "exec token covers its allow-listed verb"    0 "$TMP/exec.json"     -- --exercise mutate --app liskov-api --max-exercise-seconds 120 --verb "/usr/local/bin/liskov_ops policy-rollback-floor-census --json" --now $NOW
judge "exec token refuses an uncovered verb"       2 "$TMP/exec.json"     -- --exercise mutate --app liskov-api --max-exercise-seconds 120 --verb "/usr/local/bin/liskov_ops custody-observe" --now $NOW
judge "no allowlist is refused (flyctl default)"   2 "$TMP/exec_noallow.json" -- --exercise mutate --app liskov-api --max-exercise-seconds 120 --verb "/usr/local/bin/liskov_ops policy-rollback-floor-census" --now $NOW

echo "containment: the credential may be no wider than the tier"
judge "a bare liskov_ops prefix is refused"        2 "$TMP/exec_broad.json" -- --exercise mutate --app liskov-api --max-exercise-seconds 120 --verb "/usr/local/bin/liskov_ops policy-rollback-floor-census" --now $NOW
judge "a shell prefix is refused"                  2 "$TMP/exec_shell.json" -- --exercise mutate --app liskov-api --max-exercise-seconds 120 --verb "/bin/sh -c id" --now $NOW
judge "an arming verb is refused at mutate"        2 "$TMP/exec_arming.json" -- --exercise mutate --app liskov-api --max-exercise-seconds 120 --verb "/usr/local/bin/liskov_ops policy-rollback-floor-census" --now $NOW
judge "the same token is permitted at spend"       0 "$TMP/exec_arming.json" -- --exercise spend --app liskov-api --max-exercise-seconds 120 --verb "/usr/local/bin/liskov_ops platform-coverage-funding budget-activate" --now $NOW

echo "the target app"
judge "a valid token for the wrong app refuses"    2 "$TMP/exec_wrongapp.json" -- --exercise mutate --app liskov-api --max-exercise-seconds 120 --verb "/usr/local/bin/liskov_ops policy-rollback-floor-census" --now $NOW
judge "an unknown target refuses (no descriptor)"  2 "$TMP/readonly.json" -- --exercise observe --app liskov-console --max-exercise-seconds 120 --now $NOW

echo "the exercise deadline"
judge "twenty-year expiry is refused"              2 "$TMP/exec_20yr.json"    -- --exercise mutate --app liskov-api --max-exercise-seconds 120 --verb "/usr/local/bin/liskov_ops policy-rollback-floor-census" --now $NOW
judge "no expiry at all is refused"                2 "$TMP/exec_noexp.json"   -- --exercise mutate --app liskov-api --max-exercise-seconds 120 --verb "/usr/local/bin/liskov_ops policy-rollback-floor-census" --now $NOW
judge "an expired token is refused"                2 "$TMP/exec_expired.json" -- --exercise mutate --app liskov-api --max-exercise-seconds 120 --verb "/usr/local/bin/liskov_ops policy-rollback-floor-census" --now $NOW
judge "a token expiring in minutes is refused"     2 "$TMP/exec.json" -- --exercise mutate --app liskov-api --max-exercise-seconds 120 --verb "/usr/local/bin/liskov_ops policy-rollback-floor-census" --now $((NOW+86400-300))
judge "a not-yet-valid token is refused"           2 "$TMP/readonly_future.json" -- --exercise observe --app liskov-api --max-exercise-seconds 120 --now $NOW
judge "an hour of life covers the 30-minute floor" 0 "$TMP/readonly_hour.json" -- --exercise observe --app liskov-api --max-exercise-seconds 120 --now $NOW
judge "but not a 59-minute exercise plus cleanup"  2 "$TMP/readonly_hour.json" -- --exercise observe --app liskov-api --max-exercise-seconds 3540 --now $NOW
judge "an exercise beyond the target ceiling refuses" 2 "$TMP/readonly.json" -- --exercise observe --app liskov-api --max-exercise-seconds 100000 --now $NOW

echo "ambiguous caveats refuse instead of flattening"
judge "two Commands caveats refuse"                2 "$TMP/exec_two_commands.json" -- --exercise mutate --app liskov-api --max-exercise-seconds 120 --verb "/usr/local/bin/liskov_ops policy-rollback-floor-census" --now $NOW
judge "two Apps caveats refuse"                    2 "$TMP/exec_two_apps.json" -- --exercise mutate --app liskov-api --max-exercise-seconds 120 --verb "/usr/local/bin/liskov_ops policy-rollback-floor-census" --now $NOW
judge "a nested IfPresent refuses"                 2 "$TMP/exec_nested.json" -- --exercise mutate --app liskov-api --max-exercise-seconds 120 --verb "/usr/local/bin/liskov_ops policy-rollback-floor-census" --now $NOW
judge "a widening IfPresent fallback refuses"      2 "$TMP/exec_wide_else.json" -- --exercise mutate --app liskov-api --max-exercise-seconds 120 --verb "/usr/local/bin/liskov_ops policy-rollback-floor-census" --now $NOW
judge "an unmodelled caveat type refuses"          2 "$TMP/exec_unknown.json" -- --exercise mutate --app liskov-api --max-exercise-seconds 120 --verb "/usr/local/bin/liskov_ops policy-rollback-floor-census" --now $NOW
judge "malformed debug output is refused"          2 /dev/null -- --exercise observe --app liskov-api --max-exercise-seconds 120 --now $NOW

if [ "${LIVETEST_CRED_TOKENS:-0}" != "1" ]; then
  echo "live-token cases SKIPPED (set LIVETEST_CRED_TOKENS=1 with token files to run)"
else
  RO="${LIVETEST_RO_TOKEN_FILE:?}"; EX="${LIVETEST_EXEC_TOKEN_FILE:?}"
  APP="${LIVETEST_APP:-liskov-api}"
  T="$(mktemp -d)"
  flyctl tokens debug -t "$(cat "$RO")" > "$T/ro.json" 2>/dev/null
  flyctl tokens debug -t "$(cat "$EX")" > "$T/ex.json" 2>/dev/null
  judge "LIVE readonly token judges as observe"    0 "$T/ro.json" -- --exercise observe --app "$APP" --max-exercise-seconds 120
  judge "LIVE readonly token refused as mutate"    2 "$T/ro.json" -- --exercise mutate --app "$APP" --max-exercise-seconds 120 --verb "/bin/true"
  judge "LIVE exec token refused as observe"       2 "$T/ex.json" -- --exercise observe --app "$APP" --max-exercise-seconds 120
  judge "LIVE exec token covers its verb"          0 "$T/ex.json" -- --exercise mutate --app "$APP" --max-exercise-seconds 120 --verb "${LIVETEST_EXEC_ALLOWED:?}"
  judge "LIVE exec token refuses another verb"     2 "$T/ex.json" -- --exercise mutate --app "$APP" --max-exercise-seconds 120 --verb "/usr/local/bin/liskov_ops custody-observe"
  rm -rf "$T"
fi

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
