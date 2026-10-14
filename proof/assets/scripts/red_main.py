#!/usr/bin/env python3
"""PROOF red-main responder: when `main` is red, one Opus 5.5 xhigh session finds the
way forward, and a person is the one who takes it.

    red_main.py [--dry-run]        one pass over every rig (the order)
    red_main.py --rig <rig> [--json]   just the facts, for a person

The facts first, because none of this is a judgement until they are on the
table, and every one of them is a query — no agent, no model, no tokens.

ADR-0115 is the authority this serves: a red `main` belongs to the merge that
turned it red, the first-red walk is how that is established, and the rule for a
red you do NOT own is revert, not fix. None of that is a judgement until the
facts are on the table, and every one of these facts is a query:

  push      the verdict for `main` itself, walked head by head along its
            first-parent chain — the same walk and the same rule the completer
            applies to one merge, imported rather than re-derived.
  owner     the commit at the first red head, its pull request, and whether it
            is a packet THIS factory merged (a branch one of the rig's packets
            pushed). `fix_forward` is only ever available for one of those.
  standing  the `schedule` and `workflow_dispatch` failures on main's CURRENT
            head, kept apart from the push verdict on purpose. A nightly
            workflow attaches to whatever head main has when it fires, so it is
            not evidence about any merge (pack 0.5.1) — and that correction left
            the other half open: a scheduled red is still real, still has an
            owner, and until now nothing in this factory said so. For each one,
            how many of that workflow's previous runs also failed, because a
            workflow that has been failing for three nights is not this head's
            fault either.

`main` being green here is the common case and costs two API calls.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

import completer                                  # its first-parent walk and its verdict
import factory_lib as fl
from factory_lib import admit

# How far back the walk may look. A red older than this is reported as bounded
# rather than blamed on the oldest head examined — attributing a red to the
# wrong commit is worse than saying "at or before".
HEADS = 25
# Runs of a scheduled workflow read to say whether tonight's red is new.
HISTORY = 5


def gh_json(args, cwd):
    return completer.gh_json(args, cwd)


def recent_heads(clone, limit=HEADS):
    """The commits `main` actually pointed at, oldest first, newest last."""
    fl.sh(["git", "-C", clone, "fetch", "--quiet", "origin"])
    out = fl.sh(["git", "-C", clone, "rev-list", "--first-parent", "-n", str(limit), "origin/main"])
    return list(reversed(out.stdout.split())) if out.returncode == 0 else []


def push_runs(slug, sha, clone):
    return gh_json(["api", "repos/%s/actions/runs?head_sha=%s&event=push&per_page=100" % (slug, sha)],
                   clone)["workflow_runs"]


def not_push(runs):
    """Everything attached to a head that the PUSH did not trigger.

    The separation is the whole point, so it is its own function and its own
    test: a `schedule` failure on a merge SHA looks identical to a push failure
    in a rollup, and reading them together is what kept one item `working` for
    four hours over a pipeline that was green and deployed (pack 0.5.1)."""
    return [r for r in runs if r.get("event") != "push"]


def only_push(runs):
    return [r for r in runs if r.get("event") == "push"]


def other_runs(slug, sha, clone):
    return not_push(gh_json(["api", "repos/%s/actions/runs?head_sha=%s&per_page=100" % (slug, sha)],
                            clone)["workflow_runs"])


def standing_reds(runs, history):
    """[(name, event, url, how many of the previous runs also failed)] for the
    failures that were not triggered by the push.

    `history(name)` returns that workflow's recent runs, newest first, so a red
    that has been red for three nights reads as one, not as this head's."""
    out = []
    for run in runs:
        if run.get("conclusion") not in completer.BAD:
            continue
        before = [r for r in history(run["name"]) if r.get("id") != run.get("id")][:HISTORY]
        also = sum(1 for r in before if r.get("conclusion") in completer.BAD)
        out.append((run["name"], run.get("event") or "?", run.get("html_url") or "", also, len(before)))
    return sorted(out)


