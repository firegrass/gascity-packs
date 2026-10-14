#!/usr/bin/env python3
"""PROOF claim: `gc hook --claim --json` that never hands a session an
orchestrator topology bead. A script — no model, no tokens.

    claim.py [--drain-ack]

Prints exactly one `gc hook --claim --json` result line on stdout, as gc does,
and exits with gc's exit code — so a caller reads it as it reads gc's own.

Why it exists. gc 1.4.2 routes the scope bead of every SECOND and later
iteration of a ralph loop (`implement`, `review-cycle`) to the loop's worker
pool: `spawnNextAttempt` (internal/dispatch/control.go) routes each step of the
attempt recipe except spec and control kinds, and the iteration's root is a
`gc.kind=scope` bead. The compile path skips topology kinds
(`graphroute.IsWorkflowTopologyKind`); the attempt path does not. The scope is
blocked by the iteration's steps, so it is ready from the moment the last one
closes until the control dispatcher closes it, and the first session of the
pool to claim in that window is handed it — usually the worker that just closed
the step, sometimes a fresh session or one on another packet. Either the
dispatcher closes it a second later (the worker sees "already closed") or it
stays `in_progress` and assigned, and every later claim answers
`existing_assignment` with it, hiding the worker's real steps. Workers rightly
refused to execute it and mailed BLOCKED, and the packet stalled (2026-10-03 to
2026-10-09: lrs-iwis4q, lrs-661bq7, lrs-pvjini, lrs-qs3kyu, lrs-gvxwxi,
lrs-ad2o0u, lrs-aya055, orc-cymq3, lui-sjp5r). The upstream issue draft is
docs/raw/2026-10-09-gc-scope-bead-claimed-by-worker-upstream-issue.md.

What it does with each claimed bead:

- `gc.kind` workflow, scope or spec, not closed, assigned to THIS session:
  released — `gc bd update <id> --assignee "" --force`, which leaves the status
  alone (an `in_progress`, unassigned bead is never offered again; the control
  dispatcher closes it as before) — and the claim is asked again.
- Closed, any kind: not work any more. Nothing is changed; the claim is asked
  again.
- Anything else, including a control kind (`ralph`, `retry`, `check`, …), is
  passed through unchanged: those are routed to the control dispatcher and a
  session receiving one is a new fault, which the session escalates.

It only ever releases a bead assigned to one of the calling session's own
identities ($GC_SESSION_NAME, $GC_SESSION_ID, $GC_ALIAS), and never one a
control dispatcher holds, so it cannot take a bead from a controller. The
watchdog makes the same release every factory pass as a backstop.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

TOPOLOGY = ("workflow", "scope", "spec")   # beadmeta.WorkflowTopologyKinds: never work
ROUNDS = 6                                 # releases before giving up on a claim


def identities(env):
    """The calling session's own names; empty outside a session."""
    out = []
    for key in ("GC_SESSION_NAME", "GC_SESSION_ID", "GC_ALIAS"):
        value = (env.get(key) or "").strip()
        if value and "control-dispatcher" not in value and value not in out:
            out.append(value)
    return out


def verdict(bead, mine):
    """"work" | "release" | "again" for a bead `gc hook --claim` returned.

    release: a topology bead held by this session — unassign it, claim again.
    again:   closed, or a topology bead not held by this session — claim again,
             change nothing.
    work:    everything else, unchanged."""
    md = bead.get("metadata") or {}
    kind = (md.get("gc.kind") or "").strip()
    if (bead.get("status") or "").strip() == "closed":
        return "again"
    if kind not in TOPOLOGY:
        return "work"
    holder = (bead.get("assignee") or "").strip()
    return "release" if holder and holder in mine else "again"


def last_json(text):
    """The last JSON object line of gc's stdout, or None."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def show(gc, bead_id):
    done = gc("bd", "show", bead_id, "--json")
    if done.returncode != 0:
        return None
    try:
        value = json.loads(done.stdout or "null")
    except ValueError:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
    return value if isinstance(value, dict) else None


def claim(gc, drain_ack, env, err):
    """(stdout line, exit code) — the first claim result that is real work, a
    drain, or a refusal to go round again."""
    mine = identities(env)
    flags = ["hook", "--claim", "--json"] + (["--drain-ack"] if drain_ack else [])
    released = []
    for _ in range(ROUNDS):
        done = gc(*flags)
        result = last_json(done.stdout)
        if not result or result.get("action") != "work" or not result.get("bead_id"):
            return (done.stdout or "").strip(), done.returncode
        bead_id = result["bead_id"]
        bead = show(gc, bead_id)
        if bead is None:
            return (done.stdout or "").strip(), done.returncode    # cannot judge it: the session reads it itself
        what = verdict(bead, mine)
        kind = (bead.get("metadata") or {}).get("gc.kind") or "task"
        if what == "work":
            return (done.stdout or "").strip(), done.returncode
        if bead_id in released:
            break                                                 # released, and handed straight back
        if what == "release":
            undone = gc("bd", "update", bead_id, "--assignee", "", "--force")
            if undone.returncode != 0:
                print("claim.py: could not release %s (gc.kind=%s): %s"
                      % (bead_id, kind, (undone.stderr or undone.stdout or "").strip()[-200:]), file=err)
                break
            print("claim.py: released %s (gc.kind=%s, %s) — orchestrator topology, not work; claiming again"
                  % (bead_id, kind, bead.get("status")), file=err)
        else:
            print("claim.py: skipped %s (gc.kind=%s, %s) — not work; claiming again"
                  % (bead_id, kind, bead.get("status")), file=err)
        released.append(bead_id)
    refusal = {"schema_version": "1", "ok": False, "command": "hook", "action": "error",
               "reason": "orchestrator_bead_kept_returning", "bead_ids": released}
    print("claim.py: gc hook --claim kept returning orchestrator beads %s; escalate" % ", ".join(released), file=err)
    return json.dumps(refusal, separators=(",", ":")), 2


def run_gc(*args):
    return subprocess.run(["gc", *args], capture_output=True, text=True)


def main(argv):
    line, code = claim(run_gc, "--drain-ack" in argv, os.environ, sys.stderr)
    if line:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
