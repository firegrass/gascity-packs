#!/usr/bin/env python3
"""Path rules the ORCHESTRATOR applies to a packet, never the agent.

Four sets live here, because three scripts and one check need the identical
answer and a second copy of a path predicate drifts — the lesson
`plan_trigger.py` records about re-implementing `admissible()`.

* **RESERVED** — paths in the orchestrator repository that no unattended agent
  may write. A mockup is a decision a person made about what the operator
  console says; if the factory can write one, "the design was agreed" means
  only that some agent typed it. Reserving the path is what makes a mockup on
  `main` *evidence* of a human-led session (ADR-0150).
* **RENDER** — the `liskov-ui` paths that make a change material to an operator.
  Materiality is decided by path and fails closed: no judgement may excuse a
  change, because a Noul over untrusted text may only ever add caution, never
  authorize (`judge.py`, ADR-0144).
* **PROTECTED** — paths a packet's diff may reach only when its declared
  write_scope names them: CI workflows, deploy and Fly configuration,
  environment and secret files, and the factory's own code in the orchestrator.
  Everything else outside the declared scope is allowed and recorded (ADR-0159).
* **GENERATED_VIEWS** — the orchestrator's generated views, in every
  orchestrator packet's write_scope by default (`default_scope`).

Self-contained on purpose. `plan_scope.py` and `landing_scope.py` both state
that nothing they run comes out of the packet worktree, and importing the
orchestrator's `docs_model` from a worktree under inspection would break that.
`docs_model` keeps its own copy of the render predicate, for the validator, and
`gascity/tests/test_path_rules.py` pins the two to the same answers on a shared
table of cases.
"""
from __future__ import annotations

import fnmatch
import re

# The repository each set is about. A `liskov-ui:mockups/**` scope is not the
# orchestrator's mockup tree, and a `liskov-rs:web/**` scope renders nothing.
RESERVED_REPO = "liskov-agent-orchestrator"
RENDER_REPO = "liskov-ui"

# A trailing "/" means a directory and everything under it; anything else is an
# exact file. Each carries the sentence a person reads when it refuses them.
RESERVED = (
    ("mockups/",
     "a mockup is agreed design evidence: only the owner writes one, in a "
     "human-led session (ADR-0150, docs/meta/doctrine/ui-design-mockup.md)"),
    ("scripts/build_mockup_catalog.py",
     "the catalog generator is what `--check` enforces agreements with; a packet "
     "that may rewrite it may rewrite what counts as agreed (ADR-0150)"),
    ("scripts/build_execution_mockups.py",
     "the execution-page generator writes mockups (ADR-0150)"),
)

# Paths a packet may change only when its declared write_scope names them
# (ADR-0159). A path outside the declared scope is otherwise allowed and
# recorded; these are the classes where "the change forced it" is not enough,
# because what they change is the gate, the deploy or a secret rather than the
# job. Each entry is (repository, or "" for every repository; target; why).
# A target with a wildcard is a file-name pattern matched at any depth; any
# other target is anchored at the repository root, with a trailing "/" for a
# directory — the same shape as RESERVED.
PROTECTED = (
    ("", ".github/workflows/",
     "a CI workflow decides what every later change is checked by"),
    ("", "fly/", "deploy configuration decides what production runs"),
    ("", "fly*.toml", "Fly configuration decides what production runs"),
    ("", ".env*", "an environment file carries configuration and secrets"),
    ("", "secrets/", "a secrets directory carries credentials"),
    (RESERVED_REPO, "gascity/",
     "the factory's own code: a packet that may rewrite it may rewrite its own gate"),
    (RESERVED_REPO, "scripts/",
     "the validator, the queue and the routers every session trusts"),
    (RESERVED_REPO, "AGENTS.md", "the workspace constitution"),
    (RESERVED_REPO, "docs/meta/", "the documentation system's rules and doctrine"),
)

