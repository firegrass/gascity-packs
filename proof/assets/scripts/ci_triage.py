#!/usr/bin/env python3
"""PROOF CI triage: answer a packet's red pull request instead of mailing it once.

    ci_triage.py [--dry-run]

No agent waits for CI in this factory and none should: the worker's prompt
forbids polling it, `arm-automerge` arms the merge path and stops, and the
completer does the watching as a script. What was missing is the NEXT move. On
2026-09-20 every red path ended in one mail to a person, and a pull request that
stalled filled `max_unmerged`, which caps dispatch.

So this order is the move, and it is deliberately two passes:

  judge   a red pull request with no triage in flight gets ONE short session of
          `proof.triage`, with the first causal failure already extracted by
          `ci_failure.py`. The session is read-only: it records a verdict and a
          reason on its bead and closes. No model reads a whole log, and no
          model performs an action.
  act     the next pass reads that verdict and does what the rig is AUTHORIZED
          to do — nothing more:

            infrastructure  re-run the failed jobs of the existing run, once.
            packet          reopen the packet's work bead and re-admit it, so
                            the worker resumes on the commit it already made
                            with the failure quoted to it.
            foreign         a red that is not this packet's. Mail.
            unclear         mail.

Every outward action is off by default and turned on per rig in the operator's
`<city>/proof/policy.toml`, exactly as `push`, `open_pr` and `automerge` are:

    [ci_triage]
    enabled = false      # the order does nothing at all without this
    max_attempts = 2     # sessions spent on one packet before it is a person's
    rerun = []           # rigs whose failed jobs it may re-run
    resume = []          # rigs whose packets it may reopen and re-admit

Three things it must never do, each paid for by this repository already: it
never CANCELS a run (cancelling a delivery run reds the required check, and a
duplicate run must never be cancelled), it never dispatches a fresh run to get
a clean result (`pg-suite-sweep` holds one repo-wide slot and a dispatch cancels
a peer's), and it never touches `main` — a red main is the responder's subject,
not this one's.

A judgement here may only add caution. It re-runs, resumes or escalates; it can
never mark a packet passed, complete an item, approve a review or change an
exit code, and every failure path — no session, no verdict, no authorization,
attempts spent — falls through to the mail that existed before it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone

import ci_failure
import factory_lib as fl
from factory_lib import admit

ME = "ci-triage"
TRIAGE = "proof.triage"
FORMULA = "mol-proof-ci-triage"
VERDICTS = ("infrastructure", "packet", "foreign", "unclear")
CI_DEFAULTS = {"enabled": False, "max_attempts": 2, "rerun": [], "resume": []}


def settings(policy):
    table = dict(CI_DEFAULTS)
    table.update(policy.get("ci_triage") or {})
    return table


def signature(found):
    """What this red IS, so a different red starts a new triage and the same one
    does not. The names and the runs, never the times: a re-run of the same
    failing job is the same failure until its conclusion changes."""
    parts = sorted("%s@%s:%s" % (f["name"], f["run_id"], f["conclusion"]) for f in found)
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12] if parts else ""


def stage(md, bead, sig, max_attempts):
    """What this pass should do for one packet: (what, why).

    judge      no triage in flight for this failure; spend a session
    wait       a session is open, or the action for this failure is done
    act        a verdict is recorded and nothing has been done with it yet
    exhausted  this packet has had its attempts
    blind      the session closed without recording a verdict

    Everything a pass records carries the SIGNATURE of the red it answered:
    `proof.ci_acted` is "<sig> rerun:123", not "rerun:123". A packet whose red
    changes — the re-run went green and a different job failed, the worker
    pushed a fix that failed elsewhere — must start a fresh cycle, and a state
    stamped without the signature reads as "already answered" for ever. (bd
    stores metadata as strings; clearing a key by writing "" is not a thing
    this script relies on.)

    `md` is the work bead's metadata, `bead` the triage bead (or None). A pending
    claim is not a triage bead: while it is younger than the order timeout the
    line names it, and once it is older the pre-claim metadata is what this
    reads, so a session that never started does not count as spent."""
    md = live_md(md)
    spent = int(md.get("proof.ci_attempts") or 0)
    if md.get("proof.ci_signature") != sig or not md.get("proof.ci_bead"):
        if spent >= max_attempts:
            return "exhausted", "%d triage session(s) already spent on this packet" % spent
        return "judge", "a red that has not been triaged"
    if md.get("proof.ci_bead") == CLAIM:
        return "wait", "judge claim pending since %s is still open" % (md.get("proof.ci_since") or "?")
    if bead is None or bead.get("status") != "closed":
        return "wait", "triage %s is still open" % md.get("proof.ci_bead")
    if (md.get("proof.ci_acted") or "").startswith(sig + " "):
        return "wait", "already answered this red: %s" % md["proof.ci_acted"]
    verdict = (bead.get("metadata") or {}).get("proof.ci_verdict")
    if verdict not in VERDICTS:
        return "blind", "triage %s closed without a verdict" % bead["id"]
    return "act", verdict


def action_for(verdict, rig, ci):
    """(what to do, why) for a verdict in a rig — the authorization gate.

    An unauthorized verdict is not an error and not a retry: it is the mail,
    with the verdict in it, which is strictly more than a person had before."""
    if verdict == "infrastructure":
        return ("rerun", "") if rig in (ci.get("rerun") or []) else \
            ("mail", "rig %s is not authorized to re-run (policy [ci_triage] rerun)" % rig)
    if verdict == "packet":
        return ("resume", "") if rig in (ci.get("resume") or []) else \
            ("mail", "rig %s is not authorized to resume a packet (policy [ci_triage] resume)" % rig)
    return "mail", "verdict %s is always a person's" % verdict


def had_review(beads, root_id):
    """Preserve the exact attempt's admission setting, including graph-v2 roots."""
    if not root_id:
        return False
    for bead in beads:
        md = bead.get("metadata") or {}
        if bead.get("id") == root_id and (
                fl.flag(md.get("gc.var.review")) or md.get("proof.review") == "on"):
            return True
        if md.get("gc.root_bead_id") == root_id and "review" in (md.get("gc.step_ref") or ""):
            return True
    return False


