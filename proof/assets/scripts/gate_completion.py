"""Completion evidence for explicitly opted-in repositories without main CI."""
from __future__ import annotations

import re

import factory_lib as fl


OID = re.compile(r"[0-9a-f]{40}\Z")


def verdict(clone, slug, merge_sha, policy, packet, pr, gh_json):
    """Verify the workflow's approved snapshot is what main actually received.

    Work-bead notes are not gate evidence. Read the orchestrator's workflow-root
    stamps, bind them to this PR head, and compare real Git trees. A merge that
    adds unvalidated main changes needs revalidation, even without conflicts.
    """
    def pending(reason):
        return "pending", merge_sha, "gate completion held: " + reason

    if policy.get("merge") != "on-gate" or policy.get("merge_requires"):
        return pending('requires merge="on-gate" and no merge_requires')
    packet, pr = packet or {}, pr or {}
    md = packet.get("md") or {}
    root = packet.get("root") or {}
    proof = root.get("metadata") or {}
    if (packet.get("state") != "passed" or md.get("proof.landing") != "pr"
            or root.get("status") != "closed" or proof.get("gc.outcome") != "pass"):
        return pending("no completed, passed PR workflow")
    if not packet.get("id") or proof.get("gc.var.packet_id") != packet["id"]:
        return pending("workflow belongs to another packet")
    if proof.get("proof.repo") != slug.rsplit("/", 1)[-1]:
        return pending("workflow belongs to another repository")
    commit, tree = proof.get("proof.validated_commit"), proof.get("proof.validated_tree")
    if not all(isinstance(x, str) and OID.fullmatch(x) for x in (commit, tree, merge_sha)):
        return pending("missing or malformed validated commit/tree")
    if proof.get("proof.approved_commit") != commit or proof.get("proof.approved_tree") != tree:
        return pending("approval does not match the validated commit/tree")
    if proof.get("proof.approved_by") not in ("gate", "reviewer"):
        return pending("approval source is missing")
    if fl.flag(proof.get("gc.var.review")) and proof.get("proof.approved_by") != "reviewer":
        return pending("required reviewer approval is missing")
    if (pr.get("state") != "MERGED" or (pr.get("mergeCommit") or {}).get("oid") != merge_sha
            or pr.get("headRefOid") != commit):
        return pending("merged PR head does not match the gate-approved commit")
    if pr.get("statusCheckRollup"):
        return pending("unexpected PR checks; configure main verification for CI")

    if fl.sh(["git", "-C", clone, "fetch", "--quiet", "origin"]).returncode != 0:
        return pending("cannot refresh main")
    heads = fl.sh(["git", "-C", clone, "rev-list", "--first-parent", "origin/main"])
    if heads.returncode != 0 or merge_sha not in heads.stdout.split():
        return pending("PR merge is not on main's first-parent chain")
    ancestor = fl.sh(["git", "-C", clone, "merge-base", "--is-ancestor", commit, merge_sha])
    if ancestor.returncode != 0:
        return pending("main merge does not contain the approved commit")
    for revision in (commit, merge_sha):
        actual = fl.sh(["git", "-C", clone, "rev-parse", "--verify", revision + "^{tree}"])
        if actual.returncode != 0 or actual.stdout.strip() != tree:
            return pending("merge contents differ from the approved snapshot; revalidate the merged tree")
        statuses = gh_json(["api", "repos/%s/commits/%s/status" % (slug, revision)], clone)
        checks = gh_json(["api", "repos/%s/commits/%s/check-runs" % (slug, revision)], clone)
        runs = gh_json(["api", "repos/%s/actions/runs?head_sha=%s&event=push&per_page=1"
                        % (slug, revision)], clone)
        if (statuses.get("sha") != revision or statuses.get("statuses") != []
                or statuses.get("total_count") != 0 or checks.get("total_count") != 0
                or checks.get("check_runs") != [] or runs.get("total_count") != 0
                or runs.get("workflow_runs") != []):
            return pending("unexpected or incomplete CI evidence; configure main verification for CI")
    return "green", merge_sha, "gate-approved merge tree %s (no repository CI)" % tree
