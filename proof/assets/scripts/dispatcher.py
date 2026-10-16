#!/usr/bin/env python3
"""PROOF dispatcher: fill each rig's free worker slots from the delivery queue,
under the operator's policy file. A script — no agent, no model, no tokens.

    dispatcher.py [--dry-run]

One pass, per rig the policy names:

  1. stop unless the policy is enabled, the rig is not suspended, the failure
     breaker and the unmerged cap have room, and the day's token budget is left;
  2. admit anything already claimed for the rig's seat that has no bead yet —
     a claim a crashed pass left behind, or one a person made;
     — and treat a DECLINED packet whose item has been released and TRIAGED on
     main (re-shaped, naming the stop code it was declined with) as new work,
     superseding its old bead; one declined for want of an independent reviewer
     waits until its frontmatter or scope draws one;
  3. take the first ready items the policy allows, in delivery-queue order — and
     that ADMISSION WOULD ACCEPT: `admit.preflight` is asked before the claim, so
     an item admission refuses is never claimed (it used to be claimed on main,
     refused, and left `working` with no worker for the whole claim) — claim
     them with the workspace's own tool, and land the whole batch on main as ONE
     commit from the Gas City-owned orchestrator clone;
  4. admit each claimed item with the rig's landing level, and RELEASE, in the
     same pass, the claim of any item admission still refuses.

A condition that stands from pass to pass — an overlap, a skip, a refusal — is
logged when it changes, not every five minutes (`factory_lib.Standing`), and
`gc proof status` lists what is standing and since when.

What it may dispatch is what admission accepts — ready, not a gate, a declared
write_scope, one repository — narrowed by the policy. Readiness, order and the
claim itself stay `next_work.py`'s: nothing here reimplements them. `gc rig
suspend <rig>` or `enabled = false` stops it within one pass; running packets
finish either way.

A LIVE TEST (an item with `live_target`, ADR-0151) is admitted at the exercise
its item declares, under the rig's `[rigs.<rig>.livetest]` table, through the
same admission refusals — never as an implementation packet. At most one runs
across the whole city: it needs the one live slot (`livetest_state`), which it
holds for its lifetime, and it takes neither a rig's `wip` nor `max_running`.
A live item that finds the slot held WAITS, unclaimed, and the pass says who
holds it; implementation packets are dispatched beside it exactly as before —
a live slot that stalled the factory would be worse than no live testing.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import factory_lib as fl
import livetest_contract as lt
import test_companions
import work_authority as wa
from factory_lib import admit

ME = "dispatcher"


def admissible(city, policy_rig, repo, frontier, branches, known, said=None, backlog=None, rig="this rig", evidence_context=None):
    """Ready items this rig may take, in queue order.

    With `backlog`, an item admission would refuse is left out as well — asked
    here, before anything is claimed — and a live test is marked with the
    exercise it would be admitted at (`liveExercise`). `said` is the pass's
    standing log; without one (a count, a test) the skips are silent."""
    def say(key, message):
        if said is not None:
            said.say(key, message)

    for item in frontier.get("ready", []):
        pid, scopes = item["id"], item.get("writeScope") or []
        if item.get("gate") or not scopes or item.get("claim") != "none" or item.get("status") != "open":
            continue
        if {s.split(":", 1)[0] if ":" in s else admit.BACKLOG_REPO for s in scopes} != {repo}:
            continue
        if pid in known or pid in policy_rig["exclude"]:
            continue
        if policy_rig["items"] and pid not in policy_rig["items"]:
            continue
        if policy_rig["epics"] and item.get("parentEpic") not in policy_rig["epics"]:
            continue
        # The queue is already in milestone order, but the dispatcher takes the first
        # ADMISSIBLE items, not the first ready ones: when shaping lags, that is a
        # `later` item delivered while launch work waits unshaped.
        if policy_rig["milestones"] and item.get("milestone") not in policy_rig["milestones"]:
            continue
        if policy_rig["lanes"] and item.get("lane") not in policy_rig["lanes"]:
            continue
        if policy_rig["priorities"] and item.get("priority") not in policy_rig["priorities"]:
            continue
        code = pid.rsplit("-", 1)[1]
        # An `open` item can still have someone's unfinished branch: never start a
        # second implementation beside it.
        carrier = next((b for b in branches if re.search(r"(^|[-/_])%s([-/_]|$)" % re.escape(code), b)), None)
        if carrier:
            say("branch:" + pid, "skip %s: remote branch %s already carries its code — finish or delete the "
                                 "branch, or the item is never dispatched" % (pid, carrier))
            continue
        if backlog:
            why = (refusal(backlog, pid, repo, dict(evidence_context, review=wants_review(policy_rig, item)))
                   if evidence_context is not None else refusal(backlog, pid, repo))
            if not why:
                item["liveExercise"], why = live_check(backlog, pid, policy_rig, rig)
            if why:
                say("preflight:" + pid, "skip %s: admission would refuse it, so it is not claimed — %s"
                    % (pid, " ".join(why.split())[:400]))
                continue
            why = review_hold(backlog, policy_rig, item)
            if why:
                say("review:" + pid, "skip %s: %s" % (pid, why))
                continue
        yield item


def refusal(backlog, pid, repo, evidence_context=None):
    """Why admission would refuse this item before any claim exists, or ''."""
    try:
        _path, fm, body, dm = admit.load_item(backlog, pid)
        admit.preflight(pid, fm, dm, repo, backlog)
        admit.check_shaping_evidence(pid, fm, body, dm, backlog, **(evidence_context or {}))
    except fl.Refused as why:
        return str(why)
    return ""


def live_check(backlog, pid, policy_rig, rig="this rig"):
    """(exercise, why admission would refuse it) for a live test; ('', '') for
    an implementation item. The exercise is the one the item declares: the
    dispatcher never admits a live test below what its acceptance needs, and
    the rig's `max_exercise` refuses one above what the operator allows."""
    try:
        _path, fm, _body, dm = admit.load_item(backlog, pid)
    except fl.Refused:
        return "", ""
    if not lt.is_live_test(fm):
        return "", ""
    level = lt.exercise(fm) or admit.DEFAULT_EXERCISE
    try:
        scopes = dm.write_scopes(type("Rec", (), {"fm": fm})())
        admit.check_live(pid, fm, scopes, fl.livetest_policy(policy_rig, rig), level, rig=rig)
    except fl.Refused as why:
        return level, str(why)
    return level, ""