def notify_once(city, rig, packet, state, subject, body, dry_run=False):
    """One mail per state per RED. Its own key: the completer already mails
    `proof.pr_notified` for a failed check, and this must add to that, not
    silence it or double it. `state` carries the signature, so the same
    condition on a different red is said again."""
    if packet["md"].get("proof.ci_notified") == state:
        return
    if dry_run:
        fl.log(ME, "%s: would mail — %s" % (packet["id"], subject))
        return
    if fl.mail(subject, body):
        fl.set_bead(city, rig, packet["bead"]["id"], proof__ci_notified=state)
    fl.log(ME, "%s: %s" % (packet["id"], subject))


def bead_body(packet, rig, pr_url, report, attempts, max_attempts):
    return (
        "Classify one red pull request. You are READ-ONLY: record a verdict and close.\n\n"
        "packet   %s\n"
        "rig      %s\n"
        "pr       %s\n"
        "attempt  %d of %d\n\n"
        "The first causal failure, extracted from GitHub before this session started — you do not\n"
        "need to fetch anything to answer:\n\n%s\n"
        % (packet["id"], rig, pr_url, attempts, max_attempts, report))


def start_session(city, rig, packet, pr_url, report, attempts, max_attempts, dry_run):
    """One triage bead, slung at one short session. Returns the bead id."""
    if dry_run:
        fl.log(ME, "%s: would spend a triage session (attempt %d of %d)" % (packet["id"], attempts, max_attempts))
        return ""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(bead_body(packet, rig, pr_url, report, attempts, max_attempts))
    meta = {"proof.ci_for": packet["id"], "proof.ci_pr": pr_url,
            "proof.ci_started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        out = admit.gc(city, "bd", "--rig", rig, "create", "Triage the red pull request of %s" % packet["id"],
                       "--body-file", fh.name, "--metadata", json.dumps(meta), "--json")
    finally:
        os.unlink(fh.name)
    created = json.loads(out.stdout)
    bead_id = (created[0] if isinstance(created, list) else created)["id"]
    slung = admit.gc(city, "sling", "%s/%s" % (rig, TRIAGE), bead_id, "--on", FORMULA,
             "--var", "packet_id=" + packet["id"], "--var", "pr_url=" + pr_url)
    root = re.search(r"workflow (\S+)", slung.stdout or "")
    if root:
        fl.set_bead(city, rig, bead_id, proof__ci_root=root.group(1))
    fl.log(ME, "%s: triage %s slung to %s/%s (attempt %d of %d)"
           % (packet["id"], bead_id, rig, TRIAGE, attempts, max_attempts))
    return bead_id


