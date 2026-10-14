"""Atomic receipts for deterministic factory passes, independent of event logging."""
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone


def stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read(city, name="factory"):
    try:
        value = json.loads((Path(city) / "proof" / (name + "-heartbeat.json")).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def record(city, name, stage, code=None):
    now = stamp()
    old = read(city, name)
    value = {**old, "updated": now, "stage": stage}
    if stage == "start":
        value = {"started": now, "updated": now, "stage": stage,
                 "last_success": old.get("last_success", "")}
    if code is not None:
        value.update(finished=now, exit_code=int(code), stage=old.get("stage", stage))
        if int(code) == 0:
            value["last_success"] = now
    directory = Path(city) / "proof"
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as out:
        path = Path(out.name)
        json.dump(value, out)
        out.write("\n")
    try:
        os.replace(path, directory / (name + "-heartbeat.json"))
    finally:
        path.unlink(missing_ok=True)


def summary(city, name="factory", now=None):
    receipt = read(city, name)
    if not receipt:
        return "no heartbeat recorded"
    now = now or datetime.now(timezone.utc)
    try:
        age = int((now - datetime.fromisoformat(receipt["updated"].replace("Z", "+00:00"))).total_seconds())
    except (KeyError, ValueError, TypeError):
        return "unreadable heartbeat timestamp"
    if "finished" in receipt:
        state = "ok" if receipt.get("exit_code") == 0 else "FAILED (exit %s)" % receipt.get("exit_code", "unknown")
    else:
        state = "in progress"
    stale = "STALE; " if age > 20 * 60 else ""
    return "%s%s; stage %s, updated %s (%ds ago); last success %s" % (
        stale, state, receipt.get("stage", "unknown"), receipt["updated"], age, receipt.get("last_success") or "none")


if __name__ == "__main__":
    record(*sys.argv[1:])