def is_live(packet):
    """A live-test packet, which holds the live slot rather than a worker slot."""
    return bool(packet["md"].get("proof.live_run_id"))


def overlapping(backlog, frontier, item, extra_scopes=(), planned=()):
    """The live claim whose write scope this item's overlaps, or ''.

    `next_work.py --claim` is the authority and refuses such a claim; this is the
    same rule asked without claiming, for the planner's trigger, which has to
    know how many items could be TAKEN now, not how many are shaped."""
    try:
        _path, fm, _body, dm = admit.load_item(backlog, item["id"])
    except fl.Refused:
        return ""
    rec = type("Rec", (), {"fm": fm})()
    mine = dm.write_scopes(rec) + list(extra_scopes)
    for other in frontier.get("claimed", []):
        if other.get("id") == item["id"]:
            continue
        try:
            _p, ofm, _b, _dm = admit.load_item(backlog, other["id"])
        except fl.Refused:
            continue
        theirs = dm.write_scopes(type("Rec", (), {"fm": ofm})())
        if mine and theirs and dm.claims_collide(mine, theirs):
            return other["id"]
    for other in planned:
        theirs = dm.write_scopes(type('Record', (), {'fm': {'write_scope': other['writeScope']}})())
        if dm.claims_collide(mine, theirs):
            return other['id']
    return ""


def shape_digest(backlog, rel, item_id=None):
    """What the item ASKS, hashed — the file without its claim and status lines.
    Under the Beads authority it is the ledger row's semantic digest, which
    excludes exactly those lines."""
    if wa.beads(backlog):
        row = wa.row(backlog, item_id) if item_id else None
        return str((row or {}).get("semantic_digest") or "")
    text = fl.sh(["git", "-C", backlog, "show", "HEAD:" + rel])
    return hashlib.sha1(fl.shaped(text.stdout).encode("utf-8")).hexdigest() if text.returncode == 0 and rel else ""


# An admission that failed on infrastructure (admit's UNAVAILABLE_EXIT) is held
# this long, not until the item changes. Bounded rather than retried: nothing
# before the claim sees Dolt, so every pass of an outage would otherwise claim
# and release on main. Fixed here, not a policy key (BKLG-20260929-69gp).
OUTAGE_HOLD = timedelta(hours=1)
STAMP = "%Y-%m-%dT%H:%M:%SZ"


def now():
    return datetime.now(timezone.utc)


def hold_entry(why, shape, outcome, at):
    """The `refused_after_claim` entry for an admission refused after a claim.
    An outage carries `until`; a refusal about the item carries none."""
    entry = {"why": " ".join(why.split())[:400], "shape": shape}
    if outcome == "unavailable":
        entry["until"] = (at + OUTAGE_HOLD).strftime(STAMP)
    return entry