def do_rerun(slug, clone, cause, dry_run):
    """Re-run the FAILED JOBS of the run that is already there. Never a cancel,
    and never a fresh dispatch: both take something away from someone else."""
    run_id = (cause or {}).get("run_id") or ""
    if not run_id:
        return "", "the failing check has no workflow run to re-run"
    if dry_run:
        return "rerun:%s" % run_id, "would re-run the failed jobs of run %s" % run_id
    out = fl.sh(["gh", "run", "rerun", run_id, "--failed", "--repo", slug], cwd=clone)
    if out.returncode != 0:
        detail = (out.stderr or out.stdout).strip()
        if fl.rerun_already_going(detail):
            return "rerun:%s" % run_id, "re-run already running for run %s" % run_id
        return "", "re-run refused: %s" % detail[-200:]
    return "rerun:%s" % run_id, "re-ran the failed jobs of run %s" % run_id


# `gc bd update --if-status` writes nothing and exits 13 when the bead is no
# longer in the expected status (gc 1.4.2). Another writer moved it first.
GUARD_MISSED = 13
# Stamped on the work bead before a session is spent. Not a bead id. `stage`
# then waits, and a session that never started is unstamped — signature, bead
# and attempt count put back as they were — so the next pass can judge and an
# attempt is spent only when a session actually starts. Two overlapping passes
# both observe `judge`; the second finds this and spends nothing. `proof.ci_since`
# is when the claim was written. The order's own timeout is 20m
# (proof-ci-triage.toml): a pending claim older than that is not a live
# session. The next pass decides from the pre-claim metadata. It does not
# release the claim first — that release matches any pending claim for the
# same red, including one another pass just took. `claim_judge` replaces an
# expired claim under the lock.
CLAIM = "pending"
CLAIM_TTL_S = 20 * 60
# Stored when the value being replaced was absent. A bead id and a signature
# never look like this.
PRIOR_ABSENT = "-"
_CLAIM_KEYS = ("proof.ci_signature", "proof.ci_bead", "proof.ci_attempts",
               "proof.ci_since", "proof.ci_prior_signature", "proof.ci_prior_bead",
               "proof.ci_prior_attempts")


def restored_md(md):
    """`md` with a pending claim removed and the pre-claim signature, bead and
    attempt count put back. Other keys are left as they are."""
    out = dict(md)
    for key in ("proof.ci_since", "proof.ci_prior_signature", "proof.ci_prior_bead", "proof.ci_prior_attempts"):
        out.pop(key, None)
    sig = md.get("proof.ci_prior_signature", PRIOR_ABSENT)
    bead = md.get("proof.ci_prior_bead", PRIOR_ABSENT)
    attempts = str(md.get("proof.ci_prior_attempts") or "0")
    if sig != PRIOR_ABSENT:
        out["proof.ci_signature"] = sig
    else:
        out.pop("proof.ci_signature", None)
    if bead != PRIOR_ABSENT:
        out["proof.ci_bead"] = bead
    else:
        out.pop("proof.ci_bead", None)
    if attempts not in ("", "0"):
        out["proof.ci_attempts"] = attempts
    else:
        out.pop("proof.ci_attempts", None)
    return out


def live_md(md):
    """The metadata a decision should read. A pending claim younger than the
    order timeout stays; an older one reads as the memory from before it."""
    if md.get("proof.ci_bead") == CLAIM and fl.claim_stale(md.get("proof.ci_since"), CLAIM_TTL_S):
        return restored_md(md)
    return md


