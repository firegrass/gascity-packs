#!/usr/bin/env python3
"""Observe conflicting packet PRs and own bounded recovery through a native formula.

The original packet remains passed/awaiting merge, retaining its scope claim.
Recovery gets an isolated worktree and fresh gate/review receipts. Its durable
record owns the PR writer until publication or explicit escalation settlement.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import factory_lib as fl
from factory_lib import admit

FORMULA = "mol-proof-pr-recovery"
ACTIVE = {"launching", "running", "escalated"}
DEFAULTS = {"enabled": False, "rigs": [], "max_attempts": 2, "max_hours": 6}
BUNDLE = "crates/slipway-policy-client-contract/bundle/policy-client-bundle.json"
# Conflicts a repository-owned generator settles without a model's judgement:
# each row is (repo, the conflicting paths it covers, the side to take before
# regenerating, the generator commands, the paths to stage afterwards). The
# generator reads the merged tree, so which side is taken first is immaterial
# except for `Cargo.lock`, where main's lock plus `cargo update --workspace`
# re-adds only the branch's own workspace entries (BKLG-20261016-dk56).
REGENERABLE = (
    ("liskov-rs", (BUNDLE,), "--ours",
     ([sys.executable, "scripts/liskov/generate-policy-client-bundle.py", "--write"],),
     (BUNDLE,
      "crates/slipway-policy-client-contract/bundle/policy-client-contract.wasm.gz",
      "crates/slipway-policy-client-contract/bundle/policy-client.cjs",
      "crates/slipway-policy-client-contract/bundle/policy-client.d.ts",
      "crates/slipway-policy-client-contract/bundle/policy-client-conformance.json")),
    ("liskov-rs", ("scripts/liskov/services-cargo-metadata.json", "scripts/liskov/services-census.json"), "--ours",
     ([sys.executable, "scripts/liskov/validate-services.py", "--write-metadata-fixture"],
      [sys.executable, "scripts/liskov/validate-services.py", "--write-census"]),
     ("scripts/liskov/services-cargo-metadata.json", "scripts/liskov/services-census.json")),
    ("liskov-rs", ("Cargo.lock",), "--theirs",
     (["cargo", "update", "--workspace"],),
     ("Cargo.lock",)),
)


def regeneration_plan(repo, conflicts):
    """The REGENERABLE rows that settle some of `conflicts`, and what is left.

    A row applies when any of its paths conflicts (the generator rewrites the
    whole set); a partial census conflict is still regenerated whole. The
    remainder is the model's to resolve."""
    rows = []
    remaining = set(conflicts)
    for row in REGENERABLE:
        if row[0] == repo and remaining & set(row[1]):
            rows.append(row)
            remaining -= set(row[1])
    return rows, sorted(remaining)


def now():
    return datetime.now(timezone.utc)


def stamp():
    return now().strftime("%Y-%m-%dT%H:%M:%SZ")


def settings(policy):
    cfg = {**DEFAULTS, **policy.get("pr_recovery", {})}
    if set(cfg) != set(DEFAULTS) or type(cfg["enabled"]) is not bool:
        raise fl.Refused("invalid pr_recovery policy")
    if not isinstance(cfg["rigs"], list) or any(not isinstance(r, str) or not r for r in cfg["rigs"]):
        raise fl.Refused("pr_recovery.rigs must be an explicit allowlist")
    for k, ceiling in (("max_attempts", 3), ("max_hours", 24)):
        if type(cfg[k]) is not int or not 1 <= cfg[k] <= ceiling:
            raise fl.Refused("invalid pr_recovery." + k)
    return cfg


def run(argv, cwd=None, check=True):
    out = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=180)
    if check and out.returncode:
        raise fl.Refused("%s: %s" % (argv[0], (out.stderr or out.stdout)[-1500:]))
    return out


def git(path, *args, check=True):
    return run(["git", "-C", str(path), *args], check=check).stdout.strip()


def gh(slug, number):
    return json.loads(run(["gh", "pr", "view", str(number), "--repo", slug, "--json",
        "number,url,state,isCrossRepository,headRefName,headRefOid,baseRefName,mergeable,statusCheckRollup"]).stdout)


