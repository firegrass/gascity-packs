"""One owner for the clone checks used by admission, startup and the doctor."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone


def problem(root):
    """Return a concrete refusal, or ''. Never read a failed command as clean."""
    env = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX"):
        env.pop(key, None)

    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], env=env,
                              capture_output=True, text=True, timeout=30)

    try:
        directory = git("rev-parse", "--absolute-git-dir")
        if directory.returncode:
            return "not a Git repository: " + directory.stderr.strip()[-300:]
        if not (Path(directory.stdout.strip()) / "gascity-owned").is_file():
            return "not a Gas City-owned clone (no gascity-owned marker)"
        bare = git("rev-parse", "--is-bare-repository")
        if bare.returncode or bare.stdout.strip() != "false":
            return "rig clone is bare or unreadable; inspect core.bare before restarting workers"
        inside = git("rev-parse", "--is-inside-work-tree")
        if inside.returncode or inside.stdout.strip() != "true":
            return "rig root is not a worktree"
        status = git("status", "--porcelain", "--untracked-files=no")
        if status.returncode:
            return "git status failed: " + status.stderr.strip()[-300:]
        if status.stdout.strip():
            return "tracked files are modified: " + "; ".join(status.stdout.splitlines()[:5])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "cannot inspect rig clone: " + str(exc)
    return ""


def receipt_path(city, root):
    digest = hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:20]
    return Path(city) / "proof" / "startup" / (digest + ".json")


def receipt(city, root):
    try:
        value = json.loads(receipt_path(city, root).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def record(city, root, why):
    """Record startup failures across replacement sessions; success resets it."""
    path = receipt_path(city, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = receipt(city, root)
        if not why:
            path.unlink(missing_ok=True)
            return 0
        same = previous.get("reason") == why
        before = previous.get("attempts", 0)
        count = before + 1 if same and isinstance(before, int) and before >= 0 else 1
        delay = min(60, 2 ** min(count - 1, 6)) if count > 1 else 0
        now = datetime.now(timezone.utc).isoformat()
        value = {"root": str(root), "reason": why, "attempts": count,
                 "since": previous.get("since", now) if same else now,
                 "last_attempt": now, "backoff_seconds": delay}
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(value))
        os.replace(temp, path)
        return delay


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root")
    parser.add_argument("--startup-city", help="record and back off repeated startup refusals")
    args = parser.parse_args(argv)
    why = problem(args.root)
    delay = 0
    if args.startup_city:
        try:
            delay = record(args.startup_city, args.root, why)
        except OSError as exc:
            print("rig-health: cannot record startup health: %s" % exc, file=sys.stderr)
            why = why or "cannot persist startup health"
    if why:
        print("prestart-guard: %s: %s%s" % (args.root, why,
              "; retry backoff %ds" % delay if delay else ""), file=sys.stderr, flush=True)
        if delay:
            time.sleep(delay)  # deterministic pre-start, before any model is launched
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
