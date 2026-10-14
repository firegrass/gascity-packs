#!/usr/bin/env python3
"""Refresh a completed landing candidate without changing reviewed content.

Called by the trusted landing check in the packet's own worktree. Only the
packet's completion and generated views may be resolved automatically. The
original and merged index both pass landing_scope; a changed task or claim
refuses. Source conflicts are aborted, never resolved by this helper.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
import subprocess
import sys
from pathlib import Path

import landing_scope


class Refused(RuntimeError):
    pass


def run(root, *args, check=True):
    out = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=120)
    if check and out.returncode:
        raise Refused("%s: %s" % (" ".join(args[:3]), (out.stderr or out.stdout).strip()[-1200:]))
    return out


def git(root, *args, **kwargs):
    return run(root, "git", *args, **kwargs)


def assert_scope(root, approved, upstream, item, candidate="HEAD"):
    problems, _, _ = landing_scope.inspect(root, approved, upstream, item, candidate)
    if problems:
        raise Refused("; ".join(problems))


def same_claim_and_task(before, current, now=None):
    old, new = landing_scope.split_item(before), landing_scope.split_item(current)
    if old is None or new is None:
        return False
    if old[1] != new[1] or landing_scope.without_completion(old[0]) != landing_scope.without_completion(new[0]):
        return False
    fields = lambda lines: dict(line.split(": ", 1) for line in lines if ": " in line and not line.startswith(" "))
    a, b = fields(old[0]), fields(new[0])
    try:
        expiry = datetime.fromisoformat(b.get("claim_expires", "").replace("Z", "+00:00"))
    except ValueError:
        return False
    return (expiry.tzinfo is not None and expiry > (datetime.now(timezone.utc) if now is None else now)
            and b.get("status") == "working" and all(a.get(k) and a[k] == b.get(k)
            for k in ("claimed_by", "claimed_at")))


def regular_generator_inputs(root):
    """Generated output must not follow a packet-controlled symlink."""
    root = Path(root)
    for directory in ("docs", "scripts"):
        if (root / directory).is_symlink():
            raise Refused("generator input directory is a symlink: " + directory)
    generator = root / "scripts/validate_docs.py"
    if not generator.is_file() or generator.is_symlink():
        raise Refused("the repository documentation generator is missing or is a symlink")
    rows = git(root, "ls-files", "--stage", "-z", "--", "docs", "scripts/validate_docs.py").stdout.split("\0")
    if any(row.startswith("120000 ") for row in rows if row):
        raise Refused("tracked generator input/output is a symlink; preserve and inspect")
    return generator


def stageable(root, paths):
    """The generator's outputs that are tracked content, not a local read model.

    The queue's JSON frontier is refreshed into the working tree but gitignored
    (ADR-0161); `git add` refuses an ignored path, and the dirty check below
    already leaves ignored files alone.
    """
    return [path for path in paths
            if git(root, "check-ignore", "-q", "--", path, check=False).returncode != 0]


def refresh(root, approved, upstream, item):
    if git(root, "status", "--porcelain", "--untracked-files=all").stdout.strip():
        raise Refused("candidate is dirty; preserve and reconcile it")
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    if git(root, "merge-base", "--is-ancestor", upstream, head, check=False).returncode == 0:
        return head
    base = git(root, "merge-base", head, upstream).stdout.strip()
    assert_scope(root, approved, base, item)
    if not same_claim_and_task(landing_scope.text(root, base, item), landing_scope.text(root, upstream, item)):
        raise Refused("the task or claim changed or expired on main; refresh requires the same live claim")
    regular_generator_inputs(root)
    merged = False
    try:
        result = git(root, "merge", "--no-ff", "--no-commit", upstream, check=False)
        merged = git(root, "rev-parse", "--verify", "MERGE_HEAD", check=False).returncode == 0
        if not merged:
            raise Refused("integration did not prepare a merge: " + result.stderr[-600:])
        conflicts = git(root, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
        for path in conflicts:
            if path == item:
                continue
            if not landing_scope.generated_only(path, landing_scope.text(root, base, path),
                                                  landing_scope.text(root, head, path)):
                raise Refused("source or hand-authored conflict needs review: " + path)
        views = [path for path in conflicts if path != item]
        if views:
            git(root, "restore", "--source=" + upstream, "--staged", "--worktree", "--", *views)
        # A lease renewal can merge claim fields back into the completed item.
        # Task/claim identity was checked above; restore only its authorized completion.
        git(root, "restore", "--source=" + head, "--staged", "--worktree", "--", item)
        generator = regular_generator_inputs(root)
        generated = run(root, sys.executable, str(generator), "--refresh-epic-views")
        paths = stageable(root, re.findall(r"^validate_docs: refreshed (docs/\S+)$", generated.stdout, re.M))
        if paths:
            git(root, "add", "--", *paths)
        if git(root, "diff", "--name-only").stdout.strip() or git(root, "ls-files", "--others", "--exclude-standard").stdout.strip():
            raise Refused("generator left unexpected unstaged or untracked output; preserve and inspect")
        tree = git(root, "write-tree").stdout.strip()
        assert_scope(root, approved, upstream, item, tree)
        packet = Path(item).name[:18]
        git(root, "commit", "-m", "Integrate main for checked packet landing",
            "-m", "Refs %s; preserves approved content and regenerates only landing-owned views." % packet)
        merged = False
        if git(root, "rev-parse", "HEAD^{tree}").stdout.strip() != tree:
            raise Refused("commit hooks changed the inspected tree; validate again")
        return git(root, "rev-parse", "HEAD").stdout.strip()
    except BaseException:
        if merged:
            git(root, "merge", "--abort")
        raise


def main(argv):
    if len(argv) != 4:
        raise SystemExit("landing_refresh.py WORKTREE APPROVED UPSTREAM ITEM")
    try:
        print("landing-refresh: integrated " + refresh(*argv), file=sys.stderr)
    except (Refused, subprocess.TimeoutExpired) as exc:
        print("landing-refresh: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