def held_reason(entry, shape, at):
    """Why a held item stays out of this pass, or "" when it is offered again.
    A shape change on main frees either kind at once; an entry with no `until`
    (a refusal, or one written before outages were told apart) waits for one."""
    if not entry or entry.get("shape") != shape:
        return ""
    why = (entry.get("why") or "")[:300]
    until = entry.get("until")
    if until is None:
        return "admission refused it after a claim and the item has not changed since — %s" % why
    try:
        due = datetime.strptime(until, STAMP).replace(tzinfo=timezone.utc)
    except ValueError:
        return ""                    # an unreadable bound holds nothing: the item is offered
    if at < due:
        return "admission failed on infrastructure after a claim; offered again at %s — %s" % (until, why)
    return ""


def known_except_reshaped(backlog, frontier, packets, reason_of=None, said=None):
    """Packet ids unavailable for dispatch, and declined packets triaged on main.

    The planner and dispatcher must count the same eligible work. A declined
    packet becomes eligible only when its released item is ready again and its
    substance changed since the claim — and, when `reason_of(packet)` gives the
    worker's stop reason, the item now NAMES that stop code (`factory_lib.
    triaged`). Claim/status churn alone is not a change, and neither is an edit
    that leaves the decline unanswered. `said` is the pass's standing log.
    """
    paths = {item["id"]: item["path"] for item in frontier.get("ready", [])
             if item.get("status") == "open" and item.get("claim") == "none"}
    reshaped = {}
    native = wa.beads(backlog)
    for packet in packets:
        if packet["state"] != "declined" or packet["bead"].get("status") == "closed":
            continue
        rel = paths.get(packet["id"])
        claim_commit = packet["md"].get("proof.claim_commit")
        if not rel or not claim_commit:
            continue
        reason = reason_of(packet) if reason_of else ""
        if native:
            row = wa.row(backlog, packet["id"])
            why = fl.triaged_row(packet["md"], row, reason)
            edited = bool(row) and (row.get("semantic_digest") or "") != (packet["md"].get("proof.packet_digest") or "")
        else:
            given = fl.sh(["git", "-C", backlog, "show", "%s:%s" % (claim_commit, rel)])
            today = fl.sh(["git", "-C", backlog, "show", "HEAD:" + rel])
            if given.returncode != 0 or today.returncode != 0:
                continue
            why = fl.triaged(given.stdout, today.stdout, reason)
            edited = fl.shaped(given.stdout) != fl.shaped(today.stdout)
        if not why:
            reshaped[packet["id"]] = packet
        elif said is not None and edited:
            said.say("triage:" + packet["id"], "skip %s: %s" % (packet["id"], why))
    return {packet["id"] for packet in packets} - set(reshaped), reshaped


def review_hold(backlog, policy_rig, item):
    """Why an item waits for a reviewer, or "". An item whose text names the
    review decline (`factory_lib.REVIEW_DECLINE`) was once refused by a worker
    for want of an independent reviewer; it is admitted again only by a path
    that draws one, whoever claims it and however many times it is re-shaped."""
    if not backlog or wants_review(policy_rig, item):
        return ""
    try:
        _path, _fm, body, _dm = admit.load_item(backlog, item["id"])
    except fl.Refused:
        return ""
    if fl.REVIEW_DECLINE not in body:
        return ""
    return ("%s was declined for want of an independent reviewer and nothing draws one now — set "
            "`review: required` in its frontmatter, or hand-admit it with --review" % item["id"])