# The orchestrator documents that `validate_docs.py --refresh-epic-views` (and
# the V5 readiness generator) rewrite: the files `.gitattributes` routes through
# the `generated-views` merge driver, and `gascity/tests/test_path_rules.py`
# pins the two lists together. Completing or claiming ANY item re-renders the
# delivery queue, its epic and its milestone, so no author lists them and every
# packet in this repository touches them. They are in its write_scope by
# default: a worker that treated them as out of scope left them stale and failed
# the gate, or stopped the packet over a file the refresh wrote. Declaring them
# grants the refresh output only — `landing_scope.py` still refuses
# hand-written text that no reviewer saw.
GENERATED_VIEWS = (
    "docs/backlog/delivery-queue.md",
    "docs/backlog/v5-release-readiness.json",
    "docs/backlog/epics/*.md",
    "docs/backlog/milestones/*.md",
    "docs/raw/README.md",
    "docs/work-notes/README.md",
    "docs/questions/README.md",
    "docs/knowledge/README.md",
    "docs/decisions/README.md",
    "docs/business/README.md",
)

# What an operator sees. `src/**` is the server-side managed-acurast code and
# renders nothing; `test/**`, `tsconfig.json` and the build config render
# nothing either. Everything else that ships to a browser is here.
RENDER = ("web-app/src/", "web/", "pages/")


def _head(glob):
    """The fixed part of a glob, before its first wildcard."""
    return re.split(r"[*?\[]", glob, 1)[0]


def reaches(glob, target):
    """Can a write_scope glob name something at or under `target`?

    `target` is a directory prefix (trailing "/") or an exact file path. Three
    directions, all deliberately generous: this answers a refusal, so a glob
    whose reach is uncertain is treated as reaching.

    down     the glob's fixed part is at or under the target
    up       the glob is a wildcard-free directory at or above the target
    across   the pattern covers a probe path inside the target. `fnmatch` lets
             `*` cross a `/`, which is wider than git's glob — wider means more
             refusals, which is the safe direction.
    """
    glob = str(glob).strip("/")
    if not glob:
        return True
    head = _head(glob)
    stem = target.rstrip("/")
    if head == stem or head.startswith(stem + "/"):
        return True
    if head == glob and (stem == glob or stem.startswith(glob + "/")):
        return True
    probe = stem + "/PROBE/PROBE.probe" if target.endswith("/") else stem
    return fnmatch.fnmatch(probe, glob) or fnmatch.fnmatch(probe, glob + "/**")


def under(path, target):
    """Is a CONCRETE path at or under `target`? No globbing: a diff has none."""
    path = str(path).strip("/")
    stem = target.rstrip("/")
    return path == stem or (target.endswith("/") and path.startswith(stem + "/"))


def reserved_scope(repo, glob):
    """The reason a write_scope entry is refused, or None."""
    if repo != RESERVED_REPO:
        return None
    for target, why in RESERVED:
        if reaches(glob, target):
            return "`%s` reaches %s — %s" % (glob, target, why)
    return None


def reserved_path(path):
    """The reason a concrete orchestrator path may not be written, or None."""
    for target, why in RESERVED:
        if under(path, target):
            return "%s is a reserved path — %s" % (path, why)
    return None


def _protects(target, path):
    if _head(target) == target:
        return under(path, target)
    # A pattern names a file, never a directory, so it is matched against the
    # file name alone: `*` must not cross a `/` here as it does in `reaches`.
    return fnmatch.fnmatchcase(str(path).rstrip("/").rsplit("/", 1)[-1], target)


def protected_path(repo, path):
    """The reason a concrete path in `repo` may change only when declared, or None."""
    for owner, target, why in PROTECTED:
        if owner in ("", repo) and _protects(target, path):
            return "%s is protected — %s" % (path, why)
    return None


def default_scope(repo):
    """The write_scope every packet in `repo` holds without declaring it."""
    return [(repo, glob) for glob in GENERATED_VIEWS] if repo == RESERVED_REPO else []


def scope_renders(repo, glob):
    """Can this write_scope entry change what an operator sees?"""
    return repo == RENDER_REPO and any(reaches(glob, target) for target in RENDER)


def path_renders(path):
    """Is this concrete liskov-ui path one that renders?"""
    return any(under(path, target) for target in RENDER)
