#!/usr/bin/env python3
"""Decide whether what a planning run would put on main is only planning output.

    plan_scope.py <worktree> <upstream-ref> <assigned-items-csv>

A planning run has no reviewer. What it writes is read by the dispatcher and by
every worker after it, so the rule that keeps it inside its own job is this one,
evaluated by the ORCHESTRATOR immediately before it pushes — never by the agent
that wrote the commit.

What may land, and nothing else:

  1. an EXISTING backlog item named in assigned_items — re-shaped or
     completed — provided the copy on main is not claimed or `working`: an item
     a worker is holding right now is that packet's, and re-shaping it underneath
     the worker changes the job after admission recorded its digest;
  2. an open question linked in an assigned item's depends_on;
  3. a generated view, changed only inside its generated blocks (the rig's gate
     verifies those blocks are what the generator renders).

Anything else — a doctrine body, a script, an answered question, a deletion — is
the planner editing the workspace rather than the queue, and refuses the landing.
A reserved path (`path_rules.RESERVED`) refuses by name before any of that, so
the planner is told it may not write a mockup rather than that it wrote an
unexpected file.

Nothing here executes code from the worktree: the arguments come from
workflow-root metadata the orchestrator recorded and from formula variables
whose patterns the compiler enforces.

Exit 0 when the landing is in scope; 1 with one line per violation otherwise.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import landing_scope as ls  # noqa: E402  the generated-view rule is shared, not re-stated
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import path_rules  # noqa: E402  the reserved set is shared with admission and the packet landing
import planner_assignments as assignments  # noqa: E402

ITEM = re.compile(r"^docs/backlog/items/BKLG-[0-9]{8}-[a-z0-9]{4}-[A-Za-z0-9._-]+[.]md$")
QUESTION = re.compile(r"^docs/questions/open/Q-[0-9]{8}-[a-z0-9]{4}-[A-Za-z0-9._-]+[.]md$")
# A claim on main is a worker holding that item. `status: working` without one is
# a claim that expired or a session that stopped: still not the planner's to edit.
HELD = re.compile(r"^(status: working|claimed_by: .+)$", re.M)


def frontmatter(doc):
    if doc is None or not doc.startswith("---\n"):
        return ""
    end = doc.find("\n---\n", 4)
    return doc[4:end] if end >= 0 else ""


def question_dependencies(doc):
    """Read only literal Q ids in the canonical block/inline dependency list.

    The repository gate checks full YAML validity. Comments and prose cannot
    authorize a question edit here, and unfamiliar syntax refuses the link.
    """
    match = re.search(r"^depends_on:([^\n]*(?:\n[ \t]+[^\n]*)*)", frontmatter(doc), re.M)
    if not match:
        return set()
    raw = "\n".join(line.split("#", 1)[0] for line in match[1].splitlines()).strip()
    if raw.startswith("[") and raw.endswith("]"):
        values = raw[1:-1].split(",")
    else:
        values = [line.strip()[2:] for line in raw.splitlines() if line.strip().startswith("- ")]
    return {value.strip().strip("\"'") for value in values
            if re.fullmatch(r"Q-[0-9]{8}-[a-z0-9]{4}", value.strip().strip("\"'"))}


def main(argv):
    if len(argv) != 3:
        raise SystemExit(__doc__.split("\n\n")[0])
    worktree, upstream, ids = argv
    try:
        assigned = set(assignments.assigned_ids(ids))
    except assignments.fl.Refused as exc:
        print("plan-scope: %s" % exc, file=sys.stderr)
        return 1
    landing = ls.changed_paths(worktree, upstream, "HEAD")
    problems = []
    # Assignments are EXISTING items. Do not let a planner invent new work by
    # choosing another filename with the assigned id or remove the old record.
    tracked = ls.git(worktree, "ls-tree", "-r", "--name-only", upstream, "--", "docs/backlog/items").stdout.decode().splitlines()
    permitted = {path for path in tracked if ITEM.match(path)
                 and re.search(r"BKLG-[0-9]{8}-[a-z0-9]{4}", path)[0] in assigned}
    if any(sum(pid in path for path in permitted) != 1 for pid in assigned):
        problems.append("every assigned id must resolve to one existing item on upstream")
    questions = set().union(*(question_dependencies(ls.text(worktree, "HEAD", path)) for path in permitted))

    for path in sorted(landing):
        before, after = ls.text(worktree, upstream, path), ls.text(worktree, "HEAD", path)
        reserved = path_rules.reserved_path(path)
        if reserved:
            problems.append(reserved)
        elif ls.blob(worktree, "HEAD", path) is None:
            problems.append("%s: a planning run may not delete or rename a file" % path)
        elif ITEM.match(path):
            if path not in permitted:
                problems.append("%s: item is outside this run's assigned_items" % path)
            elif before is not None and HELD.search(frontmatter(before)):
                problems.append("%s: the item is claimed or `working` on %s — it belongs to the packet "
                                "holding it, not to the planner" % (path, upstream))
        elif QUESTION.match(path):
            qid = re.search(r"Q-[0-9]{8}-[a-z0-9]{4}", path)[0]
            if qid not in questions:
                problems.append("%s: question is not a dependency of an assigned item" % path)
        elif not ls.generated_only(path, before, after):
            problems.append("%s: a planning run writes backlog items, open questions and the views "
                            "the validator regenerates — nothing else" % path)

    for line in problems:
        print("plan-scope: " + line, file=sys.stderr)
    if not problems:
        print("plan-scope: %d path(s), all assigned items, associated questions or generated views"
              % len(landing), file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
