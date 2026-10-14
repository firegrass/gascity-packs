#!/usr/bin/env python3
"""One screen for the operator: what the factory is doing and what needs you.

    gc proof status            # or: python3 <city>/packs/proof/assets/scripts/status.py

A script — no agent, no model, no tokens. Read-only.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

import rig_health
import factory_health
import planner_assignments
import watchdog

import factory_lib as fl
from factory_lib import admit


DESIGN_RE = re.compile(r"^conforms:(.+)$")
# Nearest-to-unblocking first: a lapsed agreement needs a look and a paste, a
# named page needs the owner's yes, an unnamed one needs a design drawn.
OWED_ORDER = {"lapsed": 0, "unagreed": 1, "not a current page": 2, "no page named": 3}


def mail_lines(mail, view):
    """The mail section, counted by the watchdog's own rule (`watchdog.human_mail`):
    a mail whose matter is settled is not counted — the watchdog marks it read on its
    next pass — and a NOTE is counted apart, because the worker that sent it is
    proceeding and the daily report digests it."""
    need = view["need"]
    lines = ["", "mail     %d unread, %d need you, %d notes%s" % (
        len(mail) - len(view["settled"]), len(need), len(view["notes"]), ":" if need else "")]
    lines += ["  %-16s %s" % (m["id"], (m.get("subject") or "")[:110]) for m in need[:12]]
    return lines


def designs_owed(city, rigs):
    """Active items that can change what a Console operator sees and have no
    live owner agreement to build to (ADR-0150).

    Read from the Gas City-owned orchestrator clone at origin/main — the same
    tree admission judges against, so this screen and the refusal cannot
    disagree. Returns (rows, reason): a reason instead of rows when the clone or
    the registry cannot be read, because a status screen reports what it could
    not see rather than printing an empty section.
    """
    try:
        clone = admit.backlog_clone_of(rigs)
        if os.path.join(clone, "scripts") not in sys.path:
            sys.path.insert(0, os.path.join(clone, "scripts"))
        import importlib
        dm = importlib.import_module("docs_model")
        catalog = importlib.import_module("build_mockup_catalog")
        agreements = catalog.load_agreements()
        corpus = dm.Corpus.load(os.path.join(clone, "docs"))
    except Exception as exc:  # noqa: BLE001 - one line, never a broken screen
        return [], "cannot read the backlog or the agreements: %s" % exc

    def state_of(page):
        if page not in agreements:
            return "unagreed"
        page_file = os.path.join(clone, "mockups", "liskov-console", "current", page + ".html")
        if not os.path.isfile(page_file):
            return "not a current page"
        try:
            actual = catalog.page_digest(catalog.Path(page_file))
        except Exception:  # noqa: BLE001
            return "unreadable"
        return "live" if actual == str(agreements[page].get("digest") or "").strip() else "lapsed"

    rows = []
    for record in corpus.items:
        if record.status not in dm.ACTIVE_STATUSES:
            continue
        if not dm.scopes_render(dm.write_scopes(record)):
            continue
        value = str(record.fm.get("design_mockup") or "").strip()
        named = DESIGN_RE.match(value) if value and value != "none" else None
        page = named.group(1) if named else ""
        state = state_of(page) if page else "no page named"
        if state == "live":
            continue
        rows.append((OWED_ORDER.get(state, 9), state, page, dm.record_id(record),
                     record.fm.get("claimed_by") or "", str(record.fm.get("title") or "")))
    rows.sort()
    return rows, ""


def runtime_status(packet, beads, sessions, step, now=None):
    """Workflows can outlive their workers; report the current step's runtime."""
    if sessions is None:
        return "unknown", "session state unavailable"
    if step == "controller":
        return "waiting", "workflow control/dependencies; no runnable agent task"
    if step == "finishing":
        return "finishing", "controller steps"
    root = (packet.get("root") or {}).get("id")
    tasks = [b for b in beads if (b.get("metadata") or {}).get("gc.root_bead_id") == root
             and b.get("status") != "closed"
             and ((b.get("metadata") or {}).get("gc.step_ref") or "").endswith(step)]
    holders = {b.get("assignee") for b in tasks if b.get("assignee")}
    matching = [s for s in sessions if holders.intersection(
        {s.get("session_name"), s.get("id"), s.get("name")}) and not s.get("closed")]
    active = [s for s in matching if s.get("state") == "active"]
    if active:
        return "working", active[0].get("name") or active[0].get("id") or "active worker"
    born = min((b.get("created_at") or "" for b in tasks), default="")
    born = born or (packet.get("root") or {}).get("created_at") or ""
    try:
        started = datetime.fromisoformat(born.replace("Z", "+00:00"))
        started = started.replace(tzinfo=started.tzinfo or timezone.utc)
        age = ((now or datetime.now(timezone.utc)) - started).total_seconds()
    except ValueError:
        age = 0
    why = ", ".join(s.get("state") or "unknown" for s in matching) or "no assigned worker"
    return ("STALLED" if age >= 120 else "queued"), "%s; step created %s" % (why, born or "unknown")


