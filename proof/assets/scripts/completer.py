#!/usr/bin/env python3
"""PROOF completer: mark a packet's backlog item done once its pull request has
merged AND main is green with it. A script — no agent, no model, no tokens.

    completer.py [--dry-run]

For every packet that passed with landing `pr` or `automerge` and is not yet
completed, it reads the pull request for the packet branch:

  merged    find the first commit on main, at or after the merge, whose
            PUSH-triggered workflows all completed without a cancellation (a
            superseded run proves nothing; a scheduled run is not about this
            merge). When no head since the merge has a run and no workflow's
            own push filters select any of them (`push_selection`), there is
            no main CI for the change and the gate is the evidence: green.
            For a rig explicitly configured with
            main_verification="commit-status", instead require its named
            merge_requires contexts as successful statuses on that exact main
            commit, with no unexpected check runs. Green: complete the item.
            Red: tell a human once; a later green head completes it.
            main_verification="gate" is opt-in for on-gate repositories with
            no CI: verify the workflow-approved PR head and exact merge tree,
            main ancestry, and absence of unexpected checks before completion.
  open      tell a human once if it conflicts or a check failed; else wait —
            unless the rig's policy says to merge it here:
              merge = "when-green"  a repository whose main is NOT protected,
                where arming auto-merge merges at once, before CI. Merge only
                when every check named in `merge_requires` is present and green
                and nothing failed.
              merge = "on-gate"  a repository with NO pull-request CI at all,
                where no check will ever report and the orchestrator's gate is
                the only evidence there is. Merge when nothing is pending and
                nothing failed. Set no `merge_requires` with it.
            A rig with neither waits for a person, which is the right answer
            wherever merging costs money or deploys something nobody watched.
            Either way the pull request holds its claim: with less than two
            hours left it is renewed for `claim_hours`, every renewal of a pass
            in one commit on main, until 72 hours after the pull request was
            opened. Then it is left to expire and a human is told once
            (`renew_open`).
  closed    tell a human once. Someone decided; the claim is theirs to release.

It also RELEASES the claim of every packet that stopped — declined at
load-context, or failed later — in one commit. A stopped item needs triage, by a
person or the planner; its claim must not hold other items' write scopes while
it waits (in the first batch one declined claim refused two good items for
twelve hours). Released unchanged, the dispatcher never retakes it; released and
re-shaped on main, it is new work. `gc proof status` lists what awaits triage.

It also PUBLISHES the evidence of every settled live-test run, on a path of its
own that shares nothing with the pull-request completion above but the landing
routine (`publish_live_results`, `livetest_evidence.py`). A live test is closed
by that evidence, never by a merge or by its tester: only a `pass` completes an
item, and only the item the run was admitted for. The run journal, not the
workflow's beads, is the input, so a run whose formula body aborted still
publishes; and a retry — after a crash, a lost receipt or a push that lost the
race for main — reconciles from the remote commit and never reads production.

Completion is the backlog's own procedure ("Pull work", step 3): status done,
done_at, evidence, claim fields removed, views regenerated, validated, landed on
main — every completion of a pass in ONE commit, from the Gas City-owned
orchestrator clone. Then the packet's merged worktree and branch are removed;
git refuses on its own if anything in them is unmerged.
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import factory_lib as fl
import work_authority as wa
from factory_lib import admit

ME = "completer"
BAD = {"failure", "timed_out", "action_required", "startup_failure"}
# Fixed here, not policy keys (BKLG-20260929-qty6): an open pull request's claim
# is renewed once less than RENEW_WITHIN is left, and never once the pull request
# has been open RENEW_FOR_AT_MOST.
RENEW_WITHIN = timedelta(hours=2)
RENEW_FOR_AT_MOST = timedelta(hours=72)


def gh_json(args, cwd):
    out = fl.sh(["gh", *args], cwd=cwd)
    if out.returncode != 0:
        raise fl.Refused("gh %s failed: %s" % (" ".join(args[:3]), out.stderr.strip()[-300:]))
    return json.loads(out.stdout or "null")


def heads_since(clone, merge_sha):
    """The commits `main` actually pointed at, from the merge forward, oldest first
    — its first-parent chain. [] when the clone cannot say (not fetched yet)."""
    fl.sh(["git", "-C", clone, "fetch", "--quiet", "origin"])
    out = fl.sh(["git", "-C", clone, "rev-list", "--first-parent", "--reverse", "%s^..origin/main" % merge_sha])
    return out.stdout.split() if out.returncode == 0 else []


def verdict_over(shas, runs_of, selects=None):
    """('green'|'red'|'pending', sha, detail) over `shas`, oldest first, where
    `runs_of(sha)` is that commit's PUSH-triggered workflow runs.

    A later green head settles an earlier red one: the item is done when main is
    green WITH the change, whether that took a fix-forward or not.

    A commit with no push run is skipped unless it is the newest: only the newest
    can be a head whose runs have not registered yet. Any older one never got a
    run and never will — a docs-only push the workflows filter out, or (before
    the walk followed first parents) a pull request's own commit, which the
    commits API lists between two merges although `main` never pointed at it.
    That case read as "stop here": BKLG-20260913-524k's merge went red on a flaky
    test, the next commit listed was the following pull request's branch commit,
    and the red stood for eight hours over four green heads, its claim holding
    six shaped items out of a rig with idle workers (2026-09-20).

    The newest head's missing run is final only when NO head since the merge
    got a run and `selects(sha)` — the workflow files' own push filters, read by
    `push_selection` — says for every one of them that no workflow would start.
    Then there is no main CI for this change at all and the gate that passed it
    is the evidence. A path-filtered repository otherwise waits for ever:
    BKLG-20260925-z47h added a root LICENSE to liskov-marketplace-offerings,
    whose two push workflows run only for `uptime-prober/**` (2026-09-26)."""
    first_red, saw_runs = None, False
    for n, sha in enumerate(shas):
        runs = runs_of(sha)
        if not runs:
            if n + 1 < len(shas):
                continue
            if first_red:
                return "red", first_red[0], first_red[1]
            if not saw_runs and selects and all(selects(s) is False for s in shas):
                return "green", sha, "no push workflow selects any head since the merge; the gate is the evidence"
            return "pending", sha, "push workflows have not started"
        saw_runs = True
        if any(r["status"] != "completed" for r in runs):
            if first_red:
                break
            return "pending", sha, "push workflows still running"
        bad = sorted(r["name"] for r in runs if r["conclusion"] in BAD)
        if bad:
            first_red = first_red or (sha, ", ".join(bad))
            continue
        if any(r["conclusion"] == "cancelled" for r in runs):
            continue                                        # superseded: judge a later head
        return "green", sha, "%d push workflow(s)" % len(runs)
    if first_red:
        return "red", first_red[0], first_red[1]
    return "pending", shas[-1] if shas else "", "every run since the merge was superseded; waiting for the next head"


def status_verdict_over(shas, statuses_of, checks_of, required):
    """Judge Vercel-only main heads by exact commit statuses, never PR previews.

    This mode is opt-in. A new check run means the repository acquired another
    verification path; hold completion until the policy names its authority.
    """
    first_red = None
    for n, sha in enumerate(shas):
        doc = statuses_of(sha)
        if doc.get("sha") != sha:
            return "pending", sha, "commit status response did not identify the requested main head"
        checks = checks_of(sha)
        if checks:
            return "pending", sha, "unexpected check runs on status-only main; update main_verification"
        latest = {}
        for status in sorted(doc.get("statuses") or [], key=lambda s: s.get("created_at") or s.get("updated_at") or ""):
            if status.get("context"):
                latest[status["context"]] = status
        missing = [name for name in required if name not in latest]
        if missing:
            if n + 1 < len(shas):
                continue
            return ("red", first_red[0], first_red[1]) if first_red else (
                "pending", sha, "main statuses have not registered: %s" % ", ".join(missing))
        failed = sorted(name for name, status in latest.items() if status.get("state") in ("failure", "error"))
        if failed or doc.get("state") in ("failure", "error"):
            first_red = first_red or (sha, ", ".join(failed) or "commit status failed")
            continue
        if doc.get("state") != "success" or any(latest[name].get("state") != "success" for name in required):
            if first_red:
                break
            return "pending", sha, "main commit statuses are still pending"
        return "green", sha, "main commit status: %s" % ", ".join(required)
    if first_red:
        return "red", first_red[0], first_red[1]
    return "pending", shas[-1] if shas else "", "main commit statuses have not registered"


def main_verdict(clone, slug, merge_sha, merged_at, rig_policy=None, packet=None, pr=None):
    """The verdict for main since the merge.

    The default judges push-triggered workflows, head by head from the merge
    forward. A nightly scheduled workflow can attach to an already green head;
    that red is real but is not evidence about the merge. A rig that explicitly
    selects commit-status mode instead uses its named status contexts on exact
    main heads. This is for Vercel-only repositories with no push workflow."""
    rig_policy = rig_policy or {}
    mode = rig_policy.get("main_verification") or "push-workflows"
    if mode == "gate":
        import gate_completion
        return gate_completion.verdict(clone, slug, merge_sha, rig_policy, packet, pr, gh_json)
    shas = heads_since(clone, merge_sha)
    if not shas:                                            # the clone could not say: ask GitHub, as before
        commits = gh_json(["api", "repos/%s/commits?sha=main&since=%s&per_page=100" % (slug, merged_at)], clone)
        shas = [c["sha"] for c in reversed(commits)]           # oldest first
        if merge_sha in shas:
            shas = shas[shas.index(merge_sha):]
    shas = shas or [merge_sha]
    if mode == "commit-status":
        required = rig_policy.get("merge_requires") or []
        if not isinstance(required, list) or not required or any(not isinstance(x, str) or not x for x in required):
            return "pending", shas[-1], "main_verification=commit-status needs named merge_requires"
        return status_verdict_over(
            shas,
            lambda sha: gh_json(["api", "repos/%s/commits/%s/status" % (slug, sha)], clone),
            lambda sha: gh_json(["api", "repos/%s/commits/%s/check-runs" % (slug, sha)], clone)["check_runs"],
            required,
        )
    if mode != "push-workflows":
        return "pending", shas[-1], "unknown main_verification %r" % mode
    import push_selection
    return verdict_over(shas, lambda sha: gh_json(
        ["api", "repos/%s/actions/runs?head_sha=%s&event=push&per_page=100" % (slug, sha)], clone)["workflow_runs"],
        lambda sha: push_selection.selects(clone, sha))


def notify_once(city, rig, packet, state, subject, body, key="proof.pr_notified"):
    """Mail once per `state` under `key`. A notice that can stand beside another
    — the three-day notice beside "cannot merge" — takes a key of its own, or the
    two would overwrite each other's record and each mail again on every pass."""
    if packet["md"].get(key) == state:
        return
    if fl.mail(subject, body):
        fl.set_bead(city, rig, packet["bead"]["id"], **{key.replace(".", "__"): state})
    fl.log(ME, "%s: %s" % (packet["id"], subject))