def key_for(rig, number):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", rig) or not str(number).isdigit():
        raise fl.Refused("invalid recovery identity")
    return "%s-%s" % (rig, number)


def location(city, key):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*-[0-9]+", key):
        raise fl.Refused("invalid recovery key")
    return Path(city) / "proof/pr-recovery" / (key + ".json")


def read(city, key):
    p = location(city, key)
    return json.loads(p.read_text()) if p.exists() else None


def save(city, record):
    fl._write_state(str(location(city, record["key"])), record)


@contextlib.contextmanager
def lock(city, key):
    p = location(city, key).with_suffix(".lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def mark(city, record, state, detail):
    record.update(state=state, detail=detail, updated_at=stamp())
    save(city, record)
    result = fl.set_bead(city, record["rig"], record["work_bead"],
        proof__pr_recovery_state=state, proof__pr_recovery_key=record["key"],
        proof__pr_recovery_detail=detail)
    if result.returncode:
        raise fl.Refused("recovery saved but packet marker failed; next observer pass reconciles it")
    fl.log("pr-recovery", "%s: %s — %s" % (record["packet"], state, detail))


def allowed(city, rig):
    policy = fl.load_policy(city)
    cfg = settings(policy)
    if not policy["enabled"] or not cfg["enabled"] or rig not in cfg["rigs"]:
        raise fl.Refused("PR recovery is not enabled for " + rig)
    rigs = admit.load_rigs(city)
    if rig not in fl.active_rigs(rigs, fl.rig_states(city), policy):
        raise fl.Refused("rig is suspended or disabled")
    return cfg, rigs


def semantic(text, dm):
    fm, body, problems = dm.split_frontmatter(text)
    if problems or not isinstance(fm, dict):
        raise fl.Refused("unreadable packet record")
    # Renewal is not a new claim or a changed implementation packet.
    return {k: str(v) for k, v in fm.items() if k not in ("claim_expires", "last_reviewed")}, body


def claim(city, record, rigs):
    """The packet's claim still belongs to this rig and the item is the one
    admitted. Git: the file's semantic text against the admitted blob. Beads:
    the snapshot row's semantic digest against the digest admission recorded,
    and a live lease — no Markdown is read (ADR-0236)."""
    import work_authority as wa
    backlog = admit.backlog_clone_of(rigs)
    with fl.backlog_write(city):
        admit.refresh_backlog_clone(backlog)
        if wa.beads(backlog):
            wa.forget(backlog)
            row = wa.row(backlog, record["packet"])
            if row is None or row.get("semantic_digest") != record["packet_digest"]:
                raise fl.Refused("packet or claim identity changed since admission")
            fm = row.get("fields") or {}
            item_path = str(Path(backlog) / row["path"])
            if not row.get("lease_id"):
                raise fl.Refused("packet claim expired")
        else:
            item_path, fm, body, dm = admit.load_item(backlog, record["packet"])
            old = git(backlog, "show", record["packet_digest"])
            if semantic(old, dm) != semantic(Path(item_path).read_text(), dm):
                raise fl.Refused("packet or claim identity changed since admission")
        if fm.get("status") != "working" or not admit.held_by_us(fm.get("claimed_by"), record["rig"]):
            raise fl.Refused("packet no longer belongs to this rig")
        if admit.parse_ts(fm.get("claim_expires")) <= now():
            raise fl.Refused("packet claim expired")
    return backlog, item_path


def budget(record, cfg):
    return ((now() - admit.parse_ts(record["started_at"])).total_seconds() < cfg["max_hours"] * 3600)


def roots_for(beads, token):
    return [b for b in beads if (b.get("metadata") or {}).get("gc.kind") == "workflow"
            and (b.get("metadata") or {}).get("gc.var.pr_recovery_token") == token]


def reconcile(city, r, beads, cfg):
    roots = roots_for(beads, r["token"])
    if len(roots) > 1:
        mark(city, r, "escalated", "multiple workflows claim this attempt; owner must reconcile")
    elif roots:
        root = roots[0]
        r["root"] = root["id"]
        # Repair a crash after sling but before stamping the scope/review record.
        md = r["admission"]
        if "proof.review" not in (root.get("metadata") or {}) and not admit.write_admission_record(
                city, r["rig"], root["id"], r["work_dir"], r["branch"],
                md["repo"], md["design"], True, md["scopes"]):
            raise fl.Refused("could not stamp recovery admission")
        if root.get("status") == "closed" and r["state"] != "published":
            mark(city, r, "escalated", "recovery workflow ended without publication; inspect " + root["id"])
        elif r["state"] == "launching" or (r["state"] == "escalated" and
                r.get("detail", "").startswith("launch needs reconciliation:") and budget(r, cfg)):
            mark(city, r, "running", "workflow " + root["id"])
    elif r["state"] == "launching" and fl.claim_stale(r["attempt_at"], 300):
        mark(city, r, "escalated", "launch uncertain; inspect the recorded input before retrying")
    if r["state"] in ("launching", "running") and not budget(r, cfg):
        mark(city, r, "escalated", "elapsed recovery budget exhausted; publication fenced")
    return r


def launch(city, r, packet, rigs):
    backlog, item_path = claim(city, r, rigs)
    clone = r["clone"]
    git(clone, "fetch", "origin", r["head_branch"], r["base_branch"])
    if git(clone, "ls-remote", "origin", "refs/heads/" + r["head_branch"]).split()[0] != r["head"]:
        raise fl.Refused("PR head changed before launch")
    # One isolated source worktree and build target per recovery attempt.
    script = str(Path(backlog) / "scripts/new-agent-worktree.sh")
    run([script, "--repo", clone, "--name", Path(r["work_dir"]).name,
         "--branch", r["branch"], "--base", r["head"]])
    body = ("Recover PR %s for packet %s. Original work bead %s; original review root %s.\n"
            "Read %s and its referenced doctrine. Acceptance and scope stay binding.\n"
            "Work ONLY in %s on %s. Retained commit %s is this packet's approved work.\n"
            "Recovery record: %s. Preserve both branches' intent. An ambiguous contract needs an owner.\n"
            % (r["url"], r["packet"], r["work_bead"], r["parent_root"], item_path,
               r["work_dir"], r["branch"], r["head"], location(city, r["key"])))
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body)
    try:
        out = admit.gc(city, "bd", "--rig", r["rig"], "create", "Recover conflicting PR " + r["url"],
            "--body-file", f.name, "--metadata", json.dumps({"proof.pr_recovery_for": r["packet"],
            "proof.pr_recovery_token": r["token"]}), "--json")
    finally:
        os.unlink(f.name)
    bead = json.loads(out.stdout)
    r["input_bead"] = (bead[0] if isinstance(bead, list) else bead)["id"]
    save(city, r)
    args = ["sling", r["rig"] + "/proof.worker", r["input_bead"], "--on", FORMULA]
    variables = {"packet_id": r["packet"], "work_dir": r["work_dir"], "branch": r["branch"],
        "base_branch": r["base_branch"], "prior_commit": r["head"], "review": "true", "push": "true",
        "open_pr": "false", "automerge": "false", "land_main": "false",
        "pr_recovery_key": r["key"], "pr_recovery_token": r["token"]}
    for k, v in variables.items():
        args += ["--var", k + "=" + v]
    admit.gc(city, *args)
    reconcile(city, r, fl.rig_beads(city, r["rig"]), settings(fl.load_policy(city)))


