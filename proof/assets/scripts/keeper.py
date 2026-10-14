#!/usr/bin/env python3
"""PROOF keeper: one pass over every admitted packet, notifying a human once per
state change. Run by the `proof-keeper` exec order; safe to run by hand.

States it reports, each at most once per work bead (recorded as
`proof.notified` on the bead):

  done      the workflow passed and the work bead closed — the change exists at
            the recorded branch/commit, and the backlog item is STILL `working`:
            landing and completion stay on the workspace's existing path.
  failed    the workflow root closed with gc.outcome=fail and the work bead is
            still open — a gate or review stopped it, which is the system
            working; a person decides what happens next.
  expiring  the backlog claim has under an hour left while the packet is open.

It reads the backlog from the Gas City-owned orchestrator clone after a
fast-forward, never from the shared checkout.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

BACKLOG_REPO = "liskov-agent-orchestrator"
RECIPIENT = os.environ.get("PROOF_KEEPER_RECIPIENT", "human")
EXPIRY_WARNING = timedelta(hours=1)
RIG_PATHS = {}
RIG_USAGE = {}


def sh(argv, cwd=None):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)


def gc_json(*args, default):
    out = sh(["gc", *args])
    if out.returncode != 0:
        return default
    try:
        return json.loads(out.stdout or "null") or default
    except ValueError:
        return default


def claim_expiry(backlog_clone, item_id):
    """When the item's claim ends: from its file under Git, from the client
    snapshot's row under Beads (ADR-0236) — never a Markdown read then."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import work_authority as wa
    try:
        native = wa.beads(backlog_clone)
    except Exception as why:  # noqa: BLE001 - an unreadable marker skips the check, never guesses
        print("keeper: %s; skipping claim-expiry checks" % why, file=sys.stderr)
        return None
    if native:
        try:
            row = wa.row(backlog_clone, item_id)
        except Exception as why:  # noqa: BLE001
            print("keeper: cannot read the work ledger: %s" % why, file=sys.stderr)
            return None
        fm = (row or {}).get("fields") or {}
    else:
        sys.path.insert(0, os.path.join(backlog_clone, "scripts"))
        try:
            import docs_model as dm
        except Exception:
            return None
        files = glob.glob(os.path.join(backlog_clone, "docs/backlog/items", item_id + "-*.md"))
        if len(files) != 1:
            return None
        fm, _body, _problems = dm.split_frontmatter(open(files[0], encoding="utf-8").read())
    if not isinstance(fm, dict) or not fm.get("claim_expires"):
        return None
    stamp = datetime.fromisoformat(str(fm["claim_expires"]).strip().replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def renew_lease(backlog_clone, rig, bead, pid):
    """Under the Beads authority the keeper renews a running packet's lease
    itself, through the client, for the policy's `claim_hours`; a refusal, or
    a write freeze, leaves the expiring notice to go out as before. Returns
    the renewal result, or None when nothing was renewed."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import factory_lib as fl
    import work_authority as wa
    if not wa.beads(backlog_clone) or wa.frozen(backlog_clone):
        return None
    seat = "gascity/" + rig
    lease = wa.lease_of(backlog_clone, pid, seat, bead.get("metadata") or {})
    if not lease:
        return None
    try:
        hours = fl.load_policy(fl.city_dir())["claim_hours"]
    except Exception:  # noqa: BLE001 - no policy, the default claim length
        hours = fl.POLICY_DEFAULTS["claim_hours"]
    try:
        result = wa.client(backlog_clone, "renew", item=pid, by=seat, lease=lease, hours=hours)
    except Exception as why:  # noqa: BLE001 - a refused renewal is reported, never retried blindly
        print("keeper: could not renew %s: %s" % (pid, str(why)[-200:]), file=sys.stderr)
        return None
    wa.forget(backlog_clone)
    new_lease = str((result or {}).get("lease_id") or lease)
    if new_lease != (bead.get("metadata") or {}).get(wa.LEASE_KEY):
        sh(["gc", "bd", "--rig", rig, "update", bead["id"], "--set-metadata", "%s=%s" % (wa.LEASE_KEY, new_lease)])
    print("keeper: renewed the lease of %s for %s hours in the work ledger" % (pid, hours))
    return result


def record_tokens(rig, rig_path, bead, packet_id):
    """Stamp what the packet cost on its work bead when it ends, so the daily
    report and anyone reading the bead get the figure without re-deriving it."""
    try:
        use = RIG_USAGE[rig][bead["id"]]
    except Exception as why:  # a measurement must never stop a notification
        print("keeper: could not measure %s: %s" % (packet_id, why), file=sys.stderr)
        return ""
    sh(["gc", "bd", "--rig", rig, "update", bead["id"],
        "--set-metadata", "proof.tokens_accounting=admission-v2",
        "--set-metadata", "proof.tokens_input=%d" % use["input_tokens"],
        "--set-metadata", "proof.tokens_output=%d" % use["output_tokens"],
        "--set-metadata", "proof.tokens_cache_read=%d" % use["cache_read_input_tokens"],
        "--set-metadata", "proof.tokens_cache_write=%d" % use["cache_creation_input_tokens"],
        "--set-metadata", "proof.conversations=%d" % use["conversations"]])
    return ("\ntokens   %d output, %d cache-read, %d cache-write, %d conversation(s)"
            % (use["output_tokens"], use["cache_read_input_tokens"], use["cache_creation_input_tokens"],
               use["conversations"]))


def notify(rig, bead, state, subject, body):
    if state in ("done", "failed"):
        body += record_tokens(rig, RIG_PATHS.get(rig, ""), bead, (bead.get("metadata") or {}).get("proof.packet_id", ""))
    sent = sh(["gc", "mail", "send", RECIPIENT, "-s", subject, "-m", body])
    if sent.returncode != 0:
        print("keeper: could not mail %s about %s: %s" % (RECIPIENT, bead["id"], sent.stderr.strip()[-200:]),
              file=sys.stderr)
        return
    sh(["gc", "bd", "--rig", rig, "update", bead["id"], "--set-metadata", "proof.notified=" + state])
    print("keeper: %s %s -> %s" % (bead["id"], state, RECIPIENT))


def main():
    rig_doc = gc_json("rig", "list", "--json", default={})
    rigs = {r["name"]: r for r in rig_doc.get("rigs", []) if not r.get("hq")}
    RIG_PATHS.update({name: r.get("path") or "" for name, r in rigs.items()})
    # The backlog lives in whichever rig is a Gas City-owned clone of BACKLOG_REPO
    # — found by its origin, not by what the rig is called. Only ever touch an
    # owned clone: in a shared checkout other sessions are working, so skip the
    # claim-expiry check rather than fetch or fast-forward underneath them.
    backlog_clone = ""
    for name in sorted(rigs):
        path = rigs[name].get("path") or ""
        git_dir = sh(["git", "-C", path, "rev-parse", "--absolute-git-dir"]).stdout.strip() if path else ""
        if not git_dir or not os.path.isfile(os.path.join(git_dir, "gascity-owned")):
            continue
        url = sh(["git", "-C", path, "remote", "get-url", "origin"]).stdout.strip()
        repo = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        if (repo[:-4] if repo.endswith(".git") else repo) == BACKLOG_REPO:
            backlog_clone = path
            break
    if not backlog_clone:
        print("keeper: no Gas City-owned clone of %s; skipping claim-expiry checks" % BACKLOG_REPO,
              file=sys.stderr)
    else:
        sh(["git", "-C", backlog_clone, "fetch", "--quiet", "origin"])
        sh(["git", "-C", backlog_clone, "merge", "--quiet", "--ff-only", "origin/main"])

    now = datetime.now(timezone.utc)
    for rig in sorted(rigs):
        beads = gc_json("bd", "--rig", rig, "list", "--all", "--json", "--limit=0", default=[])
        import tokens
        RIG_USAGE[rig] = tokens.usage_by_admission(RIG_PATHS.get(rig, ""), beads)
        # A re-admitted packet has one workflow root per attempt. Judge each work
        # bead by the newest root that ran FOR IT, the pairing the completer uses:
        # an old failure must neither outvote a live retry nor stand in for a
        # workflow that admission has not slung yet.
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import factory_lib as fl
        roots = {p["bead"]["id"]: p["root"] for p in fl.packets_of(beads)}
        for b in beads:
            md = b.get("metadata") or {}
            pid = md.get("proof.packet_id")
            if not pid or str(md.get("proof.superseded")).lower() == "true":   # bd stores a boolean
                continue
            seen = md.get("proof.notified") or ""
            facts = ("packet   %s\nbead     %s (rig %s)\nbranch   %s\ncommit   %s\ntree     %s\nlanding  %s\nworktree %s"
                     % (pid, b["id"], rig, md.get("proof.branch", "?"), md.get("proof.commit", "?"),
                        md.get("proof.tree", "?"), md.get("proof.landing", "?"), md.get("work_dir", "?")))
            root = roots.get(b["id"]) or {}
            root_md = root.get("metadata") or {}

            passed = root.get("status") == "closed" and root_md.get("gc.outcome") == "pass"
            landed = root_md.get("proof.landed_commit")
            if landed:
                # Landed is a fact about origin's main, whatever happens next. Wait
                # for the workflow to close before tidying: the `report` step still
                # runs in the worktree after the landing.
                if root.get("status") != "closed":
                    continue
                if seen != "done":
                    # The orchestrator's landing check recorded this commit only
                    # after seeing it on origin's main, so there is nothing left
                    # to land and the item is already done there. What remains is
                    # the packet's own worktree and branch: both removals refuse
                    # on their own if anything in them is unmerged or uncommitted.
                    tidy = "left in place (no owned clone found)"
                    work_dir, branch = md.get("work_dir") or "", md.get("proof.branch") or ""
                    if backlog_clone and work_dir.startswith(os.path.join(backlog_clone, ".worktrees") + os.sep):
                        gone = sh(["git", "-C", backlog_clone, "worktree", "remove", work_dir]).returncode == 0
                        gone = gone and sh(["git", "-C", backlog_clone, "branch", "-d", branch]).returncode == 0
                        tidy = "removed" if gone else "left in place (git refused; look before removing)"
                    notify(rig, b, "done", "PROOF packet landed: %s" % pid,
                           facts + "\napproved tree  %s\nlanded commit  %s\nlanded tree    %s\n"
                           "shared checkout %s\nworktree+branch %s\n\n"
                           "The change was approved by: %s. The orchestrator then checked that the\n"
                           "landing commit added only the item's completion and regenerated views to it, gated\n"
                           "that final tree, and pushed it to main itself. The backlog item is done on main.\n"
                           "Nothing here needs you; read the work bead's notes for anything the packet left open.%s"
                           % (root_md.get("proof.approved_tree", "?"), landed,
                              root_md.get("proof.landed_tree", "?"),
                              root_md.get("proof.republished", "not configured"), tidy,
                              root_md.get("proof.approved_by", "reviewer"),
                              "" if passed else "\n\nThe workflow itself closed with gc.outcome=%s AFTER the landing "
                              "— a step after `land` failed. The change is on main regardless; inspect with "
                              "`gc bd show %s`." % (root_md.get("gc.outcome", "?"), root.get("id", "?"))))
                continue
            if b.get("status") == "closed" or passed:
                if seen != "done":
                    notify(rig, b, "done", "PROOF packet ready: %s" % pid,
                           facts + "\nvalidated tree %s\napproved tree  %s\n\n"
                           "The orchestrator gated this tree; approved by: %s.\n"
                           "The backlog item is still `working`. With landing `pr` or `automerge` the completer\n"
                           "marks it done once the pull request has merged and main is green; at any other level,\n"
                           "land the change and complete the item on the existing path."
                           % (root_md.get("proof.validated_tree", "?"), root_md.get("proof.approved_tree", "?"),
                              root_md.get("proof.approved_by", "reviewer")))
                continue
            if root.get("status") == "closed" and root_md.get("gc.outcome") == "fail" and seen != "failed":
                notify(rig, b, "failed", "PROOF packet stopped: %s" % pid,
                       facts + "\n\nThe workflow %s closed with gc.outcome=fail, so a gate, a review or a step\n"
                       "stopped it and the work bead stays open. Inspect with `gc bd show %s`, decide whether to\n"
                       "fix the packet, re-admit, or release the claim." % (root.get("id", "?"), root.get("id", "?")))
                continue
            if backlog_clone and seen not in ("expiring", "failed"):
                expires = claim_expiry(backlog_clone, pid)
                if expires is not None and expires - now < EXPIRY_WARNING and renew_lease(backlog_clone, rig, b, pid):
                    continue
                if expires is not None and expires - now < EXPIRY_WARNING:
                    notify(rig, b, "expiring", "PROOF claim expiring: %s" % pid,
                           facts + "\n\nThe backlog claim ends at %s and the packet is still open.\n"
                           "Renew it on the existing path if the work should continue:\n"
                           "  python3 scripts/next_work.py --renew %s --by gascity/%s"
                           % (expires.isoformat(), pid, rig))
    planning_pass(rigs, backlog_clone)
    return 0


def planning_pass(rigs, backlog_clone):
    """Planning runs are not packets, so nothing above sees them. A run that STOPS
    leaves its work bead open, and an open bead is what stops the trigger planning
    that rig again — deliberately: a person reads it first. That only works if the
    person is told. Once per run: mail when it stops; record what it cost when it ends."""
    if not backlog_clone:
        return
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import factory_lib as fl
    import tokens
    city = fl.city_dir()
    home = next((name for name, r in rigs.items() if os.path.realpath(r.get("path") or "") == os.path.realpath(backlog_clone)), "")
    if not home:
        return
    for run in fl.planning_runs(city, home):
        bead, md, root, state = run["bead"], run["md"], run["root"], run["state"]
        if state == "running" or md.get("proof.notified") == state:
            continue
        ended = root.get("closed_at") or bead.get("closed_at") or ""
        use = tokens.planning_usage(backlog_clone, run["target"], md.get("proof.plan_started") or bead.get("created_at") or "", ended)
        sh(["gc", "bd", "--rig", home, "update", bead["id"],
            "--set-metadata", "proof.tokens_output=%d" % use["output_tokens"],
            "--set-metadata", "proof.tokens_cache_read=%d" % use["cache_read_input_tokens"],
            "--set-metadata", "proof.conversations=%d" % use["conversations"]])
        if state != "stopped":
            sh(["gc", "bd", "--rig", home, "update", bead["id"], "--set-metadata", "proof.notified=" + state])
            print("keeper: planning run %s for %s %s (%d output tokens)" % (bead["id"], run["target"], state, use["output_tokens"]))
            continue
        why = fl.stop_reason(city, home, {"root": root}) if root else "its workflow never started (the sling failed)"
        notify(home, bead, "stopped", "PROOF planning run stopped: %s" % run["target"],
               "rig      %s\nbead     %s (rig %s)\nworkflow %s\nreason   %s\nworktree %s\nbranch   %s\n"
               "tokens   %d output, %d cache-read, %d conversation(s)\n\n"
               "NO FURTHER PLANNING RUN starts for %s while this bead is open — that is deliberate, so that a\n"
               "person reads why it stopped before the factory plans that queue again. Dispatch is not affected:\n"
               "items that are already admissible still run. Read the bead and its notes:\n"
               "  gc bd --rig %s show %s\n"
               "Anything it committed is on the branch above and was NOT landed. When you have read it, let the\n"
               "planner run again by closing the bead:\n"
               "  gc bd --rig %s close %s --reason \"read; plan again\""
               % (run["target"], bead["id"], home, root.get("id", "none"), why or "no reason recorded",
                  md.get("work_dir", "?"), md.get("proof.branch", "?"), use["output_tokens"],
                  use["cache_read_input_tokens"], use["conversations"], run["target"], home, bead["id"], home, bead["id"]))


if __name__ == "__main__":
    sys.exit(main())