def retained_line(packet):
    """Under a packet row: the tag admission archived its dirty retained worktree to."""
    tag = packet["md"].get("proof.retained_archive")
    return "  %-9s %s" % ("retained", tag) if tag else None


def print_retained(packet):
    line = retained_line(packet)
    if line:
        print(line)


def live_slot_line(city, now=None):
    """Who owns live testing in this city, since when, and where their run got
    to. Read-only: a look creates no lock file. A slot that cannot be read, or
    whose holder's journal cannot, is said to need a person — it is never
    reported free, and its age is never a reason to take it."""
    import livetest_state as ls
    store = ls.RunStore(city)
    if not os.path.exists(store.slot_path):
        return "free — no live test has held it"
    try:
        slot = store.slot()
    except ls.Refused as why:
        return "NEEDS INTERVENTION — the slot record cannot be read (%s: %s)" % (why.code, why.detail)
    if not slot.held and not slot.needs_intervention:
        return "free%s" % (" — last released %s" % slot.released_at if slot.released_at else "")
    try:
        acquired = datetime.strptime(slot.acquired_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age = "%dm" % (((now or datetime.now(timezone.utc)) - acquired).total_seconds() // 60)
    except ValueError:
        age = "unknown age"
    head = "held by %s, run %s, since %s (%s)" % (slot.item_id or "?", slot.run_id, slot.acquired_at or "?", age)
    if slot.needs_intervention:
        return "NEEDS INTERVENTION — %s: %s" % (head, slot.detail)
    try:
        run = store.status(slot.run_id)
    except ls.Refused as why:
        return "NEEDS INTERVENTION — %s: %s: %s" % (head, why.code, why.detail)
    return "%s%s — lifecycle %s, %d of %d reads used%s" % (
        "NEEDS INTERVENTION — " if run.needs_intervention else "", head, run.lifecycle, run.attempts_used,
        run.attempts_used + run.attempts_remaining, ": " + run.detail if run.detail else "")


def main(argv):
    city = fl.city_dir()
    print("factory  " + factory_health.summary(city), flush=True)
    print("planning " + factory_health.summary(city, "planner-trigger"), flush=True)
    skip = factory_health.read(city, "factory-skip")
    if skip:
        print("         last lock skip " + skip.get("updated", "unknown"), flush=True)
    from pathlib import Path
    for path in sorted((Path(city) / "proof/pr-recovery").glob("*.json")):
        record = json.loads(path.read_text())
        print("recovery %s %s attempt=%s owner=%s — %s" % (
            record["key"], record["state"], record["attempts"],
            record.get("root", "controller"), record.get("detail", "")), flush=True)
    policy = fl.load_policy(city)
    rigs = admit.load_rigs(city)
    states = fl.rig_states(city)
    observed = admit.gc(city, "session", "list", "--json", check=False)
    try:
        sessions = json.loads(observed.stdout) if observed.returncode == 0 else None
        if isinstance(sessions, dict):
            sessions = sessions.get("sessions")
        if not isinstance(sessions, list):
            sessions = None
    except ValueError:
        sessions = None
    print("policy   %s%s" % ("ENABLED" if policy["enabled"] else "disabled — nothing dispatches",
                             ", max_running %d" % policy["max_running"] if policy["max_running"] else ""))
    print("alerts   %s; CI triage %s; red-main response %s; output cap %s" % (
        "configured" if policy.get("notify_command") else "no push channel",
        "on" if (policy.get("ci_triage") or {}).get("enabled") else "off",
        "on" if (policy.get("red_main") or {}).get("enabled") else "off",
        str(policy["daily_output_tokens"]) if policy["daily_output_tokens"] else "uncapped"))
    triage, completed = [], set()
    for rig in sorted(rigs):
        health = rig_health.problem(rigs[rig].get("Path") or "")
        cfg = policy["rigs"].get(rig)
        known = fl.packets(city, rig)
        completed |= watchdog.completed(known)
        count = lambda state: sum(1 for p in known if p["state"] == state and p["bead"].get("status") != "closed" or
                                  (state == "running" and p["state"] == "running"))
        waiting = [p for p in known if p["state"] == "passed" and p["md"].get("proof.landing") in ("pr", "automerge")
                   and not p["md"].get("proof.completed")]
        print("\n%-28s %s, %s" % (rig, "SUSPENDED" if states.get(rig, {}).get("suspended") else "live",
                                  "fed by the policy (wip %d, landing %s)" % (cfg["wip"], cfg["landing"]) if cfg else "not in the policy"))
        if health:
            receipt = rig_health.receipt(city, rigs[rig].get("Path") or "")
            print("  UNHEALTHY %s%s" % (health, " since " + receipt["since"] if receipt.get("since") else ""))
        if cfg and cfg["livetest"]["enabled"]:
            live = cfg["livetest"]
            print("  live tests  on — targets %s, recipes %s, at most %s"
                  % (", ".join(live["targets"]) or "NONE (empty admits none)",
                     ", ".join(live["recipes"]) or "NONE (empty admits none)", live["max_exercise"]))
        for p in known:
            if p['bead'].get('status') != 'closed' and not p['md'].get('proof.completed') and p['md'].get('proof.recovery_state'):
                print('  recovery  %s  %s  task %s' % (p['id'], p['md']['proof.recovery_state'],
                      p['md'].get('proof.recovery_task') or '?'))
            if p["state"] == "running":
                if p.get("admission_pending"):
                    detail = ("the dispatcher admits it again on its next pass" if fl.admission_retryable(p)
                              else "live test is not retried; inspect run %s and the live slot" % p["md"]["proof.live_run_id"])
                    print("  admitted  %s  admitted, no workflow — %s" % (p["id"], detail))
                    print_retained(p)
                    continue
                if p.get("record_pending") and fl.admission_retryable(p):
                    print("  admitted  %s  workflow %s has no admission record — the dispatcher writes it on its next pass"
                          % (p["id"], p["root"]["id"]))
                    print_retained(p)
                    continue
                step = (fl.current_step(city, rig, p, fl.LIVE_STEP_ORDER, "mol-proof-livetest")
                        if p["md"].get("proof.live_run_id") else fl.current_step(city, rig, p))
                label, detail = runtime_status(p, fl.rig_beads(city, rig), sessions, step)
                if health:
                    label, detail = "STALLED", health
                print("  %-9s %s  %s  %s" % (label, p["id"], step, detail))
                print_retained(p)
        for p in waiting:
            print("  awaiting  %s  %s  %s" % (p["id"], fl.recorded_pr_url(city, rig, p) or
                                              "no pull request recorded yet (the completer looks it up by branch %s)"
                                              % p["md"].get("proof.branch", "?"),
                                              p["md"].get("proof.pr_notified") or "waiting for merge and a green main"))
            print_retained(p)
        for p in known:
            if p["state"] in ("declined", "failed") and p["bead"].get("status") != "closed":
                triage.append((rig, p))
        done = sum(1 for p in known if p["md"].get("proof.completed") not in (None, "", "not-ours")
                   or (p["root"].get("metadata") or {}).get("proof.landed_commit"))
        print("  totals    %d delivered, %d awaiting merge, %d awaiting triage" % (done, len(waiting), sum(1 for r, _ in triage if r == rig)))

    print("\nlive slot " + live_slot_line(city))

    # Planning runs live in the rig that owns the backlog and are not packets.
    try:
        backlog = admit.backlog_clone_of(rigs)
        home = next(r for r in rigs if os.path.realpath(rigs[r].get("Path") or "") == os.path.realpath(backlog))
        runs = fl.planning_runs(city, home)
    except (fl.Refused, StopIteration):
        home, runs = "", []
    try:
        mode = ("bounded — released stops and named requests, at most 3 items/run"
                if planner_assignments.enabled(policy) else "OFF — dispatch remains independent")
    except fl.Refused as exc:
        mode = "INVALID: " + str(exc)
    print("\nplanner  " + mode)
    latest = {}
    for run in runs:
        latest[run["target"]] = run
    for target, run in sorted(latest.items()):
        md, state = run["md"], run["state"]
        if md.get("proof.plan_assignments"):
            try:
                rows = md["proof.plan_assignments"]
                rows = json.loads(rows) if isinstance(rows, str) else rows
                print("  assigned  %s" % ", ".join(row["id"] + " (" + row["kind"] + ")" for row in rows))
            except (ValueError, TypeError, KeyError):
                print("  assigned  INVALID receipt — inspect this planning bead")

        if state == "running":
            print("  running   %-22s %s" % (target, fl.current_step(city, home, {"root": run["root"]}, fl.PLAN_STEP_ORDER, "mol-proof-plan")))
        elif state == "stopped":
            print("  STOPPED   %-22s %s" % (target, fl.stop_reason(city, home, {"root": run["root"]}) or "its workflow never started"))
            print("            no further planning run for %s until you have read it and closed the bead:" % target)
            print("            gc bd --rig %s show %s    then    gc bd --rig %s close %s --reason \"read; plan again\""
                  % (home, run["bead"]["id"], home, run["bead"]["id"]))
        elif state == "cleared":
            print("  cleared   %-22s a stopped run, read and closed by a person %s; the planner may run again"
                  % (target, (run["bead"].get("closed_at") or "")[:16]))
        else:
            print("  %-9s %-22s %s  %s  %s output tokens" % (state, target, (run["bead"].get("closed_at") or "")[:16],
                                                          (md.get("proof.commit") or "")[:10], md.get("proof.tokens_output", "?")))

    if triage:
        print("\nawaiting triage — a person or the planner re-shapes the item; changed on main, it is taken again")
        for rig, p in triage:
            fl.stop_reason(city, rig, p)           # fills the bead cache
            print("  %-9s %s  %s%s" % (p["state"], p["id"], fl.stop_reason(city, rig, p) or "no reason recorded",
                                       "" if p["md"].get("proof.released") else "  [claim not yet released]"))
            print_retained(p)
        print("  the worker's reasoning: gc mail inbox | grep BLOCKED     then: gc mail read <id>")

    owed, why = designs_owed(city, rigs)
    if why:
        print("\ndesigns owed — %s" % why)
    elif owed:
        print("\ndesigns owed — a Console item is not delivered until the owner agrees the mockup it "
              "builds to (ADR-0150)")
        for _order, state, page, item_id, claimed_by, title in owed:
            print("  %-13s %-26s %s  %s%s" % (state, page or "—", item_id, title[:46],
                                              "  [held by %s]" % claimed_by if claimed_by else ""))
        print("  agree one, in the orchestrator: python3 scripts/build_mockup_catalog.py --digest <page>")

    mail = watchdog.inbox(city)
    for line in mail_lines(mail, watchdog.human_mail(city, rigs, mail, completed)):
        print(line)
    # What a pass keeps finding and no longer repeats in the log: an item it
    # cannot take and why, a rig with free slots and nothing to take, a planner
    # that is backing off. The counts and verdict lines are in the sections above.
    standing = [row for row in fl.standing(city) if not row[1].startswith(("counts:", "verdict:"))]
    if standing:
        print("\nstanding — true on every pass since the time shown; logged once, not every five minutes")
        for _who, _key, message, since in standing:
            print("  %s  %s" % (since[5:16].replace("T", " "), message[:200]))
    log = os.path.join(city, "proof", "factory.log")
    if os.path.isfile(log):
        print("\nlast events")
        for line in open(log, encoding="utf-8", errors="replace").read().splitlines()[-6:]:
            print("  " + line[:150])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