def record_pr_url(city, rig, packet, pr, dry_run):
    """Record the pull request found by branch as the work bead's `proof.pr_url`.

    The open-pr worker is asked to record it, and usually writes it on its own
    step bead instead: BKLG-20261009-gpuk's work bead had none, so status said
    "no pull request recorded" beside liskov-rs#1558 (86 liskov-rs packets the
    same). The completer finds the newest one by branch on every pass anyway,
    so the record is made here, deterministically."""
    if dry_run or not pr.get("url") or packet["md"].get("proof.pr_url") == pr["url"]:
        return
    fl.set_bead(city, rig, packet["bead"]["id"], proof__pr_url=pr["url"])
    packet["md"]["proof.pr_url"] = pr["url"]                # a failed write is retried next pass


def renew_open(city, policy, backlog, waiting, dry_run, now=None):
    """Renew, in one commit, the claim of every packet whose pull request is open.

    `waiting` is [(rig, packet, pr)] for passed `pr`/`automerge` packets whose
    newest pull request is OPEN. While it waits the item's write scope must go on
    refusing overlapping packets: BKLG-20260926-mbwn's claim ran out while
    liskov-rs #1281 sat red, BKLG-20260925-k8gz was claimed over the same
    `migrations/**` and both took migration 0287 (2026-09-29).

    A claim is renewed for `claim_hours` once less than RENEW_WITHIN is left, and
    only while it is still `working` for this rig's seat — a released, completed
    or re-claimed item is someone else's. `next_work.py --renew` makes its own
    ownership and overlap checks and runs in the owned backlog clone, as
    `--claim` and `--release` do; a refusal is logged and the rest go on. After
    RENEW_FOR_AT_MOST from the pull request's opening the claim is left to
    expire and a human is told once."""
    if not waiting:
        return
    import docs_model as dm
    now = now or datetime.now(timezone.utc)
    admit.refresh_backlog_clone(backlog)
    native = wa.beads(backlog)
    if native:
        wa.forget(backlog)
    candidates = []
    for rig, p, pr in waiting:
        fm = item_fields(backlog, p["id"])
        if not isinstance(fm, dict) or not admit.held_by_us(fm.get("claimed_by"), rig) or fm.get("status") != "working":
            continue                                        # not ours to hold
        expires, opened = dm.parse_timestamp(fm.get("claim_expires")), dm.parse_timestamp(pr.get("createdAt"))
        if expires is None or opened is None:
            fl.log(ME, "%s: cannot read %s; its claim is not renewed" % (
                p["id"], "claim_expires %r" % fm.get("claim_expires") if expires is None
                else "when %s was opened (%r)" % (pr["url"], pr.get("createdAt"))))
            continue
        recovery_owns = (p["md"].get("proof.pr_recovery_key")
                         and p["md"].get("proof.pr_recovery_state") in ("launching", "running", "escalated"))
        # A bounded recovery can stop writing without settling its PR. Preserve
        # exclusion until its named owner resolves it; expiry is not resolution.
        if now - opened >= RENEW_FOR_AT_MOST and not recovery_owns:
            notify_once(city, rig, p, "waited", "PROOF pull request open three days, claim left to expire: %s" % p["id"],
                        "packet  %s\nbead    %s (rig %s)\npr      %s\nopened  %s\nclaim   expires %s\n\n"
                        "The completer renews a packet's claim while its pull request is open, for %d hours\n"
                        "after it was opened. That is over: the claim is no longer renewed, and when it runs out\n"
                        "the item's write scope is free for overlapping packets. Merge or close the pull request,\n"
                        "or renew or release the claim yourself."
                        % (p["id"], p["bead"]["id"], rig, pr["url"], pr["createdAt"], fm["claim_expires"],
                           RENEW_FOR_AT_MOST // timedelta(hours=1)),
                        key="proof.renewal_notified")
            continue
        if expires - now >= RENEW_WITHIN:
            continue
        if dry_run:
            fl.log(ME, "%s: would renew its claim (expires %s) while %s is open" % (p["id"], fm["claim_expires"], pr["url"]))
            continue
        candidates.append((rig, p, pr, fm["claim_expires"]))
    if not candidates:
        return
    if native:
        for rig, p, pr, was in candidates:
            renew_lease(city, backlog, rig, p, policy["claim_hours"], "its pull request %s is open" % pr["url"])
        return
    try:
        with fl.backlog_write(city):
            renewed = []
            for rig, p, pr, was in candidates:
                seat = holder_of(backlog, rig, p["id"]) or admit.seat(rig)
                out = fl.sh([sys.executable, "scripts/next_work.py", "--renew", p["id"], "--by", seat,
                             "--hours", str(policy["claim_hours"])], cwd=backlog)
                if out.returncode == 0:
                    renewed.append((p, pr, was))
                else:
                    fl.log(ME, "could not renew %s: %s" % (p["id"], (out.stderr or out.stdout).strip()[-200:]))
            if not renewed:
                return
            ids = [p["id"] for p, _, _ in renewed]
            landed = fl.land_backlog_change(
                backlog,
                "Renew %d claim%s held by an open pull request\n\n"
                "Renewed by the PROOF completer (gascity/packs/proof/assets/scripts/completer.py) for %s hours:\n"
                "the packet's pull request is still open, so its write scope keeps refusing overlapping packets\n"
                "until it merges. Renewal stops %d hours after the pull request was opened.\n\n%s\n\n"
                "Refs BKLG-20260929-qty6, %s."
                % (len(ids), "" if len(ids) == 1 else "s", policy["claim_hours"], RENEW_FOR_AT_MOST // timedelta(hours=1),
                   "\n".join("%s: %s, opened %s; the claim was to expire %s" % (p["id"], pr["url"], pr["createdAt"], was)
                             for p, pr, was in renewed), ", ".join(ids)),
                policy["shared_checkout"])
    except fl.BacklogBusy:
        return
    fl.log(ME, "renewed the claims of %s on main as %s: their pull requests are open" % (", ".join(ids), landed[:10]))


def live_unpublished(city, packet):
    """True for a live-test packet whose run has not published its evidence.

    Its workflow ending is not its result: the journal is. Until the evidence
    has landed its claim stays, so a pass can still close the item it was
    admitted for and a failure is recorded against it, not against an item
    already handed back. A journal that cannot be read holds the claim too —
    that is a person's call, not a release."""
    run_id = packet["md"].get("proof.live_run_id")
    if not run_id:
        return False
    import livetest_state as ls
    try:
        return ls.RunStore(city).status(run_id).lifecycle != "published"
    except ls.Refused:
        return True


def item_fields(backlog, item_id):
    """The item's frontmatter, from its file under Git or the snapshot row under
    Beads; None when it cannot be read."""
    if wa.beads(backlog):
        row = wa.row(backlog, item_id)
        return dict(row["fields"]) if row and isinstance(row.get("fields"), dict) else None
    import docs_model as dm
    files = glob.glob(os.path.join(backlog, "docs/backlog/items", item_id + "-*.md"))
    return dm.split_frontmatter(open(files[0], encoding="utf-8").read())[0] if len(files) == 1 else None


def holder_of(backlog, rig, item_id):
    """The seat string the ledger row names for an item this factory holds
    (this factory's seat, or the two-part one of an older claim); '' when the
    row is not ours — or cannot be read: a caller then acts as this factory's
    seat, exactly as before 0.44.0, and the ledger still judges the lease."""
    try:
        fm = item_fields(backlog, item_id) or {}
    except (fl.Refused, admit.Unavailable, OSError):
        return ""
    holder = fm.get("claimed_by") or ""
    return holder if admit.held_by_us(holder, rig) else ""


def renew_lease(city, backlog, rig, packet, hours, why):
    """Renew a factory seat's lease on `packet`'s item through the client
    (Beads). A changed lease id is recorded back on the execution bead."""
    seat = holder_of(backlog, rig, packet["id"]) or admit.seat(rig)
    lease = wa.lease_of(backlog, packet["id"], seat, packet["md"])
    if not lease:
        fl.log(ME, "could not renew %s: no lease for %s in the ledger" % (packet["id"], seat))
        return False
    try:
        result = wa.client(backlog, "renew", item=packet["id"], by=seat, lease=lease, hours=hours)
    except fl.Refused as exc:
        fl.log(ME, "could not renew %s: %s" % (packet["id"], str(exc)[-200:]))
        return False
    wa.forget(backlog)
    new_lease = str((result or {}).get("lease_id") or lease)
    if new_lease != packet["md"].get(wa.LEASE_KEY):
        fl.set_bead(city, rig, packet["bead"]["id"], **{wa.LEASE_KEY.replace(".", "__"): new_lease})
        packet["md"][wa.LEASE_KEY] = new_lease
    fl.log(ME, "renewed the lease of %s for %d hours in the work ledger: %s" % (packet["id"], hours, why))
    return True


def release_lease(backlog, rig, packet):
    """Release a factory seat's lease through the client (Beads). Returns the
    operation id of the release, or '' when it was refused."""
    seat = holder_of(backlog, rig, packet["id"]) or admit.seat(rig)
    lease = wa.lease_of(backlog, packet["id"], seat, packet["md"])
    if not lease:
        fl.log(ME, "could not release %s: no lease for %s in the ledger" % (packet["id"], seat))
        return ""
    try:
        result = wa.client(backlog, "release", item=packet["id"], by=seat, lease=lease)
    except fl.Refused as exc:
        fl.log(ME, "could not release %s: %s" % (packet["id"], str(exc)[-200:]))
        return ""
    wa.forget(backlog)
    return str((result or {}).get("operation_id") or "released")


def release_stopped(city, policy, rigs, backlog, dry_run):
    """Release, in one commit, the claim of every stopped packet that still holds one."""
    stopped = []
    for rig in sorted(rigs):
        try:
            admit.owned_clone(rigs, rig)
        except fl.Refused:
            continue
        stopped += [(rig, p) for p in fl.packets(city, rig)
                    if p["state"] in ("declined", "failed") and not p["md"].get("proof.released")
                    and not live_unpublished(city, p)]
    if not stopped:
        return
    admit.refresh_backlog_clone(backlog)
    native = wa.beads(backlog)
    if native:
        wa.forget(backlog)
    candidates = []
    for rig, p in stopped:
        fm = item_fields(backlog, p["id"])
        if not isinstance(fm, dict) or not admit.held_by_us(fm.get("claimed_by"), rig) or fm.get("status") != "working":
            if not dry_run:                                  # already released, completed or re-claimed: theirs
                fl.set_bead(city, rig, p["bead"]["id"], proof__released="not-ours")
            continue
        if dry_run:
            candidates.append((rig, p))
            continue
        candidates.append((rig, p))
    if not candidates:
        return
    if dry_run:
        fl.log(ME, "would release the claims of stopped packets %s" % ", ".join(p["id"] for _, p in candidates))
        return
    reasons = {p["id"]: _reason(city, rig, p) for rig, p in candidates}
    if native:
        for rig, p in candidates:
            receipt = release_lease(backlog, rig, p)
            if receipt:
                fl.set_bead(city, rig, p["bead"]["id"], proof__released=receipt)
                fl.log(ME, "released %s for triage in the work ledger: %s (%s)" % (p["id"], p["state"], reasons[p["id"]]))
        return
    try:
        with fl.backlog_write(city):
            released = []
            for rig, p in candidates:
                seat = holder_of(backlog, rig, p["id"]) or admit.seat(rig)
                out = fl.sh([sys.executable, "scripts/next_work.py", "--release", p["id"], "--by", seat], cwd=backlog)
                if out.returncode == 0:
                    released.append((rig, p))
                else:
                    fl.log(ME, "could not release %s: %s" % (p["id"], (out.stderr or out.stdout).strip()[-200:]))
            if not released:
                return
            ids = [p["id"] for _, p in released]
            landed = fl.land_backlog_change(
                backlog,
                "Release %d stopped item%s from the Gas City seat for triage\n\n"
                "Released by the PROOF completer (gascity/packs/proof/assets/scripts/completer.py): the worker\n"
                "declined the packet at load-context or stopped it later, and says why on its bead and in a mail to\n"
                "`human`. The item needs triage by a person or the planner; its claim must not hold other items'\n"
                "write scopes while it waits. Unchanged, the dispatcher will not retake it.\n\n%s\n\nRefs %s."
                % (len(ids), "" if len(ids) == 1 else "s",
                   "\n".join("%s: %s (%s)" % (p["id"], p["state"], reasons[p["id"]]) for rig, p in released), ", ".join(ids)),
                policy["shared_checkout"])
    except fl.BacklogBusy:
        return
    for rig, p in released:
        fl.set_bead(city, rig, p["bead"]["id"], proof__released=landed)
    fl.log(ME, "released %s for triage on main as %s" % (", ".join(ids), landed[:10]))


def publish_live_results(city, policy, backlog, dry_run):
    """Publish each settled live-test run's checked evidence on main, once.

    Separate from pull-request completion on purpose: a live test has no pull
    request and no merge, and nothing here reads one. A refused publication is
    logged with its typed code and retried on the next pass; it never becomes
    a completion. Anything that is not a clean pass that completed its item is
    mailed once, when it lands: that record is the defect or the intervention."""
    import livetest_evidence as le
    runs, problems = le.pending(city)
    for name, code, detail in problems:
        fl.log(ME, "live run %s cannot be read (%s: %s); its evidence is not published and it needs "
               "a person" % (name, code, detail))
    for run_id in runs:
        if dry_run:
            fl.log(ME, "would publish the evidence of live run %s" % run_id)
            continue
        try:
            receipt = le.publish_run(city, backlog, run_id, shared_checkout=policy["shared_checkout"])
        except le.EvidenceRefused as why:
            fl.log(ME, "live run %s: evidence refused — %s; nothing is published or completed" % (run_id, why))
            continue
        except fl.Refused as why:
            fl.log(ME, "live run %s: publication did not land — %s; the next pass retries from the journal, "
                   "never from production" % (run_id, why))
            continue
        fl.log(ME, "live run %s (%s): %s %s as %s; item %s" % (
            run_id, receipt.item_id, receipt.report, receipt.action, receipt.commit[:10], receipt.item_effect))
        if receipt.action != "already_recorded" and (receipt.report != "pass" or receipt.item_effect != "closed"):
            fl.mail("PROOF live test %s: %s (%s)" % (receipt.item_id, receipt.report, receipt.item_effect),
                    "run     %s\nitem    %s\nreport  %s\neffect  %s\ncommit  %s\nrecords %s\n\n"
                    "Only a pass completes an item. The work note is the defect or intervention record;\n"
                    "a rerun is a new run id, admitted explicitly." % (
                        receipt.run_id, receipt.item_id, receipt.report, receipt.item_effect, receipt.commit,
                        ", ".join(receipt.paths)))


def close_triaged(city, rigs, backlog, dry_run):
    """A stopped packet whose item has since been TRIAGED — re-shaped, given a
    question to wait on, completed or superseded — is history. Close its bead so
    it stops counting against `max_declined` / `max_failed`, which exist to catch
    a factory that is failing, not a backlog that has been sorted."""
    import docs_model as dm
    native = wa.beads(backlog)
    for rig in sorted(rigs):
        try:
            admit.owned_clone(rigs, rig)
        except fl.Refused:
            continue
        for p in fl.packets(city, rig):
            md = p["md"]
            if p["state"] not in ("declined", "failed") or p["bead"].get("status") == "closed" \
                    or not md.get("proof.released") or not md.get("proof.claim_commit"):
                continue
            if native:
                row = wa.row(backlog, p["id"])
                fm = row.get("fields") if row else None
                if not isinstance(fm, dict) or fm.get("claimed_by"):
                    continue                                   # claimed again: the dispatcher's business
                why = fl.triaged_row(md, row, fl.stop_reason(city, rig, p)) if fm.get("status") not in ("done", "superseded") else ""
                edited = (row.get("semantic_digest") or "") != (md.get("proof.packet_digest") or "")
            else:
                files = glob.glob(os.path.join(backlog, "docs/backlog/items", p["id"] + "-*.md"))
                if len(files) != 1:
                    continue
                rel = os.path.relpath(files[0], backlog)
                given = fl.sh(["git", "-C", backlog, "show", "%s:%s" % (md["proof.claim_commit"], rel)])
                today = open(files[0], encoding="utf-8").read()
                fm = dm.split_frontmatter(today)[0]
                if given.returncode != 0 or not isinstance(fm, dict) or fm.get("claimed_by"):
                    continue                                   # claimed again: the dispatcher's business
                why = fl.triaged(given.stdout, today, fl.stop_reason(city, rig, p)) if fm.get("status") not in ("done", "superseded") else ""
                edited = fl.shaped(given.stdout) != fl.shaped(today)
            if fm.get("status") not in ("done", "superseded"):
                if why:
                    if edited and not dry_run:
                        # Edited, but the decline is unanswered: say so once, and keep
                        # counting it. The dispatcher skips it for the same reason.
                        notify_once(city, rig, p, "unanswered", "PROOF packet edited but not triaged: %s" % p["id"],
                                    "packet  %s\nbead    %s (rig %s)\n\n%s\n\nQuote the stop code in a `## Triage` "
                                    "section that says what changed for it; the dispatcher retakes the item then."
                                    % (p["id"], p["bead"]["id"], rig, why), key="proof.triage_notified")
                    continue                                   # still awaiting triage
            if dry_run:
                fl.log(ME, "would close bead %s: %s has been triaged" % (p["bead"]["id"], p["id"]))
                continue
            fl.set_bead(city, rig, p["bead"]["id"], proof__superseded="true")
            admit.gc(city, "bd", "--rig", rig, "close", p["bead"]["id"], "--reason",
                     "triaged: the item was re-shaped, given a question to wait on, or completed on main", check=False)
            fl.log(ME, "%s has been triaged on main (status %s); bead %s closed" % (p["id"], fm.get("status"), p["bead"]["id"]))


def adrs_delivered(backlog, item_ids, dm, today):
    """Move `delivery: none` to `partial` on every ADR whose `delivered_by` names
    an item completed in this pass, in the same commit, and return the
    (ADR number, item id) pairs changed.

    ADR-0213 decision 7 makes `delivery: none` beside a `done` delivering item a
    validation error, and its Consequences put the edit on "the closer" of the
    item. In the factory the closer is this script, so the completion commit
    would refuse itself (the first run of BKLG-20261008-8to9 declined on exactly
    this). `partial` is the one value that is consistent with any mix of done and
    open targets; `built` and `live` are judgements and stay the owner's."""
    changed = []
    for path in sorted(glob.glob(os.path.join(backlog, "docs/decisions/adr", "*.md"))):
        text = open(path, encoding="utf-8").read()
        fm = dm.split_frontmatter(text)[0]
        if not isinstance(fm, dict) or fm.get("delivery") != "none":
            continue
        named = fm.get("delivered_by") or []
        hit = next((i for i in item_ids if i in named), None) if isinstance(named, list) else None
        if hit is None:
            continue
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(dm.rewrite_frontmatter_keys(text, {"delivery": "partial", "last_reviewed": today}))
        changed.append((os.path.basename(path).split("-", 1)[0], hit))
    return changed


def finished_with(packets):
    """{work_dir: reason} for the worktrees no packet needs any more.

    A packet is finished with its worktree when it is done on main, or was stopped
    and has been triaged (superseded), or was stopped and its claim released. A
    re-admitted item can reuse its predecessor's directory — the name comes from
    the id and the title — so a directory any OTHER packet still needs is kept."""
    needed, spare = set(), {}
    for p in packets:
        work_dir = p["md"].get("work_dir") or ""
        if not work_dir:
            continue
        if p["md"].get("proof.completed"):
            spare[work_dir] = "%s is done on main" % p["id"]
        elif p.get("superseded"):
            spare.setdefault(work_dir, "%s stopped and has been triaged" % p["id"])
        elif p["state"] in ("declined", "failed") and p["md"].get("proof.released") \
                and p["bead"].get("status") == "closed":
            spare.setdefault(work_dir, "%s stopped and its claim was released" % p["id"])
        else:
            needed.add(work_dir)
    return {d: why for d, why in spare.items() if d not in needed}


def finished_recoveries(packets, beads):
    """Closed, approved recovery worktrees whose original packet is delivered.

    Keep this lifecycle with the existing completer. A newer active admission
    of the same item, an open recovery or a failed report preserves its tree.
    """
    done = {p["id"] for p in packets if p["md"].get("proof.completed")}
    needed = {p["id"] for p in packets if not p["md"].get("proof.completed") and not p.get("superseded")}
    out = {}
    for bead in beads:
        md = bead.get("metadata") or {}
        pid = md.get("gc.var.packet_id")
        if (pid not in done - needed or bead.get("status") != "closed"
                or md.get("gc.kind") != "workflow" or md.get("gc.formula_name") != "mol-proof-pr-recovery"
                or md.get("gc.outcome") != "pass"):
            continue
        work, branch, head = (md.get("gc.var.work_dir"), md.get("gc.var.branch"), md.get("proof.approved_commit"))
        if work and branch and head:
            out[work] = ("%s is delivered; recovery %s is closed" % (pid, bead["id"]), branch, head)
    return out


def tidy_worktrees(city, rigs, said, dry_run):
    """Remove the worktrees and local branches finished packets left behind.

    Completion removes its own, but a stopped packet's was never removed, and
    neither was one whose item a person completed: six of the nine worktrees in
    the liskov-rs clone belonged to packets that were over (2026-09-20). Nothing
    is removed that holds work: the tree must be clean, and every commit on it
    must be on origin/main or on a remote branch. A stopped packet's unpushed
    commit is the next attempt's starting point (`admit.prior_work`) and stays."""
    for rig in sorted(rigs):
        try:
            clone = admit.owned_clone(rigs, rig)
        except fl.Refused:
            continue
        root = os.path.join(clone, ".worktrees") + os.sep
        packets = fl.packets(city, rig, superseded=True)
        recoveries = finished_recoveries(packets, fl.rig_beads(city, rig)) if any(
            p["md"].get("proof.completed") and p["md"].get("proof.pr_recovered_root") for p in packets) else {}
        spare = finished_with(packets)
        spare.update({work: evidence[0] for work, evidence in recoveries.items()})
        for work_dir, why in sorted(spare.items()):
            if (not os.path.realpath(work_dir).startswith(os.path.realpath(root) + os.sep)
                    or not os.path.isdir(work_dir)):
                continue
            reads = [fl.sh(["git", "-C", work_dir, *args]) for args in (
                ["status", "--porcelain"], ["rev-list", "--count", "origin/main..HEAD"],
                ["branch", "-r", "--contains", "HEAD"])]
            if any(result.returncode for result in reads):
                said.say("worktree:" + work_dir, "rig %s: keep %s; ownership/reachability could not be read" % (rig, work_dir))
                continue
            dirty, ahead, pushed = [result.stdout.strip() for result in reads]
            if dirty or (ahead not in ("", "0") and not pushed):
                said.say("worktree:" + work_dir, "rig %s: %s, but %s is kept — it holds %s"
                         % (rig, why, os.path.basename(work_dir),
                            "uncommitted changes" if dirty else "%s commit(s) that are on no remote branch" % ahead))
                continue
            branch = fl.sh(["git", "-C", work_dir, "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
            if work_dir in recoveries:
                _, expected_branch, expected_head = recoveries[work_dir]
                head = fl.sh(["git", "-C", work_dir, "rev-parse", "HEAD"])
                if head.returncode or head.stdout.strip() != expected_head or branch != expected_branch:
                    said.say("worktree:" + work_dir, "rig %s: keep %s; recovery workspace identity changed" % (rig, work_dir))
                    continue
            if dry_run:
                fl.log(ME, "rig %s: would remove worktree %s (%s)" % (rig, os.path.basename(work_dir), why))
                continue
            if fl.sh(["git", "-C", clone, "worktree", "remove", work_dir]).returncode != 0:
                said.say("worktree:" + work_dir, "rig %s: %s, and git refused to remove %s"
                         % (rig, why, os.path.basename(work_dir)))
                continue
            if branch.startswith("agent/"):
                fl.sh(["git", "-C", clone, "branch", "-D", branch])    # every commit is on a remote: checked above
            fl.log(ME, "rig %s: removed worktree %s — %s" % (rig, os.path.basename(work_dir), why))


def merge_decision(rig_policy, rollup, failed, mergeable, landing):
    """May the completer merge this OPEN pull request itself? -> (may, why, complaint).

    A rig with no `merge` rule waits for a person, which is the right answer
    wherever merging costs money or deploys something nobody watched. The two
    rules that do not wait differ in what the evidence IS:

    `when-green` — the repository has pull-request CI and the policy names the
    checks that must be green. NAMED checks, never "everything reported is
    green": a pull request whose big job has not registered yet reports a
    short, green list.

    `on-gate` — the repository has no pull-request CI, so no check will ever
    report and the orchestrator's own gate, which this packet passed on the
    exact tree being merged, is the whole of the evidence. It still refuses
    while anything is pending or anything failed, so a repository that GAINS a
    check is never merged behind its back; and it refuses outright if the
    policy also names checks, because an operator who named checks meant
    `when-green`.

    `complaint` is a configuration error worth a log line, not a refusal
    reason: a refusal with nothing to say returns ("", "").
    """
    rule = rig_policy.get("merge")
    if rule not in ("when-green", "on-gate"):
        return False, "", ""
    wanted = list(rig_policy.get("merge_requires") or [])
    if rule == "on-gate" and wanted:
        return False, "", ('merge = "on-gate" with merge_requires (%s) — say which one you mean; '
                           "nothing is merged until you do" % ", ".join(wanted))
    if landing != "pr" or failed or mergeable != "MERGEABLE":
        return False, "", ""
    green = {c.get("name") or c.get("context") for c in rollup
             if (c.get("conclusion") or c.get("state") or "").lower() in ("success", "neutral", "skipped")}
    pending = [c for c in rollup if (c.get("status") or "COMPLETED").upper() != "COMPLETED"]
    if pending:
        return False, "", ""
    if rule == "when-green":
        if not wanted:
            return False, "", ('merge = "when-green" with no merge_requires: there is no check to wait for, '
                               "so nothing is merged. Name the checks, or use `on-gate` if there are none")
        if all(name in green for name in wanted):
            return True, "when-green: %s" % ", ".join(wanted), ""
        return False, "", ""
    return True, "on-gate: no pull-request CI, the orchestrator gate is the evidence", ""


def _reason(city, rig, packet):
    return fl.stop_reason(city, rig, packet) or "no reason recorded"


def completion_evidence(city, rig, p, pr, sha):
    """What the completer hands the checked completion command (ADR-0236): the
    merged pull request, its merge commit, the green main head, the tree the
    reviewer approved, and the attempt that produced it."""
    root_md = (p.get("root") or {}).get("metadata") or {}
    attempt = {"bead_id": p["bead"]["id"], "rig": rig, "factory": admit.factory_name()}
    if p.get("root", {}).get("id"):
        attempt["root"] = p["root"]["id"]
    if root_md.get("gc.outcome"):
        attempt["outcome"] = root_md["gc.outcome"]
    evidence = {"pull_request": pr["url"], "main_sha": sha, "attempts": [attempt]}
    if (pr.get("mergeCommit") or {}).get("oid"):
        evidence["merge_sha"] = pr["mergeCommit"]["oid"]
    if root_md.get("proof.approved_tree"):
        evidence["approved_tree"] = root_md["proof.approved_tree"]
    return evidence


def expected_digest(city, backlog, p):
    """The semantic digest the completion must match: the one the contract the
    attempt executed carried (a v2 run), else — a legacy run with no ledger
    binding — the row's digest at admission, else its digest now."""
    import execution_contract as ec
    identity = p["md"].get("proof.contract_id")
    if identity:
        try:
            work = ec.read(city, identity).get("work") or {}
            if work.get("semantic_digest"):
                return work["semantic_digest"]
        except fl.Refused:
            pass
    if ec.HEX.fullmatch(str(p["md"].get("proof.packet_digest") or "")):
        return p["md"]["proof.packet_digest"]
    row = wa.row(backlog, p["id"])
    return str((row or {}).get("semantic_digest") or "")


def complete_in_ledger(city, policy, rigs, backlog, ready, dm, today):
    """Complete each merged, green packet through the checked completion
    command (Beads). The item's own state, the lease, the digest and the
    epic's last-child rule are the helper's to judge; this side supplies the
    evidence, catches the last-child refusal and holds that item for a person
    exactly as the Git path does, and lands the ADR delivery bookkeeping as a
    docs change — never a work record."""
    wa.forget(backlog)
    done, not_ours = [], []
    for rig, p, pr, sha in ready:
        fm = item_fields(backlog, p["id"])
        if not isinstance(fm, dict) or fm.get("status") != "working" or not admit.held_by_us(fm.get("claimed_by"), rig):
            fl.log(ME, "%s is not `working` for this seat in the work ledger; leaving the item alone" % p["id"])
            not_ours.append((rig, p))
            continue
        seat = fm["claimed_by"]                      # the holder: this factory's seat, or an older two-part one
        lease = wa.lease_of(backlog, p["id"], seat, p["md"])
        if not lease:
            fl.log(ME, "%s: no lease for %s in the ledger; not completed" % (p["id"], seat))
            continue
        request = {"expected_semantic_digest": expected_digest(city, backlog, p), "resolution": "done",
                   "evidence": completion_evidence(city, rig, p, pr, sha),
                   "notes": "Completed by the PROOF completer: %s merged as %s, main green at %s"
                            % (pr["url"], (pr.get("mergeCommit") or {}).get("oid", "?")[:10], sha[:10])}
        try:
            result = wa.client(backlog, "complete", item=p["id"], by=seat, lease=lease, request=request)
        except fl.Refused as exc:
            if wa.is_last_child_refusal(exc):
                epic = fm.get("parent_epic") or "its epic"
                notify_once(city, rig, p, "last-child", "PROOF packet merged; closing its epic needs you: %s" % p["id"],
                            "packet  %s\nbead    %s (rig %s)\npr      %s\nmain    green at %s\n\n"
                            "It is the last active item of %s. The work ledger refuses a factory seat that\n"
                            "completion: whether an epic's completion criteria are met is a judgement, not a\n"
                            "script's. Either complete the item with close_epic as a person, or file the epic's\n"
                            "next item first — the completer then finishes this one.\n"
                            "Until then the item stays `working` and its lease holds its write scope."
                            % (p["id"], p["bead"]["id"], rig, pr["url"], sha[:10], epic))
                fl.log(ME, "%s is the last active item of %s; held for a person" % (p["id"], epic))
            else:
                fl.log(ME, "%s: completion refused by the work ledger — %s" % (p["id"], str(exc)[-300:]))
            continue
        wa.forget(backlog)
        done.append((rig, p, pr, sha, result or {}))
    for rig, p in not_ours:
        fl.set_bead(city, rig, p["bead"]["id"], proof__completed="not-ours")
    if not done:
        return 0
    ids = [p["id"] for _, p, _, _, _ in done]
    delivered = adrs_delivered(backlog, ids, dm, today)
    if delivered:
        try:
            with fl.backlog_write(city):
                landed = fl.land_backlog_change(
                    backlog,
                    "Record ADR delivery for %s\n\n"
                    "Recorded by the PROOF completer (gascity/packs/proof/assets/scripts/completer.py) after the\n"
                    "work ledger completed %s. A docs change, not a work record: the items themselves\n"
                    "live in the ledger.\n%s\nRefs ADR-0236, %s."
                    % (", ".join(ids), "it" if len(ids) == 1 else "them",
                       "".join("%s: delivery none -> partial (delivered_by names %s)\n" % pair for pair in delivered),
                       ", ".join(ids + ["ADR-" + number for number, _ in delivered])),
                    policy["shared_checkout"])
            fl.log(ME, "ADR delivery recorded on main as %s for %s" % (landed[:10], ", ".join(ids)))
        except (fl.Refused, fl.BacklogBusy) as why:
            fl.log(ME, "ADR delivery bookkeeping for %s did not land — %s; the next pass retries"
                   % (", ".join(ids), why))
    for rig, p, pr, sha, result in done:
        receipt = str(result.get("operation_id") or result.get("ledger_revision") or "ledger")
        fl.set_bead(city, rig, p["bead"]["id"], proof__completed=receipt,
                    proof__merge_commit=(pr.get("mergeCommit") or {}).get("oid", ""), proof__main_green=sha)
        tidy_packet(city, rigs, backlog, rig, p)
        fl.log(ME, "%s done in the work ledger (operation %s); worktree and branch %s"
               % (p["id"], receipt[:12], p["md"].get("proof.tidy", "see log")))
    return 0


def tidy_packet(city, rigs, backlog, rig, p):
    """Remove a completed packet's merged worktree and branch; git refuses on
    its own if anything in them is unmerged. Records the outcome on
    `p["md"]["proof.tidy"]` for the log line."""
    clone, work_dir, branch = admit.owned_clone(rigs, rig), p["md"].get("work_dir") or "", p["md"].get("proof.branch") or ""
    tidy = "left in place"
    if work_dir.startswith(os.path.join(clone, ".worktrees") + os.sep):
        fl.sh(["git", "-C", clone, "fetch", "--quiet", "--prune", "origin"])
        can_tidy = True
        if os.path.realpath(clone) == os.path.realpath(backlog):
            try:
                with fl.backlog_write(city):
                    fl.sh(["git", "-C", clone, "merge", "--quiet", "--ff-only", "origin/main"])
            except fl.BacklogBusy:
                can_tidy = False
                tidy = "left in place (backlog writer busy)"
        else:
            fl.sh(["git", "-C", clone, "merge", "--quiet", "--ff-only", "origin/main"])
        if can_tidy:
            gone = fl.sh(["git", "-C", clone, "worktree", "remove", work_dir]).returncode == 0
            gone = gone and fl.sh(["git", "-C", clone, "branch", "-d", branch]).returncode == 0
            tidy = "removed" if gone else "left in place (git refused)"
    p["md"]["proof.tidy"] = tidy
    return tidy


def main(argv):
    """Complete what has merged, then tidy — the tidy runs whichever way the
    completions returned, and never decides whether they succeeded."""
    dry_run = "--dry-run" in argv
    city = fl.city_dir()
    said = fl.Standing(city, ME, keep=not dry_run)
    try:
        status = complete(argv, city, dry_run)
        try:
            tidy_worktrees(city, admit.load_rigs(city), said, dry_run)
        except fl.Refused as why:
            fl.log(ME, "worktree tidy did not run — %s" % why)
        return status
    finally:
        said.close()


def record_merged(backlog, rig, p, pr, city, dry_run):
    """Tell the work ledger the packet's pull request merged, once.

    `merged:<sha>` as an evidence entry on the working item: the claim stays
    (done waits for a green main, ADR-0115) but the policy's `blocks_scope`
    stops it excluding other packets from its write scope the moment the code
    is on main (BKLG-20261016-dk56). The bead stamp `proof.merged` keeps the
    entry from being sent again; a refusal is logged and the pass goes on."""
    sha = (pr.get("mergeCommit") or {}).get("oid", "")
    if not sha or p["md"].get("proof.merged") == sha or dry_run or not wa.beads(backlog):
        return
    seat = holder_of(backlog, rig, p["id"]) or admit.seat(rig)
    try:
        wa.client(backlog, "evidence", item=p["id"], by=seat, request={"entries": ["merged:" + sha]})
    except fl.Refused as why:
        fl.log(ME, "%s: the ledger refused the merged evidence — %s" % (p["id"], str(why)[-200:]))
        return
    wa.forget(backlog)
    fl.set_bead(city, rig, p["bead"]["id"], proof__merged=sha)
    fl.log(ME, "%s: merged as %s recorded in the work ledger; its claim no longer excludes" % (p["id"], sha[:10]))


def complete(argv, city, dry_run):
    policy = fl.load_policy(city)
    rigs = admit.load_rigs(city)
    backlog = admit.backlog_clone_of(rigs)
    sys.path.insert(0, os.path.join(backlog, "scripts"))
    if wa.frozen(backlog):
        # A write freeze on the work authority: nothing is completed, renewed
        # or released this pass. Reads and the worktree tidy still run.
        fl.log(ME, "pass skipped: %s" % wa.FREEZE_REASON)
        return 0
    try:
        release_stopped(city, policy, rigs, backlog, dry_run)
        admit.refresh_backlog_clone(backlog)
        close_triaged(city, rigs, backlog, dry_run)
    except fl.Refused as why:                               # never let a release stop the completions
        fl.log(ME, "release of stopped claims did not land — %s" % why)
    try:
        publish_live_results(city, policy, backlog, dry_run)
    except Exception as why:                                # logged, typed: never stops the completions
        fl.log(ME, "live-result publication did not run — %s: %s" % (type(why).__name__, why))
    ready = []                                              # (rig, packet, pr, green sha)
    waiting = []                                            # (rig, packet, pr) whose pull request is open

    for rig in sorted(rigs):
        try:
            clone = admit.owned_clone(rigs, rig)
        except fl.Refused:
            continue
        url = fl.sh(["git", "-C", clone, "remote", "get-url", "origin"]).stdout.strip()
        slug = url.split("github.com", 1)[-1].lstrip(":/").removesuffix(".git")
        for p in fl.packets(city, rig):
            md = p["md"]
            if p["state"] != "passed" or md.get("proof.landing") not in ("pr", "automerge") or md.get("proof.completed"):
                continue
            prs = gh_json(["pr", "list", "--repo", slug, "--head", md.get("proof.branch", ""), "--state", "all",
                           "--json", "number,state,url,createdAt,mergedAt,mergeCommit,mergeable,statusCheckRollup,headRefOid"],
                           clone)
            if not prs:
                continue
            pr = sorted(prs, key=lambda x: x["number"])[-1]
            record_pr_url(city, rig, p, pr, dry_run)
            facts = "packet  %s\nbead    %s (rig %s)\npr      %s" % (p["id"], p["bead"]["id"], rig, pr["url"])
            if pr["state"] == "MERGED":
                if md.get("proof.pr_recovery_state") in ("launching", "running", "escalated"):
                    # Publication still owns its handoff; do not remove the
                    # original worktree between push and its receipt.
                    continue
                record_merged(backlog, rig, p, pr, city, dry_run)
                verdict, sha, detail = main_verdict(clone, slug, pr["mergeCommit"]["oid"], pr["mergedAt"],
                                                    policy["rigs"].get(rig), p, pr)
                fl.log(ME, "%s: merged as %s; main %s at %s (%s)"
                       % (p["id"], pr["mergeCommit"]["oid"][:10], verdict, sha[:10], detail))
                if verdict == "green":
                    ready.append((rig, p, pr, sha))
                elif verdict == "red":
                    notify_once(city, rig, p, "red-main", "PROOF packet merged, main is RED: %s" % p["id"],
                                facts + "\nmain    %s failed: %s\n\nThe item stays `working`. A red main belongs to the "
                                "merge that turned it red: fix forward or revert under the repository's rules. The "
                                "completer marks the item done if a later head goes green." % (sha, detail))
            elif pr["state"] == "CLOSED":
                notify_once(city, rig, p, "closed", "PROOF pull request closed unmerged: %s" % p["id"],
                            facts + "\n\nThe item stays `working` under the Gas City seat until someone releases it.")
            else:
                waiting.append((rig, p, pr))
                rollup = pr.get("statusCheckRollup") or []
                failed = sorted({c.get("name") or c.get("context") or "?" for c in rollup
                                 if (c.get("conclusion") or c.get("state") or "").lower() in BAD | {"error"}})
                rig_policy = policy["rigs"].get(rig) or {}
                may, why, complaint = merge_decision(rig_policy, rollup, failed, pr.get("mergeable"),
                                                     md.get("proof.landing"))
                if complaint:
                    fl.log(ME, "%s: rig %s — %s" % (p["id"], rig, complaint))
                if may:
                    if dry_run:
                        fl.log(ME, "%s: would merge %s (%s)" % (p["id"], pr["url"], why))
                    else:
                        merged = fl.sh(["gh", "pr", "merge", str(pr["number"]), "--repo", slug, "--merge"], cwd=clone)
                        fl.log(ME, "%s: merged %s (%s): %s" % (
                            p["id"], pr["url"], why,
                            "ok" if merged.returncode == 0 else (merged.stderr or merged.stdout).strip()[-200:]))
                    continue
                if pr.get("mergeable") == "CONFLICTING" or failed:
                    notify_once(city, rig, p, "stuck", "PROOF pull request cannot merge: %s" % p["id"],
                                facts + "\n%s\n\nAuto-merge will not fire until this is resolved."
                                % ("conflicts with main" if pr.get("mergeable") == "CONFLICTING"
                                   else "failed checks: " + ", ".join(failed)))

    try:
        renew_open(city, policy, backlog, waiting, dry_run)
    except fl.Refused as why:                               # never let a renewal stop the completions
        fl.log(ME, "renewal of open pull requests' claims did not land — %s" % why)

    if not ready:
        return 0
    if dry_run:
        fl.log(ME, "would complete %s" % ", ".join(p["id"] for _, p, _, _ in ready))
        return 0

    import docs_model as dm
    admit.refresh_backlog_clone(backlog)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if wa.beads(backlog):
        return complete_in_ledger(city, policy, rigs, backlog, ready, dm, today)

    # Completing the LAST active item of an epic leaves an active epic with no
    # active child, which the docs validator refuses — and closing an epic is a
    # judgement ("its completion criteria are met"), not something a script may
    # make. Hold that item for a person, once, and complete the rest: the batch is
    # one commit, so one such item would otherwise refuse every completion of
    # every pass. (First seen 2026-09-20: `afy5`, the last item of EPIC-…-p20m.)
    corpus = dm.Corpus.load(dm.DOCS_DIR)
    finishing = {p["id"] for _, p, _, _ in ready}
    held = []
    for entry in list(ready):
        rig, p, pr, sha = entry
        record = corpus.by_id.get(p["id"])
        # Only an item that is still OURS to complete can be held: one a person has
        # already completed falls through to the `not-ours` branch below, instead of
        # being announced as "held" on every pass for ever.
        if record is None or record.status != "working" or not admit.held_by_us(record.fm.get("claimed_by"), rig):
            continue
        epic = record.fm.get("parent_epic")
        if epic and not any(dm.record_id(other) not in finishing and other.fm.get("parent_epic") == epic
                            and other.status in dm.ACTIVE_STATUSES for other in corpus.items):
            held.append(entry)
    for entry in held:
        rig, p, pr, sha = entry
        ready.remove(entry)
        epic = corpus.by_id[p["id"]].fm.get("parent_epic")
        notify_once(city, rig, p, "last-child", "PROOF packet merged; closing its epic needs you: %s" % p["id"],
                    "packet  %s\nbead    %s (rig %s)\npr      %s\nmain    green at %s\n\n"
                    "It is the last active item of %s. Marking it done leaves that epic active with no\n"
                    "active child, which the docs validator refuses, and whether an epic's completion\n"
                    "criteria are met is a judgement, not a script's. Either mark the item AND the epic done\n"
                    "in one commit, or file the epic's next item first — the completer then finishes this one.\n"
                    "Until then the item stays `working` and its claim holds its write scope."
                    % (p["id"], p["bead"]["id"], rig, pr["url"], sha[:10], epic))
        fl.log(ME, "%s is the last active item of %s; held for a person" % (p["id"], epic))

    if not ready:
        return 0
    not_ours = []
    try:
        with fl.backlog_write(city):
            done = []
            for rig, p, pr, sha in ready:
                files = glob.glob(os.path.join(backlog, "docs/backlog/items", p["id"] + "-*.md"))
                text = open(files[0], encoding="utf-8").read() if len(files) == 1 else ""
                fm = dm.split_frontmatter(text)[0] if text else None
                if not isinstance(fm, dict) or fm.get("status") != "working" \
                        or not admit.held_by_us(fm.get("claimed_by"), rig):
                    # Someone else already completed, released or re-claimed it. Theirs.
                    fl.log(ME, "%s is not `working` for this seat on main; leaving the item alone" % p["id"])
                    not_ours.append((rig, p))
                    continue
                evidence = [e for e in (fm.get("evidence") or []) if isinstance(e, str)]
                evidence += [e for e in (pr["url"],) if e not in evidence]
                with open(files[0], "w", encoding="utf-8") as fh:
                    fh.write(dm.rewrite_frontmatter_keys(
                        text, {"status": "done", "last_reviewed": today, "done_at": today, "evidence": evidence},
                        remove=("claimed_by", "claimed_at", "claim_expires")))
                done.append((rig, p, pr, sha))
            if done:
                ids = [p["id"] for _, p, _, _ in done]
                delivered = adrs_delivered(backlog, ids, dm, today)
                landed = fl.land_backlog_change(
                    backlog,
                    "Complete %s: merged, and main is green with %s\n\n"
                    "Recorded by the PROOF completer (gascity/packs/proof/assets/scripts/completer.py).\n%s\n%s\n"
                    "Refs %s." % (", ".join(ids), "it" if len(ids) == 1 else "them",
                                  "\n".join("%s: %s, main green at %s" % (p["id"], pr["url"], sha[:10]) for _, p, pr, sha in done),
                                  "".join("%s: delivery none -> partial (delivered_by names %s)\n" % pair for pair in delivered),
                                  ", ".join(ids + ["ADR-" + number for number, _ in delivered])),
                    policy["shared_checkout"])
    except fl.BacklogBusy:
        return 0
    for rig, p in not_ours:
        fl.set_bead(city, rig, p["bead"]["id"], proof__completed="not-ours")
    if not done:
        return 0
    for rig, p, pr, sha in done:
        fl.set_bead(city, rig, p["bead"]["id"], proof__completed=landed, proof__merge_commit=pr["mergeCommit"]["oid"],
                    proof__main_green=sha)
        clone, work_dir, branch = admit.owned_clone(rigs, rig), p["md"].get("work_dir") or "", p["md"].get("proof.branch") or ""
        tidy = "left in place"
        if work_dir.startswith(os.path.join(clone, ".worktrees") + os.sep):
            fl.sh(["git", "-C", clone, "fetch", "--quiet", "--prune", "origin"])
            can_tidy = True
            if os.path.realpath(clone) == os.path.realpath(backlog):
                try:
                    with fl.backlog_write(city):
                        fl.sh(["git", "-C", clone, "merge", "--quiet", "--ff-only", "origin/main"])
                except fl.BacklogBusy:
                    can_tidy = False
                    tidy = "left in place (backlog writer busy)"
            else:
                fl.sh(["git", "-C", clone, "merge", "--quiet", "--ff-only", "origin/main"])
            if can_tidy:
                gone = fl.sh(["git", "-C", clone, "worktree", "remove", work_dir]).returncode == 0
                gone = gone and fl.sh(["git", "-C", clone, "branch", "-d", branch]).returncode == 0
                tidy = "removed" if gone else "left in place (git refused)"
        fl.log(ME, "%s done on main as %s; worktree and branch %s" % (p["id"], landed[:10], tidy))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except fl.Refused as why:
        fl.log(ME, "stopped — %s" % why)
        sys.exit(1)
