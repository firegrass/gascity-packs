#!/usr/bin/env python3
"""Launch at most one bounded planner run for released stops or named requests.

The dispatcher independently picks up ready implementation packets. Queue depth
never starts planning. Each run records up to three exact assignments on its
work bead and carries those ids into the orchestrator's landing check.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone

import planner_assignments as assignments
import stall_recovery
import factory_health
import factory_lib as fl
from factory_lib import admit

ME = "plan-trigger"
PLANNER = "proof.planner"
FORMULA = "mol-proof-plan"


class _Tee:
    """Write to both. `factory.sh` redirects the completer and the dispatcher
    into `<city>/proof/factory.log`; this trigger is its own exec order, so it
    puts itself there — one log is what an operator reads — while still printing
    to the order's own output."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            stream.write(text)

    def flush(self):
        for stream in self.streams:
            stream.flush()


def log_to_factory(city):
    path = os.path.join(city, "proof", "factory.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        sys.stdout = _Tee(sys.stdout, open(path, "a", encoding="utf-8"))
    except OSError as why:        # a log that cannot be opened must not stop a pass
        fl.log(ME, "cannot append to %s (%s); logging to this order only" % (path, why))


LOCK_WAIT = 240


def take_lock(lock, wait=LOCK_WAIT, pause=5, sleep=None):
    """The factory lock, waiting up to `wait` seconds for a writer that holds it."""
    import time
    sleep = sleep or time.sleep
    waited = 0
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if waited >= wait:
                return False
            sleep(pause)
            waited += pause


def inherited_lock(city, fd):
    """Share the factory's open-file-description lock instead of competing for it."""
    try:
        expected = os.stat(os.path.join(city, 'proof', 'factory.lock'))
        actual = os.fstat(fd)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError('descriptor does not name this city factory.lock')
        lock = os.fdopen(os.dup(fd), 'w')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            lock.close()
            raise
        return lock
    except (OSError, ValueError) as exc:
        raise fl.Refused('invalid inherited factory lock: ' + str(exc)) from exc


def home_rig_of(rigs, backlog_clone):
    """The rig whose owned clone holds the backlog — where the planner runs."""
    for rig in sorted(rigs):
        try:
            if os.path.realpath(admit.owned_clone(rigs, rig)) == os.path.realpath(backlog_clone):
                return rig
        except fl.Refused:
            continue
    raise fl.Refused("no rig in this city owns the clone %s" % backlog_clone)


def plan_beads(city, home_rig):
    out = admit.gc(city, "bd", "--rig", home_rig, "list", "--all", "--json", "--limit=0")
    try:
        beads = json.loads(out.stdout)
        if not isinstance(beads, list):
            raise ValueError("expected a list")
    except ValueError as exc:
        raise fl.Refused("cannot read planner assignment history: %s" % exc)
    return [b for b in beads if (b.get("metadata") or {}).get("proof.plan_for")]


def open_run_for(beads, target_rig):
    """The open planning bead for a rig, if one exists. A run that stopped leaves
    its bead open on purpose: a person reads it before another run starts."""
    return next((b for b in beads
                 if (b.get("metadata") or {}).get("proof.plan_for") == target_rig
                 and b.get("status") != "closed"), None)


def ready_worktree(backlog_clone, work_dir, branch, dry_run):
    """The rig's planner worktree, fit for a new run: present, clean, on its own
    branch and level with origin/main.

    It is reused across runs — nothing else ever checks it out — but it is never
    cleaned: commits an earlier run left unlanded are work, and this script will
    not throw them away to make room. It says so and the run is skipped."""
    if not os.path.isdir(work_dir):
        if dry_run:
            return True
        admit.run([os.path.join(backlog_clone, "scripts", "new-agent-worktree.sh"),
                   "--repo", backlog_clone, "--name", os.path.basename(work_dir), "--branch", branch])
        return os.path.isdir(work_dir)
    dirty = admit.run(["git", "-C", work_dir, "status", "--porcelain"], check=False).stdout.strip()
    if dirty:
        fl.log(ME, "planner worktree %s has uncommitted changes; no run is started" % work_dir)
        return False
    on = admit.run(["git", "-C", work_dir, "rev-parse", "--abbrev-ref", "HEAD"], check=False).stdout.strip()
    if on != branch:
        fl.log(ME, "planner worktree %s is on %r, expected %s; no run is started" % (work_dir, on, branch))
        return False
    if dry_run:
        return True
    admit.run(["git", "-C", work_dir, "fetch", "--quiet", "--prune", "origin"], check=False)
    ahead = admit.run(["git", "-C", work_dir, "rev-list", "--count", "origin/main..HEAD"], check=False).stdout.strip()
    if ahead not in ("", "0"):
        fl.log(ME, "planner worktree %s carries %s commit(s) that never landed; no run is started" % (work_dir, ahead))
        return False
    forward = admit.run(["git", "-C", work_dir, "merge", "--quiet", "--ff-only", "origin/main"], check=False)
    if forward.returncode != 0:
        fl.log(ME, "planner worktree %s could not be fast-forwarded to origin/main; no run is started" % work_dir)
        return False
    return True


def readable_repos(rigs_dir, shared_checkout):
    """Where a planning run may READ each member repository: the Gas City-owned
    clone where a rig has one, else the workspace's shared checkout — through
    git at origin/main only, because other sessions work in that checkout. The
    first run could not confirm an item about a repository with no rig: "that
    repo is not checked out here"."""
    owned = sorted(n for n in (os.listdir(rigs_dir) if os.path.isdir(rigs_dir) else [])
                   if os.path.isdir(os.path.join(rigs_dir, n, ".git")))
    members = os.path.join(shared_checkout, "repos") if shared_checkout else ""
    shared = sorted(n for n in (os.listdir(members) if os.path.isdir(members) else [])
                    if n not in owned and os.path.exists(os.path.join(members, n, ".git")))
    if not owned and not shared:
        return ""
    lines = ["\n\nRead-only repository locations for the assigned work:\n"]
    lines += ["- `%s` — owned clone: `%s`" % (n, os.path.join(rigs_dir, n)) for n in owned]
    lines += ["- `%s` — SHARED checkout, git at `origin/main` only, never its working tree: `%s`"
              % (n, os.path.join(members, n)) for n in shared]
    return "\n".join(lines)


def bead_body(target_rig, repo, work_dir, branch, selected, readable=""):
    return (
        "Bounded planning for %s: Work only on these assigned items in `%s` on `%s`.\n\n"
        "Repository: `%s`. Read the assigned stopped workflow's evidence, then the item and its reads.\n"
        "No queue scan, no new backlog items, no implementation. Return substantial decomposition or\n"
        "undecided product/contract behaviour as an owner question linked from the assigned item.\n\n"
        "%s\n\nThe run's assigned_items variable is enforced at landing. Read-only context from other\n"
        "repositories does not expand the assignment. Never edit a claimed or working item."
        % (target_rig, work_dir, branch, repo,
           "\n".join("- %s: %s; source %s; recovery %s" % (row["id"], row["kind"], json.dumps(row["source"]),
                    json.dumps(row.get('recovery') or {})) for row in selected))) + readable


def start_run(city, home_rig, target_rig, repo, work_dir, branch, selected, dry_run, readable=""):
    ids = ",".join(row["id"] for row in selected)
    assignments.assigned_ids(ids)
    if dry_run:
        fl.log(ME, "rig %s: would plan assigned items %s in %s" % (target_rig, ids, work_dir))
        return ""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(bead_body(target_rig, repo, work_dir, branch, selected, readable))
    meta = {"proof.plan_for": target_rig, "proof.plan_repo": repo, "proof.branch": branch,
            "proof.plan_started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "proof.plan_assignments": json.dumps(selected), "work_dir": work_dir}
    try:
        out = admit.gc(city, "bd", "--rig", home_rig, "create", "Bounded planning for " + target_rig,
                       "--body-file", fh.name, "--metadata", json.dumps(meta), "--json")
    finally:
        os.unlink(fh.name)
    created = json.loads(out.stdout)
    bead_id = (created[0] if isinstance(created, list) else created)["id"]
    # Creation records the attempt before sling. A failed sling leaves an open
    # bead for operator recovery, never a duplicate run on the next timer tick.
    sling = ["sling", "%s/%s" % (home_rig, PLANNER), bead_id, "--on", FORMULA,
             "--var", "target_rig=" + target_rig, "--var", "repo=" + repo,
             "--var", "work_dir=" + work_dir, "--var", "branch=" + branch,
             "--var", "assigned_items=" + ids, "--var", "land_plan=true"]
    slung = admit.gc(city, *sling)
    fl.log(ME, "rig %s: planning run %s assigned %s" % (target_rig, bead_id, ids))
    match = re.search(r"workflow (\S+)", slung.stdout or "")
    if match:
        admit.gc(city, "bd", "--rig", home_rig, "update", bead_id,
                 "--set-metadata", "proof.plan_root=" + match.group(1))
        admit.gc(city, "bd", "--rig", home_rig, "update", match.group(1),
                 "--set-metadata", "gc.work_dir=" + work_dir,
                 "--set-metadata", "gc.work_branch=" + branch)
    return bead_id


def main(argv):
    dry_run = "--dry-run" in argv
    city = fl.city_dir()
    log_to_factory(city)
    policy = fl.load_policy(city)
    if not assignments.enabled(policy) or (not policy.get("enabled") and not dry_run):
        fl.log(ME, "planner off; dispatcher eligibility is independent")
        return 0

    # Exclude the factory's inline trigger from the separate planner order:
    # both prepare the same planner worktree and must decide on one run.
    # This lock is independent of the shared backlog writer's factory.lock.
    os.makedirs(os.path.join(city, "proof"), exist_ok=True)
    with open(os.path.join(city, "proof", "planner-trigger.lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fl.log(ME, "another planner trigger is running")
            return 0
        factory_health.record(city, "planner-trigger-dry-run" if dry_run else "planner-trigger", "start")
        try:
            result = locked_plan(city, policy, dry_run)
        except BaseException:
            factory_health.record(city, "planner-trigger-dry-run" if dry_run else "planner-trigger", "finished", 1)
            raise
        else:
            factory_health.record(city, "planner-trigger-dry-run" if dry_run else "planner-trigger", "finished", 0)
            return result


def locked_plan(city, policy, dry_run):
    rigs = admit.load_rigs(city)
    states = fl.rig_states(city)
    backlog = admit.backlog_clone_of(rigs)
    home_rig = home_rig_of(rigs, backlog)
    if states.get(home_rig, {}).get("suspended") and not dry_run:
        fl.log(ME, "rig %s is suspended; the planner cannot run there" % home_rig)
        return 0

    factory_health.record(city, "planner-trigger-dry-run" if dry_run else "planner-trigger", "refresh-backlog")
    admit.refresh_backlog_clone(backlog)
    beads = plan_beads(city, home_rig)

    said = fl.Standing(city, ME, keep=not dry_run)
    try:
        factory_health.record(city, "planner-trigger-dry-run" if dry_run else "planner-trigger", "assess-assignments")
        plan(city, policy, rigs, states, backlog, home_rig, beads, said, dry_run)
    finally:
        said.close()
    return 0


def plan(city, policy, rigs, states, backlog, home_rig, beads, said, dry_run):
    why = fl.limit_hold(said.state)
    if why:
        said.say("limit-hold", "no planning run: %s" % why)
        return
    why = fl.budget_hold(policy, rigs)
    if why:
        said.say("budget", "no planning run: " + why)
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    memories = said.section("planner")
    running = [r for r in fl.planning_runs(city, home_rig) if r["state"] == "running"]
    if running:
        said.say("running", "planning run %s in progress; one run at a time" % running[0]["bead"]["id"])
        return
    assignments.attempted(beads)  # validate receipts before selecting any rig
    order = sorted(policy["rigs"].items(), key=lambda kv: ((memories.get(kv[0]) or {}).get("started") or "", kv[0]))
    for target_rig, cfg in order:
        if target_rig not in fl.active_rigs(rigs, states, policy) or cfg["landing"] not in admit.LANDING:
            continue
        open_run = open_run_for(beads, target_rig)
        if open_run:
            said.say("verdict:" + target_rig, "rig %s: planning bead %s open; inspect before another run" % (target_rig, open_run["id"]))
            continue
        recovery = stall_recovery.settings(policy)
        require_recovery = recovery['enabled'] and target_rig in recovery['rigs']
        recovery_beads = fl.rig_beads(city, target_rig, fresh=True) if require_recovery else None
        packets = fl.packets_of(recovery_beads) if require_recovery else fl.packets(city, target_rig)
        pending = assignments.candidates(target_rig, cfg, packets, beads, policy.get("planner_auto_triage", False),
                                         require_recovery=require_recovery, recovery_beads=recovery_beads,
                                         recovery_cfg=recovery)
        selected = []
        for row in pending:
            try:
                path, fm, body, dm = admit.load_item(backlog, row["id"])
                if fm.get("claimed_by") or fm.get("status") != "open":
                    said.say("item:" + row["id"], "%s: held or not open; planner leaves it alone" % row["id"])
                    continue
                if row["id"] in cfg["exclude"]:
                    continue
                selected.append(row)
                if len(selected) == assignments.MAX_BATCH:
                    break
            except fl.Refused as exc:
                said.say("item:" + row["id"], str(exc))
        if not selected:
            said.say("verdict:" + target_rig, "rig %s: no new released stops or unattempted named requests; no planning run" % target_rig)
            continue
        try:
            clone = admit.healthy_clone(rigs, target_rig)
        except fl.Refused as exc:
            said.say("health:" + target_rig, str(exc))
            continue
        repo = admit.repo_name(clone)
        name = "proof-planner-%s" % target_rig
        work_dir, branch = os.path.join(backlog, ".worktrees", name), "agent/" + name
        if not ready_worktree(backlog, work_dir, branch, dry_run):
            continue
        readable = readable_repos(os.path.dirname(backlog), policy.get("shared_checkout") or "")
        said.say("verdict:" + target_rig, "rig %s: assigned %s" % (target_rig, ", ".join(row["id"] for row in selected)))
        bead_id = start_run(city, home_rig, target_rig, repo, work_dir, branch, selected, dry_run, readable)
        if bead_id:
            memories[target_rig] = {"run": bead_id, "started": now, "assigned_items": [row["id"] for row in selected]}
        return  # one city-wide run, including in dry-run


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except fl.Refused as why:
        fl.log(ME, "stopped — %s" % why)
        sys.exit(1)
