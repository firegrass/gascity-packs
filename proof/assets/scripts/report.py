#!/usr/bin/env python3
"""PROOF daily report: what the factory delivered and what it cost, mailed to a
human. A script — no agent, no model, no tokens.

    report.py [--days N] [--print]      # default: the last 1 day, mailed

Per rig, over packets admitted in the window: how many ran, passed, stopped and
completed; output tokens per packet (as the keeper recorded them, measured now
for a packet still running); CI pass rate — of the pull requests that finished,
the share that merged with main green; reverts — commits on main in the
window whose subject starts "Revert"; and how many packets changed a path
outside their declared write_scope (ADR-0159), as the diff gate recorded it in
`proof.scope_expansion` on the workflow root. `--print` lists those paths.
Gate steps closed in the window supply verdicts, non-reused timing percentiles,
reuse counts and total gate hours; `--print` lists gates strictly above p90.

Then `notes since last report`: every unread `NOTE:` mail to `human` created in
the window, per rig — a worker sends one while PROCEEDING, so it is never
pushed or counted as needing a person (BKLG-20260924-fuyh). Once the report is
mailed those NOTEs are marked read; `--print` leaves them unread.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path
from statistics import median

import factory_lib as fl
from factory_lib import admit
import ci_minutes
import tokens
import watchdog
import recovery_metrics


def main(argv):
    days = int(argv[argv.index("--days") + 1]) if "--days" in argv else 1
    city = fl.city_dir()
    rigs = admit.load_rigs(city)
    try:
        backlog = admit.backlog_clone_of(rigs)
    except fl.Refused:
        backlog = None
    policy = fl.load_policy(city)
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    floor = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    ceiling = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = ["PROOF factory report — %d day(s) to %s UTC" % (days, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")), ""]
    for rig in sorted(rigs):
        try:
            clone = admit.owned_clone(rigs, rig)
        except fl.Refused:
            continue
        # Superseded beads too: a packet that was declined and then triaged is closed
        # and marked superseded, and leaving those out printed "0 failed, 0 declined"
        # over a window in which 9 of 34 packets had stopped (2026-09-20).
        beads = fl.rig_beads(city, rig)
        all_packets = fl.packets_of(beads, superseded=True)
        newest = newest_gates(beads)
        promises = {p["id"]: item_promise(backlog, p["id"]) for p in all_packets}
        scorecards = scorecard_ledger(all_packets, newest, floor, promises)
        gates = gate_ledger(beads, floor)
        if "--print" not in argv:
            gates = gates[:1]
        usage = tokens.usage_by_admission(clone, [p["bead"] for p in all_packets])
        window = [p for p in all_packets if (p["bead"].get("created_at") or "") >= floor]
        if not window:
            lines.append("%s: no packets admitted in the window" % rig)
            lines += gates + scorecards + ci_section(clone, policy.get("rigs", {}).get(rig, {}), floor, ceiling) + [""]
            continue
        rows, spent = [], []
        for p in window:
            md = p["md"]
            # Each admission owns a disjoint interval, including retired attempts.
            out = usage.get(p["bead"]["id"], {}).get("output_tokens", 0)
            spent.append(out)
            landed = ("done" if md.get("proof.completed") or (p["root"].get("metadata") or {}).get("proof.landed_commit")
                      else (md.get("proof.pr_notified") or ""))
            grew = expansion(p)
            measured = packet_scorecard(p, newest)
            delta = delta_text(measured, nonzero=True)
            rows.append("  %-20s %-8s %-9s %9d out  %s%s" % (p["id"], p["state"], "triaged" if p.get("superseded") else landed or "-",
                                                            out, md.get("proof.pr_url", ""),
                                                            ("  +%d out of scope" % len(grew) if grew else "")
                                                            + ("  scorecard: " + delta if delta else "")))
            if "--print" in argv:
                rows += ["      %s" % path for path in grew]
        passed = [p for p in window if p["state"] == "passed"]
        finished = [p for p in passed if p["md"].get("proof.completed") or p["md"].get("proof.pr_notified")]
        green = [p for p in finished if p["md"].get("proof.completed") not in (None, "", "not-ours")]
        grown = sum(1 for p in window if expansion(p))
        fl.sh(["git", "-C", clone, "fetch", "--quiet", "origin"])
        reverts = [s for s in fl.sh(["git", "-C", clone, "log", "origin/main", "--since=" + floor, "--format=%h %s"]
                                    ).stdout.splitlines() if s.split(" ", 1)[-1].startswith("Revert")]
        lines += ["%s" % rig,
                  "  packets        %d admitted: %d passed, %d failed, %d declined at load-context, %d running — %.1f per day"
                  % (len(window), len(passed), sum(p["state"] == "failed" for p in window),
                     sum(p["state"] == "declined" for p in window),
                     sum(p["state"] == "running" for p in window), len(window) / days),
                  "  scope          expanded on %d packet%s" % (grown, "" if grown == 1 else "s"),
                  "  completed      %d marked done on main" % len(green),
                  "  tokens/packet  " + usage_summary(window, usage),
                  "  stopped output %s (recorded usage only; see coverage below)" % stopped_cost(window, spent),
                  "  retained work  %s" % retained_work(window, all_packets),
                  "  CI pass rate   %s" % ("%d of %d finished pull requests merged with main green"
                                           % (len(green), len(finished)) if finished else "no pull request has finished yet"),
                  "  reverts        %d on main%s" % (len(reverts), "".join("\n    " + r for r in reverts))]
        lines += (gates + scorecards + ci_section(clone, policy.get("rigs", {}).get(rig, {}), floor, ceiling)
                  + recovery_metrics.lines(window, beads, usage) + [""] + rows + [""])
    try:
        backlog = admit.backlog_clone_of(rigs)
        home = next(r for r in rigs if os.path.realpath(rigs[r].get("Path") or "") == os.path.realpath(backlog))
        runs = [r for r in fl.planning_runs(city, home) if (r["bead"].get("created_at") or "") >= floor]
    except (fl.Refused, StopIteration):
        runs = []
    if runs:
        spent = [int(r["md"].get("proof.tokens_output") or 0) for r in runs]
        lines += ["planner", "  runs           %d: %s" % (len(runs), ", ".join(
            "%d %s" % (sum(1 for r in runs if r["state"] == st), st) for st in ("landed", "no-op", "stopped", "cleared", "running")
            if any(r["state"] == st for r in runs))),
            "  tokens/run     %d output on average (%d total)" % (sum(spent) / len(spent), sum(spent)), ""]
    digested = [m for m in watchdog.notes(watchdog.inbox(city)) if (m.get("created_at") or "") >= floor]
    lines += notes_section(digested, lambda m: watchdog.mail_body(city, m["id"]))
    text = "\n".join(lines)
    print(text)
    if "--print" not in argv and fl.mail("PROOF factory report", text):
        for m in digested:
            admit.gc(city, "mail", "mark-read", m["id"], check=False)
    return 0


def gate_steps(beads):
    """Actual gate records, excluding metadata propagated to enclosing scopes."""
    return [b for b in beads if "proof.gate_result" in (b.get("metadata") or {})
            and (b["metadata"].get("gc.step_ref") or "").endswith((".gate", ".review-gate"))]


def gate_ledger(beads, floor):
    """Summary and gates strictly above p90, from gate steps closed in the window.

    Verdicts come from proof.gate_result, not gc.outcome. Reused checks count
    towards runs and total hours, but never the built-run timing percentiles.
    The caller includes the detail lines only for --print.
    """
    gates = [b for b in gate_steps(beads)
             if (b.get("closed_at") or b.get("created_at") or "") >= floor]
    if not gates:
        return ["  gates          none in the window"]
    runs = [(b, int(b["metadata"]["proof.gate_seconds"]),
             fl.flag(b["metadata"].get("proof.gate_reused"))) for b in gates]
    built = sorted(seconds for _, seconds, reused in runs if not reused)
    p90 = built[ceil(0.9 * len(built)) - 1] if built else None
    timing = ("median %gs, p90 %gs" % (median(built), p90) if built
              else "median n/a, p90 n/a")
    lanes = {}
    for bead, seconds, reused in runs:
        lane = bead["metadata"].get("proof.gate_lane")
        if not reused and lane:
            lanes.setdefault(lane, []).append(seconds)
    split = (" (" + ", ".join("%s %d: %gs" % (lane, len(values), median(values))
                              for lane, values in sorted(lanes.items())) + ")" if lanes else "")
    verdicts = [b["metadata"]["proof.gate_result"] for b in gates]
    lines = ["  gates          %d ran: %d passed, %d failed, %d error; %s over %d built%s; %d reused; %.1f gate hours"
             % (len(runs), verdicts.count("pass"), verdicts.count("fail"), verdicts.count("error"),
                timing, len(built), split, sum(reused for _, _, reused in runs),
                sum(seconds for _, seconds, _ in runs) / 3600)]
    roots = {b["id"]: b.get("metadata") or {} for b in beads}
    for bead, seconds, _ in sorted(runs, key=lambda run: run[1], reverse=True):
        if p90 is None or seconds <= p90:
            continue
        md = bead["metadata"]
        root = roots.get(md.get("gc.root_bead_id"), {})
        packet = root.get("gc.var.packet_id") or md.get("proof.packet_id") or md.get("gc.root_bead_id") or bead["id"]
        step = "review-gate" if (md.get("gc.step_ref") or "").endswith(".review-gate") else "gate"
        lines.append("    %s %s %ds %s" % (packet, step, seconds, md["proof.gate_result"]))
    return lines


SCORECARD_KEYS = ("slipway_src_lines", "slipway_test_targets", "workspace_test_targets")


def newest_gates(beads):
    """Latest gate by completion time; reuse resolves to a build of the same tree.

    Keep the build's completion time so later reuse cannot count its delta in
    another day's net. Missing tree evidence never borrows a measurement.
    """
    found, built = {}, {}
    for bead in sorted(gate_steps(beads), key=lambda b: (b.get("closed_at") or "", b["id"])):
        md = bead.get("metadata") or {}
        root = md.get("gc.root_bead_id")
        if not root or not bead.get("closed_at"):
            continue
        tree = md.get("proof.gate_tree")
        if fl.flag(md.get("proof.gate_reused")):
            found[root] = built.get((root, tree), bead) if tree else bead
        else:
            found[root] = bead
            if tree:
                built[root, tree] = bead
    return found


def scorecard_deltas(value):
    rows = {}
    for row in (value or "").split(";"):
        match = re.fullmatch(r"\s*(\w+)\s+(\d+)\s+->\s+(\d+)\s+\(([+-]?\d+)\)\s*", row)
        if match and match[1] in SCORECARD_KEYS:
            rows[match[1]] = int(match[4])
    return rows


def packet_scorecard(packet, newest):
    bead = newest.get(packet["root"].get("id"), {})
    md = bead.get("metadata") or {}
    return {} if fl.flag(md.get("proof.gate_reused")) else scorecard_deltas(md.get("proof.scorecard"))


def item_promise(backlog, packet_id):
    """First body Scorecard line; None is absent, {} is an explicit 'none'."""
    if backlog is None:
        return None
    files = sorted((Path(backlog) / "docs/backlog/items").glob(packet_id + "-*.md"))
    if not files:
        return None
    match = re.search(r"^Scorecard:[ \t]*(.+)$", files[0].read_text(), re.MULTILINE)
    if not match:
        return None
    if re.match(r"none(?:[.\s]|$)", match[1]):
        return {}
    promise = {}
    for row in match[1].split(","):
        part = re.fullmatch(r"\s*(\w+)\s+([+-]?\d+)\s*", row)
        if part is None or part[1] not in SCORECARD_KEYS:
            return None
        promise[part[1]] = int(part[2])
    return promise


def delta_text(rows, nonzero=False):
    return ", ".join("%s %+d" % (key, rows[key]) for key in SCORECARD_KEYS
                     if key in rows and (not nonzero or rows[key] != 0))


def scorecard_ledger(packets, newest, floor, promises):
    """Day's net from passed packets' resolved, built, passed gate in the window.

    A workflow is counted once even when it ran several implement/review gates.
    Details also include admissions without a measurement and older admissions
    gated in this window. Missing and explicit-zero promises remain distinct.
    """
    net, counted, details = {}, set(), []
    for packet in packets:
        root = packet["root"].get("id")
        bead = newest.get(root, {})
        in_window = (bead.get("closed_at") or "") >= floor
        if not in_window and (packet["bead"].get("created_at") or "") < floor:
            continue
        measured = packet_scorecard(packet, newest)
        md = bead.get("metadata") or {}
        if (packet["state"] == "passed" and in_window and measured
                and md.get("proof.gate_result") == "pass" and root not in counted):
            counted.add(root)
            for key, value in measured.items():
                net[key] = net.get(key, 0) + value
        promise = promises.get(packet["id"])
        if promise is not None or any(measured.values()):
            details.append("    %s  %s · %s" % (
                packet["id"], "no promise" if promise is None else "promised " + (delta_text(promise) or "none"),
                "measured " + delta_text(measured) if measured else "not measured"))
    return ["  scorecard      net %s over %d passed" % (delta_text(net) or "none", len(counted))] + details


def ci_section(clone, config, start, end):
    workflows = config.get("ci_minutes_workflows")
    if workflows is None or workflows == []:
        return []
    try:
        runs = ci_minutes.fetch_runs(fl.repo_slug(clone), start)
        rendered = ci_minutes.render(ci_minutes.ci_minutes(runs, start, end, workflows))
    except fl.Refused as exc:
        rendered = ["unavailable: " + " ".join(str(exc).splitlines())]
    return ["  CI minutes     " + line for line in rendered]


def notes_section(notes, body_of):
    """`HH:MM  <packet>  <first line of the body>` per NOTE, grouped by the rig
    that sent it (the sender is `<rig>/<agent>`). The packet is the first BKLG id
    in the subject or the body, else the subject's own short code."""
    if not notes:
        return ["notes since last report: none"]
    by_rig = {}
    for m in sorted(notes, key=lambda m: m.get("created_at") or ""):
        body = body_of(m) or ""
        subject = m.get("subject") or ""
        packet = (watchdog.PACKET_ID.findall(subject) or watchdog.PACKET_ID.findall(body)
                  or [subject[len("NOTE:"):].split()[0] if subject[len("NOTE:"):].split() else "-"])[0]
        first = next((line.strip() for line in body.splitlines() if line.strip()), subject)
        by_rig.setdefault((m.get("from") or "?").split("/")[0], []).append(
            "    %s  %-20s %s" % ((m.get("created_at") or "")[11:16] or "--:--", packet, first[:100]))
    lines = ["notes since last report (%d)" % len(notes)]
    for rig in sorted(by_rig):
        lines += ["  " + rig] + by_rig[rig]
    return lines + [""]


