#!/usr/bin/env bash
# gc doctor check: work-retention.
#
# Exit 0 = OK, 1 = warning, 2+ = error. The first stdout line is the message.
#
# Runs the pack's retention doctor against the explicit ledger workspace the
# city names (LISKOV_WORK_WORKSPACE and friends come from the city's order
# overrides; `gc doctor` does not carry them, so this check reads the same
# values from the city's effective config). Admission reads the state file the
# doctor writes, so a red here is a red for new admissions, which is why it is
# an error and not a warning. A city that has not installed the ledger yet
# (no LISKOV_WORK_WORKSPACE anywhere) is reported as a warning.
set -uo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
script="$here/../../assets/scripts/work_maintenance.py"

city="${GC_CITY_PATH:-$(pwd)}"
if [ -z "${LISKOV_WORK_WORKSPACE:-}" ] && command -v gc >/dev/null 2>&1; then
  resolved="$(gc config show --json 2>/dev/null | python3 -c '
import json, sys
doc = json.load(sys.stdin)
for o in (doc["config"].get("Orders") or {}).get("Overrides") or []:
    env = o.get("Env") or {}
    if o.get("Name") != "proof-work-doctor":
        continue
    # ADR-0239 decision 7: the ledger is maintained on one factory; every other
    # city disables the three proof-work-* orders by override.
    if o.get("Enabled", o.get("enabled")) is False:
        print("DISABLED")
        break
    if env.get("LISKOV_WORK_WORKSPACE"):
        print(json.dumps(env))
        break
' 2>/dev/null)"
  if [ "$resolved" = "DISABLED" ]; then
    echo "ledger maintenance runs on another factory (proof-work-doctor is disabled by override in this city)"
    exit 0
  fi
  if [ -n "$resolved" ]; then
    for name in LISKOV_WORK_WORKSPACE LISKOV_WORK_DATA_DIR LISKOV_WORK_MAX_JOURNAL_BYTES LISKOV_WORK_REMOTE LISKOV_WORK_REF LISKOV_WORK_REMOTE_NAME LISKOV_WORK_BACKUP_ROOT LISKOV_WORK_COMPACTOR_ORDER; do
      value="$(printf '%s' "$resolved" | python3 -c 'import json, sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$name")"
      [ -n "$value" ] && export "$name=$value"
    done
  fi
fi
if [ -z "${LISKOV_WORK_WORKSPACE:-}" ]; then
  echo "the PROOF work ledger is not installed in this city (no LISKOV_WORK_WORKSPACE on the proof-work-doctor order)"
  exit 1
fi

report="$(python3 "$script" --city "$city" doctor 2>&1)"
rc=$?
summary="$(printf '%s' "$report" | python3 -c '
import json, sys
text = sys.stdin.read()
try:
    doc = json.loads(text[text.index("{"):])
except Exception:
    print("retention doctor did not report: " + text.strip().splitlines()[-1] if text.strip() else "no output")
    sys.exit(0)
if doc.get("ok"):
    print("work ledger retention ok: replication lag %ss, %s hourly and %s daily backups"
          % (doc["replication"].get("lag_seconds"), doc["backup"].get("hourly"), doc["backup"].get("daily")))
else:
    print("work ledger retention drift (admission refuses): " + ", ".join(doc.get("problems") or ["unknown"]))
')"
printf '%s\n' "$summary"
printf '%s\n' "$report"
[ "$rc" -eq 0 ] && exit 0
exit 2
