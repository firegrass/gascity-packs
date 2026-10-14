#!/usr/bin/env bash
# gc doctor check: judgement-key.
#
# Exit 0 = OK, 1 = warning. This check NEVER errors: a judgement that cannot be
# made fails open to the behaviour the caller had before it, so an unreachable
# key is a thing to tell the operator, never a thing to stop the city over.
#
# Every judgement in the pack goes through `assets/scripts/judge.py`, which
# finds `typesafe_client.py` in the Gas City-owned orchestrator clone. The
# client takes its key from TYPESAFE_API_KEY, else `.secret-typesafe-api-key`
# at that clone's root. The factory reaches admission as supervisor -> factory
# order -> dispatcher -> admit.py, so what matters is the environment the
# RUNNING supervisor has, not the one in your shell and not the one a drop-in
# file describes: a drop-in written but never reloaded changes nothing, and
# that is the failure this check exists to catch.
#
# There are TWO paths to a key and they do not share one. An agent session — the
# planner running `shape_check.py` on its drafts — is a child of the city's tmux
# server, not of the supervisor, and a tmux server started before the drop-in
# never sees it: measured 2026-09-20, the supervisor had the name and all six
# live sessions did not. For a session only the key FILE in the owned clone
# works (`shape_check.py` looks in the clone above its worktree). So this check
# is OK only when both paths reach a key, and says which one does not.
#
# It calls no model. It prints whether the name is set, never a value.
set -uo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

report="$(python3 - "$here/../../assets/scripts" <<'PY'
import json, os, subprocess, sys

scripts = sys.argv[1]
sys.path.insert(0, scripts)
NAME = "TYPESAFE_API_KEY"
KEY_FILE = ".secret-typesafe-api-key"


def run(*argv):
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except Exception:
        return ""
    return out.stdout if out.returncode == 0 else ""


def supervisor_unit():
    listed = run("systemctl", "--user", "list-units", "--all", "--no-legend",
                 "gascity-supervisor-*.service")
    for line in listed.splitlines():
        for field in line.split():
            if field.endswith(".service"):
                return field
    return ""


def running_has_key(unit):
    """The environment the supervisor actually started with, name only."""
    pid = run("systemctl", "--user", "show", unit, "-p", "MainPID", "--value").strip()
    if not pid.isdigit() or pid == "0":
        return None
    try:
        with open("/proc/%s/environ" % pid, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    for entry in raw.split(b"\0"):
        name, _, value = entry.partition(b"=")
        if name.decode("utf-8", "replace") == NAME:
            return bool(value.strip())
    return False


def configured_has_key(unit):
    """What the unit and its drop-ins declare, name only."""
    for entry in run("systemctl", "--user", "show", unit, "-p", "Environment", "--value").split():
        if entry.split("=", 1)[0] == NAME:
            return True
    return False


def owned_clone():
    """The Gas City-owned clone of the repository that holds the backlog, found
    by the pack's own helper so there is one definition of it, not two."""
    try:
        import admit
        doc = json.loads(run("gc", "config", "show", "--json"))
        rigs = {r["Name"]: r for r in doc["config"].get("Rigs") or []}
        return admit.backlog_clone_of(rigs)
    except Exception:
        return ""


unit = supervisor_unit()
running = running_has_key(unit) if unit else None
configured = configured_has_key(unit) if unit else False
clone = owned_clone()
key_path = os.path.join(clone, KEY_FILE) if clone else ""
on_disk = bool(key_path) and os.path.isfile(key_path) and os.path.getsize(key_path) > 0

notes = []
notes.append("supervisor unit: %s" % (unit or "not found (gascity-supervisor-*.service)"))
notes.append("  running environment carries %s: %s"
             % (NAME, {True: "yes", False: "no", None: "unknown (no readable main pid)"}[running]))
notes.append("  unit and drop-ins declare %s: %s" % (NAME, "yes" if configured else "no"))
notes.append("owned orchestrator clone: %s" % (clone or "not found in this city"))
notes.append("  %s: %s" % (KEY_FILE, "present" if on_disk else "absent"))

factory_ok = bool(running) or on_disk     # supervisor -> dispatcher -> admit.py
session_ok = on_disk                       # tmux server -> agent session -> shape_check.py
notes.append("factory path (admission): %s" % ("reaches a key" if factory_ok else "NO key"))
notes.append("agent-session path (the planner's shape_check.py): %s"
             % ("reaches a key" if session_ok else "NO key - sessions do not inherit the supervisor's environment"))

if factory_ok and session_ok:
    print("the pack's judgements can reach a TypeSafe key on both paths: admission, and agent sessions")
    print("\n".join(notes))
    sys.exit(0)

if factory_ok:
    print("admission can reach a TypeSafe key but AGENT SESSIONS cannot: the planner's shape_check.py "
          "prints 'not evaluated' and its production warning never fires")
    notes.append("fix: install -m 600 <your key file> %s" % (key_path or "<owned clone>/" + KEY_FILE))
    print("\n".join(notes))
    sys.exit(1)

if configured and running is False:
    print("%s is configured for the supervisor but the RUNNING one does not have it "
          "— `systemctl --user daemon-reload` and restart it" % NAME)
else:
    print("the pack's judgements have no TypeSafe key; every one of them fails open "
          "and the deliverability corpus stops growing")
    notes.append("fix: add %s to a drop-in for the supervisor unit, or put the key in %s"
                 % (NAME, key_path or "<owned clone>/" + KEY_FILE))
print("\n".join(notes))
sys.exit(1)
PY
)"
rc=$?
printf '%s\n' "$report"
# Never an error: 2+ would stop the city over an advisory judgement.
exit $(( rc == 0 ? 0 : 1 ))
