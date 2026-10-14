"""Would a push of this main head start any Actions workflow? Read from the
workflow files themselves, at that commit.

The completer waits for a head's push-triggered runs. A head that no workflow
selects never gets one: `liskov-marketplace-offerings` filters both of its push
workflows to `uptime-prober/**`, so the merge that added a root LICENSE
(BKLG-20260925-z47h, 2026-09-26) left main's newest head with no run, and
"push workflows have not started" would have held its claim until it expired.

`selects` answers True, False or None. Only False lets the completer treat an
absent run as final, so every doubt — a file that does not parse, a trigger
shape this does not model, a commit without a first parent, no YAML library —
is None, and None waits exactly as before.

GitHub's rules, as documented for `on.push` filters:
  * `branches`/`branches-ignore` and `tags`/`tags-ignore` each filter their own
    ref kind; naming only tag filters means no branch push runs the workflow.
  * `paths` runs the workflow when at least one changed file matches;
    `paths-ignore` runs it unless every changed file is ignored. A `!` pattern
    re-excludes, and the last pattern that matches a name decides.
  * `*` matches within one path segment, `**` across segments, `?` and `+`
    quantify the preceding character, and `[...]` is a character class.
The changed files are the head's first-parent diff: what main moved by when it
pointed there. Judging each head alone is never less cautious than judging a
push of several at once, because "some file selects" holds for a union as soon
as it holds for any part.
"""
from __future__ import annotations

import re

import factory_lib as fl

WORKFLOWS = ".github/workflows/"


def _pattern(glob):
    out, i = [], 0
    while i < len(glob):
        c = glob[i]
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if glob.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c in "?+":
            out.append(c)                       # quantifies the preceding character, as GitHub does
        elif c == "[":
            end = glob.find("]", i + 1)
            if end < 0:
                raise ValueError("unterminated character class in %r" % glob)
            out.append(glob[i:end + 1])
            i = end + 1
            continue
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


def matches(patterns, name):
    """The last pattern that matches decides; a `!` pattern excludes."""
    hit = False
    for raw in patterns:
        negated = raw.startswith("!")
        if _pattern(raw[1:] if negated else raw).match(name):
            hit = not negated
    return hit


def _strings(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    raise ValueError("filter is not a string list: %r" % (value,))


def push_selects(on, branch, changed):
    """True/False for one workflow's `on:` value; ValueError for a shape not modelled."""
    if on is None:
        return False
    if isinstance(on, str):
        return on == "push"
    if isinstance(on, list):
        return "push" in _strings(on)
    if not isinstance(on, dict):
        raise ValueError("unrecognised `on:` %r" % (on,))
    if "push" not in on:
        return False
    push = on["push"]
    if push is None:
        return True
    if not isinstance(push, dict):
        raise ValueError("unrecognised `on.push` %r" % (push,))
    if "branches" in push:
        if not matches(_strings(push["branches"]), branch):
            return False
    elif "branches-ignore" in push:
        if matches(_strings(push["branches-ignore"]), branch):
            return False
    elif "tags" in push or "tags-ignore" in push:
        return False                            # tag filters alone: branch pushes never run it
    if "paths" in push:
        paths = _strings(push["paths"])
        return any(matches(paths, name) for name in changed)
    if "paths-ignore" in push:
        ignored = _strings(push["paths-ignore"])
        return any(not matches(ignored, name) for name in changed)
    return True


def selects(clone, sha, branch="main"):
    """Would pushing `sha` to `branch` start a workflow? True, False, or None when unsure."""
    try:
        import yaml
    except ImportError:
        return None
    diff = fl.sh(["git", "-C", clone, "diff", "--name-only", "--no-renames", "-z", sha + "^1", sha])
    listing = fl.sh(["git", "-C", clone, "ls-tree", "--name-only", "-z", sha, WORKFLOWS])
    if diff.returncode != 0 or listing.returncode != 0:
        return None
    changed = [name for name in diff.stdout.split("\0") if name]
    for path in (p for p in listing.stdout.split("\0") if p.endswith((".yml", ".yaml"))):
        body = fl.sh(["git", "-C", clone, "show", "%s:%s" % (sha, path)])
        if body.returncode != 0:
            return None
        try:
            doc = yaml.safe_load(body.stdout)
            if not isinstance(doc, dict):
                return None
            # YAML 1.1 reads a bare `on:` key as the boolean True.
            if push_selects(doc.get("on", doc.get(True)), branch, changed):
                return True
        except (yaml.YAMLError, ValueError, re.error):
            return None
    return False