def expansion(p):
    """The paths a packet changed outside its declared write_scope, `repo:path`
    each, as the diff gate recorded them on its root; [] when none or unrecorded."""
    value = ((p.get("root") or {}).get("metadata") or {}).get("proof.scope_expansion") or ""
    return [line for line in value.splitlines() if line.strip()]


def usage_summary(window, usage):
    measured = [usage[p['bead']['id']] for p in window
                if usage.get(p['bead']['id'], {}).get('coverage') in ('observed', 'recorded')]
    if not measured:
        return 'unavailable — no observed or recorded usage'
    total = sum(u.get('output_tokens', 0) for u in measured)
    return '%d output average over %d/%d measured admissions (%d recorded total)' % (
        total / len(measured), len(measured), len(window), total)


def retained_work(window, all_packets):
    stopped = {p["id"] for p in window if p["state"] in ("failed", "declined")}
    completed = {p["id"] for p in all_packets if p["md"].get("proof.completed") not in (None, "", "not-ours")}
    paths = {p["md"].get("work_dir") for p in window if p["id"] in stopped and p["md"].get("work_dir")}
    kept = sum(os.path.isdir(path) for path in paths)
    return "%d stopped packet(s) later completed; %d worktree(s) retained for review (stopped output is not all lost)" % (
        len(stopped & completed), kept)


def stopped_cost(window, spent):
    """What the packets that did NOT pass cost, as a share of the window."""
    lost = sum(out for p, out in zip(window, spent) if p["state"] in ("failed", "declined"))
    n = sum(1 for p in window if p["state"] in ("failed", "declined"))
    if not n:
        return "nothing stopped"
    return "%d output on %d stopped packet%s — %d%% of the window's tokens" % (
        lost, n, "" if n == 1 else "s", round(100 * lost / sum(spent)) if sum(spent) else 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