def claim_judge(city, rig, work_id, sig, attempts):
    """Stamp the work bead before spending a session. Returns `ok` or `stale`.

    The short state lock makes the re-read and the stamp one step. `gc bd
    update` can guard status and assignee, not metadata, so the lock is what
    makes two passes unable to both observe an unclaimed bead. The signature,
    bead and attempt count this stamp replaces are kept on the bead, so a
    session that never starts can be put back without consuming an attempt."""
    with fl.state_lock(city):
        packets = fl.packets(city, rig)
        current = next((p for p in packets if p["bead"]["id"] == work_id), None)
        md = (current or {}).get("md") or {}
        if md.get("proof.pr_recovery_state") in ("launching", "running", "escalated"):
            return "stale"
        expired = md.get("proof.ci_bead") == CLAIM and fl.claim_stale(md.get("proof.ci_since"), CLAIM_TTL_S)
        if md.get("proof.ci_bead") and md.get("proof.ci_signature") == sig and not expired:
            return "stale"
        if expired:
            spent = int(md.get("proof.ci_prior_attempts") or 0)
        else:
            spent = int(md.get("proof.ci_attempts") or 0)
        if spent >= attempts:
            return "stale"
        if md.get("proof.ci_bead") == CLAIM:
            prior_sig = md.get("proof.ci_prior_signature", PRIOR_ABSENT)
            prior_bead = md.get("proof.ci_prior_bead", PRIOR_ABSENT)
            prior_attempts = str(md.get("proof.ci_prior_attempts") or "0")
        else:
            prior_sig = md.get("proof.ci_signature") or PRIOR_ABSENT
            prior_bead = md.get("proof.ci_bead") or PRIOR_ABSENT
            prior_attempts = str(md.get("proof.ci_attempts") or "0")
        wrote = fl.set_bead(city, rig, work_id, proof__ci_signature=sig, proof__ci_bead=CLAIM,
                            proof__ci_attempts=attempts,
                            proof__ci_since=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            proof__ci_prior_signature=prior_sig, proof__ci_prior_bead=prior_bead,
                            proof__ci_prior_attempts=prior_attempts)
        if wrote.returncode != 0:
            return "stale"
        return "ok"


def release_judge(city, rig, work_id, sig):
    """Drop a claim whose session never started, and put back what it replaced.

    `sig` is the signature the claim stamped. A claim for a different red is
    left alone. The previous attempt count comes back, so a session that never
    started does not count toward the cap."""
    with fl.state_lock(city):
        packets = fl.packets(city, rig)
        current = next((p for p in packets if p["bead"]["id"] == work_id), None)
        md = (current or {}).get("md") or {}
        if md.get("proof.ci_bead") != CLAIM or md.get("proof.ci_signature") != sig:
            return
        restored = restored_md(md)
        sets, unsets = {}, []
        for key in _CLAIM_KEYS:
            if key in restored:
                if str(md.get(key)) != str(restored[key]):
                    sets["proof__" + key.split(".", 1)[1]] = restored[key]
            elif key in md:
                unsets.append(key)
        if sets:
            wrote = fl.set_bead(city, rig, work_id, **sets)
            if wrote.returncode != 0:
                return
        if unsets:
            fl.unset_bead(city, rig, work_id, *unsets)


def guarded_status(city, rig, bead_id, status, expected, note):
    """Set `status` only while the bead is still `expected`.

    The note rides the same update, so a miss writes neither. Exit 13 is that
    miss: the caller publishes nothing else and lets the next pass observe."""
    return admit.gc(city, "bd", "--rig", rig, "update", bead_id,
                    "--status", status, "--if-status", expected,
                    "--append-notes", note, check=False)