def owner_of(slug, sha, clone):
    """(the commit, its pull request or None) for the head that went red."""
    commit = gh_json(["api", "repos/%s/commits/%s" % (slug, sha)], clone)
    prs = gh_json(["api", "repos/%s/commits/%s/pulls" % (slug, sha)], clone) or []
    return commit, (sorted(prs, key=lambda p: p["number"])[-1] if prs else None)


def ours(pr, packets):
    """The packet whose branch this pull request came from, or ''.

    `fix_forward` is available only for a red this factory's own merge caused:
    a worker can be sent back to a packet it delivered, and cannot be sent at
    anything else."""
    head = ((pr or {}).get("head") or {}).get("ref") or ""
    if not head:
        return ""
    return next((p["id"] for p in packets if (p["md"].get("proof.branch") or "") == head), "")


def signature(verdict, sha, detail, standing):
    """What this red IS. A different first-red head, a different set of failing
    workflows, or a different standing red is a different red and gets its own
    response; the same one is not responded to twice."""
    parts = [verdict, sha or "", detail or ""] + ["%s/%s" % (name, event) for name, event, _u, _a, _n in standing]
    return "|".join(parts)[:400]


def facts(city, rig, clone, slug, packets):
    """Everything above, once, for one rig."""
    heads = recent_heads(clone)
    if not heads:
        raise fl.Refused("could not read main's history in %s" % clone)
    verdict, sha, detail = completer.verdict_over(heads, lambda s: push_runs(slug, s, clone))
    bounded = verdict == "red" and sha == heads[0]
    head = heads[-1]
    cache = {}

    def history(name):
        if name not in cache:
            got = gh_json(["api", "repos/%s/actions/runs?per_page=%d" % (slug, HISTORY * 4)], clone)["workflow_runs"]
            for run in got:
                cache.setdefault(run["name"], []).append(run)
        return cache.get(name, [])

    standing = standing_reds(other_runs(slug, head, clone), history)
    found = {"rig": rig, "slug": slug, "head": head, "verdict": verdict, "red_sha": sha, "failing": detail,
             "bounded": bounded, "standing": standing, "owner": None, "pr": None, "packet": ""}
    if verdict == "red":
        commit, pr = owner_of(slug, sha, clone)
        found["owner"] = {"sha": sha, "message": (commit.get("commit") or {}).get("message", "").split("\n")[0],
                          "author": ((commit.get("commit") or {}).get("author") or {}).get("name", ""),
                          "date": ((commit.get("commit") or {}).get("author") or {}).get("date", "")}
        found["pr"] = {"number": pr["number"], "url": pr["html_url"],
                       "head": (pr.get("head") or {}).get("ref", "")} if pr else None
        found["packet"] = ours(pr, packets)
    return found


def quiet(found):
    """Nothing to respond to: main is green or still running, and no standing red."""
    return found["verdict"] != "red" and not found["standing"]


def remote_head(clone):
    """main's head on the remote right now, or '' — one `ls-remote`, no fetch."""
    out = fl.sh(["git", "-C", clone, "ls-remote", "origin", "refs/heads/main"])
    words = out.stdout.split() if out.returncode == 0 else []
    return words[0] if words else ""


def recheck(found, clone, slug):
    """'' when the red observed is still the red, else what changed.

    Bounded on purpose: main's head, the push runs of the one head the verdict
    named, and the failing non-push runs on the current head — three reads.
    Never the first-red walk and never a workflow's history. A change deeper
    in the walk than this sees is the next pass's to find."""
    try:
        head = remote_head(clone)
        if head != found["head"]:
            return "main moved from %s to %s" % (found["head"][:10], head[:10] or "an unreadable head")
        sha = found["red_sha"] or ""
        if sha:
            verdict, _sha, detail = completer.verdict_over([sha], lambda s: push_runs(slug, s, clone))
            if verdict != found["verdict"] or (verdict == "red" and detail != found["failing"]):
                return "the push verdict at %s is now %s (%s)" % (sha[:10], verdict, detail)
        standing = sorted((r["name"], r.get("event") or "?") for r in other_runs(slug, head, clone)
                          if r.get("conclusion") in completer.BAD)
        if standing != sorted((name, event) for name, event, _u, _a, _n in found["standing"]):
            return "the standing reds on %s changed" % head[:10]
    except fl.Refused as why:
        return "GitHub could not be read again (%s)" % why
    return ""