def release(backlog, seat, refused, policy, leases=None):
    """Give back, in one commit, the claims of items admission refused AFTER the
    claim landed. Preflight makes this rare — main moved between the two, or the
    refusal is one only admission can make — but a claim with no packet behind it
    holds its write scope against the queue for twelve hours, so it never stays.

    Under the Beads authority the release goes through the client with the
    lease the claim printed (`leases`), or the one the snapshot row carries;
    nothing lands on main."""
    if wa.beads(backlog):
        gone = []
        for pid, why in refused:
            lease = (leases or {}).get(pid) or wa.lease_of(backlog, pid, seat, fresh=True)
            if not lease:
                fl.log(ME, "could not release %s: no lease for %s in the ledger" % (pid, seat))
                continue
            try:
                wa.client(backlog, "release", item=pid, by=seat, lease=lease)
                gone.append(pid)
            except fl.Refused as exc:
                fl.log(ME, "could not release %s: %s" % (pid, str(exc)[-200:]))
        wa.forget(backlog)
        if gone:
            fl.log(ME, "released %s in the work ledger: admission refused after the claim" % ", ".join(gone))
        return
    try:
        with fl.backlog_write():
            gone = []
            for pid, why in refused:
                out = fl.sh([sys.executable, "scripts/next_work.py", "--release", pid, "--by", seat], cwd=backlog)
                if out.returncode == 0:
                    gone.append((pid, why))
                else:
                    fl.log(ME, "could not release %s: %s" % (pid, (out.stderr or out.stdout).strip()[-200:]))
            if not gone:
                return
            landed = fl.land_backlog_change(
                backlog,
                "Release %d item%s admission refused after the claim\n\n"
                "Released by the PROOF dispatcher (gascity/packs/proof/assets/scripts/dispatcher.py): the item was\n"
                "claimed for %s and admission then refused it, so no packet exists and the claim would only hold\n"
                "its write scope against the queue. It is not retaken until the refusal no longer applies.\n\n%s\n\nRefs %s."
                % (len(gone), "" if len(gone) == 1 else "s", seat,
                   "\n".join("%s: %s" % (pid, " ".join(why.split())[:300]) for pid, why in gone),
                   ", ".join(pid for pid, _ in gone)),
                policy["shared_checkout"])
            fl.log(ME, "released %s on main as %s: admission refused after the claim"
                   % (", ".join(pid for pid, _ in gone), landed[:10]))
    except fl.BacklogBusy:
        return


def _packets_or_none(city, rigs, name):
    try:
        admit.owned_clone(rigs, name)
    except fl.Refused:
        return []
    return fl.packets(city, name)


def wants_review(policy_rig, item):
    """An independent reviewer is drawn by the ITEM (`review: required`, which the
    frontier carries as `review`) or by the RIG (a write-scope substring the
    policy names). A sentence in the body asking for `--review` is read by
    nobody here: 8to9 said it and was admitted without one, twice."""
    if item.get("review") is True or str(item.get("review") or "").strip().lower() in ("required", "true"):
        return True
    text = " ".join(item.get("writeScope") or []).lower()
    return any(needle.lower() in text for needle in policy_rig["review_if_scope_contains"])


def claim_with_companions(backlog, item, repo, files, seat, hours, max_depth=1):
    """Publish exact test scope with the ordinary claim; restore on a refused claim.

    Under the Beads authority the item is not a file: the claim goes through
    `next_work.py --claim` as before (the ledger behind it), and no companion
    scope is written — widening a record is a ledger edit, not a file edit."""
    path, fm, _body, dm = admit.load_item(backlog, item['id'])
    claim_state, _detail = dm.claim(SimpleNamespace(fm=fm), now())
    if claim_state == "live" and fm.get("claimed_by") == seat:
        return subprocess.CompletedProcess([], 1, "", "already claimed by %s in an overlapping pass" % seat), []
    if wa.beads(backlog):
        result = fl.sh([sys.executable, 'scripts/next_work.py', '--claim', item['id'], '--by', seat,
                        '--hours', str(hours)], cwd=backlog)
        wa.forget(backlog)
        return result, []
    path = Path(path)
    original = path.read_text()
    updated, added = test_companions.scope_update(original, fm, dm, repo, files, max_depth)
    if added:
        path.write_text(updated)
    try:
        result = fl.sh([sys.executable, 'scripts/next_work.py', '--claim', item['id'], '--by', seat,
                        '--hours', str(hours)], cwd=backlog)
    except BaseException:
        if added and path.read_text() == updated:
            path.write_text(original)
        raise
    if result.returncode != 0:
        if added:
            # A refused claim has made no record change. Do not overwrite an
            # unexpected edit: the factory lock excludes cooperating writers.
            if path.read_text() != updated:
                raise fl.Refused('packet changed during companion claim; preserve and inspect ' + str(path))
            path.write_text(original)
    elif added:
        item['writeScope'] = list(fm['write_scope']) + [repo + ':' + p for p in added]
    return result, added


