#!/usr/bin/env python3
"""Retention and maintenance for the PROOF work ledger (`proof_work`).

Four subcommands, all scripts (no agent, no model, no tokens):

  replicate  push the ledger's `main` to the orchestrator git remote under a
             custom Dolt data ref (`refs/dolt/liskov-work-beads` by default),
             leaving every code ref alone, and record the pushed revision.
  backup     take one full-history consistent copy with Dolt's backup
             mechanism into `<backup-root>/hourly/<UTC ts>/`, verify it by
             restoring it, promote the first copy of each UTC day into
             `daily/`, keep 24 hourly and 30 daily copies.
  doctor     the retention checks ADR-0236 asks for; `ok` false with named
             problems otherwise. Everything fails closed.
  status     print the state file and what the ledger says about itself.

State file (read by admission): `<city>/proof/work-retention.json`, schema
`proof.work-retention.v1`. Each subcommand updates its own section under a
lock; only `doctor` sets `ok`, `problems` and `checked_at`.

Why this script talks to `dolt` directly
----------------------------------------
Factory scripts route every ledger operation through `gc bd`, because a rig's
database is Gas City's to pick. The work ledger is deliberately NOT a rig
(ADR-0236): it has no `gc bd` scope, so `gc bd` cannot reach it. This script
therefore addresses the Dolt sql-server itself, and only with EXPLICIT
arguments: `--workspace` names the `.beads` directory whose `metadata.json`
must say `dolt_database: proof_work` (or an isolated `proof_test_*`), and the
server port comes from that file. Like the work helper, it refuses to run
when any ambient `BEADS_*` / `BD_*` variable is set: those redirect bd and
Dolt routing, and a redirected maintenance run would replicate or back up the
wrong database while reporting success for the right one.

Which Dolt mechanism works for a server-mode database (proved on a disposable
2.3.5 server, see proof/tests/test_work_retention.py):

  - `dolt --host H --port P --user root --password '' --no-tls --use-db DB
     sql -r json -q "CALL DOLT_REMOTE('add', '--ref', REF, NAME, URL)"`
  - `... sql -q "CALL DOLT_PUSH(NAME, 'main')"`
  - `... sql -q "CALL DOLT_BACKUP('sync-url', 'file:///...')"`
  - offline, for verification only: `dolt clone --ref REF URL DIR` and
    `dolt backup restore file:///... NAME` into a temporary directory.

`dolt remote add --ref` and `dolt push` run as CLI commands against a server
are refused by Dolt ("sql-server running while attempting to use advanced
remote parameters" / "not supported against a remote host"), and offline
commands must never be pointed at a directory the server holds.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = "proof.work-retention.v1"
DEFAULT_REF = "refs/dolt/liskov-work-beads"
DEFAULT_REMOTE_NAME = "liskov-work"
DEFAULT_COMPACTOR_ORDER = "mol-dog-compactor"
ALLOWLIST_VAR = "GC_DOLT_COMPACT_ONLY_DBS"
PROJECT_DB = "proof_work"
TEST_DB_PREFIX = "proof_test_"
HOURLY_KEEP = 24
DAILY_KEEP = 30
MAX_REPLICATION_LAG = 300
MAX_BACKUP_AGE = 90 * 60
VERIFIED = "verified.json"
TS_FORMAT = "%Y%m%dT%H%M%SZ"


class Refused(Exception):
    """A named reason this run cannot proceed; nothing was changed."""


def log(message):
    print("work-maintenance: " + message, flush=True)


def utcnow(args):
    if getattr(args, "now", None):
        return parse_time(args.now)
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_time(text):
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ambient_overrides():
    return sorted(name for name in os.environ if name.startswith(("BEADS_", "BD_")))


def clean_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BEADS_", "BD_", "DOLT_CLI_"))}
    return env


# --- workspace and ledger ----------------------------------------------------

class Workspace:
    def __init__(self, path, host=None, port=None, user=None):
        self.path = Path(path).resolve()
        metadata_path = self.path / "metadata.json"
        try:
            self.metadata = json.loads(metadata_path.read_text())
        except (OSError, ValueError) as error:
            raise Refused("workspace_unreadable:%s" % error)
        self.database = str(self.metadata.get("dolt_database") or "")
        if self.database != PROJECT_DB and not self.database.startswith(TEST_DB_PREFIX):
            raise Refused("workspace_database_not_project:%s" % (self.database or "<unset>"))
        self.host = host or self.metadata.get("dolt_server_host") or "127.0.0.1"
        self.port = int(port or self.metadata.get("dolt_server_port") or 0)
        if not self.port:
            raise Refused("workspace_unreadable:no dolt_server_port in metadata.json")
        self.user = user or self.metadata.get("dolt_server_user") or "root"

    def auto_prune_disabled(self):
        """The flat `events-journal-auto-prune: false` line the work helper
        requires; a nested spelling or a missing key is retention drift."""
        try:
            text = (self.path / "config.yaml").read_text()
        except OSError:
            return False
        for line in text.splitlines():
            if re.match(r"^events-journal-auto-prune:\s*false\s*(#.*)?$", line):
                return True
        return False


class Ledger:
    """One SQL statement at a time against the sql-server, as JSON rows."""

    def __init__(self, workspace, timeout):
        self.ws = workspace
        self.timeout = timeout

    def argv(self):
        return ["dolt", "--host", self.ws.host, "--port", str(self.ws.port), "--user", self.ws.user,
                "--password", "", "--no-tls", "--use-db", self.ws.database]

    def sql(self, query, timeout=None):
        try:
            result = subprocess.run(self.argv() + ["sql", "-r", "json", "-q", query], capture_output=True,
                                    text=True, timeout=timeout or self.timeout, env=clean_env())
        except (OSError, subprocess.TimeoutExpired) as error:
            raise Refused("ledger_unreachable:%s" % error)
        if result.returncode:
            raise Refused("ledger_unreachable:%s" % (result.stderr.strip() or result.stdout.strip()
                                                     or "exit %d" % result.returncode).splitlines()[0])
        text = result.stdout.strip()
        if not text:
            return []
        try:
            return json.loads(text).get("rows") or []
        except ValueError:
            raise Refused("ledger_unreachable:unparseable response")

    def cell(self, query, column, timeout=None):
        rows = self.sql(query, timeout=timeout)
        if not rows or column not in rows[0]:
            raise Refused("ledger_unreachable:no %s in response to %s" % (column, query[:40]))
        return rows[0][column]

    def head(self):
        return str(self.cell("SELECT DOLT_HASHOF('main') AS h", "h"))

    def remotes(self):
        found = {}
        for row in self.sql("SELECT name, url, params FROM dolt_remotes"):
            params = row.get("params")
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except ValueError:
                    params = {}
            found[row["name"]] = dict(url=row.get("url"), ref=(params or {}).get("git_ref"))
        return found


def offline_dolt_env(scratch):
    """An identity for the offline restore/clone commands, in a throwaway
    DOLT_ROOT_PATH so the operator's own Dolt config is never touched."""
    root = Path(scratch) / "dolt-root"
    root.mkdir(exist_ok=True)
    env = clean_env()
    env["DOLT_ROOT_PATH"] = str(root)
    for key, value in (("user.name", "proof work maintenance"), ("user.email", "proof-work@localhost")):
        subprocess.run(["dolt", "config", "--global", "--add", key, value], check=True, env=env,
                       capture_output=True, text=True)
    return env