def report(found):
    """The facts as the text a bead and a mail carry."""
    lines = ["rig      %s" % found["rig"], "main     %s at %s" % (found["verdict"], found["head"][:10])]
    if found["verdict"] == "red":
        lines += ["first red %s" % found["red_sha"][:10], "failing  %s" % found["failing"]]
        if found["bounded"]:
            lines.append("NOTE     the oldest head examined is itself red: the red begins at or BEFORE it, "
                         "so this commit may not be the owner")
        owner = found["owner"] or {}
        lines += ["owner    %s  %s" % (owner.get("author", "?"), owner.get("message", "")[:70])]
        if found["pr"]:
            lines.append("pr       %s (branch %s)" % (found["pr"]["url"], found["pr"]["head"]))
        lines.append("ours     %s" % (("yes — packet %s, merged by this factory" % found["packet"])
                                      if found["packet"] else
                                      "no — this factory did not merge it, so fix_forward is not available"))
    for name, event, url, also, seen in found["standing"]:
        lines.append("standing %s (%s) failed%s — %s" % (
            name, event, "" if not seen else ", and %d of its previous %d runs too" % (also, seen), url))
    return "\n".join(lines)


# --- the pass ----------------------------------------------------------------

ME = "red-main"
RESPONDER = "proof.responder"
FORMULA = "mol-proof-red-main"
RECOMMENDATIONS = ("revert", "fix_forward", "rerun", "person")
RED_DEFAULTS = {"enabled": False, "max_attempts": 2, "revert_pr": [], "rerun": []}
# Recorded in factory-state before a session is spent. Not a bead id. Two
# overlapping passes both observe `judge`; the second finds this and spends
# nothing. A session that never started puts back the memory from before the
# claim, so an attempt is spent only when a session actually starts. `since`
# is when the claim was written. The order's own timeout is 20m
# (proof-red-main.toml): a pending claim older than that is not a live
# session — the process was killed between the claim and its release. The
# next pass decides from the pre-claim memory. It does not write that view
# into the section first: the merge would treat it as this pass's edit and
# could overwrite a claim another pass just took. `claim_judge` replaces an
# expired claim itself, from the memory on disk.
PENDING = "pending"
CLAIM_TTL_S = 20 * 60
_MEMORY_KEYS = ("signature", "bead", "attempts", "acted", "notified")


def _plain(memory):
    """The rig's remembered fields, unwrapping one pending claim to what it replaced."""
    if not isinstance(memory, dict):
        return {}
    if memory.get("bead") == PENDING:
        prior = memory.get("prior")
        memory = prior if isinstance(prior, dict) else {}
    return {key: memory[key] for key in _MEMORY_KEYS if key in memory}


def live_memory(memory):
    """The memory a decision should read.

    A pending claim younger than the order timeout is a live session and is
    returned as it stands. An older one is not: the pre-claim memory is what
    the attempt cap and the next judge have to see."""
    if not isinstance(memory, dict):
        return {}
    if memory.get("bead") == PENDING and fl.claim_stale(memory.get("since"), CLAIM_TTL_S):
        prior = memory.get("prior")
        return _plain(prior if isinstance(prior, dict) else {})
    return memory