def admit_packet(city, rig, landing, pid, review, dry_run, exercise=""):
    """Admit one claimed item. Returns (ok, why, outcome), outcome one of
    "admitted", "running" (preserve the existing claim), "refused", and
    "waits" — a live test that found the slot taken
    between this pass's look and its admission: deferred, not refused, so its
    claim is released but nothing remembers it — and "unavailable": a command
    admission ran failed, so its claim is released and it is held an hour."""
    allow = os.path.join(city, "proof", "pilot-allowlist.txt")
    listed = os.path.isfile(allow) and any(line.split("#", 1)[0].strip() == pid for line in open(allow))
    if not listed and not dry_run:
        with open(allow, "a", encoding="utf-8") as fh:
            fh.write("%s   # dispatcher, under proof/policy.toml\n" % pid)
    argv = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "admit.py"),
            pid, "--rig", rig, "--city", city]
    # A live test is authorized on the production axis and takes no landing;
    # an implementation packet takes the rig's landing and never an exercise.
    argv += ["--exercise", exercise] if exercise else ["--landing", landing]
    argv += ["--review"] if review and not exercise else []
    argv += ["--dry-run"] if dry_run else []
    done = fl.sh(argv)
    why = "" if done.returncode == 0 else (done.stderr or done.stdout).strip()
    fl.log(ME, "admit %s (%s%s): %s" % (pid, "exercise " + exercise if exercise else landing,
                                        ", review" if review and not exercise else "", why[-300:] or "ok"))
    outcome = {0: "admitted", admit.RUNNING_EXIT: "running", admit.WAITS_EXIT: "waits",
               admit.UNAVAILABLE_EXIT: "unavailable"}.get(done.returncode, "refused")
    return done.returncode == 0, why, outcome


def main(argv):
    dry_run = "--dry-run" in argv
    city = fl.city_dir()
    policy = fl.load_policy(city)
    if not policy["enabled"] and not dry_run:
        fl.log(ME, "policy is not enabled; nothing dispatched")
        return 0
    said = fl.Standing(city, ME, keep=not dry_run)
    try:
        return dispatch(city, policy, said, dry_run)
    finally:
        said.close()