def do_resume(city, rig, packet, report, reason, dry_run):
    """Put the packet back in front of its own worker, on its own commit.

    A passed packet's work bead was closed by its report step. Open that exact
    bead, and only while it is still closed, so admission preserves its identity
    and records its retained HEAD, rather than refusing a finished execution.
    Superseded failed attempts are separately recoverable through their recorded
    workspace provenance; that does not authorize rerunning a successfully
    finished packet.

    Returns `("stale", why)` when the guard misses. The caller logs that and
    records nothing else: no mail, no `proof.ci_acted`."""
    bead_id, landing = packet["bead"]["id"], packet["md"].get("proof.landing") or ""
    if landing not in admit.LANDING:
        return "", "packet has no landing level to re-admit with"
    if dry_run:
        return "resume:%s" % bead_id, "would reopen %s and re-admit %s (%s)" % (bead_id, packet["id"], landing)
    reopened = guarded_status(
        city, rig, bead_id, "open", "closed",
        "CI triage: the pull request went red and the worker resumes on its own commit")
    if reopened.returncode == GUARD_MISSED:
        return "stale", ("work bead %s was no longer closed; nothing done, the next pass reads it again"
                         % bead_id)
    if reopened.returncode != 0:
        return "", "could not reopen %s: %s" % (bead_id, (reopened.stderr or reopened.stdout).strip()[-200:])
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write("CI triage %s — the pull request for this packet went red and a triage session judged the "
                 "failure to be this packet's own.\n\n%s\n\n%s\n"
                 % (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), reason, report))
    try:
        admit.gc(city, "bd", "--rig", rig, "note", bead_id, "--file", fh.name, check=False)
    finally:
        os.unlink(fh.name)
    argv = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "admit.py"),
            packet["id"], "--rig", rig, "--landing", landing, "--city", city]
    if had_review(fl.rig_beads(city, rig), (packet.get("root") or {}).get("id")):
        argv.append("--review")
    done = fl.sh(argv)
    if done.returncode != 0:
        # Put the bead back. An open work bead with no workflow behind it is read
        # as a packet in flight by everything that looks — `gc proof status`, the
        # keeper, a person — and there is no packet in flight: admission refused.
        # Undo what this function did and say why. The usual reasons are honest
        # ones a person must see: the claim has less than an hour left, or the
        # item changed on main since the bead was admitted.
        undone = guarded_status(
            city, rig, bead_id, "closed", "open",
            "CI triage: re-admission was refused, so the packet is where it was")
        if undone.returncode == GUARD_MISSED:
            # The bead moved, so nothing is stamped and nothing is mailed. The
            # refusal text is still why a person would have been told: keep it
            # on the stale line, because the next pass may not reproduce it.
            why = (done.stderr or done.stdout).strip()[-300:]
            return "stale", ("work bead %s moved while re-admission was refused (%s); nothing further recorded, "
                             "the next pass reads it again" % (bead_id, why))
        return "", "re-admission refused: %s" % (done.stderr or done.stdout).strip()[-300:]
    return "resume:%s" % bead_id, "reopened %s and re-admitted %s (%s)" % (bead_id, packet["id"], landing)


def facts(packet, rig, pr_url, report):
    return "packet  %s\nbead    %s (rig %s)\npr      %s\n\n%s" % (
        packet["id"], packet["bead"]["id"], rig, pr_url, report)


def triage_bead(beads, bead_id):
    """Read the logical classify result, linked through this exact input bead.

    The input is evidence, not the worker's assigned step. Graph-v2 propagates
    the attempt's verdict to the logical classify step, never to that input.
    Legacy runs are recovered by their convoy's tracks edge, not by packet name
    or timestamps (a packet can have several different failures).
    """
    by_id = {b["id"]: b for b in beads}
    source = by_id.get(bead_id)
    if source is None:
        return None
    md = source.get("metadata") or {}
    if source.get("status") == "closed" and md.get("proof.ci_verdict") in VERDICTS:
        return source                     # pre-workflow result format
    convoys = {b["id"] for b in beads if any(
        d.get("type") == "tracks" and d.get("depends_on_id") == bead_id
        for d in b.get("dependencies") or [])}
    roots = [b for b in beads if (b.get("metadata") or {}).get("gc.kind") == "workflow"
             and (b.get("metadata") or {}).get("gc.formula_name") == FORMULA
             and (b.get("metadata") or {}).get("gc.input_convoy_id") in convoys
             and (b.get("metadata") or {}).get("gc.var.packet_id") == md.get("proof.ci_for")
             and (b.get("metadata") or {}).get("gc.var.pr_url") == md.get("proof.ci_pr")]
    if md.get("proof.ci_root"):
        roots = [b for b in roots if b["id"] == md["proof.ci_root"]]
    if len(roots) != 1:
        return source                     # absent/ambiguous linkage: never guess
    root = roots[0]
    if root.get("status") != "closed":
        return source
    results = [b for b in beads if (b.get("metadata") or {}).get("gc.root_bead_id") == root["id"]
               and (b.get("metadata") or {}).get("gc.step_id") == "classify"
               and not (b.get("metadata") or {}).get("gc.control_for")]
    if (root.get("metadata") or {}).get("gc.outcome") == "pass" and len(results) == 1 \
            and results[0].get("status") == "closed":
        return results[0]
    # A finished workflow without a successful, unique result needs a person.
    return {"id": bead_id, "status": "closed", "metadata": {}}



