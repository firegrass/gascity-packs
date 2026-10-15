#!/usr/bin/env python3
"""Qualify the work-ledger retention script on a disposable Dolt server.

Never a live city, never port 17494: every test starts its own sql-server in a
temporary directory and stops it. Four groups, as ADR-0236 asks:

  (a) the real compactor script, run against the disposable server with an
      execution-only GC_DOLT_COMPACT_ONLY_DBS, leaves the project database's
      commit history and rows untouched — and WITHOUT the allowlist it flattens
      that history (the hazard the allowlist guards);
  (b) replication to a bare git remote under the custom ref, pushed until the
      backing ref's ancestry is OBSERVED to roll over, then a fresh clone of
      the ref carries every Dolt commit and row, and the git branch refs that
      existed before are untouched;
  (c) hourly backups across UTC day boundaries under a fake clock keep 24
      hourly and 30 daily copies, every one restorable, and never delete a
      copy that is not verified;
  (d) each doctor failure condition produces its named problem and ok=false,
      and ambient BD_*/BEADS_* variables are stripped and recorded, not refused;
  (e) storage GC through the server keeps every commit, every row at every
      commit, the event and receipt issues and a later clone intact while the
      journal shrinks, and a journal over the threshold refuses backups.

Requires bd 1.3.1, Dolt 2.3.5 and git; (a) also needs a Gas City checkout
(GASCITY_CHECKOUT, or a sibling `gascity` directory) for the compactor.
"""
import csv
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "assets" / "scripts" / "work_maintenance.py"
REF = "refs/dolt/liskov-work-beads"
COMPACTOR = Path("examples/bd/dolt/commands/compact/run.sh")


def binaries_present():
    return all(shutil.which(name) for name in ("bd", "dolt", "git"))


def gascity_checkout():
    candidates = [os.environ.get("GASCITY_CHECKOUT") or ""]
    candidates += [str(parent / "gascity") for parent in HERE.resolve().parents[:6]]
    for candidate in candidates:
        if candidate and (Path(candidate) / COMPACTOR).is_file():
            return Path(candidate)
    return None


def free_port():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def clean_env():
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("BEADS_", "BD_", "GC_", "DOLT_", "LISKOV_WORK_"))}


