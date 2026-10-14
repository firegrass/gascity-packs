#!/usr/bin/env bash
# One factory pass: refresh the rules every rig worker is given, free any packet
# that has stalled with nobody at fault (watchdog.py), complete what has merged,
# then fill the slots that freed.
# Run by the `proof-factory` exec order. Stages run unlocked; backlog writers
# take factory_lib.backlog_write only around the owned clone's edit, commit and
# push (or undo). A busy writer skips its landing, never the whole pass.
set -uo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CITY="${GC_CITY:-${GC_CITY_PATH:-$(cd -- "$HERE/../../../.." && pwd)}}"
export GC_CITY="$CITY"
mkdir -p "$CITY/proof"
LOG="$CITY/proof/factory.log"
HEALTH=factory
case " $* " in *" --dry-run "*) HEALTH=factory-dry-run ;; esac
heartbeat() { python3 "$HERE/factory_health.py" "$CITY" "$HEALTH" "$@"; }
heartbeat start
trap 'code=$?; heartbeat finished "$code"' EXIT
# A stalled connection must die in a minute: keepalives for
# every git-over-ssh call the pass makes.
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=4}"
status=0
heartbeat rig-context
case " $* " in *" --dry-run "*) ;; *) python3 "$HERE/rig_context.py" >>"$LOG" 2>&1 || status=$? ;; esac
# The watchdog never decides a pass: a fault in it must not stop completions.
heartbeat watchdog
python3 "$HERE/watchdog.py" "$@" >>"$LOG" 2>&1 || echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) watchdog: exited $?" >>"$LOG"
heartbeat stall-recovery
python3 "$HERE/stall_recovery.py" "$@" >>"$LOG" 2>&1 || status=$?
heartbeat completer
python3 "$HERE/completer.py" "$@" >>"$LOG" 2>&1 || status=$?
# Released stops get a planning turn before another long dispatch batch.
# The trigger reads and admits unlocked; its logger already writes factory.log.
heartbeat planner
python3 "$HERE/plan_trigger.py" "$@" >/dev/null 2>>"$LOG" || status=$?
heartbeat dispatcher
python3 "$HERE/dispatcher.py" "$@" >>"$LOG" 2>&1 || status=$?
tail -n 5 "$LOG"
exit "$status"