def open_prs(slug, branch, clone):
    """The OPEN pull requests from `branch`, or None when GitHub could not say."""
    listed = fl.sh(["gh", "pr", "list", "--repo", slug, "--head", branch, "--state", "open",
                    "--json", "number,url,headRefOid,statusCheckRollup"], cwd=clone)
    if listed.returncode != 0:
        return None
    try:
        return json.loads(listed.stdout or "[]")
    except ValueError:
        return None


def pr_now(slug, number, clone):
    """One pull request as it is right now, or None when GitHub could not say."""
    out = fl.sh(["gh", "pr", "view", str(number), "--repo", slug, "--json", "state,headRefOid,statusCheckRollup"],
                cwd=clone)
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout or "{}")
    except ValueError:
        return None


def red_packets(city, rig, clone, slug):
    """Every packet of a rig whose OPEN pull request has a failed check.

    A merged one is the completer's, a closed one is a person's, and a packet
    that never got as far as a pull request stopped for a reason a triage
    cannot read from CI."""
    out = []
    for p in fl.packets(city, rig):
        if p["md"].get("proof.pr_recovery_state") in ("launching", "running", "escalated"):
            continue
        if not awaiting_pr(p):
            continue
        prs = open_prs(slug, p["md"].get("proof.branch") or "", clone)
        if not prs:
            continue
        pr = sorted(prs, key=lambda x: x["number"])[-1]
        found = ci_failure.failures(pr.get("statusCheckRollup") or [])
        if found:
            out.append((p, pr, found))
    return out


def awaiting_pr(packet):
    """A passed packet whose pull request is its landing and is not complete."""
    md = packet["md"]
    return (packet["state"] == "passed" and md.get("proof.landing") in ("pr", "automerge")
            and not md.get("proof.completed") and bool(md.get("proof.branch")))


def observe(city, ci, rigs, active=None):
    """Every red pull request of every active rig, read with NO lock held, as
    snapshots: the packet's exact bead and attempt, the pull request's head, the
    failure and its signature — and, for a red about to be judged, the failing
    job's log excerpt, the slowest read there is. Nothing here writes."""
    seen = []
    for rig in sorted(rigs):
        if active is not None and rig not in active:
            continue
        try:
            clone = admit.healthy_clone(rigs, rig)
        except fl.Refused:
            continue
        slug = fl.repo_slug(clone)
        beads = fl.rig_beads(city, rig)
        for packet, pr, found in red_packets(city, rig, clone, slug):
            md, sig = packet["md"], signature(found)
            cause, symptoms = ci_failure.causal(found)
            what, _why = stage(md, triage_bead(beads, md.get("proof.ci_bead")), sig, ci["max_attempts"])
            log = ci_failure.excerpt(ci_failure.job_log(slug, (cause or {}).get("job_id"), clone)) \
                if what == "judge" else None
            seen.append({"rig": rig, "clone": clone, "slug": slug, "packet": packet["id"],
                         "bead": packet["bead"]["id"], "root": (packet.get("root") or {}).get("id") or "",
                         "pr": {"number": pr["number"], "url": pr["url"], "head": pr.get("headRefOid") or ""},
                         "found": found, "sig": sig, "cause": cause, "symptoms": symptoms, "log": log})
    return seen


def still(snap, packet):
    """'' when `packet` is the exact packet attempt observed, still awaiting its pull request."""
    if packet is None:
        return "its work bead %s is no longer a live packet" % snap["bead"]
    if ((packet.get("root") or {}).get("id") or "") != snap["root"]:
        return "it is on another attempt"
    if not awaiting_pr(packet):
        return "it is no longer a passed packet awaiting its pull request"
    return ""


def recheck(snap):
    """'' when the pull request is still open, on the same head, with the same
    failure; else what changed. One read, on the publish half."""
    now = pr_now(snap["slug"], snap["pr"]["number"], snap["clone"])
    if now is None:
        return "its pull request could not be read again"
    if now.get("state") != "OPEN":
        return "its pull request is %s" % (now.get("state") or "unreadable").lower()
    if (now.get("headRefOid") or "") != snap["pr"]["head"]:
        return "its pull request moved to %s" % (now.get("headRefOid") or "?")[:10]
    found = ci_failure.failures(now.get("statusCheckRollup") or [])
    if signature(found) != snap["sig"]:
        return "its failure changed"
    if (ci_failure.causal(found)[0] or {}).get("job_id") != (snap["cause"] or {}).get("job_id"):
        return "its causal job changed"
    return ""