class DisposableServer:
    """A sql-server in a temporary directory, with bd workspaces on it."""

    def __init__(self):
        self.base = Path(tempfile.mkdtemp(prefix="proof-work-retention-"))
        # Unix sockets are limited to 108 bytes; the base directory may be long.
        self.sock_dir = Path(tempfile.mkdtemp(prefix="pwr-", dir="/tmp"))
        self.socket = self.sock_dir / "s"
        self.port = free_port()
        self.data_dir = self.base / "server"
        self.data_dir.mkdir()
        self.env = clean_env()
        self.env.update(DOLT_ROOT_PATH=str(self.base / "dolt-root"), XDG_CONFIG_HOME=str(self.base / "xdg"),
                        GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
                        GIT_AUTHOR_NAME="retention qualification", GIT_AUTHOR_EMAIL="probe@localhost",
                        GIT_COMMITTER_NAME="retention qualification", GIT_COMMITTER_EMAIL="probe@localhost")
        (self.base / "dolt-root").mkdir()
        for key, value in (("user.name", "retention qualification"), ("user.email", "probe@localhost")):
            subprocess.run(["dolt", "config", "--global", "--add", key, value], check=True, env=self.env,
                           capture_output=True)
        config = self.base / "server.yaml"
        config.write_text("log_level: warning\nlistener:\n  host: 127.0.0.1\n  port: %d\n  socket: %s\n"
                          "data_dir: %s\nbehavior:\n  auto_gc_behavior:\n    enable: false\n"
                          % (self.port, json.dumps(str(self.socket)), json.dumps(str(self.data_dir))))
        self.log = (self.base / "server.log").open("w")
        self.process = subprocess.Popen(["dolt", "sql-server", "--config", str(config)], cwd=self.data_dir,
                                        env=self.env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 60
        while not self.socket.exists():
            if self.process.poll() is not None or time.monotonic() >= deadline:
                self.stop()
                raise RuntimeError("disposable sql-server did not start: " + (self.base / "server.log").read_text())
            time.sleep(0.05)

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.log.close()
        shutil.rmtree(self.sock_dir, ignore_errors=True)
        shutil.rmtree(self.base, ignore_errors=True)

    def workspace(self, name, database, prefix="pq"):
        project = self.base / name
        project.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=project, check=True, env=self.env)
        env = dict(self.env, BEADS_DOLT_SERVER_HOST="127.0.0.1", BEADS_DOLT_SERVER_PORT=str(self.port),
                   BEADS_DOLT_SERVER_SOCKET=str(self.socket), BEADS_DOLT_AUTO_START="0", BEADS_DOLT_SERVER_USER="root")
        subprocess.run(["bd", "init", "--server", "--external", "--server-port", str(self.port), "--server-socket",
                        str(self.socket), "--prefix", prefix, "--database", database, "--skip-agents", "--skip-hooks",
                        "--non-interactive"], cwd=project, check=True, env=env, capture_output=True, text=True)
        workspace = project / ".beads"
        with (workspace / "config.yaml").open("a") as stream:
            stream.write("\nevents-journal-auto-prune: false\n")
        return workspace

    def bd(self, workspace, *args):
        result = subprocess.run(["bd", *args], cwd=workspace.parent, env=dict(self.env, BEADS_DIR=str(workspace)),
                                capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError("bd %s: %s%s" % (" ".join(args), result.stdout, result.stderr))
        return result.stdout

    def create_issue(self, workspace, title, *extra):
        created = json.loads(self.bd(workspace, "create", title, "--json", *extra))
        created = created[0] if isinstance(created, list) else created
        return created["id"]

    def seed(self, workspace, count):
        """Versioned records across several Dolt commits: creates and updates."""
        ids = [self.create_issue(workspace, "Record %d" % i) for i in range(count)]
        for ident in ids:
            self.bd(workspace, "update", ident, "--set-metadata", "proof.seed=%s" % ident)
        return ids

    def sql(self, database, query, fmt="json"):
        result = subprocess.run(["dolt", "--host", "127.0.0.1", "--port", str(self.port), "--user", "root",
                                 "--password", "", "--no-tls", "--use-db", database, "sql", "-r", fmt, "-q", query],
                                env=self.env, capture_output=True, text=True, timeout=300)
        if result.returncode:
            raise RuntimeError("sql %s: %s%s" % (query[:60], result.stdout, result.stderr))
        if fmt == "json":
            return json.loads(result.stdout.strip()).get("rows") or [] if result.stdout.strip() else []
        return result.stdout

    def head(self, database):
        return str(self.sql(database, "SELECT DOLT_HASHOF('main') AS h")[0]["h"])

    def commits(self, database):
        return [str(r["commit_hash"]) for r in self.sql(database, "SELECT commit_hash FROM dolt_log")]

    def rows(self, database):
        return list(csv.reader(io.StringIO(self.sql(database, "SELECT * FROM issues ORDER BY id", fmt="csv"))))

    def rows_as_of(self, database, table, commit):
        order = "id" if table == "issues" else "1, 2, 3"
        try:
            text = self.sql(database, "SELECT * FROM %s AS OF '%s' ORDER BY %s" % (table, commit, order), fmt="csv")
        except RuntimeError as error:
            if "table not found" in str(error):
                return None     # a commit before bd created the table
            raise
        return list(csv.reader(io.StringIO(text)))


def offline_rows(directory, env, query="SELECT * FROM issues ORDER BY id"):
    result = subprocess.run(["dolt", "sql", "-r", "csv", "-q", query], cwd=directory, env=env,
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return list(csv.reader(io.StringIO(result.stdout)))


def offline_commits(directory, env):
    return [row[0] for row in offline_rows(directory, env, "SELECT commit_hash FROM dolt_log")[1:]]


def maintenance(server, workspace, city, subcommand, *extra, env=None, expect=0):
    argv = [sys.executable, str(SCRIPT), "--workspace", str(workspace), "--city", str(city), *extra, subcommand]
    result = subprocess.run(argv, env=env or server.env, capture_output=True, text=True, timeout=1800)
    if expect is not None and result.returncode != expect:
        raise AssertionError("work_maintenance %s exited %d, expected %d\n%s\n%s"
                             % (subcommand, result.returncode, expect, result.stdout, result.stderr))
    return result


def state_of(city):
    return json.loads((Path(city) / "proof" / "work-retention.json").read_text())


def write_city_toml(city, allowlist="hq,orc", rigs=(), compactor="mol-dog-compactor"):
    text = ""
    if allowlist is not None:
        text += '[[orders.overrides]]\nname = "%s"\n[orders.overrides.env]\nGC_DOLT_COMPACT_ONLY_DBS = "%s"\n\n' % (
            compactor, allowlist)
    for rig in rigs:
        text += '[[rigs]]\nname = "%s"\nprefix = "%s"\npath = "%s"\n\n' % rig
    (Path(city) / "city.toml").write_text(text)


def stub_gc(directory):
    """A `gc` that answers nothing: the compactor falls back to its filesystem
    scan, and the doctor to city.toml. The real gc, if installed, must not see
    a temporary city."""
    directory.mkdir(exist_ok=True)
    stub = directory / "gc"
    stub.write_text("#!/bin/sh\necho 'stub gc: no city' >&2\nexit 1\n")
    stub.chmod(0o755)
    return directory


@unittest.skipUnless(binaries_present(), "bd, dolt and git are required")
class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = DisposableServer()
        cls.city = cls.server.base / "city"
        (cls.city / "proof").mkdir(parents=True)
        cls.env = dict(cls.server.env, PATH=str(stub_gc(cls.server.base / "stub-bin")) + os.pathsep + cls.server.env["PATH"],
                       LISKOV_WORK_DATA_DIR=str(cls.server.data_dir))

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()


class CompactorAllowlist(ServerCase):
    """(a) the real compactor, allowlisted and not."""

    def test_allowlist_keeps_compactor_off_project_database(self):
        checkout = gascity_checkout()
        if checkout is None:
            self.skipTest("no Gas City checkout with the compactor (set GASCITY_CHECKOUT)")
        server = self.server
        project_db, execution_db = "proof_test_compact", "hq"
        project = server.workspace("compact-project", project_db)
        execution = server.workspace("compact-execution", execution_db, prefix="hq")
        server.seed(project, 6)
        server.seed(execution, 3)
        city = server.base / "compact-city"
        (city / ".beads").mkdir(parents=True)
        (city / ".beads" / "metadata.json").write_text(json.dumps({"dolt_database": execution_db}))
        state_dir = server.base / "compact-state"
        state_dir.mkdir()
        # The compactor stamps its watermark at HEAD on first sight, protecting
        # pre-existing history; the operator marker makes it flatten from the
        # root, which is the worst case for the ledger and what the allowlist
        # must withstand.
        for db in (project_db, execution_db):
            (server.data_dir / db / ".compact-full-history").touch()
        before_commits, before_rows, before_head = server.commits(project_db), server.rows(project_db), server.head(project_db)
        self.assertGreater(len(before_commits), 10)
        env = dict(self.env, GC_CITY_PATH=str(city), GC_PACK_DIR=str(checkout / "examples/bd/dolt"),
                   GC_DOLT_PORT=str(server.port), GC_DOLT_HOST="127.0.0.1",
                   GC_DOLT_MANAGED_LOCAL="0", GC_DOLT_DATA_DIR=str(server.data_dir), GC_PACK_STATE_DIR=str(state_dir),
                   GC_DOLT_COMPACT_THRESHOLD_COMMITS="1", GC_DOLT_COMPACT_ONLY_DBS=execution_db)
        execution_before = len(server.commits(execution_db))
        result = subprocess.run(["sh", str(checkout / COMPACTOR)], env=env, capture_output=True, text=True, timeout=900)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("db=%s not in GC_DOLT_COMPACT_ONLY_DBS" % project_db, output)
        self.assertIn("db=%s" % execution_db, output)
        self.assertLess(len(server.commits(execution_db)), execution_before, "the execution database was flattened:\n" + output)
        self.assertEqual(server.commits(project_db), before_commits, "project history changed:\n" + output)
        self.assertEqual(server.rows(project_db), before_rows)
        self.assertEqual(server.head(project_db), before_head)
        # The hazard: without the allowlist the same compactor flattens the ledger.
        del env["GC_DOLT_COMPACT_ONLY_DBS"]
        result = subprocess.run(["sh", str(checkout / COMPACTOR)], env=env, capture_output=True, text=True, timeout=900)
        output = result.stdout + result.stderr
        self.assertIn("db=%s" % project_db, output)
        after_commits = server.commits(project_db)
        self.assertLess(len(after_commits), len(before_commits), "the unguarded compactor left the project history alone:\n" + output)
        self.assertNotEqual(server.head(project_db), before_head)
        self.assertEqual(server.rows(project_db), before_rows, "flatten changed rows")


class Replication(ServerCase):
    """(b) push across an observed backing-ref rollover, then clone."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.workspace = cls.server.workspace("repl-project", "proof_test_repl")
        cls.remote = cls.server.base / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(cls.remote)], check=True, env=cls.env)
        seed = cls.server.base / "remote-seed"
        subprocess.run(["git", "init", "-q", str(seed)], check=True, env=cls.env)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "seed"], cwd=seed, check=True, env=cls.env)
        subprocess.run(["git", "push", "-q", str(cls.remote), "HEAD:refs/heads/main", "HEAD:refs/heads/feature",
                        "HEAD:refs/tags/v0"], cwd=seed, check=True, env=cls.env)
        cls.url = "git+file://" + str(cls.remote)

    def git(self, *args):
        return subprocess.run(["git", "--git-dir", str(self.remote), *args], check=True, env=self.env,
                              capture_output=True, text=True).stdout

    def refs(self):
        return {line.split()[2]: line.split()[0] for line in self.git("for-each-ref").splitlines()}

    def test_replicates_across_rollover_and_clones_complete_history(self):
        server, ws, city = self.server, self.workspace, self.city
        refs_before = self.refs()
        server.seed(ws, 2)
        tips = []
        rollover_at = None
        for step in range(200):
            server.create_issue(ws, "Change %d" % step)
            maintenance(server, ws, city, "replicate", "--remote", self.url, "--ref", REF, env=self.env)
            state = state_of(city)
            self.assertEqual(state["replication"]["pushed_revision"], server.head("proof_test_repl"))
            self.assertEqual(state["replication"]["lag_seconds"], 0)
            tip = self.refs()[REF]
            ancestry = self.git("rev-list", tip).split()
            if tips and tip != tips[-1] and tips[-1] not in ancestry:
                # The previous tip is no longer reachable: Dolt severed the
                # chain with a parentless commit. Observed, not assumed.
                rollover_at = (step, tips[-1], tip, len(ancestry))
                break
            tips.append(tip)
        self.assertIsNotNone(rollover_at, "no backing-ref rollover observed in 200 pushes; tips seen: %d" % len(tips))
        step, previous, tip, depth_after = rollover_at
        depth_before = int(self.git("rev-list", "--count", previous))
        self.assertGreaterEqual(depth_before, 64, "the chain rolled over before Dolt's documented 64 parented commits")
        self.assertLess(depth_after, depth_before)
        self.assertGreater(len(tips), 1)
        # One more change after the rollover, so the clone is from a rolled-over ref with later data.
        server.create_issue(ws, "After rollover")
        maintenance(server, ws, city, "replicate", "--remote", self.url, "--ref", REF, env=self.env)
        live_commits, live_rows = server.commits("proof_test_repl"), server.rows("proof_test_repl")
        self.assertGreater(len(live_commits), 60)
        clone_base = server.base / "clone"
        clone_base.mkdir()
        result = subprocess.run(["dolt", "clone", "--ref", REF, self.url, "restored"], cwd=clone_base, env=self.env,
                                capture_output=True, text=True, timeout=600)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        restored = clone_base / "restored"
        self.assertEqual(offline_commits(restored, self.env), live_commits, "every historical Dolt commit is in the clone")
        self.assertEqual(offline_rows(restored, self.env), live_rows)
        refs_after = self.refs()
        for name, sha in refs_before.items():
            self.assertEqual(refs_after.get(name), sha, "pre-existing ref %s changed" % name)
        new_refs = set(refs_after) - set(refs_before)
        self.assertLessEqual(new_refs, {REF, "refs/heads/__dolt_remote_info__"}, new_refs)
        self.assertIn(REF, new_refs)
        # Idempotent: nothing new to push still confirms the remote.
        before = state_of(city)["replication"]
        maintenance(server, ws, city, "replicate", "--remote", self.url, "--ref", REF, env=self.env)
        self.assertEqual(state_of(city)["replication"]["pushed_revision"], before["pushed_revision"])
        # A different remote or ref for the same name is refused, and recorded.
        result = maintenance(server, ws, city, "replicate", "--remote", self.url + "-other", "--ref", REF, env=self.env, expect=1)
        self.assertIn("replication_remote_mismatch", result.stderr)
        self.assertIn("replication_remote_mismatch", state_of(city)["replication"]["last_error"])
        self.assertEqual(state_of(city)["replication"]["pushed_revision"], before["pushed_revision"])

    def test_force_push_replaces_the_history_of_a_reinitialised_ledger(self):
        """A cutover reset drops and re-creates the database, so the next
        import is an unrelated Dolt history: a plain push is refused ("no
        common ancestor") and only `--force --reason` replaces the ref. The
        git-remote cache is carried across the re-creation the way the
        runbook does it; wiped under a running server, Dolt keeps a dead
        handle and every push fails until the server restarts (observed
        2026-10-15, live ledger)."""
        server = self.server
        ws = server.workspace("force-project", "proof_test_force")
        city = server.base / "force-city"
        (city / "proof").mkdir(parents=True)
        remote = server.base / "force-remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, env=self.env)
        seed = server.base / "force-seed"
        subprocess.run(["git", "init", "-q", str(seed)], check=True, env=self.env)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "seed"], cwd=seed, check=True, env=self.env)
        subprocess.run(["git", "push", "-q", str(remote), "HEAD:refs/heads/main"], cwd=seed, check=True, env=self.env)
        url = "git+file://" + str(remote)
        server.seed(ws, 2)
        maintenance(server, ws, city, "replicate", "--remote", url, "--ref", REF, env=self.env)
        first = state_of(city)["replication"]["pushed_revision"]
        self.assertEqual(first, server.head("proof_test_force"))
        # Re-initialise the ledger the way the cutover reset does, keeping the cache.
        cache = server.data_dir / "proof_test_force" / ".dolt" / "git-remote-cache"
        self.assertTrue(cache.is_dir(), "the first push created the git-remote cache")
        kept = server.base / "kept-cache"
        shutil.copytree(cache, kept, symlinks=True)
        subprocess.run(["dolt", "--host", "127.0.0.1", "--port", str(server.port), "--user", "root", "--password", "",
                        "--no-tls", "sql", "-q", "DROP DATABASE `proof_test_force`"], check=True, env=self.env,
                       capture_output=True, text=True)
        shutil.rmtree(server.base / "force-project")
        ws = server.workspace("force-project", "proof_test_force")
        shutil.copytree(kept, cache, symlinks=True)
        server.seed(ws, 3)
        self.assertNotEqual(server.head("proof_test_force"), first)
        result = maintenance(server, ws, city, "replicate", "--remote", url, "--ref", REF, env=self.env, expect=1)
        self.assertIn("no common ancestor", result.stderr + state_of(city)["replication"]["last_error"])
        self.assertEqual(state_of(city)["replication"]["pushed_revision"], first, "a refused push records nothing new")
        result = maintenance(server, ws, city, "replicate", "--remote", url, "--ref", REF, "--force", env=self.env, expect=1)
        self.assertIn("--reason", result.stderr)
        maintenance(server, ws, city, "replicate", "--remote", url, "--ref", REF, "--force", "--reason",
                    "cutover import epoch test", env=self.env)
        state = state_of(city)["replication"]
        self.assertEqual(state["pushed_revision"], server.head("proof_test_force"))
        self.assertEqual(state["forced_reason"], "cutover import epoch test")
        self.assertIsNotNone(state["forced_at"])
        self.assertEqual(state["lag_seconds"], 0)
        # The replaced ref clones back to exactly the new history.
        clone_base = server.base / "force-clone"
        clone_base.mkdir()
        result = subprocess.run(["dolt", "clone", "--ref", REF, url, "restored"], cwd=clone_base, env=self.env,
                                capture_output=True, text=True, timeout=600)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(offline_commits(clone_base / "restored", self.env), server.commits("proof_test_force"))
        self.assertEqual(offline_rows(clone_base / "restored", self.env), server.rows("proof_test_force"))
        # A plain push after the force is an ordinary fast-forward again.
        server.create_issue(ws, "After force")
        maintenance(server, ws, city, "replicate", "--remote", url, "--ref", REF, env=self.env)
        self.assertEqual(state_of(city)["replication"]["pushed_revision"], server.head("proof_test_force"))

    def test_strips_ambient_routing_override(self):
        """The city's controller sets BD_* for every exec order; they are
        stripped from the dolt/bd subprocesses and recorded, never refused."""
        result = maintenance(self.server, self.workspace, self.city, "replicate", "--remote", self.url, "--ref", REF,
                             env=dict(self.env, BEADS_DIR="/wrong", BD_BIN="/wrong/bd"))
        self.assertIn("stripping ambient routing override(s)", result.stdout)
        state = state_of(self.city)
        self.assertEqual(state["ambient_overrides_stripped"], ["BD_BIN", "BEADS_DIR"])
        self.assertEqual(state["replication"]["pushed_revision"], self.server.head("proof_test_repl"))


class BackupRotation(ServerCase):
    """(c) hourly copies under a fake clock, across day boundaries."""

    def restore_ok(self, run_dir):
        facts = json.loads((run_dir / "verified.json").read_text())
        with tempfile.TemporaryDirectory(prefix="pwr-restore-") as scratch:
            result = subprocess.run(["dolt", "backup", "restore", "file://" + str(run_dir / "dolt"), "r"], cwd=scratch,
                                    env=self.env, capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            rows = offline_rows(Path(scratch) / "r", self.env, "SELECT DOLT_HASHOF('main') AS h")
            self.assertEqual(rows[1][0], facts["head"])
            self.assertEqual(len(offline_rows(Path(scratch) / "r", self.env)) - 1, facts["issues"])

    def test_rotation_keeps_24_hourly_and_30_daily_restorable_copies(self):
        server, city = self.server, self.city
        ws = server.workspace("backup-project", "proof_test_backup")
        server.seed(ws, 3)
        root = server.base / "backups"
        # Things rotation must never touch: an unverified copy and a stray directory.
        (root / "hourly" / "20200101T000000Z.unverified").mkdir(parents=True)
        (root / "hourly" / "20200102T000000Z").mkdir(parents=True)
        (root / "daily" / "20200101T000000Z").mkdir(parents=True)
        clock = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
        stamps = []
        for hour in range(36):
            now = clock + timedelta(hours=hour)
            if hour % 7 == 0:
                server.create_issue(ws, "Hour %d" % hour)
            maintenance(server, ws, city, "backup", "--backup-root", str(root), "--now", now.isoformat(), env=self.env)
            stamps.append(now.strftime("%Y%m%dT%H%M%SZ"))
        hourly = sorted(p.name for p in (root / "hourly").iterdir() if (p / "verified.json").is_file())
        daily = sorted(p.name for p in (root / "daily").iterdir() if (p / "verified.json").is_file())
        self.assertEqual(hourly, stamps[-24:])
        self.assertEqual(daily, [stamps[0], stamps[24]], "the first copy of each UTC day is promoted")
        day_clock = datetime(2026, 1, 3, 0, 30, tzinfo=timezone.utc)
        for day in range(35):
            now = day_clock + timedelta(days=day)
            maintenance(server, ws, city, "backup", "--backup-root", str(root), "--now", now.isoformat(), env=self.env)
            stamps.append(now.strftime("%Y%m%dT%H%M%SZ"))
        hourly = sorted(p.name for p in (root / "hourly").iterdir() if (p / "verified.json").is_file())
        daily = sorted(p.name for p in (root / "daily").iterdir() if (p / "verified.json").is_file())
        self.assertEqual(len(hourly), 24)
        self.assertEqual(hourly, stamps[-24:])
        self.assertEqual(len(daily), 30)
        self.assertEqual(daily, (stamps[:1] + stamps[24:25] + stamps[36:])[-30:])
        self.assertTrue((root / "hourly" / "20200101T000000Z.unverified").is_dir())
        self.assertTrue((root / "hourly" / "20200102T000000Z").is_dir())
        self.assertTrue((root / "daily" / "20200101T000000Z").is_dir())
        for name in hourly:
            self.restore_ok(root / "hourly" / name)
        for name in daily:
            self.restore_ok(root / "daily" / name)
        state = state_of(city)["backup"]
        self.assertEqual((state["hourly"], state["daily"]), (24, 30))
        self.assertEqual(state["last_hourly_at"], "2026-02-06T00:30:00Z")
        self.assertEqual(state["last_daily_at"], "2026-02-06T00:30:00Z")
        # A second run in the same second is refused rather than overwriting a copy.
        result = maintenance(server, ws, city, "backup", "--backup-root", str(root), "--now", stamps[-1], env=self.env, expect=1)
        self.assertIn("backup_target_exists", result.stderr)


class Doctor(ServerCase):
    """(d) every failing condition is named."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.db = "proof_test_doctor"
        cls.workspace = cls.server.workspace("doctor-project", cls.db)
        cls.server.seed(cls.workspace, 2)
        cls.remote = cls.server.base / "doctor-remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(cls.remote)], check=True, env=cls.env)
        seed = cls.server.base / "doctor-seed"
        subprocess.run(["git", "init", "-q", str(seed)], check=True, env=cls.env)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "seed"], cwd=seed, check=True, env=cls.env)
        subprocess.run(["git", "push", "-q", str(cls.remote), "HEAD:refs/heads/main"], cwd=seed, check=True, env=cls.env)
        cls.url = "git+file://" + str(cls.remote)
        cls.now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.city = self.server.base / ("doctor-city-%s" % self._testMethodName)
        (self.city / "proof").mkdir(parents=True)
        self.root = self.city / "backups"
        write_city_toml(self.city)
        maintenance(self.server, self.workspace, self.city, "replicate", "--remote", self.url, "--ref", REF,
                    "--now", (self.now - timedelta(seconds=30)).isoformat(), env=self.env)
        maintenance(self.server, self.workspace, self.city, "backup", "--backup-root", str(self.root),
                    "--now", (self.now - timedelta(minutes=20)).isoformat(), env=self.env)

    def doctor(self, *extra, env=None, now=None):
        result = maintenance(self.server, self.workspace, self.city, "doctor", "--remote", self.url, "--ref", REF,
                             "--now", (now or self.now).isoformat(), *extra, env=env or self.env, expect=None)
        state = state_of(self.city)
        self.assertEqual(result.returncode, 0 if state["ok"] else 1, result.stdout + result.stderr)
        return state

    def assertProblem(self, state, prefix):
        self.assertFalse(state["ok"])
        self.assertTrue(any(p.startswith(prefix) for p in state["problems"]),
                        "expected a %s problem, got %s" % (prefix, state["problems"]))

    def test_healthy(self):
        state = self.doctor()
        self.assertTrue(state["ok"], state["problems"])
        self.assertEqual(state["problems"], [])
        self.assertEqual(state["replication"]["lag_seconds"], 30)
        self.assertEqual(state["checked_at"], "2026-03-01T12:00:00Z")
        self.assertEqual(state["schema"], "proof.work-retention.v1")
        self.assertEqual(set(state["replication"]) >= {"pushed_revision", "pushed_at", "lag_seconds"}, True)
        self.assertEqual(set(state["backup"]) >= {"last_hourly_at", "last_daily_at", "hourly", "daily"}, True)

    def test_compactor_allowlist_unset(self):
        write_city_toml(self.city, allowlist=None)
        self.assertProblem(self.doctor(), "compactor_allowlist_unset")
        write_city_toml(self.city, allowlist="")
        self.assertProblem(self.doctor(), "compactor_allowlist_unset")
        write_city_toml(self.city, allowlist="hq", compactor="some-other-order")
        self.assertProblem(self.doctor(), "compactor_allowlist_unset")

    def test_compactor_allowlist_includes_project_database(self):
        write_city_toml(self.city, allowlist="hq," + self.db)
        self.assertProblem(self.doctor(), "compactor_allowlist_includes_project_database:" + self.db)
        write_city_toml(self.city, allowlist="hq,proof_work")
        self.assertProblem(self.doctor(), "compactor_allowlist_includes_project_database:proof_work")

    def test_workspace_auto_prune(self):
        config = self.workspace / "config.yaml"
        original = config.read_text()
        try:
            config.write_text(original.replace("events-journal-auto-prune: false", "events-journal-auto-prune: true"))
            self.assertProblem(self.doctor(), "workspace_auto_prune_not_false")
            config.write_text(original.replace("events-journal-auto-prune: false", ""))
            self.assertProblem(self.doctor(), "workspace_auto_prune_not_false")
        finally:
            config.write_text(original)
        state = self.doctor(env=dict(self.env, BD_EVENTS_JOURNAL_AUTO_PRUNE="true"))
        self.assertTrue(state["ok"], state["problems"])
        self.assertEqual(state["ambient_overrides_stripped"], ["BD_EVENTS_JOURNAL_AUTO_PRUNE"])
        self.assertEqual(self.doctor()["ambient_overrides_stripped"], [])

    def test_registered_as_rig(self):
        write_city_toml(self.city, rigs=[(self.db, "pdx", "/nowhere")])
        self.assertProblem(self.doctor(), "project_database_registered_as_rig:" + self.db)
        write_city_toml(self.city, rigs=[("ledger", self.db, "/nowhere")])
        self.assertProblem(self.doctor(), "project_database_registered_as_rig:ledger")
        write_city_toml(self.city, rigs=[("ledger", "ldg", str(self.workspace.parent))])
        self.assertProblem(self.doctor(), "project_database_registered_as_rig:ledger")
        write_city_toml(self.city)
        rig = self.city / "rigs" / "sneaky" / ".beads"
        rig.mkdir(parents=True)
        (rig / "metadata.json").write_text(json.dumps({"dolt_database": self.db}))
        self.assertProblem(self.doctor(), "project_database_registered_as_rig:sneaky")
        shutil.rmtree(self.city / "rigs")
        (self.city / ".beads").mkdir()
        (self.city / ".beads" / "metadata.json").write_text(json.dumps({"dolt_database": self.db}))
        self.assertProblem(self.doctor(), "project_database_registered_as_rig:city")

    def test_record_retention_drift(self):
        ident = self.server.create_issue(self.workspace, "Drifting")
        server, db = self.server, self.db
        cases = [("ephemeral", "UPDATE issues SET ephemeral = 1 WHERE id = '%s'" % ident, "UPDATE issues SET ephemeral = 0 WHERE id = '%s'" % ident),
                 ("no_history", "UPDATE issues SET no_history = 1 WHERE id = '%s'" % ident, "UPDATE issues SET no_history = 0 WHERE id = '%s'" % ident),
                 ("expiring", "UPDATE issues SET metadata = JSON_SET(COALESCE(metadata, '{}'), '$.expires_at', '2030-01-01T00:00:00Z') WHERE id = '%s'" % ident,
                  "UPDATE issues SET metadata = JSON_REMOVE(metadata, '$.expires_at') WHERE id = '%s'" % ident),
                 ("compacted", "UPDATE issues SET compaction_level = 1 WHERE id = '%s'" % ident, "UPDATE issues SET compaction_level = 0 WHERE id = '%s'" % ident)]
        for name, drift, undo in cases:
            server.sql(db, drift)
            server.sql(db, "CALL DOLT_COMMIT('-am', 'drift %s')" % name)
            try:
                self.assertProblem(self.doctor(), "%s_project_records:1" % name)
            finally:
                server.sql(db, undo)
                server.sql(db, "CALL DOLT_COMMIT('-am', 'undo %s')" % name)
        self.assertTrue(self.doctor()["ok"])

    def test_replication_and_backup_freshness(self):
        state = self.doctor(now=self.now + timedelta(seconds=271))
        self.assertProblem(state, "replication_lag_exceeded:301s")
        self.assertEqual(state["replication"]["lag_seconds"], 301)
        state = self.doctor(now=self.now + timedelta(minutes=71))
        self.assertProblem(state, "backup_stale:91m")
        state = self.doctor(now=self.now + timedelta(seconds=269))
        self.assertNotIn("replication_lag_exceeded", " ".join(state["problems"]))
        self.assertProblem(self.doctor("--remote-name", "nothing"), "replication_remote_missing:nothing")
        self.assertProblem(self.doctor("--ref", "refs/dolt/other"), "replication_remote_mismatch")
        (self.city / "proof" / "work-retention.json").unlink()
        state = self.doctor()
        self.assertProblem(state, "replication_never_pushed")
        self.assertProblem(state, "backup_never_verified")

    def test_unreachable_ledger_and_wrong_workspace(self):
        state = self.doctor("--port", "1")
        self.assertProblem(state, "ledger_unreachable")
        metadata = self.workspace / "metadata.json"
        original = metadata.read_text()
        try:
            metadata.write_text(json.dumps(dict(json.loads(original), dolt_database="hq")))
            self.assertProblem(self.doctor(), "workspace_database_not_project:hq")
            metadata.write_text("{")
            self.assertProblem(self.doctor(), "workspace_unreadable")
        finally:
            metadata.write_text(original)
        state = self.doctor()
        self.assertTrue(state["ok"], state["problems"])
        result = maintenance(self.server, self.workspace, self.city, "status", env=self.env)
        doc = json.loads(result.stdout)
        self.assertTrue(doc["state"]["ok"])
        self.assertEqual(doc["ledger"]["database"], self.db)
        self.assertIn("liskov-work", doc["ledger"]["remotes"])


class GarbageCollection(ServerCase):
    """(e) DOLT_GC through the server, proven history-preserving."""

    def snapshot(self, db, commits):
        """Rows of issues and dependencies AS OF every commit, independently of the script."""
        return {commit: (self.server.rows_as_of(db, "issues", commit), self.server.rows_as_of(db, "dependencies", commit))
                for commit in commits}

    def test_gc_preserves_history_and_shrinks_the_journal(self):
        server, city, db = self.server, self.city, "proof_test_gc"
        ws = server.workspace("gc-project", db)
        ids = server.seed(ws, 12)
        for i in range(1, 4):
            for ident in ids:
                server.bd(ws, "update", ident, "--set-metadata", "bloat%d=%s" % (i, "x" * 2000))
        for left, right in zip(ids, ids[1:]):
            server.bd(ws, "dep", "add", left, right)
        for name in ("pw-event-a1b2-0", "pw-event-a1b2-1", "pw-op-a1b2"):
            server.create_issue(ws, name, "--id", name, "--force")
        noms = server.data_dir / db / ".dolt" / "noms"
        journal_before = (noms / ("v" * 32)).stat().st_size
        self.assertGreater(journal_before, 1_000_000, "the seed must bloat the journal")
        commits = server.commits(db)
        self.assertGreater(len(commits), 60)
        before = self.snapshot(db, commits)
        self.assertEqual(len(before[commits[0]][1]) - 1, len(ids) - 1, "dependency rows are present at head")
        # A journal over the threshold refuses backups and is reported by the doctor.
        root = server.base / "gc-backups"
        result = maintenance(server, ws, city, "backup", "--backup-root", str(root), "--max-journal-bytes", "1", env=self.env, expect=1)
        self.assertIn("ledger_not_collected:%d" % journal_before, result.stderr)
        self.assertFalse((root / "hourly").exists())
        self.assertIn("ledger_not_collected", state_of(city)["backup"]["last_error"])
        write_city_toml(city)
        maintenance(server, ws, city, "doctor", "--max-journal-bytes", "1", env=self.env, expect=1)
        state = state_of(city)
        self.assertIn("ledger_not_collected:%d" % journal_before, state["problems"])
        self.assertEqual(state["gc"]["journal_bytes"], journal_before)
        # GC itself.
        maintenance(server, ws, city, "gc", env=self.env)
        gc = state_of(city)["gc"]
        print("gc sizes:", json.dumps({k: gc.get(k) for k in ("bytes_before", "bytes_after", "journal_bytes_before",
                                                             "journal_bytes_after", "commits", "verified")}), flush=True)
        self.assertTrue(gc["verified"], gc)
        self.assertEqual(gc["commits"], len(commits))
        self.assertEqual(gc["journal_bytes_before"], journal_before)
        self.assertLess(gc["bytes_after"], gc["bytes_before"], "noms did not shrink")
        self.assertLess(gc["journal_bytes_after"], journal_before, "the journal did not shrink")
        self.assertFalse((noms / ("v" * 32)).exists() and (noms / ("v" * 32)).stat().st_size >= journal_before)
        self.assertEqual(server.commits(db), commits)
        self.assertEqual(self.snapshot(db, commits), before, "rows at some commit changed")
        head_rows = server.rows(db)
        self.assertEqual(sum(1 for r in head_rows[1:] if r[0].startswith("pw-event-")), 2)
        self.assertEqual(sum(1 for r in head_rows[1:] if r[0].startswith("pw-op-")), 1)
        # Backups are accepted again, and a fresh clone of the pushed ref is complete.
        maintenance(server, ws, city, "backup", "--backup-root", str(root), env=self.env)
        self.assertEqual(state_of(city)["backup"]["hourly"], 1)
        remote = server.base / "gc-remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, env=self.env)
        seed = server.base / "gc-seed"
        subprocess.run(["git", "init", "-q", str(seed)], check=True, env=self.env)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "seed"], cwd=seed, check=True, env=self.env)
        subprocess.run(["git", "push", "-q", str(remote), "HEAD:refs/heads/main"], cwd=seed, check=True, env=self.env)
        url = "git+file://" + str(remote)
        maintenance(server, ws, city, "replicate", "--remote", url, "--ref", REF, env=self.env)
        clone_base = server.base / "gc-clone"
        clone_base.mkdir()
        result = subprocess.run(["dolt", "clone", "--ref", REF, url, "restored"], cwd=clone_base, env=self.env,
                                capture_output=True, text=True, timeout=600)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        restored = clone_base / "restored"
        self.assertEqual(offline_commits(restored, self.env), commits)
        self.assertEqual(offline_rows(restored, self.env), head_rows)
        self.assertEqual(offline_rows(restored, self.env, "SELECT * FROM dependencies ORDER BY 1, 2, 3"),
                         server.rows_as_of(db, "dependencies", commits[0]))
        # A GC whose verification cannot run is not a success.
        result = maintenance(server, ws, city, "gc", "--data-dir", str(server.base / "nowhere"), env=self.env, expect=1)
        self.assertIn("data_dir_unreadable", result.stderr)
        self.assertFalse(state_of(city)["gc"]["verified"])


if __name__ == "__main__":
    unittest.main()