def observe(city, rig=None, number=None, dry_run=False):
    cfg = settings(fl.load_policy(city))
    if not cfg["enabled"]:
        return
    for name in ([rig] if rig else cfg["rigs"]):
        cfg, rigs = allowed(city, name)
        clone = admit.healthy_clone(rigs, name)
        hold = fl.budget_hold(fl.load_policy(city), rigs) or fl.limit_hold(fl.Standing(city, "pr-recovery", keep=False).state)
        remote = git(clone, "remote", "get-url", "origin")
        slug = remote.split("github.com", 1)[-1].lstrip(":/").removesuffix(".git")
        for packet in fl.packets(city, name):
            if packet["state"] != "passed" or packet["md"].get("proof.completed"):
                continue
            md = packet["md"]
            if md.get("proof.landing") not in ("pr", "automerge"):
                continue
            prs = json.loads(run(["gh", "pr", "list", "--repo", slug, "--head", md["proof.branch"],
                "--state", "open", "--json", "number"]).stdout)
            if len(prs) != 1 or (number and prs[0]["number"] != number):
                continue
            n = prs[0]["number"]
            key = key_for(name, n)
            with (contextlib.nullcontext() if dry_run else lock(city, key)):
                pr = gh(slug, n)
                r = read(city, key)
                if dry_run:
                    fl.log("pr-recovery", "%s %s state=%s mergeable=%s" %
                           (packet["id"], pr["url"], (r or {}).get("state", "unclaimed"), pr["mergeable"]))
                    continue
                if r:
                    # The incident file is written before the bead projection.
                    # Repair a lost projection write before allowing CI repair or
                    # completion to infer ownership from that projection.
                    if (md.get("proof.pr_recovery_state"), md.get("proof.pr_recovery_key")) != (r["state"], key):
                        with fl.state_lock(city):
                            mark(city, r, r["state"], r.get("detail", "reconciled receipt"))
                    reconcile(city, r, fl.rig_beads(city, name), cfg)
                    if r["state"] in ACTIVE:
                        continue
                if pr["state"] != "OPEN" or pr["mergeable"] != "CONFLICTING" or pr["isCrossRepository"]:
                    continue
                if pr["headRefName"] != md["proof.branch"] or pr["baseRefName"] != "main":
                    continue
                if r and (r["attempts"] >= cfg["max_attempts"] or not budget(r, cfg)):
                    mark(city, r, "escalated", "PR recovery budget exhausted; main advances do not reset it")
                    continue
                if r and pr["headRefOid"] == r["head"]:
                    continue
                if hold:
                    fl.log("pr-recovery", hold)
                    continue
                # Share the CI controller's short decision lock. Its claim checks
                # our marker; neither controller can start a second branch writer.
                with fl.state_lock(city):
                    fresh = next(p for p in fl.packets(city, name) if p["bead"]["id"] == packet["bead"]["id"])
                    fm = fresh["md"]
                    if fresh["state"] != "passed" or fm.get("proof.pr_recovery_state") in ACTIVE:
                        continue
                    if fm.get("proof.ci_bead") and not fm.get("proof.ci_acted", "").startswith(fm.get("proof.ci_signature", "!") + " "):
                        continue
                    attempt = (r or {}).get("attempts", 0) + 1
                    token = hashlib.sha256((key + ":" + str(attempt) + ":" + pr["headRefOid"]).encode()).hexdigest()[:20]
                    root_md = fresh["root"]["metadata"]
                    if root_md.get("proof.approved_commit") != pr["headRefOid"] and not (
                            r and r.get("published_head") == pr["headRefOid"]):
                        fl.log("pr-recovery", "unrecognized PR head; no automatic recovery for " + packet["id"])
                        continue
                    history = (r or {}).get("history", [])
                    if r:
                        history = [*history, {k: r.get(k) for k in ("token", "head", "base", "root", "state", "published_head", "attempt_at")} ]
                    r = {"history": history, "key": key, "rig": name, "slug": slug, "number": n, "url": pr["url"],
                         "packet": packet["id"], "work_bead": packet["bead"]["id"],
                         "parent_root": fresh["root"]["id"], "packet_digest": md["proof.packet_digest"],
                         "head": pr["headRefOid"], "head_branch": pr["headRefName"], "base_branch": pr["baseRefName"],
                         "clone": clone, "branch": "agent/pr-recovery-" + token,
                         "work_dir": str(Path(clone) / ".worktrees" / ("pr-recovery-" + token)),
                         "token": token, "attempts": attempt, "attempt_at": stamp(),
                         "started_at": (r or {}).get("started_at", stamp()),
                         "landing": md["proof.landing"], "admission": {"repo": root_md["proof.repo"],
                         "design": root_md.get("proof.design_mockup", ""),
                         "scopes": [s.split(":", 1) for s in root_md["proof.write_scope"].splitlines()]}}
                    r["escalation_owner"] = "human"
                    mark(city, r, "launching", "reserved attempt %s on %s" % (attempt, r["head"]))
                try:
                    launch(city, r, fresh, rigs)
                except Exception as exc:
                    mark(city, r, "escalated", "launch needs reconciliation: " + str(exc)[-1000:])