def triage(city, policy, ci, rigs, said, dry_run, seen, active=None):
    """Publish what the snapshots call for. No factory lock: `policy`, `ci`,
    `rigs`, `active` and `said` are read again here, and so are the packets
    and beads, because any of them may have changed while the snapshot was read."""
    live, beads = {}, {}
    for snap in seen:
        rig, pid = snap["rig"], snap["packet"]
        if active is not None and rig not in active:
            said.say("stale:" + pid, "%s: rig %s was suspended or withdrawn while it was read; nothing done"
                     % (pid, rig))
            continue
        if rig not in live:
            live[rig] = fl.packets(city, rig)
            beads[rig] = fl.rig_beads(city, rig, fresh=True)
        packet = next((p for p in live[rig] if p["bead"]["id"] == snap["bead"]), None)
        moved = still(snap, packet) or recheck(snap)
        if moved:
            said.say("stale:" + pid, "%s: %s since it was read; nothing done, the next pass reads it again"
                     % (pid, moved))
            continue
        md, sig, cause, symptoms = packet["md"], snap["sig"], snap["cause"], snap["symptoms"]
        slug, clone, pr_url = snap["slug"], snap["clone"], snap["pr"]["url"]
        if md.get("proof.ci_bead") == CLAIM and fl.claim_stale(md.get("proof.ci_since"), CLAIM_TTL_S):
            # The order that claimed this was killed before it could release.
            # Decide from the pre-claim metadata. Do not release the bead here.
            # `release_judge` re-reads under the lock, but a newer claim for
            # this same red is also pending with this signature, and restoring
            # it would wipe that claim so this pass could take another.
            # `claim_judge` already treats an expired claim as released.
            said.say("stale:" + pid, "%s: judge claim pending since %s outlived the order; "
                                     "deciding from the memory before the claim, without spending an attempt"
                                     % (pid, md.get("proof.ci_since") or "an unknown time"))
            md = live_md(md)
            packet["md"] = md
        what, why = stage(md, triage_bead(beads[rig], md.get("proof.ci_bead")), sig, ci["max_attempts"])
        said.say("stage:" + pid, "%s: %s — %s" % (pid, what, why))
        if what == "wait":
            continue
        report = ci_failure.summarise(cause, symptoms, "", url=pr_url)
        if what == "exhausted":
            notify_once(city, rig, packet, "%s exhausted" % sig, "PROOF CI triage is spent: %s" % pid,
                        facts(packet, rig, pr_url, report) +
                        "\n\n%s. The next move is yours." % why, dry_run)
            continue
        if what == "blind":
            notify_once(city, rig, packet, "%s blind" % sig, "PROOF CI triage recorded no verdict: %s" % pid,
                        facts(packet, rig, pr_url, report) + "\n\n%s." % why, dry_run)
            continue
        if what == "judge":
            if snap["log"] is None:
                said.say("stale:" + pid, "%s: it became ready to judge after it was read; the next pass fetches "
                                         "its log" % pid)
                continue
            budget = fl.budget_hold(policy, rigs)
            if budget:
                said.say("budget", budget)
                continue
            full = ci_failure.summarise(cause, symptoms, snap["log"], url=pr_url)
            attempts = int(md.get("proof.ci_attempts") or 0) + 1
            if dry_run:
                start_session(city, rig, packet, pr_url, full, attempts, ci["max_attempts"], True)
                continue
            # Claim before spending. Both overlapping passes already returned
            # `judge` from the snapshot; the stamp is what the second one sees.
            if claim_judge(city, rig, packet["bead"]["id"], sig, attempts) == "stale":
                said.say("stale:" + pid, "%s: another pass already claimed this red; nothing done, "
                                         "the next pass reads it again" % pid)
                continue
            bead_id = ""
            try:
                bead_id = start_session(city, rig, packet, pr_url, full, attempts, ci["max_attempts"], False)
            finally:
                if bead_id:
                    fl.set_bead(city, rig, packet["bead"]["id"], proof__ci_signature=sig,
                                proof__ci_bead=bead_id, proof__ci_attempts=attempts)
                    fl.unset_bead(city, rig, packet["bead"]["id"], "proof.ci_since",
                                  "proof.ci_prior_signature", "proof.ci_prior_bead", "proof.ci_prior_attempts")
                else:
                    release_judge(city, rig, packet["bead"]["id"], sig)
            continue
        # act — `stage` returns the recorded verdict as its reason
        verdict = why
        do, refused = action_for(verdict, rig, ci)
        if do == "rerun":
            acted, told = do_rerun(slug, clone, cause, dry_run)
        elif do == "resume":
            budget = fl.budget_hold(policy, rigs)
            if budget:
                said.say("budget", budget)
                continue
            acted, told = do_resume(city, rig, packet, report, verdict, dry_run)
        else:
            acted, told = "mail", refused
        if acted == "stale":
            # The guarded write lost the race. Publish nothing else and record
            # nothing else; the next pass observes again.
            said.say("stale:" + pid, "%s: %s" % (pid, told))
            continue
        if not acted:
            notify_once(city, rig, packet, "%s acted" % sig, "PROOF CI triage could not act: %s" % pid,
                        facts(packet, rig, pr_url, report) +
                        "\n\nverdict %s, and %s" % (verdict, told), dry_run)
            continue
        if acted == "mail":
            notify_once(city, rig, packet, "%s acted" % sig, "PROOF CI triage: %s is %s" % (pid, verdict),
                        facts(packet, rig, pr_url, report) + "\n\n%s." % told, dry_run)
        fl.log(ME, "%s: verdict %s — %s" % (pid, verdict, told))
        if not dry_run:
            fl.set_bead(city, rig, packet["bead"]["id"], proof__ci_acted="%s %s (%s)" % (sig, acted, verdict))


