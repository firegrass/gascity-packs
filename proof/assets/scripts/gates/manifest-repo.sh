#!/usr/bin/env bash
# Shared gate for the three repositories that hold no source — only Acurast job
# manifests, `.liskov` policy documents and the workflows that dispatch them:
# liskov-marketplace-offerings, liskov-marketplace-admittance and
# liskov-diagnostic-cargo. One script rather than three copies, because the
# check is the same and three copies drift.
#
# What it can honestly check without spending money: that every JSON document
# parses, that every workflow parses as YAML and declares the trigger and jobs
# it claims to, and that every shell and Node helper is syntactically sound.
# What it cannot: whether a policy is accepted, or whether a probe runs. Those
# cost a dispatch, and a dispatch in these repositories is a paid Acurast job —
# which is also why no policy here merges a pull request unattended.
set -euo pipefail
echo "gate: JSON documents" >&2
python3 - <<'PY'
import json, pathlib, sys
bad = []
for p in sorted(pathlib.Path(".").rglob("*.json")):
    if ".git" in p.parts or "node_modules" in p.parts:
        continue
    try:
        json.loads(p.read_text())
    except Exception as exc:
        bad.append("%s: %s" % (p, exc))
print("  %d JSON document(s) parsed" % len(list(pathlib.Path(".").rglob("*.json"))))
if bad:
    print("\n".join(bad), file=sys.stderr)
    sys.exit(1)
PY
echo "gate: workflow YAML" >&2
python3 - <<'PY'
import pathlib, sys, yaml
bad = []
workflows = sorted(pathlib.Path(".github/workflows").glob("*.y*ml")) if pathlib.Path(".github/workflows").is_dir() else []
for p in workflows:
    try:
        doc = yaml.safe_load(p.read_text())
    except Exception as exc:
        bad.append("%s: %s" % (p, exc)); continue
    if not isinstance(doc, dict):
        bad.append("%s: not a mapping" % p); continue
    # PyYAML reads an unquoted `on:` key as the boolean True (YAML 1.1).
    if doc.get(True) is None and doc.get("on") is None:
        bad.append("%s: no `on:` trigger" % p)
    if not doc.get("jobs"):
        bad.append("%s: no jobs" % p)
print("  %d workflow(s) parsed" % len(workflows))
if bad:
    print("\n".join(bad), file=sys.stderr)
    sys.exit(1)
PY
echo "gate: shell and Node helpers" >&2
while IFS= read -r -d '' script; do bash -n "$script"; done < <(find . -path ./.git -prune -o -name '*.sh' -print0)
echo "gate: passed" >&2