def fenced(city, r, publishing=False):
    cfg, rigs = allowed(city, r["rig"])
    if r["state"] not in ("running", "launching") or not budget(r, cfg):
        raise fl.Refused("recovery is no longer authorized or its elapsed budget expired")
    claim(city, r, rigs)
    pr = gh(r["slug"], r["number"])
    if pr["state"] != "OPEN" or pr["isCrossRepository"] or pr["headRefName"] != r["head_branch"]             or pr["baseRefName"] != r["base_branch"]:
        raise fl.Refused("PR identity or state changed")
    if git(r["work_dir"], "branch", "--show-current") != r["branch"]:
        raise fl.Refused("recovery worktree changed branch")
    return pr


def prepare(city, key):
    with lock(city, key):
        r = read(city, key)
        pr = fenced(city, r)
        work = r["work_dir"]
        if r.get("base"):
            merge = run(["git", "-C", work, "rev-parse", "--verify", "MERGE_HEAD"], check=False)
            integrated = run(["git", "-C", work, "merge-base", "--is-ancestor", r["base"], "HEAD"], check=False)
            if merge.returncode == 0 or integrated.returncode == 0:
                print(json.dumps({"base": r["base"], "conflicts": git(work, "diff", "--name-only", "--diff-filter=U").splitlines()}))
                return
        if pr["headRefOid"] != r["head"] or git(work, "rev-parse", "HEAD") != r["head"]:
            raise fl.Refused("PR head moved before integration")
        if git(work, "status", "--porcelain"):
            raise fl.Refused("new recovery worktree is dirty")
        if not r.get("base"):
            git(work, "fetch", "origin", r["base_branch"])
            r["base"] = git(work, "rev-parse", "origin/" + r["base_branch"])
        save(city, r)   # Persist provenance before the first integration write.
        out = run(["git", "-C", work, "merge", "--no-commit", "--no-ff", r["base"]], check=False)
        conflicts = git(work, "diff", "--name-only", "--diff-filter=U").splitlines()
        if out.returncode and not conflicts:
            raise fl.Refused("merge failed: " + out.stderr[-1000:])
        _rows, left = regeneration_plan(r["admission"]["repo"], conflicts)
        print(json.dumps({"base": r["base"], "conflicts": conflicts,
                          "generated_bundle": conflicts == [BUNDLE], "regenerable_only": bool(conflicts) and not left}))