def wanted(policy, dry_run):
    """Whether the policy lets this order do anything at all."""
    if not settings(policy)["enabled"]:
        fl.log(ME, "[ci_triage] enabled is not set in the policy; a red pull request is mailed and nothing else")
        return False
    if not policy["enabled"] and not dry_run:
        fl.log(ME, "policy is not enabled; no triage")
        return False
    return True


def publish(city, dry_run, seen, observed):
    """The second half of a pass: read again everything a decision rests on —
    the policy and its authorization tables (`observed` is the table the
    snapshots were read under), the limit hold and rig suspension —
    then triage the snapshots still true. Takes no `factory.lock`. A judge
    claims the work bead under the short `factory-state` lock before it
    spends a session, and `close` merges that file under the same lock."""
    policy = fl.load_policy(city)
    if not wanted(policy, dry_run):
        return
    said = fl.Standing(city, ME, keep=not dry_run)
    try:
        if settings(policy) != observed:
            # The authorization the snapshots were read under is not the one in
            # force: discard them rather than act on either reading.
            said.say("stale:policy", "[ci_triage] changed while its pull requests were read; nothing done, the next pass reads it again")
            return
        why = fl.limit_hold(said.state)
        if why:
            said.say("limit-hold", "no triage session: %s" % why)
            return
        rigs = admit.load_rigs(city)
        active = fl.active_rigs(rigs, fl.rig_states(city), policy)
        triage(city, policy, settings(policy), rigs, said, dry_run, seen, active=active)
    finally:
        said.close()


def one_pass(city, dry_run):
    """Observe, then revalidate and publish. Neither half opens `factory.lock`:
    a factory pass must not starve this order. A judge claims its work bead
    before spending a session. Every other write is repeat-safe or a guarded
    bead update. `factory-state.json` is merged under its own short lock."""
    policy = fl.load_policy(city)
    if not wanted(policy, dry_run):
        return 0
    observed, seen = settings(policy), []
    if not fl.limit_hold(fl.Standing(city, ME, keep=False).state):
        rigs = admit.load_rigs(city)
        seen = observe(city, observed, rigs, fl.active_rigs(rigs, fl.rig_states(city), policy))
    publish(city, dry_run, seen, observed)
    return 0


def main(argv):
    # Imported here, not at the top: the pure rules above are what the tests
    # exercise, and they must not need the dispatcher's import chain to do it.
    import plan_trigger                              # its log tee

    city = fl.city_dir()
    plan_trigger.log_to_factory(city)
    return one_pass(city, "--dry-run" in argv)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except fl.Refused as why:
        fl.log(ME, "stopped — %s" % why)
        sys.exit(1)
