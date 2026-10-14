"""Read-only GitHub CI durations, including queueing (updatedAt - createdAt).

The arithmetic is pure and reusable for daily or ISO-week windows [start, end).
Cancelled runs are excluded from duration percentiles. `last` also carries
`n` and `median` for the non-cancelled completed scheduled runs in the window.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import factory_lib as fl


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise fl.Refused("invalid CI timestamp: %r" % value) from exc
    # Accept ISO dates as UTC midnight, as well as timezone-aware timestamps.
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _percentile(values, fraction):
    if not values:
        return None
    return round(sorted(values)[math.ceil(len(values) * fraction) - 1], 1)


def ci_minutes(runs, start, end, workflows):
    """Per workflow: PR n/median/p90/cancelled, main n/median, scheduled last.

Only completed runs created in [start, end) contribute. Failed runs still
measure duration. A cancelled-only PR group has n=0 and null percentiles so
its cancellation count remains visible. Absent groups are None.
"""
    floor, ceiling = _timestamp(start), _timestamp(end)
    result = {}
    for workflow in workflows:
        pr, main, scheduled = [], [], []
        cancelled = 0
        for run in runs:
            if run.get("workflowName") != workflow or run.get("status") != "completed":
                continue
            created = _timestamp(run.get("createdAt"))
            if not floor <= created < ceiling:
                continue
            event = run.get("event")
            is_pr = event == "pull_request"
            is_main = event == "push" and run.get("headBranch") == "main"
            if not (is_pr or is_main or event == "schedule"):
                continue
            if run.get("conclusion") == "cancelled" and is_pr:
                cancelled += 1
                continue
            minutes = (_timestamp(run.get("updatedAt")) - created).total_seconds() / 60
            if minutes < 0:
                raise fl.Refused("CI run updatedAt precedes createdAt")
            if event == "schedule":
                scheduled.append((created, minutes, run))
            elif run.get("conclusion") != "cancelled":
                (pr if is_pr else main).append(minutes)
        last = None
        if scheduled:
            created, minutes, run = max(scheduled, key=lambda entry: entry[0])
            durations = [m for _, m, r in scheduled if r.get("conclusion") != "cancelled"]
            last = {"minutes": round(minutes, 1), "conclusion": run.get("conclusion"),
                    "created_at": created.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "n": len(durations), "median": _percentile(durations, 0.5)}
        result[workflow] = {
            "pr": {"n": len(pr), "median": _percentile(pr, 0.5), "p90": _percentile(pr, 0.9),
                   "cancelled": cancelled} if pr or cancelled else None,
            "main": {"n": len(main), "median": _percentile(main, 0.5)} if main else None,
            "last": last,
        }
    return result


def render(result):
    """One plain report line per configured workflow, in configuration order."""
    lines = []
    for workflow, summary in result.items():
        parts = []
        pr, main, last = summary["pr"], summary["main"], summary["last"]
        if pr is not None:
            if pr["n"]:
                parts.append("PR median %.1f min, p90 %.1f over %d (%d cancelled)" %
                             (pr["median"], pr["p90"], pr["n"], pr["cancelled"]))
            else:
                parts.append("PR none completed (%d cancelled)" % pr["cancelled"])
        if main is not None:
            parts.append("main median %.1f over %d" % (main["median"], main["n"]))
        if last is not None:
            part = "last %.1f min %s on %s" % (last["minutes"], last["conclusion"], last["created_at"][:10])
            if last["n"]:
                part += "; median %.1f over %d" % (last["median"], last["n"])
            parts.append(part)
        lines.append("%s: %s" % (workflow, "; ".join(parts) if parts else "none in the window"))
    return lines


def fetch_runs(slug, since, *, identities=False):
    """Fetch at most 1000 runs once; surface stderr rather than hiding errors."""
    try:
        completed = fl.sh(["gh", "run", "list", "--repo", slug, "--created", ">=" + since,
                           "--limit", "1000", "--json",
                           "workflowName,createdAt,updatedAt,status,conclusion,event,headBranch"
                           + (",databaseId,headSha" if identities else "")])
    except OSError as exc:
        raise fl.Refused(str(exc)) from exc
    if completed.returncode != 0:
        raise fl.Refused(completed.stderr.strip() or "gh run list exited %d" % completed.returncode)
    try:
        runs = json.loads(completed.stdout)
    except ValueError as exc:
        raise fl.Refused("gh run list returned invalid JSON: %s" % exc) from exc
    if not isinstance(runs, list) or any(not isinstance(run, dict) for run in runs):
        raise fl.Refused("gh run list did not return a list of runs")
    return runs