def claim_judge(said, rig, sig, attempts):
    """Record that this pass is spending a session, before it spends one.

    Returns true when this pass owns the claim. The short state lock covers
    the re-read and the write, so a second pass that already decided `judge`
    from a stale snapshot sees the claim and does not start another session.
    The memory this claim replaces is kept on the claim, so a session that
    never starts can be put back without resetting the attempt count.

    This rig's memory is the one on disk, not the merged section. The merge
    keeps edits this pass made to other rigs, but an edit of this rig — a
    stale claim unwrapped in memory, a placeholder, anything not yet on disk —
    must not hide a claim another pass wrote since this one read the file.
    A refusal puts the on-disk value back, so that edit is not what gets written."""
    def fn(section):
        fresh = fl._read_state(said.path)
        disk = fresh.get("red_main") if isinstance(fresh.get("red_main"), dict) else {}
        memory = disk.get(rig) if isinstance(disk.get(rig), dict) else {}

        def keep_disk():
            if rig in disk:
                section[rig] = copy.deepcopy(memory)
            else:
                section.pop(rig, None)
            return False

        expired = memory.get("bead") == PENDING and fl.claim_stale(memory.get("since"), CLAIM_TTL_S)
        if memory.get("signature") == sig and memory.get("bead") and not expired:
            return keep_disk()
        base = _plain(memory.get("prior") if memory.get("bead") == PENDING else memory)
        spent = int(base.get("attempts") or 0)
        if spent >= attempts:
            return keep_disk()
        claim = {"signature": sig, "bead": PENDING, "attempts": attempts,
                 "since": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if base:
            claim["prior"] = base
        section[rig] = claim
        return True
    return said.transact_section("red_main", fn)


def _keep_unwrapped(said, rig, view, since):
    """Persist a decision read from a stale claim, without replacing a newer one.

    Only while the on-disk claim is still that stale one. A claim another pass
    took in between belongs to that pass and is left alone."""
    def fn(section):
        fresh = fl._read_state(said.path)
        disk = fresh.get("red_main") if isinstance(fresh.get("red_main"), dict) else {}
        current = disk.get(rig) if isinstance(disk.get(rig), dict) else {}
        if current.get("bead") != PENDING or current.get("since") != since:
            return False
        if not fl.claim_stale(current.get("since"), CLAIM_TTL_S):
            return False
        section[rig] = dict(view)
        return True
    said.transact_section("red_main", fn)


def finish_judge(said, rig, sig, attempts, bead_id):
    """Write the session id, or put back the memory a claim replaced.

    An empty `bead_id` means the session never started. Dropping the rig's
    whole memory would reset the attempt count; the pre-claim memory is
    restored instead, and a first claim (nothing before it) is removed."""
    def fn(section):
        current = section.get(rig) if isinstance(section.get(rig), dict) else {}
        if bead_id:
            kept = {k: current[k] for k in ("acted", "notified") if k in current}
            section[rig] = {"signature": sig, "bead": bead_id, "attempts": attempts, **kept}
        elif current.get("bead") == PENDING and current.get("signature") == sig:
            prior = current.get("prior")
            plain = _plain(prior if isinstance(prior, dict) else {})
            if plain:
                section[rig] = plain
            else:
                section.pop(rig, None)
        return None
    said.transact_section("red_main", fn)


def settings(policy):
    table = dict(RED_DEFAULTS)
    table.update(policy.get("red_main") or {})
    return table


def stage(memory, bead, sig, max_attempts):
    """What this pass should do about one rig's red: (what, why).

    The same five outcomes as the CI triage, for the same reasons, keyed on the
    same kind of signature — a different first-red head or a different set of
    failing workflows is a different red and gets its own response. A pending
    claim is not a responder bead: while it is younger than the order timeout
    the line names it, and once it is older `live_memory` has already put the
    pre-claim memory back so this can judge again."""
    memory = live_memory(memory)
    spent = int(memory.get("attempts") or 0)
    if memory.get("signature") != sig or not memory.get("bead"):
        if spent >= max_attempts:
            return "exhausted", "%d responder session(s) already spent on this red" % spent
        return "judge", "a red main nobody has looked at"
    if memory.get("bead") == PENDING:
        return "wait", "judge claim pending since %s is still open" % (memory.get("since") or "?")
    if bead is None or bead.get("status") != "closed":
        return "wait", "responder %s is still open" % memory.get("bead")
    if memory.get("acted"):
        return "wait", "already answered this red: %s" % memory["acted"]
    said = (bead.get("metadata") or {}).get("proof.red_recommendation")
    if said not in RECOMMENDATIONS:
        return "blind", "responder %s closed without a recommendation" % bead["id"]
    return "act", said


def action_for(recommendation, rig, found, red):
    """(what to do, why) — the authorization gate, and the one rule the script
    enforces over the session's answer.

    `fix_forward` is available only for a red THIS factory's own merge caused.
    A session that recommends it for anything else is not overruled quietly:
    the recommendation is carried to a person as `person`, with the reason. A
    judgement may only add caution, and sending a worker at someone else's red
    is the opposite (ADR-0115: for a red you do not own the rule is revert, not
    fix)."""
    if recommendation == "fix_forward" and not found.get("packet"):
        return "mail", ("fix_forward is only available for a red this factory merged, and %s is not one; "
                        "carried to you as-is" % (found.get("red_sha") or "?")[:10])
    if recommendation == "fix_forward":
        return "mail", ("the red is packet %s, which this factory merged — shaping or re-admitting it is a "
                        "decision, so it comes to you" % found["packet"])
    if recommendation == "revert":
        return ("revert", "") if rig in (red.get("revert_pr") or []) else \
            ("mail", "rig %s is not authorized to open a revert pull request (policy [red_main] revert_pr)" % rig)
    if recommendation == "rerun":
        return ("rerun", "") if rig in (red.get("rerun") or []) else \
            ("mail", "rig %s is not authorized to re-run (policy [red_main] rerun)" % rig)
    return "mail", "a person decides this one"


def rerun_target(found):
    """The run to re-run: the standing scheduled red if that is all there is,
    else nothing — a red PUSH pipeline is the repository's delivery run and
    re-running it is not this responder's to do without a person."""
    if found["verdict"] == "red":
        return "", "main's own push pipeline is red; re-running it is a person's call, not a responder's"
    for _name, _event, url, _also, _seen in found["standing"]:
        found_id = url.rsplit("/", 1)[-1] if url else ""
        if found_id.isdigit():
            return found_id, ""
    return "", "no standing run with an id to re-run"


def do_rerun(slug, clone, found, dry_run):
    run_id, why = rerun_target(found)
    if not run_id:
        return "", why
    if dry_run:
        return "rerun:%s" % run_id, "would re-run the failed jobs of run %s" % run_id
    out = fl.sh(["gh", "run", "rerun", run_id, "--failed", "--repo", slug], cwd=clone)
    if out.returncode != 0:
        detail = (out.stderr or out.stdout).strip()
        if fl.rerun_already_going(detail):
            return "rerun:%s" % run_id, "re-run already running for run %s" % run_id
        return "", "re-run refused: %s" % detail[-200:]
    return "rerun:%s" % run_id, "re-ran the failed jobs of run %s" % run_id


def revert_worktree(clone, rig):
    return os.path.join(clone, ".worktrees", "proof-red-main-%s" % rig)


def do_revert_pr(clone, slug, rig, found, dry_run):
    """Open a revert pull request for the owning commit. It is NEVER merged
    here, and `main` is never written to: the whole of the outward action is a
    branch and a pull request a person reads and merges.

    In a worktree of its own, because the rig's clone is where the dispatcher
    and the completer work."""
    sha = found.get("red_sha") or ""
    if not sha:
        return "", "no owning commit to revert"
    if found.get("bounded"):
        return "", ("the red begins at or before the oldest head examined, so the owning commit is not "
                    "established; a revert would be a guess")
    work, branch = revert_worktree(clone, rig), "agent/revert-%s" % sha[:10]
    if dry_run:
        return "revert:%s" % sha[:10], "would open a revert pull request for %s on %s" % (sha[:10], branch)
    fl.sh(["git", "-C", clone, "fetch", "--quiet", "--prune", "origin"])
    if os.path.isdir(work):
        if fl.sh(["git", "-C", work, "status", "--porcelain"]).stdout.strip():
            return "", "the responder worktree %s has uncommitted changes; nothing was done" % work
    else:
        made = fl.sh(["git", "-C", clone, "worktree", "add", "--detach", work, "origin/main"])
        if made.returncode != 0:
            return "", "could not create %s: %s" % (work, (made.stderr or made.stdout).strip()[-200:])
    if fl.sh(["git", "-C", work, "checkout", "-B", branch, "origin/main"]).returncode != 0:
        return "", "could not start %s" % branch
    parents = fl.sh(["git", "-C", work, "rev-list", "--parents", "-n", "1", sha]).stdout.split()
    argv = ["git", "-C", work, "revert", "--no-edit"] + (["-m", "1"] if len(parents) > 2 else []) + [sha]
    reverted = fl.sh(argv)
    if reverted.returncode != 0:
        fl.sh(["git", "-C", work, "revert", "--abort"])
        return "", "the revert does not apply cleanly: %s" % (reverted.stderr or reverted.stdout).strip()[-200:]
    if fl.sh(["git", "-C", work, "push", "--quiet", "-u", "origin", branch]).returncode != 0:
        return "", "could not push %s" % branch
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write("`main` went red at %s and this reverts the commit the first-red walk attributes it to.\n\n"
                 "Opened by the PROOF red-main responder. It is NOT merged and no auto-merge is armed: "
                 "whether to revert or to fix forward is yours, and ADR-0115's rule for a red you do not own "
                 "is revert, not fix.\n\n```\n%s\n```\n" % (sha[:10], report(found)))
    try:
        made = fl.sh(["gh", "pr", "create", "--repo", slug, "--head", branch, "--base", "main",
                      "--title", "Revert %s: main is red" % sha[:10], "--body-file", fh.name], cwd=work)
    finally:
        os.unlink(fh.name)
    if made.returncode != 0:
        return "", "could not open the pull request: %s" % (made.stderr or made.stdout).strip()[-200:]
    return "revert:%s" % sha[:10], "opened a revert pull request, unmerged: %s" % made.stdout.strip()[-200:]


def bead_body(found, attempts, max_attempts):
    return (
        "Decide the way forward for a red `main`. You are READ-ONLY: record a recommendation and close.\n\n"
        "attempt  %d of %d\n\n"
        "The facts, established by `red_main.py` before this session started — the first-red walk, the "
        "commit it attributes the red to, and separately the standing scheduled reds on main's current "
        "head:\n\n%s\n" % (attempts, max_attempts, report(found)))


def start_session(city, rig, found, attempts, max_attempts, dry_run):
    if dry_run:
        fl.log(ME, "rig %s: would spend a responder session (attempt %d of %d)" % (rig, attempts, max_attempts))
        return ""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(bead_body(found, attempts, max_attempts))
    meta = {"proof.red_for": rig, "proof.red_sha": found.get("red_sha") or "",
            "proof.red_started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        out = admit.gc(city, "bd", "--rig", rig, "create", "Find the way forward for a red main (%s)" % rig,
                       "--body-file", fh.name, "--metadata", json.dumps(meta), "--json")
    finally:
        os.unlink(fh.name)
    created = json.loads(out.stdout)
    bead_id = (created[0] if isinstance(created, list) else created)["id"]
    admit.gc(city, "sling", "%s/%s" % (rig, RESPONDER), bead_id, "--on", FORMULA, "--var", "rig=" + rig)
    fl.log(ME, "rig %s: responder %s slung (attempt %d of %d)" % (rig, bead_id, attempts, max_attempts))
    return bead_id


def observe(city, rigs, active=None):
    """Every active rig's main — the first-red walk, its owner, the standing
    reds — read with NO lock held, as snapshots. Nothing here writes: a slow
    GitHub costs this pass time and no other factory writer anything."""
    seen = []
    for rig in sorted(rigs):
        if active is not None and rig not in active:
            continue
        try:
            clone = admit.healthy_clone(rigs, rig)
        except fl.Refused:
            continue
        slug = fl.repo_slug(clone)
        try:
            found = facts(city, rig, clone, slug, fl.packets(city, rig))
        except fl.Refused as why:
            seen.append({"rig": rig, "refused": str(why)})
            continue
        seen.append({"rig": rig, "clone": clone, "slug": slug, "found": found})
    return seen


def respond(city, policy, red, rigs, said, dry_run, seen, active=None):
    """Publish what the snapshots call for. No factory lock: `policy`, `red`,
    `rigs`, `active` and `said` are read again here, because any of them may
    have changed while the snapshot was read."""
    memories = said.section("red_main")
    for snap in seen:
        rig = snap["rig"]
        if active is not None and rig not in active:
            said.say("stale:" + rig, "rig %s: suspended or withdrawn while main was read; nothing done" % rig)
            continue
        if "refused" in snap:
            said.say("facts:" + rig, "rig %s: could not read main — %s" % (rig, snap["refused"]))
            continue
        clone, slug, found = snap["clone"], snap["slug"], snap["found"]
        if quiet(found) and not memories.get(rig):
            said.say("state:" + rig, "rig %s: main %s, nothing standing" % (rig, found["verdict"]))
            continue
        moved = recheck(found, clone, slug)
        if moved:
            said.say("stale:" + rig, "rig %s: %s since it was read; nothing done, the next pass reads it again"
                     % (rig, moved))
            continue
        memory = memories.setdefault(rig, {})
        if quiet(found):
            if memory:
                fl.log(ME, "rig %s: main is %s again; the response is closed" % (rig, found["verdict"]))
                memories[rig] = {}
            said.say("state:" + rig, "rig %s: main %s, nothing standing" % (rig, found["verdict"]))
            continue
        sig = signature(found["verdict"], found["red_sha"], found["failing"], found["standing"])
        unwrapped, unwrapped_since = False, None
        if memory.get("bead") == PENDING and fl.claim_stale(memory.get("since"), CLAIM_TTL_S):
            # The order that claimed this was killed before it could release.
            # Decide from the pre-claim memory. Leave the section alone: writing
            # the unwrapped view here is an edit the next claim merges over
            # whatever another pass has claimed since, and both passes start a
            # session. `claim_judge` replaces an expired claim from disk.
            unwrapped, unwrapped_since = True, memory.get("since")
            memory = live_memory(memory)
            said.say("stale:" + rig, "rig %s: judge claim pending since %s outlived the order; "
                                     "deciding from the memory before the claim, without spending an attempt"
                                     % (rig, unwrapped_since or "an unknown time"))
        bead = next((b for b in fl.rig_beads(city, rig, fresh=True) if b["id"] == memory.get("bead")), None) \
            if memory.get("bead") else None
        what, why = stage(memory, bead, sig, red["max_attempts"])
        said.say("state:" + rig, "rig %s: %s — %s" % (rig, what, why))
        if what == "wait":
            continue
        if what in ("exhausted", "blind"):
            if memory.get("notified") != what + sig:
                if not dry_run and fl.mail("PROOF red main needs you: %s" % rig, report(found) + "\n\n%s." % why):
                    memory["notified"] = what + sig
                    if unwrapped:
                        _keep_unwrapped(said, rig, memory, unwrapped_since)
                fl.log(ME, "rig %s: %s — mailed" % (rig, why))
            continue
        if what == "judge":
            budget = fl.budget_hold(policy, rigs)
            if budget:
                said.say("budget", budget)
                continue
            attempts = int(memory.get("attempts") or 0) + 1
            if dry_run:
                start_session(city, rig, found, attempts, red["max_attempts"], True)
                continue
            # Claim before spending. Both overlapping passes already returned
            # `judge` from the snapshot; the record is what the second one sees.
            if not claim_judge(said, rig, sig, attempts):
                said.say("stale:" + rig, "rig %s: another pass already claimed this red; nothing done, "
                                         "the next pass reads it again" % rig)
                continue
            bead_id = ""
            try:
                bead_id = start_session(city, rig, found, attempts, red["max_attempts"], False)
            finally:
                finish_judge(said, rig, sig, attempts, bead_id)
            continue
        recommendation = why
        do, refused = action_for(recommendation, rig, found, red)
        if do == "revert":
            acted, told = do_revert_pr(clone, slug, rig, found, dry_run)
        elif do == "rerun":
            acted, told = do_rerun(slug, clone, found, dry_run)
        else:
            acted, told = "mail", refused
        if not acted:
            acted, told = "mail", "%s, and it could not be done: %s" % (recommendation, told)
        if acted == "mail" and not dry_run:
            fl.mail("PROOF red main — %s recommends %s" % (rig, recommendation),
                    report(found) + "\n\nrecommendation  %s\n%s.\n\nNothing was done to main." % (recommendation, told))
        fl.log(ME, "rig %s: %s — %s" % (rig, recommendation, told))
        if not dry_run:
            memory["acted"] = "%s (%s)" % (acted, recommendation)
            if unwrapped:
                _keep_unwrapped(said, rig, memory, unwrapped_since)


def wanted(policy, dry_run):
    """Whether the policy lets this order do anything at all."""
    if not settings(policy)["enabled"]:
        fl.log(ME, "[red_main] enabled is not set in the policy; a red main is nobody's until a person sees it")
        return False
    if not policy["enabled"] and not dry_run:
        fl.log(ME, "policy is not enabled; no responder")
        return False
    return True


def publish(city, dry_run, seen, observed):
    """The second half of a pass: read again everything a decision rests on —
    the policy and its authorization tables (`observed` is the table the
    snapshots were read under), the limit hold, rig suspension and
    this order's own memory — then respond to the snapshots still true.
    Takes no `factory.lock`. A judge records its claim under the short
    `factory-state` lock before it spends a session, and `close` merges
    that file under the same lock."""
    policy = fl.load_policy(city)
    if not wanted(policy, dry_run):
        return
    said = fl.Standing(city, ME, keep=not dry_run)
    try:
        if settings(policy) != observed:
            # The authorization the snapshots were read under is not the one in
            # force: discard them rather than act on either reading.
            said.say("stale:policy", "[red_main] changed while main was read; nothing done, the next pass reads it again")
            return
        why = fl.limit_hold(said.state)
        if why:
            said.say("limit-hold", "no responder session: %s" % why)
            return
        rigs = admit.load_rigs(city)
        active = fl.active_rigs(rigs, fl.rig_states(city), policy)
        respond(city, policy, settings(policy), rigs, said, dry_run, seen, active=active)
    finally:
        said.close()


def one_pass(city, dry_run):
    """Observe, then revalidate and publish. Neither half opens `factory.lock`:
    a factory pass must not starve this order. A judge records its claim
    before spending a session. The other writes are repeat-safe — a re-run
    GitHub refuses while one is running (that refusal is the answer), a
    revert pull request on the fixed branch `agent/revert-<sha>`, and mail
    once. `factory-state.json` is merged under its own short lock."""
    policy = fl.load_policy(city)
    if not wanted(policy, dry_run):
        return 0
    observed, seen = settings(policy), []
    if not fl.limit_hold(fl.Standing(city, ME, keep=False).state):
        rigs = admit.load_rigs(city)
        seen = observe(city, rigs, fl.active_rigs(rigs, fl.rig_states(city), policy))
    publish(city, dry_run, seen, observed)
    return 0


def pass_over(argv):
    """One cooldown pass over every rig. The order's entry point."""
    import plan_trigger                              # its log tee

    city = fl.city_dir()
    plan_trigger.log_to_factory(city)
    return one_pass(city, "--dry-run" in argv)


def main(argv):
    if "--rig" not in argv:
        return pass_over(argv)
    ap = argparse.ArgumentParser(prog="red_main.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--rig", required=True)
    ap.add_argument("--city", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    city = args.city or fl.city_dir()
    rigs = admit.load_rigs(city)
    clone = admit.owned_clone(rigs, args.rig)
    slug = fl.repo_slug(clone)
    found = facts(city, args.rig, clone, slug, fl.packets(city, args.rig))
    print(json.dumps(found, indent=1) if args.json else report(found))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except fl.Refused as why:
        fl.log("red-main", "stopped — %s" % why)
        sys.exit(1)
