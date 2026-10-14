#!/usr/bin/env python3
"""The first causal CI failure, as text — no agent, no model, no tokens.

    ci_failure.py --repo <owner/name> --pr <number>
    ci_failure.py --repo <owner/name> --job <job id>

A red pull request reports every job that went red, and most of them are
downstream of one. The aggregate gate a repository makes required is red
BECAUSE a job it needs is red; a deploy that never ran is red because the build
it needed failed; a matrix leg that was cancelled is red because its sibling
failed first. Handing all of that to a model costs tokens to rediscover what the
timestamps already say.

So this module answers one question — which failure happened FIRST, and what did
it print — and leaves the judgement to whoever reads it. Everything here is pure
except the two `gh` calls at the bottom, so the rules can be tested against
recorded payloads with no network and no key.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

BAD = {"failure", "timed_out", "action_required", "startup_failure", "error"}
# Red because something else is red. Named as symptoms, never read for a cause,
# unless nothing else failed — then the aggregate IS all the evidence there is.
AGGREGATE = re.compile(r"(?i)\b(gate|gates|all[ -]?checks?|required|summary|status|aggregate|result|results|"
                       r"conclusion|finalize|report)\b")
# Lines worth centring an excerpt on. The first match wins: a compiler's first
# error is the one that caused the rest, and a test harness prints its summary
# after the failure it is summarising.
SIGNAL = re.compile(r"(?i)(##\[error\]|^error(\[[A-Z0-9]+\])?:|\bpanicked at\b|^\s*FAILED\b|^\s*failures:|"
                    r"\bassertion (`|failed)|^\s*thread '.*' panicked|^E\s+|\bAssertionError\b|"
                    r"^\s*\d+\) )", re.M)
EXCERPT_LINES = 120
EXCERPT_BEFORE = 12


def run_id_of(url):
    """The workflow-run id inside a check's details URL, or ''."""
    found = re.search(r"/actions/runs/(\d+)", url or "")
    return found.group(1) if found else ""


def job_id_of(url):
    found = re.search(r"/job/(\d+)", url or "")
    return found.group(1) if found else ""


def failures(rollup):
    """Every failed check in a pull request's `statusCheckRollup`, normalised.

    A rollup mixes check runs (which carry a workflow, a job and times) with
    commit statuses (which carry a context and a state and nothing else). Both
    can be red; only the first can be ordered in time, so a status with no time
    sorts last rather than pretending to be first."""
    out = []
    for check in rollup or []:
        verdict = (check.get("conclusion") or check.get("state") or "").lower()
        if verdict not in BAD:
            continue
        url = check.get("detailsUrl") or check.get("targetUrl") or ""
        out.append({
            "name": check.get("name") or check.get("context") or "?",
            "workflow": check.get("workflowName") or "",
            "started_at": check.get("startedAt") or "",
            "completed_at": check.get("completedAt") or "",
            "conclusion": verdict,
            "url": url,
            "run_id": run_id_of(url),
            "job_id": job_id_of(url),
        })
    return out


def is_aggregate(failure):
    """A job whose name says it reports on other jobs rather than doing work."""
    return bool(AGGREGATE.search(failure["name"]) or AGGREGATE.search(failure["workflow"]))


def causal(found):
    """(the failure to read, [the symptoms]) — the one that happened first.

    Ordered by when it STARTED, not when it finished: an aggregate gate starts
    last and finishes first, and sorting on completion makes the symptom look
    like the cause. A failure with no start time (a commit status) sorts last;
    it is a cause only when nothing else failed."""
    if not found:
        return None, []
    work = [f for f in found if not is_aggregate(f)] or list(found)
    order = sorted(work, key=lambda f: (f["started_at"] == "", f["started_at"], f["name"]))
    cause = order[0]
    return cause, [f for f in found if f is not cause]


def excerpt(text, limit=EXCERPT_LINES, before=EXCERPT_BEFORE):
    """The part of a job log worth reading, capped at `limit` lines.

    Centred on the FIRST line that looks like a failure, with a little context
    above it, because that is the error that caused the rest. With no such line
    — a job killed for time, a runner that died — the tail is the evidence."""
    lines = (text or "").splitlines()
    if len(lines) <= limit:
        return "\n".join(lines).strip()
    found = SIGNAL.search("\n".join(lines))
    if not found:
        return "\n".join(["… %d earlier lines" % (len(lines) - limit)] + lines[-limit:]).strip()
    at = "\n".join(lines).count("\n", 0, found.start())
    start = max(0, at - before)
    end = min(len(lines), start + limit)
    head = ["… %d earlier lines" % start] if start else []
    tail = ["… %d later lines" % (len(lines) - end)] if end < len(lines) else []
    return "\n".join(head + lines[start:end] + tail).strip()


def summarise(cause, symptoms, log="", event="", url=""):
    """The whole readback, as the text a bead or a mail carries."""
    if not cause:
        return "no failed check"
    lines = ["first causal failure: %s" % cause["name"]]
    if cause["workflow"]:
        lines.append("workflow            %s%s" % (cause["workflow"], " (%s)" % event if event else ""))
    lines.append("conclusion          %s" % cause["conclusion"])
    if cause["started_at"]:
        lines.append("started             %s" % cause["started_at"])
    if cause["url"] or url:
        lines.append("run                 %s" % (url or cause["url"]))
    if symptoms:
        lines.append("also red, after it: %s" % ", ".join(
            "%s%s" % (s["name"], " (aggregate)" if is_aggregate(s) else "") for s in symptoms))
    if log:
        lines += ["", "--- log of the failing step " + "-" * 44, log, "-" * 72]
    return "\n".join(lines)


# --- the two calls that touch the network -----------------------------------

def gh(args, cwd=None, timeout=120):
    try:
        return subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args, 1, "", "gh %s did not finish" % " ".join(args[:3]))


def rollup_of(slug, number, cwd=None):
    out = gh(["pr", "view", str(number), "--repo", slug, "--json", "statusCheckRollup,url"], cwd)
    if out.returncode != 0:
        return [], ""
    doc = json.loads(out.stdout or "{}")
    return doc.get("statusCheckRollup") or [], doc.get("url") or ""


def job_log(slug, job_id, cwd=None):
    """The failing job's log. `gh run view --job` prints the whole job; the
    excerpt is what bounds it, not the fetch, because a job that failed in its
    third step has nothing useful in its last hundred lines otherwise."""
    if not job_id:
        return ""
    out = gh(["run", "view", "--repo", slug, "--job", str(job_id), "--log"], cwd, timeout=180)
    return out.stdout if out.returncode == 0 else ""


def for_pr(slug, number, cwd=None):
    """(summary text, the causal failure, the symptoms) for one pull request."""
    rollup, url = rollup_of(slug, number, cwd)
    cause, symptoms = causal(failures(rollup))
    if not cause:
        return "no failed check on %s" % (url or number), None, []
    return summarise(cause, symptoms, excerpt(job_log(slug, cause["job_id"], cwd)), url=cause["url"]), cause, symptoms


def main(argv):
    ap = argparse.ArgumentParser(prog="ci_failure.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--pr", type=int, help="pull request number")
    ap.add_argument("--job", help="job id, to print one job's excerpt instead")
    ap.add_argument("--cwd", default=None, help="directory to run gh in (a clone with credentials)")
    args = ap.parse_args(argv)
    if args.job:
        print(excerpt(job_log(args.repo, args.job, args.cwd)))
        return 0
    if not args.pr:
        ap.error("give --pr or --job")
    print(for_pr(args.repo, args.pr, args.cwd)[0])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
