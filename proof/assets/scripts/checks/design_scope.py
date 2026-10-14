#!/usr/bin/env python3
"""Decide whether a packet's diff is one it was authorized to make.

    design_scope.py <worktree> <base-ref> <repo> <design-mockup>
                    [<write-scope-file> [<review:on|off> <review-substrings-file>]]

`repo` may be empty: a bead admitted by an older pack carries none, and this
script then derives it exactly as admission does, from the worktree's `origin`.
It refuses only when neither yields a name — it never guesses, and it never
decides a diff is immaterial because it could not tell which repository it is in.

Two rules, both evaluated by the ORCHESTRATOR on the diff the worker actually
produced, never on the agent's account of it:

  1. **The design rule** (ADR-0150). A change under `web-app/src/**`, `web/**`
     or `pages/**` in `liskov-ui` is material to an operator, decided by path
     and failing closed. It is allowed only when admission recorded a live
     owner agreement for the page this packet builds to. No judgement can
     excuse a path: a Noul over untrusted text may only ever add caution
     (`judge.py`, ADR-0144), so materiality is never a probability.

  2. **The write-scope rule** (ADR-0159). The declared `write_scope` is intent,
     not a fence: a change forces files its author did not list, and a worker
     that edits them is doing the job. So a changed path no declared entry
     reaches is refused only when it is
       * RESERVED (ADR-0150) — refused even when declared;
       * PROTECTED (`path_rules.PROTECTED`: CI workflows, deploy and secret
         files, the factory's own code) and undeclared;
       * matched by one of the rig's `review_if_scope_contains` substrings while
         the packet has no independent reviewer — the repair is admission with
         `--review`, not a wider scope.
     Every other such path is allowed and EXPANDS the scope: it is printed on
     one `design-scope: expanded` line, and written to stdout as `repo:path`,
     one a line, for `packet-gates.sh` to record on the workflow root.
     The orchestrator's generated views (`path_rules.GENERATED_VIEWS`) count
     as declared for every packet in that repository: the refresh rewrites
     them for any item, so they are never an expansion.

Admission is what supplies the design, the scope and the reviewer flag, because
it is the only place that can see the item, the agreement registry and the
packet at once; it stamps them on the workflow root and `packet-gates.sh` reads
them from there, with the review substrings from the city's policy file. So:

  * no design metadata AND a render-path diff -> refuse. That is the case the
    rule exists for: a packet admitted for a non-rendering scope that went and
    changed what an operator sees.
  * no write-scope metadata -> refuse. Admission writes it on the workflow
    root; a root cut off mid-admission is repaired by the dispatcher on its
    next pass. The write-scope rule is never skipped.
  * no review arguments -> no reviewer and no substrings, loudly.

Nothing here executes code from the packet worktree: `path_rules` comes from
the pack, and every value comes from bead metadata or the policy file the
orchestrator owns.

Exit 0 when the diff is allowed; 1 with one line per violation otherwise.
"""
from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import path_rules  # noqa: E402  the render, reserved and protected sets, shared with admission

RUN_TIMEOUT = 600


def changed_paths(worktree, base):
    out = subprocess.run(
        ["git", "-C", worktree, "diff", "--name-only", "--no-renames", "-z", base + "...HEAD"],
        capture_output=True, timeout=RUN_TIMEOUT)
    if out.returncode != 0:
        raise SystemExit("design-scope: git diff failed: %s"
                         % out.stderr.decode(errors="replace").strip()[-400:])
    return sorted({p.decode() for p in out.stdout.split(b"\0") if p})


def origin_repo(worktree):
    """The repository name from the worktree's `origin`, the same derivation
    `admit.repo_name` makes from the rig clone — a worktree shares its clone's
    remotes, so the two agree. "" when there is no usable remote."""
    out = subprocess.run(["git", "-C", worktree, "remote", "get-url", "origin"],
                         capture_output=True, text=True, timeout=RUN_TIMEOUT)
    if out.returncode != 0:
        return ""
    name = out.stdout.strip().rstrip("/").rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".git") else name


def read_scope(path):
    """The packet's write_scope as admission recorded it: one `repo:glob` a
    line. An absent or empty file means it was not recorded."""
    if not path or not os.path.isfile(path):
        return None
    entries = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        repo, _, glob = line.partition(":")
        entries.append((repo, glob) if glob else (path_rules.RESERVED_REPO, repo))
    return entries or None


