"""The one place the factory clients ask which work authority is in force
(ADR-0236) and talk to the orchestrator's thin client when it is Beads.

The marker is `gascity/work-authority.json` in the Gas City-owned orchestrator
clone (`proof.work-authority.v1`). Absent, or `authority: git`, every client
keeps its Git/Markdown path exactly as before. `authority: beads` means the
project ledger is the authority: records come from the client's snapshot,
mutations go through `python3 scripts/work_client.py <action> … --json` run in
that clone, and no client may write a Markdown work record. A marker that
cannot be read, or names an authority this pack does not know, refuses both
paths rather than guessing which one is live.

`freeze: true` on the marker (either authority) is a write freeze: the
dispatcher admits and claims nothing, the completer completes and renews
nothing, the keeper renews nothing. Reads stay available.

Everything here is a thin wrapper: no policy, no readiness, no ordering. The
snapshot is cached per process and per clone; `fresh=True` reads it again.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import admit  # noqa: E402

MARKER = "gascity/work-authority.json"
MARKER_SCHEMA = "proof.work-authority.v1"
CLIENT = "scripts/work_client.py"
AUTHORITIES = ("git", "beads")
FREEZE_REASON = "work authority write freeze"
LEASE_KEY = "proof.lease_id"
RETENTION = "proof/work-retention.json"
RETENTION_SCHEMA = "proof.work-retention.v1"
RETENTION_MAX_AGE = 600       # seconds: the file must be younger than ten minutes
RETENTION_MAX_LAG = 300       # seconds: replication lag the dispatcher admits under
# A completion the helper refuses because the item is the last active child of
# its epic: `proof_work_evaluate.complete` says "last active child of <EPIC>:
# a person must complete with close_epic". Matched on that sentence, so the
# epic's other refusals ("close_epic requires…", "can never close an epic")
# are never mistaken for a hold.
LAST_CHILD_RE = re.compile(r"last active child of \S+: a person must complete with close_epic", re.I)


class ClientRefused(admit.Refused):
    """The client (or the helper behind it) refused a mutation; `detail` is its
    stderr, one line."""


_snapshots = {}


def marker(clone):
    """The parsed marker, or the default Git marker when the file is absent."""
    path = os.path.join(clone, MARKER)
    if not os.path.isfile(path):
        return {"schema": MARKER_SCHEMA, "authority": "git", "epoch": None, "source_sha": None,
                "switched_at": None, "workspace": None, "helper": None}
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        raise admit.Refused("the work authority marker %s cannot be read (%s); refusing both the Git and "
                            "the Beads path" % (path, exc))
    if not isinstance(doc, dict) or doc.get("schema") != MARKER_SCHEMA or doc.get("authority") not in AUTHORITIES:
        raise admit.Refused("the work authority marker %s is not a %s naming git or beads; refusing both paths"
                            % (path, MARKER_SCHEMA))
    return doc


def authority(clone):
    return marker(clone)["authority"]


def beads(clone):
    return authority(clone) == "beads"


def frozen(clone):
    """True while the marker carries `freeze: true` — a write freeze under
    either authority."""
    return marker(clone).get("freeze") is True


def refuse_markdown_write(clone, what):
    """Raise when a legacy Markdown work-record write is about to happen under
    the Beads authority. Called by every client path that still edits a record."""
    if beads(clone):
        raise admit.Refused("%s is a Markdown work-record write, refused under the beads work authority "
                            "(ADR-0236)" % what)


def client(clone, action, *, item=None, by=None, hours=None, repos=None, lease=None, request=None,
           mutation=True):
    """Run `scripts/work_client.py <action> … --json` in the owned clone and
    return its result document.

    A mutation that the client refuses (exit 1, one line on stderr) raises
    `ClientRefused`; a read that fails raises `admit.Unavailable`, because a
    snapshot that cannot be read is an outage, not a decision about an item."""
    argv = [sys.executable, CLIENT, action]
    if item is not None:
        argv += ["--id", item]
    if by is not None:
        argv += ["--by", by]
    if hours is not None:
        argv += ["--hours", str(hours)]
    if repos:
        argv += ["--repos", ",".join(repos)]
    if lease is not None:
        argv += ["--lease", lease]
    path = None
    try:
        if request is not None:
            fd, path = tempfile.mkstemp(prefix="work-request.", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(request, fh, sort_keys=True)
            argv += ["--request-file", path]
        argv.append("--json")
        try:
            out = subprocess.run(argv, cwd=clone, capture_output=True, text=True, timeout=admit.RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise admit.Unavailable("`work_client.py %s` did not finish in %d s and was killed" % (action, admit.RUN_TIMEOUT))
        except OSError as exc:
            raise admit.Unavailable("`work_client.py %s` could not run: %s" % (action, exc))
    finally:
        if path:
            os.unlink(path)
    if out.returncode != 0:
        detail = (out.stderr or out.stdout).strip().splitlines()
        detail = detail[-1] if detail else "exit %d" % out.returncode
        if mutation:
            raise ClientRefused("work_client.py %s refused: %s" % (action, detail[-400:]))
        raise admit.Unavailable("work_client.py %s failed (%d): %s" % (action, out.returncode, detail[-400:]))
    try:
        return json.loads(out.stdout or "null")
    except ValueError as exc:
        raise admit.Unavailable("work_client.py %s printed no JSON result: %s" % (action, exc))


def snapshot(clone, fresh=False):
    """(state, rows, frontier) from the client, cached per process and clone."""
    key = os.path.realpath(clone)
    if fresh or key not in _snapshots:
        doc = client(clone, "snapshot", mutation=False)
        if not isinstance(doc, dict) or not isinstance(doc.get("records"), list):
            raise admit.Unavailable("work_client.py snapshot returned no record list")
        rows = [r for r in doc["records"] if isinstance(r, dict) and isinstance(r.get("id"), str)]
        state = {k: v for k, v in doc.items() if k not in ("records", "frontier", "native_records")}
        _snapshots[key] = (state, rows, doc.get("frontier") or {})
    return _snapshots[key]


def forget(clone=None):
    """Drop the cached snapshot after a mutation, so the next read is current."""
    if clone is None:
        _snapshots.clear()
    else:
        _snapshots.pop(os.path.realpath(clone), None)


def rows_by_id(clone, fresh=False):
    return {row["id"]: row for row in snapshot(clone, fresh)[1]}


def row(clone, item_id, fresh=False):
    """The project record row for `item_id`, or None when the ledger has none."""
    return rows_by_id(clone, fresh).get(item_id)


def ledger_revision(clone, fresh=False):
    return snapshot(clone, fresh)[0].get("revision")


def epoch(clone, fresh=False):
    return snapshot(clone, fresh)[0].get("epoch")


def corpus(clone, fresh=False):
    """A storage-free corpus over the snapshot rows, with the selectors the
    Git corpus offers (`items`, `epics`, `by_id`, `Record.fm/status/path`)."""
    import proof_work_records as records
    return records.Corpus(snapshot(clone, fresh)[1], root=clone)


def lease_of(clone, item_id, seat, bead_md=None, fresh=False):
    """The lease a factory seat holds on `item_id`: what admission stored on
    the execution bead (`proof.lease_id`), else the snapshot row's `lease_id`
    when that row's claim is the seat's. '' when there is none."""
    stored = (bead_md or {}).get(LEASE_KEY)
    if stored:
        return str(stored)
    current = row(clone, item_id, fresh)
    if current and (current.get("fields") or {}).get("claimed_by") == seat and current.get("lease_id"):
        return str(current["lease_id"])
    return ""


def retention_problem(city, now=None):
    """Why the dispatcher must admit nothing under the Beads authority, or ''.

    `<city>/proof/work-retention.json` is written by the pack's maintenance
    (ADR-0236): it must exist, be younger than ten minutes, say `ok: true` and
    report replication lag of at most five minutes. Anything else — including
    a file that cannot be read — refuses every new admission."""
    import time
    path = os.path.join(city, RETENTION)
    if not os.path.isfile(path):
        return "retention state %s is missing; no admission until the maintenance order writes it" % path
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        age = (now if now is not None else time.time()) - os.stat(path).st_mtime
    except (OSError, ValueError) as exc:
        return "retention state %s cannot be read (%s); no admission" % (path, exc)
    if not isinstance(doc, dict) or doc.get("schema") != RETENTION_SCHEMA:
        return "retention state %s is not a %s document; no admission" % (path, RETENTION_SCHEMA)
    if age > RETENTION_MAX_AGE:
        return "retention state %s is %d s old (limit %d); no admission until maintenance runs again" % (
            path, int(age), RETENTION_MAX_AGE)
    if doc.get("ok") is not True:
        problems = doc.get("problems") if isinstance(doc.get("problems"), list) else []
        return "retention state says ok=%r (%s); no admission" % (
            doc.get("ok"), "; ".join(str(p) for p in problems[:4]) or "no problem listed")
    lag = (doc.get("replication") or {}).get("lag_seconds") if isinstance(doc.get("replication"), dict) else None
    if not isinstance(lag, (int, float)) or isinstance(lag, bool) or lag < 0:
        return "retention state carries no replication.lag_seconds; no admission"
    if lag > RETENTION_MAX_LAG:
        return "replication lag is %d s (limit %d); no admission until the ledger is replicated off-host" % (
            int(lag), RETENTION_MAX_LAG)
    return ""


def is_last_child_refusal(exc):
    return bool(LAST_CHILD_RE.search(str(exc)))


def printed_lease(text):
    """The lease id a claim printed (`next_work.py --claim` under the Beads
    authority prints it), or ''."""
    match = re.search(r"lease(?:[_ ]id)?[=: ]+([A-Za-z0-9][A-Za-z0-9._:-]*)", text or "", re.I)
    return match.group(1) if match else ""
