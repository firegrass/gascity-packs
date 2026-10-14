#!/usr/bin/env python3
"""Decide whether what a packet would put on main is what was reviewed.

    landing_scope.py <worktree> <approved-commit> <upstream-ref> <item-path>

A direct landing cannot land the approved tree itself: completing the backlog
item, and integrating whatever reached main while the packet ran, both change
the tree after review. So the rule is stated about CONTENT, and the orchestrator
— not the agent — evaluates it, immediately before it pushes:

  1. every path the reviewer saw change is byte-identical at HEAD to what was
     approved, or differs from it only inside generated blocks — completing the
     item and integrating main re-render the views the reviewer saw, and the
     rig's gate verifies those blocks are exactly what the generator renders;
     if integration altered a reviewed file's hand-written content, the review
     is stale — unless the reviewer only ever saw that file's generated blocks
     change, in which case its hand-written content is held to main's copy;
  2. every OTHER path that differs from main is one of
       - the packet's own backlog item, changed only in the frontmatter fields
         that completion writes, with its body untouched;
       - a generated view, changed only inside its generated blocks — and the
         rig's gate verifies those blocks are exactly what the generator renders;
  3. anything else is unreviewed content and refuses the landing.

A path outside both sets is identical to main's copy by construction. That
includes the gate's own scripts: a packet cannot land with a gate it quietly
edited, because the edit is either reviewed (rule 1) or refused (rule 3).

Rule 4 is narrower than all of that: a reserved path (`path_rules.RESERVED`)
refuses even when a reviewer approved it, because review is a second agent
reading a diff and the reserve exists so that no unattended path writes the file
at all (ADR-0150).

Nothing here executes code from the packet worktree, and no bead text reaches
it: the arguments come from workflow-root metadata the orchestrator recorded and
from formula variables whose patterns admission and the compiler enforce.

Exit 0 when the landing is in scope; 1 with one line per violation otherwise.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import path_rules  # noqa: E402  the reserved set is shared with admission and the planning landing

# The orchestrator repository's wholly generated files. Direct landing is only
# ever authorized for that repository (admission refuses it elsewhere), so its
# generator's outputs are named here rather than configured.
GENERATED_FILES = {
    "docs/backlog/v5-release-readiness.json",
}
GENERATED_BLOCK = re.compile(
    r"<!-- BEGIN GENERATED ([A-Z ]+) -->.*?<!-- END GENERATED \1 -->", re.S)

# Frontmatter lines completion may remove, and may add. Everything else in the
# item — every other field, and the whole body — must be unchanged.
COMPLETION_LINE = re.compile(
    r"^(status: (working|done)|done_at: \d{4}-\d{2}-\d{2}|last_reviewed: \d{4}-\d{2}-\d{2}"
    r"|claimed_by: .*|claimed_at: .*|claim_expires: .*)$")


def git(worktree, *args, check=True):
    out = subprocess.run(["git", "-C", worktree, *args], capture_output=True)
    if check and out.returncode != 0:
        raise SystemExit("landing-scope: git %s failed: %s"
                         % (" ".join(args), out.stderr.decode(errors="replace").strip()))
    return out


def changed_paths(worktree, old, new):
    out = git(worktree, "diff", "--name-only", "--no-renames", "-z", old, new).stdout
    return {p.decode() for p in out.split(b"\0") if p}


def blob(worktree, rev, path):
    """The blob id of `path` at `rev`, or None when the path does not exist there."""
    out = git(worktree, "rev-parse", "--verify", "--quiet", "%s:%s" % (rev, path), check=False)
    return out.stdout.decode().strip() if out.returncode == 0 else None


def text(worktree, rev, path):
    out = git(worktree, "show", "%s:%s" % (rev, path), check=False)
    return out.stdout.decode(errors="replace") if out.returncode == 0 else None


def split_item(doc):
    """(frontmatter lines, body) — or None when the document has no frontmatter."""
    if doc is None or not doc.startswith("---\n"):
        return None
    end = doc.find("\n---\n", 4)
    if end < 0:
        return None
    return doc[4:end].split("\n"), doc[end + 5:]


def without_completion(lines):
    """Frontmatter with the fields completion writes taken out."""
    kept, in_evidence = [], False
    for line in lines:
        if in_evidence and line.startswith("  "):
            continue
        in_evidence = line == "evidence:"
        if in_evidence or COMPLETION_LINE.match(line):
            continue
        kept.append(line)
    return kept


def item_violation(before, after):
    old, new = split_item(before), split_item(after)
    if old is None or new is None:
        return "the backlog item is missing or has no frontmatter on one side"
    if old[1] != new[1]:
        return "the backlog item's body changed; completion edits frontmatter only"
    if without_completion(old[0]) != without_completion(new[0]):
        return ("the backlog item's frontmatter changed beyond completion "
                "(status, done_at, evidence, last_reviewed, the claim fields)")
    if "status: done" not in new[0]:
        return "the backlog item is not marked `status: done`"
    if not any(line.startswith("done_at: ") for line in new[0]):
        return "the backlog item carries no `done_at`"
    if any(line.startswith(("claimed_by:", "claimed_at:", "claim_expires:")) for line in new[0]):
        return "the backlog item is done but still carries claim fields"
    return None


def generated_only(path, before, after):
    if path in GENERATED_FILES:
        return True
    if before is None or after is None:
        return False
    strip = lambda doc: GENERATED_BLOCK.sub(lambda m: "<generated %s>" % m.group(1), doc)
    return strip(before) == strip(after)


def inspect(worktree, approved, upstream, item_path, candidate="HEAD"):
    """Apply the same boundary to a committed candidate or an index tree."""
    base = git(worktree, "merge-base", approved, upstream).stdout.decode().strip()
    reviewed = changed_paths(worktree, base, approved)
    landing = changed_paths(worktree, upstream, candidate)
    problems = []

    for path in sorted(reviewed - {item_path}):
        if blob(worktree, candidate, path) == blob(worktree, approved, path):
            continue
        # A reviewed view is re-rendered by completion (the item leaves the
        # claimed list) and by integration; the gate proves the blocks, so only
        # a hand-written difference makes the review stale.
        if generated_only(path, text(worktree, approved, path), text(worktree, candidate, path)):
            continue
        # The reviewer saw only this file's generated blocks change (a renderer
        # or an item edit re-rendered it). Its hand-written content was never
        # the packet's, so it is held to main's copy, not to the approved
        # snapshot: a claim on main may have moved an epic's frontmatter since.
        if generated_only(path, text(worktree, base, path), text(worktree, approved, path)) \
                and generated_only(path, text(worktree, upstream, path), text(worktree, candidate, path)):
            continue
        problems.append("%s: reviewed, but HEAD no longer has the approved content "
                        "(integration or a later edit changed it) — it needs review again" % path)

    for path in sorted(landing - (reviewed - {item_path})):
        if path == item_path:
            baseline = approved if path in reviewed else upstream
            why = item_violation(text(worktree, baseline, path), text(worktree, candidate, path))
            if why:
                problems.append("%s: %s" % (path, why))
        elif not generated_only(path, text(worktree, upstream, path), text(worktree, candidate, path)):
            problems.append("%s: would land on %s but was never reviewed" % (path, upstream))

    # A reserved path refuses even when a reviewer approved it: review is a second
    # agent reading a diff, and the point of the reserve is that no unattended
    # path produces the file at all.
    for path in sorted(landing | reviewed):
        reserved = path_rules.reserved_path(path)
        if reserved:
            problems.append(reserved)

    if item_path not in landing and item_path not in reviewed:
        problems.append("%s: the backlog item is unchanged — complete it before landing" % item_path)

    return problems, reviewed, landing


def main(argv):
    if len(argv) != 4:
        raise SystemExit(__doc__.split("\n\n")[0])
    problems, reviewed, landing = inspect(*argv)
    for line in problems:
        print("landing-scope: " + line, file=sys.stderr)
    if not problems:
        print("landing-scope: %d reviewed path(s) intact; %d further path(s) are completion "
              "or generated views" % (len(reviewed), len(landing - reviewed)), file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
