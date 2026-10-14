#!/usr/bin/env python3
"""Export the part of an operator's Claude Code memory that is about one
repository into a directory a rig's agents can SEARCH — not into anything that
is loaded for them. Run by the operator, by hand; never an order.

    export_knowledge.py --rig liskov-rs --from ~/.claude/projects/<project>/memory \\
        --section "liskov-rs code and validation" --section "SQL and migrations" \\
        [--line-matching REGEX] [--drop-line-matching REGEX] [--exclude SLUG-SUBSTRING ...]

Why it exists: the traps a workspace has paid for — a migration that must take
two tables in the projector's order, the registries a new crate feeds — live in
the memory of sessions started in ONE directory on ONE machine. A rig worker
starts somewhere else and has none of it.

Why it is not the rig's auto-loaded memory: an index every conversation carries
is paid for on every turn of every packet, to make a worker re-derive what a
planner should have put in its item. Knowledge is spent once, at planning; this
directory is for the planner to read whole and for a worker to grep when
something surprises it. Output: `<rigs dir>/knowledge/<rig>/INDEX.md` plus the
memory files its lines link to. Re-running replaces the export.

A memory directory is personal and can describe production access. `--section`
and `--line-matching` choose lines of MEMORY.md; `--drop-line-matching` and
`--exclude` remove lines and files. Read INDEX.md before pointing agents at it.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import datetime, timezone

import factory_lib as fl
from factory_lib import admit

LINK = re.compile(r"\[([^\]]*)\]\(([A-Za-z0-9._-]+\.md)\)")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rig", required=True)
    ap.add_argument("--from", dest="source", required=True, help="a Claude Code memory directory holding MEMORY.md")
    ap.add_argument("--section", action="append", default=[], help="a `## ` heading of MEMORY.md to take whole")
    ap.add_argument("--line-matching", default="", help="also take any index line matching this regex")
    ap.add_argument("--drop-line-matching", default="", help="drop any chosen line matching this regex")
    ap.add_argument("--exclude", action="append", default=[], help="never export a file whose name contains this")
    args = ap.parse_args(argv)

    city = fl.city_dir()
    clone = admit.owned_clone(admit.load_rigs(city), args.rig)
    source = os.path.expanduser(args.source)
    index = os.path.join(source, "MEMORY.md")
    if not os.path.isfile(index):
        raise fl.Refused("%s has no MEMORY.md" % source)

    chosen, heading = {}, ""
    for line in open(index, encoding="utf-8").read().splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            continue
        if not line.startswith("- "):
            continue
        take = heading in args.section or (args.line_matching and re.search(args.line_matching, line))
        if take and not (args.drop_line_matching and re.search(args.drop_line_matching, line)):
            chosen.setdefault(heading if heading in args.section else "Other", []).append(line)
    missing = [s for s in args.section if s not in chosen]
    if missing:
        raise fl.Refused("MEMORY.md has no section (or no lines under) %s" % ", ".join(repr(m) for m in missing))

    target = os.path.join(os.path.dirname(clone.rstrip("/")), "knowledge", args.rig)
    staged = target + ".new"
    shutil.rmtree(staged, ignore_errors=True)
    os.makedirs(staged)
    copied, dropped = 0, []

    def keep(match):
        nonlocal copied
        name = match.group(2)
        path = os.path.join(source, name)
        if any(bad in name for bad in args.exclude) or not os.path.isfile(path):
            dropped.append(name)
            return match.group(1)                 # the words stay, the link goes
        if not os.path.isfile(os.path.join(staged, name)):
            shutil.copyfile(path, os.path.join(staged, name))
            copied += 1
        return match.group(0)

    out = ["# Traps this workspace has already paid for — %s" % args.rig, "",
           "Exported %s from an operator's session memory, for the `%s` rig. **Search it; do not read it."
           % (datetime.now(timezone.utc).strftime("%Y-%m-%d"), args.rig),
           "** `grep -ril <word> .` here when something surprises you. Each line links a file beside this",
           "one. A memory reflects what was true when it was written: if it names a file, function or",
           "flag, check that still exists before relying on it.", ""]
    for section in args.section + (["Other"] if "Other" in chosen else []):
        out += ["## %s" % section, ""] + [LINK.sub(keep, line) for line in chosen[section]] + [""]
    with open(os.path.join(staged, "INDEX.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    shutil.rmtree(target, ignore_errors=True)
    os.replace(staged, target)
    print("exported %d memory files and INDEX.md to %s" % (copied, target))
    if dropped:
        print("not exported (excluded, or the file is gone): %s" % ", ".join(sorted(set(dropped))))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except fl.Refused as why:
        print("export_knowledge: refused — %s" % why, file=sys.stderr)
        sys.exit(1)
