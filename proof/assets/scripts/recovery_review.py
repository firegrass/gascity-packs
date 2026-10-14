#!/usr/bin/env python3
"""Request independent review without abandoning an admitted recovery-trial packet.

require STEP records the need for review. submit STEP freezes a clean candidate
and routes a raw task to the existing reviewer pool. wait STEP waits in this
script, not in model turns. verify ROOT is the implementation gate's exact-tree
receipt check. No command approves work, changes scope or closes graph controls.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import tomllib

import admit


def variable(root, name):
    md = root.get("metadata") or {}
    value = md.get("gc.var." + name)
    if value is None:
        values = md.get("gc.graphv2_vars.v1") or {}
        if isinstance(values, str):
            values = json.loads(values)
        value = values.get(name, "")
    return str(value).lower() if isinstance(value, bool) else str(value)


def trial_for(city, rig, packet, history):
    """Explicit ids only, one trial admission per item; existing work is unchanged."""
    path = Path(city) / "proof/policy.toml"
    if not path.exists():
        return ""
    policy = tomllib.loads(path.read_text())
    cfg = policy.get("recovery_trial") or {}
    if cfg.get("enabled") is not True:
        return ""
    identity, items, rigs = cfg.get("id"), cfg.get("items"), cfg.get("rigs")
    if (not isinstance(identity, str) or not re.fullmatch(r"[a-z0-9-]{1,80}", identity)
            or not isinstance(items, list) or not 1 <= len(items) <= 6
            or any(not isinstance(p, str) or not admit.ID_RE.fullmatch(p) for p in items)
            or not isinstance(rigs, list) or any(not isinstance(r, str) for r in rigs)):
        raise admit.Refused("recovery_trial needs an id, rigs and one to six exact packet ids")
    if packet not in items or rig not in rigs:
        return ""
    if any((b.get("metadata") or {}).get("proof.recovery_trial") == identity for b in history):
        return ""
    return identity


def provenance(city):
    """Diagnostic run identity, never an authorization or a completion receipt."""
    result = {"proof.engine_commit": "unknown", "proof.pack_tree": "unknown"}
    try:
        payload = json.loads(admit.gc(city, "version", "--json").stdout)
        if not isinstance(payload, dict):
            raise ValueError("engine identity is not an object")
        value = payload.get("commit", "")
        if re.fullmatch(r"[0-9a-f]{40}", value):
            result["proof.engine_commit"] = value
    except (admit.Refused, ValueError, OSError, TypeError):
        result["proof.provenance_gap"] = "engine identity unavailable"
    try:
        for line in (Path(city) / "proof/deployed-pack.txt").read_text().splitlines():
            if re.fullmatch(r"pack_tree=[0-9a-f]{40}", line):
                result["proof.pack_tree"] = line.split("=", 1)[1]
    except OSError:
        result["proof.provenance_gap"] = "installed pack receipt unavailable"
    return result


def trial_root(root):
    if root.get("status") == "closed" or (root.get("metadata") or {}).get("gc.kind") != "workflow":
        raise admit.Refused("review recovery needs an active packet workflow")
    if not re.fullmatch(r"[a-z0-9-]{1,80}", variable(root, "recovery_trial")):
        raise admit.Refused("this packet was not admitted to the recovery trial")
    if not admit.ID_RE.fullmatch(variable(root, "packet_id")):
        raise admit.Refused("workflow has no valid packet identity")


def allowed_step(step, root):
    trial_root(root)
    md = step.get("metadata") or {}
    if (step.get("status") == "closed" or md.get("gc.kind") not in (None, "", "task")
            or md.get("gc.root_bead_id") != root.get("id")):
        raise admit.Refused("request must come from an open task in this workflow")
    ref = md.get("gc.step_ref") or ""
    if not re.search(r"(?:^|\.)(?:load-context|workspace)\.attempt\.\d+$|(?:^|\.)implement\.iteration\.\d+(?:\.code)?$", ref):
        raise admit.Refused("review can be requested only before implementation completes")


def receipt(root, request, tree):
    """Return a verdict only for a closed, independently routed, matching review."""
    md = request.get("metadata") or {}
    expected = md.get("proof.review_agent")
    routed = md.get("gc.routed_to") or md.get("gc.execution_routed_to")
    repo = (root.get("metadata") or {}).get("proof.repo")
    if (md.get("proof.review_parent") != root.get("id")
            or md.get("proof.review_packet") != variable(root, "packet_id")
            or md.get("proof.review_candidate_tree") != tree
            or not repo or expected != repo + "/proof.reviewer"
            or routed != expected):
        raise admit.Refused("review receipt has the wrong packet, tree or reviewer route")
    if request.get("status") != "closed":
        return "pending"
    session = md.get("gc.session_name") or ""
    if not re.search(r"(?:^|[./_-])reviewer(?:[./_-]|$)", session):
        raise admit.Refused("review was not claimed by an independent reviewer session")
    if (md.get("gc.outcome") != "pass" or md.get("proof.reviewed_tree") != tree
            or md.get("proof.review_verdict") not in ("approve", "iterate", "reject")):
        raise admit.Refused("closed review has no valid verdict for this exact tree")
    return md["proof.review_verdict"]


class Store:
    def __init__(self, city, rig):
        self.city, self.rig = city, rig

    def gc(self, *args):
        return admit.gc(self.city, *args)

    def bd(self, *args):
        return self.gc("bd", "--rig", self.rig, *args)

    def show(self, identity):
        value = json.loads(self.bd("show", identity, "--json").stdout)
        return value[0] if isinstance(value, list) else value

    def requests(self, root):
        return json.loads(self.bd("list", "--all", "--limit=0", "--metadata-field",
                                 "proof.review_parent=" + root["id"], "--json").stdout)

    def require(self, root):
        # The request persists before approval is cleared; the implementation
        # gate cannot publish a gate-only approval once this marker is present.
        self.bd("update", root["id"], "--set-metadata", "proof.review_requested=true",
                "--unset-metadata", "proof.approved_tree", "--unset-metadata", "proof.approved_commit",
                "--unset-metadata", "proof.approved_by")


def candidate(root):
    directory, branch = variable(root, "work_dir"), variable(root, "branch")
    if not directory or not branch:
        raise admit.Refused("packet has no workspace identity")
    def git(*args):
        return admit.run(["git", "-C", directory, *args]).stdout.strip()
    if git("branch", "--show-current") != branch or git("status", "--porcelain"):
        raise admit.Refused("commit this packet's work on its named branch before requesting review")
    return directory, branch, git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")


@contextlib.contextmanager
def request_lock(city, root):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", root):
        raise admit.Refused("invalid workflow identity")
    directory = Path(city) / "proof/review-locks"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / (root + ".lock")).open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def submit(store, root):
    directory, branch, commit, tree = candidate(root)
    requests = store.requests(root)
    matches = [b for b in requests if (b.get("metadata") or {}).get("proof.review_candidate_tree") == tree]
    if len(matches) > 1:
        raise admit.Refused("multiple reviews name this candidate; preserve them for reconciliation")
    if matches:
        request = matches[0]
    else:
        if len(requests) >= 3:
            raise admit.Refused("three candidates have been reviewed; preserve findings and ask for a design decision")
        target = store.rig + "/proof.reviewer"
        metadata = {"proof.review_parent": root["id"], "proof.review_packet": variable(root, "packet_id"),
                    "proof.review_candidate_commit": commit, "proof.review_candidate_tree": tree,
                    "proof.review_agent": target, "proof.recovery_trial": variable(root, "recovery_trial")}
        body = (f"Review packet {variable(root, 'packet_id')} for workflow {root['id']}.\n\n"
                f"Read-only independent review of {commit}, tree {tree}, branch {branch} in {directory}.\n"
                "Read the parent's work bead through its convoy, its acceptance criteria and repository doctrine. "
                "This is a same-packet review request; no new implementation or publication is authorized. "
                "Compare the candidate to origin/main. Verify HEAD still names the candidate before and after review. "
                "Read prior review requests with proof.review_parent equal to this workflow and preserve their findings. "
                "Do not edit the worktree. Put findings and concrete reproducers in this bead's notes. "
                "On THIS bead record proof.review_verdict=approve|iterate|reject, proof.reviewed_tree, "
                "gc.outcome=pass, gc.work_outcome=no-op and status closed. "
                "A changed candidate must never be approved. The parent gate, not this review task, authorizes publication.\n")
        with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8") as handle:
            handle.write(body); handle.flush()
            created = json.loads(store.bd("create", "Independent review: " + variable(root, "packet_id"),
                                          "--body-file", handle.name, "--metadata", json.dumps(metadata), "--json").stdout)
        request = created[0] if isinstance(created, list) else created
    # Recover a crash between create and sling using the same durable request.
    md = request.get("metadata") or {}
    if request.get("status") != "closed" and not (md.get("gc.routed_to") or md.get("gc.execution_routed_to")):
        store.gc("sling", store.rig + "/proof.reviewer", request["id"], "--no-formula", "--no-convoy")
    return store.show(request["id"]), tree


def verify(store, root):
    trial_root(root)
    _directory, _branch, _commit, tree = candidate(root)
    matches = [b for b in store.requests(root)
               if (b.get("metadata") or {}).get("proof.review_candidate_tree") == tree]
    if len(matches) != 1 or receipt(root, matches[0], tree) != "approve":
        raise admit.Refused("independent recovery review has not approved the exact candidate")
    return matches[0]["id"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("require", "submit", "wait", "verify"))
    parser.add_argument("bead")
    parser.add_argument("--city", default=os.environ.get("GC_CITY") or os.environ.get("GC_CITY_PATH"))
    parser.add_argument("--rig", default=os.environ.get("GC_RIG"))
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args(argv)
    city = args.city or admit.city_root()
    if not args.rig:
        raise admit.Refused("--rig is required")
    if not 0 <= args.timeout <= 3600:
        raise admit.Refused("timeout must be between 0 and 3600 seconds")
    store = Store(city, args.rig)
    bead = store.show(args.bead)
    root = bead if args.action == "verify" else store.show((bead.get("metadata") or {}).get("gc.root_bead_id", ""))
    if args.action == "verify":
        print(verify(store, root)); return 0
    allowed_step(bead, root)
    if variable(root, "review") == "true":
        print("Independent review is already scheduled in this packet; continue to its review cycle."); return 0
    with request_lock(city, root["id"]):
        root = store.show(root["id"])
        allowed_step(store.show(args.bead), root)
        if (root.get("metadata") or {}).get("proof.repo") != args.rig:
            raise admit.Refused("review request rig does not match the admitted packet repository")
        store.require(root)
        if args.action == "require":
            print("Review required; retain this packet, implement, then submit the clean candidate."); return 0
        request, tree = submit(store, root)
    deadline = time.monotonic() + args.timeout
    announced = False
    while True:
        verdict = receipt(root, request, tree)
        if not announced or verdict != "pending" or time.monotonic() >= deadline:
            print(json.dumps({"review": request["id"], "verdict": verdict, "tree": tree,
                              "findings": request.get("notes") or ""}), flush=True)
            announced = True
        if verdict != "pending" or args.action != "wait" or time.monotonic() >= deadline:
            return {"approve": 0, "iterate": 3, "reject": 2, "pending": 75}[verdict]
        time.sleep(min(20, max(0, deadline - time.monotonic())))
        trial_root(store.show(root["id"]))
        if candidate(root)[3] != tree:
            raise admit.Refused("candidate changed while review was pending; retain findings and submit the new tree")
        request = store.show(request["id"])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (admit.Refused, ValueError, OSError, subprocess.SubprocessError) as exc:
        print("recovery-review: " + str(exc), file=sys.stderr)
        sys.exit(1)