def offline_sql(directory, env, query, column, timeout=120):
    result = subprocess.run(["dolt", "sql", "-r", "json", "-q", query], cwd=directory, env=env,
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise Refused("restore_query_failed:%s" % (result.stderr.strip() or result.stdout.strip()).splitlines()[0])
    rows = json.loads(result.stdout.strip()).get("rows") or []
    if not rows or column not in rows[0]:
        raise Refused("restore_query_failed:no %s" % column)
    return rows[0][column]


# --- state file ----------------------------------------------------------------

def empty_state():
    return {"schema": SCHEMA, "ok": False, "checked_at": None, "problems": ["doctor_never_ran"],
            "replication": {"pushed_revision": None, "pushed_at": None, "lag_seconds": None},
            "backup": {"last_hourly_at": None, "last_daily_at": None, "hourly": 0, "daily": 0}}


def state_path(city):
    return Path(city) / "proof" / "work-retention.json"


@contextlib.contextmanager
def state_lock(city):
    proof = Path(city) / "proof"
    proof.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(proof / "work-retention.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read_state(city):
    path = state_path(city)
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return empty_state()
    if not isinstance(loaded, dict) or loaded.get("schema") != SCHEMA:
        return empty_state()
    state = empty_state()
    state.update(loaded)
    for key in ("replication", "backup"):
        section = dict(empty_state()[key])
        section.update(loaded.get(key) or {})
        state[key] = section
    return state


def write_state(city, state):
    path = state_path(city)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def update_state(city, mutate):
    with state_lock(city):
        state = read_state(city)
        mutate(state)
        write_state(city, state)
        return state


def lag_of(replication, now):
    pushed_at = replication.get("pushed_at")
    if not pushed_at:
        return None
    try:
        return max(0, int((now - parse_time(pushed_at)).total_seconds()))
    except ValueError:
        return None


# --- replicate ----------------------------------------------------------------

def replicate(args, ws, ledger, now):
    if not args.remote:
        raise Refused("replicate: --remote (or PROOF_WORK_REMOTE) is required")
    remotes = ledger.remotes()
    existing = remotes.get(args.remote_name)
    if existing is None:
        quoted = ", ".join(sql_string(v) for v in ("add", "--ref", args.ref, args.remote_name, args.remote))
        status = ledger.cell("CALL DOLT_REMOTE(%s)" % quoted, "status")
        if str(status) != "0":
            raise Refused("replication_remote_add_failed:%s" % status)
        log("remote %s -> %s (%s) added" % (args.remote_name, args.remote, args.ref))
    elif existing["url"] != args.remote or existing["ref"] != args.ref:
        raise Refused("replication_remote_mismatch:%s is %s ref %s, expected %s ref %s"
                      % (args.remote_name, existing["url"], existing["ref"], args.remote, args.ref))
    head_before = ledger.head()
    rows = ledger.sql("CALL DOLT_PUSH(%s, 'main')" % sql_string(args.remote_name), timeout=args.push_timeout)
    if not rows or str(rows[0].get("status")) != "0":
        raise Refused("replication_push_failed:%s" % ((rows or [{}])[0].get("message") or "no status"))
    tracked = ledger.sql("SELECT hash FROM dolt_remote_branches WHERE name = %s"
                         % sql_string("remotes/%s/main" % args.remote_name))
    pushed = str(tracked[0]["hash"]) if tracked else head_before
    message = (rows[0].get("message") or "").strip().splitlines()
    log("pushed main=%s to %s %s (%s)" % (pushed, args.remote_name, args.ref, message[-1] if message else "ok"))
    return pushed


def sql_string(value):
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def cmd_replicate(args):
    ws = Workspace(args.workspace, args.host, args.port, args.user)
    ledger = Ledger(ws, args.timeout)
    now = utcnow(args)
    try:
        pushed = replicate(args, ws, ledger, now)
    except Refused as error:
        def failed(state):
            state["replication"]["last_attempt_at"] = iso(now)
            state["replication"]["last_error"] = str(error)
            state["replication"]["lag_seconds"] = lag_of(state["replication"], now)
        update_state(args.city, failed)
        raise

    def succeeded(state):
        state["replication"].update(pushed_revision=pushed, pushed_at=iso(now), lag_seconds=0,
                                    last_attempt_at=iso(now), last_error=None)
    update_state(args.city, succeeded)


# --- backup -------------------------------------------------------------------

def verified_runs(directory):
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.is_dir() and (p / VERIFIED).is_file())


def verify_copy(ledger, url, scratch):
    env = offline_dolt_env(scratch)
    restore_dir = Path(scratch) / "restore"
    restore_dir.mkdir()
    result = subprocess.run(["dolt", "backup", "restore", url, "restored"], cwd=restore_dir, env=env,
                            capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise Refused("backup_restore_failed:%s" % (result.stderr.strip() or result.stdout.strip()).splitlines()[0])
    restored = restore_dir / "restored"
    head = str(offline_sql(restored, env, "SELECT DOLT_HASHOF('main') AS h", "h"))
    issues = int(offline_sql(restored, env, "SELECT COUNT(*) AS n FROM issues", "n"))
    commits = int(offline_sql(restored, env, "SELECT COUNT(*) AS n FROM dolt_log", "n"))
    in_history = int(ledger.cell("SELECT COUNT(*) AS n FROM dolt_log WHERE commit_hash = %s" % sql_string(head), "n"))
    if in_history != 1:
        raise Refused("backup_head_not_in_ledger_history:%s" % head)
    live_issues = int(ledger.cell("SELECT COUNT(*) AS n FROM issues AS OF %s" % sql_string(head), "n"))
    if live_issues != issues:
        raise Refused("backup_issue_count_mismatch:restored %d, ledger as of %s %d" % (issues, head, live_issues))
    return dict(head=head, issues=issues, dolt_commits=commits)


def rotate(directory, keep):
    runs = verified_runs(directory)
    removed = []
    for old in runs[:-keep] if len(runs) > keep else []:
        shutil.rmtree(old)
        removed.append(old.name)
    return removed


def cmd_backup(args):
    if not args.backup_root:
        raise Refused("backup: --backup-root (or PROOF_WORK_BACKUP_ROOT) is required")
    ws = Workspace(args.workspace, args.host, args.port, args.user)
    ledger = Ledger(ws, args.timeout)
    now = utcnow(args)
    root = Path(args.backup_root).resolve()
    hourly, daily = root / "hourly", root / "daily"
    stamp = now.strftime(TS_FORMAT)
    run_dir = hourly / stamp
    if run_dir.exists() or run_dir.with_name(stamp + ".unverified").exists():
        raise Refused("backup_target_exists:%s" % run_dir)
    run_dir.mkdir(parents=True)
    url = "file://" + str(run_dir / "dolt")
    try:
        status = ledger.cell("CALL DOLT_BACKUP('sync-url', %s)" % sql_string(url), "status", timeout=args.backup_timeout)
        if str(status) != "0":
            raise Refused("backup_sync_failed:%s" % status)
        with tempfile.TemporaryDirectory(prefix="proof-work-verify-") as scratch:
            facts = verify_copy(ledger, url, scratch)
    except Refused as error:
        quarantined = run_dir.with_name(stamp + ".unverified")
        run_dir.rename(quarantined)
        log("backup %s FAILED verification, kept at %s: %s" % (stamp, quarantined, error))

        def failed(state):
            state["backup"]["last_error"] = str(error)
            state["backup"]["last_attempt_at"] = iso(now)
        update_state(args.city, failed)
        raise
    facts.update(schema="proof.work-backup.v1", database=ws.database, backup_at=iso(now), verified_at=iso(utcnow(args)))
    (run_dir / VERIFIED).write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n")
    log("backup %s verified: head=%s issues=%d dolt_commits=%d" % (stamp, facts["head"], facts["issues"], facts["dolt_commits"]))
    promoted = None
    daily.mkdir(parents=True, exist_ok=True)
    if not any(p.name[:8] == stamp[:8] for p in verified_runs(daily)):
        shutil.copytree(run_dir, daily / stamp)
        promoted = stamp
        log("promoted %s to daily" % stamp)
    for name, directory, keep in (("hourly", hourly, HOURLY_KEEP), ("daily", daily, DAILY_KEEP)):
        removed = rotate(directory, keep)
        if removed:
            log("rotated %s: removed %s" % (name, ", ".join(removed)))
    hourly_runs, daily_runs = verified_runs(hourly), verified_runs(daily)

    def succeeded(state):
        state["backup"].update(last_hourly_at=iso(now), hourly=len(hourly_runs), daily=len(daily_runs),
                               last_error=None, last_attempt_at=iso(now))
        if promoted:
            state["backup"]["last_daily_at"] = iso(now)
        elif daily_runs:
            state["backup"].setdefault("last_daily_at", None)
    update_state(args.city, succeeded)


# --- doctor ---------------------------------------------------------------------

def city_config(city):
    """The city's effective order overrides and rigs: `gc config show --json`
    when gc can produce it, else city.toml itself. Both carry the compactor
    override, which is the thing the doctor must read."""
    city = Path(city)
    if shutil.which("gc"):
        try:
            result = subprocess.run(["gc", "config", "show", "--json"], cwd=city, capture_output=True, text=True,
                                    timeout=120, env=clean_env())
            if result.returncode == 0:
                config = json.loads(result.stdout)["config"]
                orders = config.get("Orders") or {}
                return dict(source="gc config show",
                            overrides=[dict(name=o.get("Name") or "", env=o.get("Env") or {})
                                       for o in orders.get("Overrides") or []],
                            skipped=list(orders.get("Skip") or []),
                            rigs=[dict(name=r.get("Name") or "", path=r.get("Path") or "", prefix=r.get("Prefix") or "")
                                  for r in config.get("Rigs") or []])
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            pass
    try:
        doc = tomllib.loads((city / "city.toml").read_text())
    except (OSError, ValueError) as error:
        raise Refused("city_config_unreadable:%s" % error)
    orders = doc.get("orders") or {}
    return dict(source="city.toml",
                overrides=[dict(name=o.get("name") or "", env=o.get("env") or {}) for o in orders.get("overrides") or []],
                skipped=list(orders.get("skip") or []),
                rigs=[dict(name=r.get("name") or "", path=r.get("path") or "", prefix=r.get("prefix") or "")
                      for r in doc.get("rigs") or []])


def metadata_database(path):
    try:
        return str(json.loads(Path(path).read_text()).get("dolt_database") or "")
    except (OSError, ValueError):
        return ""


def doctor_checks(args, now):
    problems = []
    ws = None
    ledger = None
    for name in ambient_overrides():
        problems.append("ambient_override:%s" % name)
    try:
        ws = Workspace(args.workspace, args.host, args.port, args.user)
    except Refused as error:
        problems.append(str(error))
    database = ws.database if ws else PROJECT_DB
    # (b) the workspace must keep its events journal.
    if ws and not ws.auto_prune_disabled():
        problems.append("workspace_auto_prune_not_false")
    # (a) the compactor only ever sees an explicit execution allowlist.
    try:
        config = city_config(args.city)
    except Refused as error:
        problems.append(str(error))
        config = dict(overrides=[], skipped=[], rigs=[], source="none")
    allowlist = ""
    for override in config["overrides"]:
        if override["name"] == args.compactor_order and override["env"].get(ALLOWLIST_VAR):
            allowlist = str(override["env"][ALLOWLIST_VAR])
    entries = [e.strip() for e in allowlist.split(",") if e.strip()]
    if not entries:
        problems.append("compactor_allowlist_unset")
    for entry in entries:
        if entry == database or entry == PROJECT_DB or entry.startswith(("proof_work", TEST_DB_PREFIX)):
            problems.append("compactor_allowlist_includes_project_database:%s" % entry)
    # (c) the ledger is not a rig, so no rig-scoped housekeeping reaches it.
    city = Path(args.city).resolve()
    rig_paths = []
    for rig in config["rigs"]:
        if database in (rig["name"], rig["prefix"]):
            problems.append("project_database_registered_as_rig:%s" % rig["name"])
        if rig["path"]:
            rig_paths.append((rig["name"], Path(rig["path"])))
    for candidate in (city / "rigs", city.parent / "rigs"):
        if candidate.is_dir():
            rig_paths.extend((p.name, p) for p in sorted(candidate.iterdir()) if p.is_dir())
    seen = set()
    for name, path in rig_paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if metadata_database(path / ".beads" / "metadata.json") == database:
            problems.append("project_database_registered_as_rig:%s" % name)
        if ws and ws.path == (path / ".beads").resolve():
            problems.append("project_database_registered_as_rig:%s" % name)
    if metadata_database(city / ".beads" / "metadata.json") == database:
        problems.append("project_database_registered_as_rig:city")
    # (d) every project bead is durable, versioned, without expiry, uncompacted.
    if ws:
        ledger = Ledger(ws, args.timeout)
        try:
            row = ledger.sql("SELECT COALESCE(SUM(ephemeral = 1), 0) AS ephemeral, "
                             "COALESCE(SUM(no_history = 1), 0) AS no_history, "
                             "COALESCE(SUM(JSON_EXTRACT(metadata, '$.expires_at') IS NOT NULL), 0) AS expiring, "
                             "COALESCE(SUM(compaction_level > 0 OR compacted_at IS NOT NULL), 0) AS compacted, "
                             "COALESCE(SUM(storage_class <> 'versioned'), 0) AS unversioned FROM issues")[0]
            for key in ("ephemeral", "no_history", "expiring", "compacted", "unversioned"):
                if int(row.get(key) or 0):
                    problems.append("%s_project_records:%d" % (key, int(row[key])))
            if args.remote:
                remote = ledger.remotes().get(args.remote_name)
                if remote is None:
                    problems.append("replication_remote_missing:%s" % args.remote_name)
                elif remote["url"] != args.remote or remote["ref"] != args.ref:
                    problems.append("replication_remote_mismatch:%s" % args.remote_name)
        except Refused as error:
            problems.append(str(error))
    # (e) off-host copies are fresh.
    state = read_state(args.city)
    lag = lag_of(state["replication"], now)
    if lag is None:
        problems.append("replication_never_pushed")
    elif lag > MAX_REPLICATION_LAG:
        problems.append("replication_lag_exceeded:%ds" % lag)
    last_backup = state["backup"].get("last_hourly_at")
    if not last_backup:
        problems.append("backup_never_verified")
    else:
        age = int((now - parse_time(last_backup)).total_seconds())
        if age > MAX_BACKUP_AGE:
            problems.append("backup_stale:%dm" % (age // 60))
    return problems, lag


def cmd_doctor(args):
    now = utcnow(args)
    problems, lag = doctor_checks(args, now)
    ok = not problems

    def record(state):
        state.update(ok=ok, checked_at=iso(now), problems=problems)
        state["replication"]["lag_seconds"] = lag
    state = update_state(args.city, record)
    print(json.dumps(state, indent=2, sort_keys=True))
    return 0 if ok else 1


def cmd_status(args):
    state = read_state(args.city)
    live = {}
    try:
        ws = Workspace(args.workspace, args.host, args.port, args.user)
        ledger = Ledger(ws, args.timeout)
        live = dict(database=ws.database, head=ledger.head(), remotes=ledger.remotes(),
                    issues=int(ledger.cell("SELECT COUNT(*) AS n FROM issues", "n")))
    except Refused as error:
        live = dict(error=str(error))
    print(json.dumps(dict(state=state, ledger=live, state_file=str(state_path(args.city))), indent=2, sort_keys=True))
    return 0


# --- entry ---------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="Retention and maintenance for the PROOF work ledger.")
    parser.add_argument("--workspace", default=os.environ.get("PROOF_WORK_WORKSPACE"),
                        help="the ledger's .beads directory (env PROOF_WORK_WORKSPACE)")
    parser.add_argument("--city", default=os.environ.get("GC_CITY_PATH"),
                        help="city root; the state file is <city>/proof/work-retention.json (env GC_CITY_PATH)")
    parser.add_argument("--remote", default=os.environ.get("PROOF_WORK_REMOTE"),
                        help="git URL of the orchestrator remote, as a Dolt git+ URL (env PROOF_WORK_REMOTE)")
    parser.add_argument("--ref", default=os.environ.get("PROOF_WORK_REF") or DEFAULT_REF,
                        help="git ref carrying the Dolt data (env PROOF_WORK_REF, default %s)" % DEFAULT_REF)
    parser.add_argument("--remote-name", default=os.environ.get("PROOF_WORK_REMOTE_NAME") or DEFAULT_REMOTE_NAME)
    parser.add_argument("--backup-root", default=os.environ.get("PROOF_WORK_BACKUP_ROOT"),
                        help="directory holding hourly/ and daily/ (env PROOF_WORK_BACKUP_ROOT)")
    parser.add_argument("--compactor-order", default=os.environ.get("PROOF_WORK_COMPACTOR_ORDER") or DEFAULT_COMPACTOR_ORDER)
    parser.add_argument("--host", help="override the sql-server host from metadata.json")
    parser.add_argument("--port", type=int, help="override the sql-server port from metadata.json")
    parser.add_argument("--user", help="override the sql-server user (default root)")
    parser.add_argument("--timeout", type=int, default=120, help="seconds per SQL statement")
    parser.add_argument("--push-timeout", type=int, default=300)
    parser.add_argument("--backup-timeout", type=int, default=900)
    parser.add_argument("--now", help="fake UTC clock for qualification runs (ISO 8601)")
    parser.add_argument("subcommand", choices=("replicate", "backup", "doctor", "status"))
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not args.workspace or not args.city:
        print("work-maintenance: --workspace and --city are required (or PROOF_WORK_WORKSPACE and GC_CITY_PATH)",
              file=sys.stderr)
        return 2
    if args.subcommand != "doctor":
        ambient = ambient_overrides()
        if ambient:
            print("work-maintenance: refusing to run with ambient routing override(s) %s set" % ", ".join(ambient),
                  file=sys.stderr)
            return 2
    try:
        if args.subcommand == "replicate":
            cmd_replicate(args)
            return 0
        if args.subcommand == "backup":
            cmd_backup(args)
            return 0
        if args.subcommand == "doctor":
            return cmd_doctor(args)
        return cmd_status(args)
    except Refused as error:
        print("work-maintenance: %s: %s" % (args.subcommand, error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