def integrate(city, key):
    prepare(city, key)
    # The observer must stay responsive while Wasm builds. A separate worktree
    # lock excludes repeated helpers without holding the incident/state lock.
    with lock(city, key):
        r = read(city, key)
        fenced(city, r)
    work = r["work_dir"]
    workspace_lock = location(city, key).with_name(r["token"] + ".workspace-lock")
    with workspace_lock.open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        conflicts = git(work, "diff", "--name-only", "--diff-filter=U").splitlines()
        rows, _left = regeneration_plan(r["admission"]["repo"], conflicts)
        regenerated = []
        for _repo, paths, side, commands, outputs in rows:
            conflicting = [path for path in paths if path in conflicts]
            git(work, "checkout", side, "--", *conflicting)
            for command in commands:
                subprocess.run(command, cwd=work, check=True, timeout=7200)
            git(work, "add", "--", *outputs)
            regenerated.extend(conflicting)
        remaining = git(work, "diff", "--name-only", "--diff-filter=U").splitlines()
        print(json.dumps({"regenerated": regenerated, "remaining_source_conflicts": remaining}))


def publish(city, key):
    with lock(city, key):
        r = read(city, key)
        if r["state"] == "published":
            if r["landing"] == "automerge" and not r.get("automerge_armed"):
                current = gh(r["slug"], r["number"])
                if current["state"] == "OPEN" and current["headRefOid"] == r["published_head"]:
                    allowed(city, r["rig"])
                    run(["gh", "pr", "merge", str(r["number"]), "--repo", r["slug"], "--merge", "--auto"])
                    r["automerge_armed"] = True
                    save(city, r)
            print(r["published_head"])
            return
        pr = fenced(city, r, publishing=True)
        work = r["work_dir"]
        head, tree = git(work, "rev-parse", "HEAD"), git(work, "rev-parse", "HEAD^{tree}")
        if git(work, "status", "--porcelain"):
            raise fl.Refused("uncommitted recovery work")
        roots = roots_for(fl.rig_beads(city, r["rig"]), r["token"])
        if len(roots) != 1:
            raise fl.Refused("recovery workflow is absent or ambiguous")
        md = roots[0].get("metadata") or {}
        if (md.get("proof.approved_commit"), md.get("proof.approved_tree"), md.get("proof.validated_tree"),
                md.get("proof.approved_by")) != (head, tree, tree, "reviewer"):
            raise fl.Refused("fresh independent review and gate must approve the exact recovery tree")
        for ancestor in (r["head"], r.get("base", "")):
            if not ancestor or run(["git", "-C", work, "merge-base", "--is-ancestor", ancestor, head], check=False).returncode:
                raise fl.Refused("candidate lost the observed PR head or integrated base")
        # CI re-admission uses the original packet's recorded worktree. Keep it
        # fast-forwarded to the delivered head, without changing its identity or
        # discarding dirt. The recovery marker excludes the CI writer until done.
        packets = fl.packets(city, r["rig"])
        packet = next((p for p in packets if p["bead"]["id"] == r["work_bead"]), None)
        if packet is None or packet["state"] != "passed":
            raise fl.Refused("original packet is no longer awaiting merge")
        original = packet["md"].get("work_dir")
        if not original or git(original, "branch", "--show-current") != r["head_branch"]:
            raise fl.Refused("original packet workspace identity changed")
        if git(original, "status", "--porcelain") or git(original, "rev-parse", "HEAD") not in (r["head"], head):
            raise fl.Refused("original packet has foreign work; preserve it and escalate")
        # Reconcile a lost successful push response without pushing again.
        if pr["headRefOid"] != head:
            if pr["headRefOid"] != r["head"]:
                raise fl.Refused("another writer advanced the PR; preserve both histories and escalate")
            run(["git", "-C", work, "push", "origin", head + ":refs/heads/" + r["head_branch"]])
        refs = git(work, "ls-remote", "origin", "refs/heads/" + r["head_branch"]).split()
        remote = refs[0] if refs else None
        if remote != head and not (remote is None and pr["state"] == "MERGED" and pr["headRefOid"] == head):
            raise fl.Refused("remote head differs after ordinary push")
        git(original, "merge", "--ff-only", head)
        wrote = fl.set_bead(city, r["rig"], r["work_bead"], proof__commit=head, proof__tree=tree,
                            proof__pr_recovered_root=roots[0]["id"])
        if wrote.returncode:
            raise fl.Refused("published head needs its original packet handoff receipt; retry safely")
        r.update(published_head=head, published_tree=tree)
        mark(city, r, "published", "validated and reviewed " + head)
        if r["landing"] == "automerge":
            run(["gh", "pr", "merge", str(r["number"]), "--repo", r["slug"], "--merge", "--auto"])
            r["automerge_armed"] = True
            save(city, r)
        print(head)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("observe", "prepare", "integrate", "publish", "status"))
    parser.add_argument("key", nargs="?")
    parser.add_argument("--city", default=fl.city_dir())
    parser.add_argument("--rig")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.action == "observe":
        import plan_trigger
        plan_trigger.log_to_factory(args.city)
        observe(args.city, args.rig, args.pr, args.dry_run)
    elif args.action == "status":
        for p in sorted((Path(args.city) / "proof/pr-recovery").glob("*.json")):
            r = json.loads(p.read_text())
            print(json.dumps({k: r.get(k) for k in ("key", "packet", "state", "attempts", "started_at", "root", "detail", "published_head")}))
    else:
        {"prepare": prepare, "integrate": integrate, "publish": publish}[args.action](args.city, args.key)


if __name__ == "__main__":
    try:
        main()
    except (fl.Refused, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        sys.exit("pr-recovery: " + str(exc))