def dispatch(city, policy, said, dry_run):
    why = fl.limit_hold(said.state)
    if why:
        said.say("limit-hold", "nothing dispatched: %s" % why)
        return 0
    rigs = admit.load_rigs(city)
    states = fl.rig_states(city)
    backlog = admit.backlog_clone_of(rigs)
    evidence_context = {"rigs": rigs, "refreshed": set()}

    # The work authority marker (ADR-0236). A write freeze admits and claims
    # nothing under either authority; under Beads nothing new is admitted
    # unless the pack's retention state is fresh, healthy and replicated.
    if wa.frozen(backlog):
        said.say("freeze", "nothing dispatched: %s" % wa.FREEZE_REASON)
        return 0
    native = wa.beads(backlog)
    if native:
        why = wa.retention_problem(city)
        if why:
            said.say("retention", "nothing dispatched under the beads work authority: %s" % why)
            return 0
        wa.forget(backlog)

    why = fl.budget_hold(policy, rigs)
    if why:
        said.say("budget", why)
        return 0

    # One host runs every rig's workers. A per-rig limit cannot see that three Rust
    # builds and three Node builds share the same memory, so `max_running` caps
    # the packets running across ALL rigs; 0 leaves only the per-rig limits.
    # A live test holds the live slot, not a worker slot: it is left out of both.
    running_everywhere = sum(1 for name in rigs for p in _packets_or_none(city, rigs, name)
                             if p["state"] == "running" and not is_live(p))
    # The city's ONE live slot, read once for the pass. `live_free` goes false
    # when this pass admits a live test, so a second is never admitted beside it.
    live_free, live_holder = admit.live_slot(city)

    absent = sorted(rig for rig, cfg in policy["rigs"].items() if rig not in rigs or cfg["landing"] not in admit.LANDING)
    if absent:
        said.say("absent", "%d policy table%s skipped — not a rig in this city, or no landing level: %s"
                 % (len(absent), "" if len(absent) == 1 else "s", ", ".join(absent)))

    for rig, cfg in sorted(policy["rigs"].items()):
        if rig in absent:
            continue
        if states.get(rig, {}).get("suspended") and not dry_run:
            said.say("suspended:" + rig, "rig %s is suspended; nothing dispatched" % rig)
            continue
        try:
            clone = admit.healthy_clone(rigs, rig)
        except fl.Refused as exc:
            said.say("health:" + rig, "rig %s: dispatch held: %s" % (rig, exc))
            continue
        repo = admit.repo_name(clone)
        history = fl.packets(city, rig, superseded=True)
        known = [p for p in history if not p.get("superseded")]
        retired_rootless = {p["id"] for p in history if p.get("superseded") and not p["root"]
                            and p["bead"].get("status") != "closed"} - {p["id"] for p in known}
        pending = {p["id"]: p for p in known if fl.admission_retryable(p)}
        running = [p for p in known if p["state"] == "running" and not is_live(p)]
        live_enabled = fl.livetest_policy(cfg, rig)["enabled"]
        live_room = live_free and live_enabled
        failed = [p for p in known if p["state"] == "failed" and p["bead"].get("status") != "closed"]
        declined = [p for p in known if p["state"] == "declined" and p["bead"].get("status") != "closed"]
        unmerged = [p for p in known if p["state"] == "passed" and p["md"].get("proof.landing") in ("pr", "automerge")
                    and not p["md"].get("proof.completed")]
        free = cfg["wip"] - len(running)
        said.say("counts:" + rig, "rig %s: %d running, %d failed, %d declined, %d awaiting merge, %d free of %d"
                 % (rig, len(running), len(failed), len(declined), len(unmerged), max(free, 0), cfg["wip"]))
        if len(failed) >= policy["max_failed"] or len(declined) >= policy["max_declined"]:
            said.say("breaker:" + rig, "rig %s: %d failed (max %d), %d declined (max %d); dispatch holds until a "
                     "person clears them" % (rig, len(failed), policy["max_failed"], len(declined), policy["max_declined"]))
            continue
        if len(unmerged) >= policy["max_unmerged"]:
            said.say("max_unmerged:" + rig, "rig %s: %d packets await landing (max %d); dispatch holds until completion"
                     % (rig, len(unmerged), policy["max_unmerged"]))
        free = min(free, policy["max_unmerged"] - len(unmerged))
        if policy["max_running"]:
            if running_everywhere >= policy["max_running"]:
                said.say("max_running:" + rig, "rig %s: %d packets running across all rigs reach max_running; "
                         "nothing dispatched" % (rig, running_everywhere))
            free = min(free, policy["max_running"] - running_everywhere)
        if free <= 0 and not live_room and not pending:
            if live_enabled and not live_free:
                said.say("live-slot:" + rig, "rig %s: no free worker slot, and live tests wait on the city's one "
                         "live slot — %s" % (rig, live_holder or "a live test was admitted earlier in this pass"))
            continue

        admit.refresh_backlog_clone(backlog)
        evidence_context["refreshed"].add(os.path.realpath(backlog))
        out = fl.sh([sys.executable, "scripts/next_work.py", "--json"], cwd=backlog)
        if out.returncode != 0:
            raise fl.Refused("next_work.py --json failed: %s" % out.stderr.strip()[-300:])
        frontier = json.loads(out.stdout)
        seat = admit.SEAT_PREFIX + rig
        # A released, substantively re-shaped decline is new work. The planner
        # uses this same classification when counting what this rig can take.
        known_ids, reshaped = known_except_reshaped(backlog, frontier, known,
                                                    reason_of=lambda p: fl.stop_reason(city, rig, p), said=said)
        refused = []
        # An item admission refused AFTER a claim is not retaken until it changes:
        # claim, refuse, release, claim again would be two commits on main per pass.
        # One refused on infrastructure is retaken an hour later (`held_reason`).
        held = said.section("refused_after_claim")
        paths = {i["id"]: i.get("path") or "" for i in frontier.get("ready", []) + frontier.get("claimed", [])}
        leases = {}                           # the lease each claim of this pass printed (Beads)

        # 2. claims without a bead, or interrupted before sling or its record write
        for item in frontier.get("claimed", []):
            recovery = pending.get(item["id"])
            # …but never one that is blocked: a claimed item that has since been given a
            # question to wait on is not work, and a worker would only decline it.
            if item["id"] in retired_rootless or (item["id"] in known_ids and recovery is None) or item.get("blockers") \
                    or ("claimed by %s " % seat) not in (item.get("claimDetail") or ""):
                continue
            if recovery is not None:
                if recovery.get("record_pending"):
                    said.say("record-pending:" + item["id"],
                             "%s workflow %s has no admission record; admitting again to write it"
                             % (item["id"], recovery["root"]["id"]))
                else:
                    said.say("rootless:" + item["id"], "%s bead %s admitted without a workflow; admitting again"
                             % (item["id"], recovery["bead"]["id"]))
            # A running workflow already owns this claim. Let admission check
            # its record repair; a preflight refusal must not release its scope.
            repairing_record = recovery is not None and recovery.get("record_pending")
            # A claim with no workflow behind it is given back if admission
            # can never accept it.
            why = "" if repairing_record else refusal(
                backlog, item["id"], repo, dict(evidence_context, review=wants_review(cfg, item)))
            level = ""
            if not why and not repairing_record:
                level, why = live_check(backlog, item["id"], cfg, rig)
            hold = review_hold(backlog, cfg, item) if not why and not repairing_record else ""
            if why:
                refused.append((item["id"], why))
            elif hold:
                # A person's claim ahead of `gc proof admit --review`: leave it to them.
                said.say("review:" + item["id"], "%s is claimed for %s and waits — %s" % (item["id"], seat, hold))
            elif level and not live_room:
                said.say("live-slot:" + item["id"], "%s is claimed for %s and waits on the city's one live slot — %s"
                         % (item["id"], seat, live_holder or "a live test was admitted earlier in this pass"))
            elif level or free > 0 or recovery is not None:
                ok, why, outcome = admit_packet(city, rig, cfg["landing"], item["id"], wants_review(cfg, item),
                                               dry_run, exercise=level)
                if outcome == "running":
                    said.say("running:" + item["id"], "%s is already admitted — %s"
                             % (item["id"], " ".join(why.split())[:300]))
                elif ok and level:
                    live_free = live_room = False
                elif ok:
                    if recovery is None:
                        free -= 1
                        running_everywhere += 1
                else:
                    said.say("admit:" + item["id"], "%s is claimed for %s and admission refuses it — %s"
                             % (item["id"], seat, " ".join(why.split())[:300]))
            known_ids.add(item["id"])

        try:
            with contextlib.ExitStack() as writes:
                writing = False
                # 3. claim a batch, one commit
                batch = []
                ordinary = 0                         # implementation items in the batch; live ones take the slot
                if free > 0 or live_room:
                    companion_files, companion_sha = {}, ''
                    if cfg.get('test_companions'):
                        # Pin origin/main once per rig/pass; never inspect a worker's
                        # edited tests or execute repository code to grant more scope.
                        try:
                            admit.run(['git', '-C', clone, 'fetch', '--quiet', 'origin'])
                            companion_sha, companion_files = test_companions.snapshot(clone)
                        except (fl.Refused, ValueError) as exc:
                            said.say('companions:' + rig, 'test companion discovery unavailable; explicit scope remains: ' + str(exc))
                    heads = fl.sh(["git", "-C", clone, "ls-remote", "--heads", "origin"]).stdout
                    branches = [line.split("refs/heads/", 1)[1] for line in heads.splitlines() if "refs/heads/" in line]
                    candidates = list(admissible(city, cfg, repo, frontier, branches, known_ids, said, backlog, rig, evidence_context))
                    for item in candidates:
                        level = item.get("liveExercise") or ""
                        if level and not live_room:
                            # Unclaimed, so it holds nothing while it waits; the queue behind it moves.
                            said.say("live-slot:" + item["id"], "skip %s: a live test, waiting on the city's one live slot — %s"
                                     % (item["id"], live_holder or "a live test was admitted earlier in this pass"))
                            continue
                        if not level and ordinary >= free:
                            if not live_enabled:
                                break                # nothing further down the queue can use this pass
                            continue                 # a live item further down may still take, or wait on, the slot
                        before = held.get(item["id"])
                        still = held_reason(before, shape_digest(backlog, paths.get(item["id"], ""), item["id"]), now()) if before else ""
                        if still:
                            said.say("held:" + item["id"], "skip %s: %s" % (item["id"], still))
                            continue
                        held.pop(item["id"], None)
                        # Ask the claim's own rule BEFORE claiming, in both modes. The
                        # ledger claim is still the authority and is still asked for every
                        # item this lets through; this only spares the ledger a round trip
                        # whose answer the frontier already holds. Under the Beads authority
                        # a refused `next_work.py --claim` costs about 30 s (the corpus load
                        # and the transaction) against 2 s under Git, and the dispatcher
                        # holds the factory lock throughout: on 2026-10-15, 21 of the 22
                        # liskov-rs candidates collided with one live claim on the shared
                        # crate-boundary registry, a pass ran past its interval, four passes
                        # overlapped, and the completer's landings were skipped for want of
                        # the lock.
                        patterns = [entry.split(':', 1)[1] for entry in item['writeScope'] if ':' in entry]
                        added = test_companions.related(companion_files, patterns, cfg.get('test_companion_import_depth', 1))
                        holder = overlapping(backlog, frontier, item, [(repo, path) for path in added], batch)
                        if holder:
                            said.say("claim:" + item["id"], "claim %s for %s: write scope overlaps active claim %s"
                                     % ("would be refused" if dry_run else "not sent", item["id"], holder))
                            continue
                        if dry_run:                  # no claim is made
                            item['writeScope'] = list(item['writeScope']) + [repo + ':' + path for path in added]
                            batch.append(item)
                            if level:
                                live_free = live_room = False
                            else:
                                ordinary += 1
                            if added:
                                fl.log(ME, '%s would include companion tests at %s: %s' % (item['id'], companion_sha[:10], ', '.join(added)))
                            continue
                        if not writing:
                            writes.enter_context(fl.backlog_write(city))
                            writing = True
                        claim, added = claim_with_companions(backlog, item, repo, companion_files, seat, policy['claim_hours'],
                                                            cfg.get('test_companion_import_depth', 1))
                        if claim.returncode == 0:
                            batch.append(item)
                            lease = wa.printed_lease(claim.stdout)
                            if lease:
                                leases[item["id"]] = lease
                            if level:
                                live_free = live_room = False
                            else:
                                ordinary += 1
                            if added:
                                fl.log(ME, '%s includes companion tests at %s: %s' % (item['id'], companion_sha[:10], ', '.join(added)))
                        else:  # overlaps a live claim, or capacity: the next item may still fit
                            said.say("claim:" + item["id"], "claim refused for %s: %s"
                                     % (item["id"], (claim.stderr or claim.stdout).strip()[-200:]))
                    if not batch:
                        said.say("empty:" + rig, "rig %s: %d free slot%s and nothing it can take under the policy"
                                 % (rig, free, "" if free == 1 else "s"))
                ids = [i["id"] for i in batch]
                if dry_run:
                    if ids:
                        fl.log(ME, "rig %s: would claim and admit %s" % (rig, ", ".join(ids)))
                    if refused:
                        fl.log(ME, "rig %s: would release %s" % (rig, ", ".join(pid for pid, _ in refused)))
                    continue
                if ids and native:
                    # The claims are in the ledger already; there is no record to land.
                    fl.log(ME, "rig %s: claimed %s in the work ledger%s" % (
                        rig, ", ".join(ids), " (leases %s)" % ", ".join(leases[i] for i in ids if i in leases) if leases else ""))
                elif ids:
                    epics = sorted({i.get("parentEpic") for i in batch if i.get("parentEpic")})
                    landed = fl.land_backlog_change(
                        backlog,
                        "Claim %d item%s for the Gas City %s seat\n\n"
                        "Dispatched by the PROOF dispatcher (gascity/packs/proof/assets/scripts/dispatcher.py)\n"
                        "under the operator's policy file, in delivery-queue order, for %d free worker\n"
                        "slot%s. Claimed on the existing path with next_work.py for %s.\n\n"
                        "Refs %s." % (len(ids), "" if len(ids) == 1 else "s", rig, len(ids),
                                      "" if len(ids) == 1 else "s", seat, ", ".join(ids + epics)),
                        policy["shared_checkout"])
                    fl.log(ME, "rig %s: claimed %s on main as %s" % (rig, ", ".join(ids), landed[:10]))

        except fl.BacklogBusy:
            # No edit began. Release earlier refused claims independently.
            if refused:
                release(backlog, seat, refused, policy, leases)
            continue

        # 4. admit
        for item in batch:
            if item["id"] in reshaped:
                old = reshaped[item["id"]]["bead"]["id"]
                fl.set_bead(city, rig, old, proof__superseded="true")
                admit.gc(city, "bd", "--rig", rig, "close", old, "--reason",
                         "declined; the item was re-shaped on main and is admitted again as a new bead", check=False)
                fl.log(ME, "%s was re-shaped since it was declined; bead %s superseded" % (item["id"], old))
            level = item.get("liveExercise") or ""
            ok, why, outcome = admit_packet(city, rig, cfg["landing"], item["id"], wants_review(cfg, item), dry_run,
                                            exercise=level)
            if outcome == "running":
                said.say("running:" + item["id"], "%s is already admitted — %s"
                         % (item["id"], " ".join(why.split())[:300]))
            elif ok:
                running_everywhere += 0 if level else 1
            else:
                refused.append((item["id"], why))
                # A live test that lost the slot to a direct admission is deferred,
                # not refused: its claim goes back, and nothing holds it out later.
                if outcome != "waits":
                    held[item["id"]] = hold_entry(why, shape_digest(backlog, paths.get(item["id"], ""), item["id"]),
                                                  outcome, now())
        if refused:
            release(backlog, seat, refused, policy, leases)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except fl.Refused as why:
        fl.log(ME, "stopped — %s" % why)
        sys.exit(1)