def design_problems(paths, repo, design):
    """The design rule. `design` is `<page>@<digest>` as admission recorded it,
    or "" when admission recorded nothing."""
    rendering = [p for p in paths if repo == path_rules.RENDER_REPO and path_rules.path_renders(p)]
    if not rendering:
        return []
    if not design:
        return ["%d path(s) change what an operator sees (%s) and this packet carries no agreed "
                "design. A material Console change builds to an owner-agreed mockup — see "
                "docs/meta/doctrine/ui-design-mockup.md. Narrow the change, or have the item "
                "declare `design_mockup: conforms:<page>` for a page the owner has agreed."
                % (len(rendering), ", ".join(rendering[:5]))]
    return []


def read_lines(path):
    """Non-empty, non-comment lines of a file; [] when there is none."""
    if not path or not os.path.isfile(path):
        return []
    return [line.strip() for line in open(path, encoding="utf-8")
            if line.strip() and not line.strip().startswith("#")]


def undeclared(paths, repo, scopes):
    """The changed paths no declared entry, in this packet's repository, reaches."""
    return [path for path in paths
            if not any(r == repo and path_rules.reaches(glob, path) for r, glob in scopes)]


def scope_problems(paths, repo, scopes):
    """What the declared write_scope does not cover, one line a path.

    Since ADR-0159 this is not a refusal: it is the question the dispatcher's
    test-companion closure is pinned against (`tests/test_test_companions.py`),
    and `deny_problems` decides which of these paths the check refuses."""
    return ["%s: changed, and no write_scope entry declares it" % path
            for path in undeclared(paths, repo, scopes)]


def deny_problems(paths, repo, scopes, reviewed=False, substrings=()):
    """The write-scope rule. Returns (problems, expanded): the refusals, and the
    paths allowed although no declared entry reaches them."""
    problems, expanded = [], []
    outside = set(undeclared(paths, repo, scopes))
    for path in paths:
        why = path_rules.reserved_path(path) if repo == path_rules.RESERVED_REPO else None
        if why:
            problems.append("%s. It is refused even when the write_scope declares it." % why)
            continue
        if path not in outside:
            continue
        why = path_rules.protected_path(repo, path)
        if why:
            problems.append("%s, and no write_scope entry declares it. Declare it on the item and "
                            "re-admit, or leave the file alone (ADR-0159)." % why)
            continue
        hit = next((s for s in substrings if s.lower() in path.lower()), None)
        if hit is not None and not reviewed:
            problems.append("%s: changed outside the declared write_scope, and it matches the rig's "
                            "review substring `%s` while this packet has no independent reviewer. "
                            "Re-admit with --review (ADR-0159)." % (path, hit))
            continue
        expanded.append(path)
    return problems, expanded


def main(argv):
    if len(argv) not in (4, 5, 7):
        raise SystemExit(__doc__.split("\n\n")[0])
    worktree, base, repo, design = argv[:4]
    scope_file = argv[4] if len(argv) >= 5 else ""
    if len(argv) == 7:
        if argv[5] not in ("on", "off"):
            raise SystemExit("design-scope: the review flag is %r; it must be `on` or `off`" % argv[5])
        reviewed, substrings = argv[5] == "on", read_lines(argv[6])
    else:
        print("design-scope: no reviewer flag or review substrings were passed; the packet is "
              "treated as having no reviewer and no substring applies", file=sys.stderr)
        reviewed, substrings = False, []

    repo = repo.strip() or origin_repo(worktree)
    if not repo:
        raise SystemExit("design-scope: this workflow records no repository and %s has no `origin` to "
                         "derive one from. The check refuses rather than guessing: a guess that missed "
                         "would let a Console change through unagreed." % worktree)

    paths = changed_paths(worktree, base)
    problems = design_problems(paths, repo, design.strip())

    expanded = []
    scopes = read_scope(scope_file)
    if scopes is not None:
        # Declared or not, a packet in the orchestrator holds its generated views.
        scopes += path_rules.default_scope(repo)
    if scopes is None:
        problems.append("this workflow records no write_scope. Admission writes it on the workflow "
                        "root; a root without it was cut off mid-admission and the dispatcher writes "
                        "it on its next pass (BKLG-20261011-92r0). The scope rule is not skipped.")
    else:
        found, expanded = deny_problems(paths, repo, scopes, reviewed, substrings)
        problems += found

    for line in problems:
        print("design-scope: " + line, file=sys.stderr)
    if problems:
        return 1
    print("design-scope: %d path(s) allowed%s" % (
        len(paths),
        (", building to %s" % design.strip()) if design.strip() else ""), file=sys.stderr)
    if expanded:
        print("design-scope: expanded %d path(s) outside the declared write_scope: %s"
              % (len(expanded), " ".join(expanded)), file=sys.stderr)
        for path in expanded:
            print("%s:%s" % (repo, path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
