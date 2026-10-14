"""Shared by the dispatcher, the completer and the daily report: the policy
file, packet state read from beads, and the one routine that lands a backlog
change on main from the Gas City-owned orchestrator clone.

All of it is script: no agent, no model, no tokens.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import admit  # noqa: E402  the owned-clone and rig helpers live with admission

Refused = admit.Refused


class BacklogBusy(Refused):
    """Only this backlog write must wait for the next pass."""


_backlog_writes = {}


@contextlib.contextmanager
def backlog_write(city=None):
    """Exclude other writers from the owned backlog tree through push or undo.

    Re-entering in one process shares the descriptor: flocking a second open
    descriptor would deadlock against our own outer section. Republish callbacks
    run after the outermost exit, because the shared checkout is another tree.
    """
    city = os.path.realpath(city if city is not None else city_dir())
    key = (os.getpid(), city)
    if key in _backlog_writes:
        yield
        return
    proof = os.path.join(city, "proof")
    os.makedirs(proof, exist_ok=True)
    wait = float(os.environ.get("GC_PROOF_FACTORY_LOCK_WAIT_SECONDS") or "120")
    deadline = time.monotonic() + wait
    fd = os.open(os.path.join(proof, "factory.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    callbacks = []
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    why = "backlog write lock held for %s s; this landing skipped" % wait
                    log("factory", why)
                    raise BacklogBusy(why)
                time.sleep(min(0.05, remaining))
        _backlog_writes[key] = callbacks
        try:
            yield
        finally:
            del _backlog_writes[key]
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    for callback in callbacks:
        callback()


POLICY_DEFAULTS = {"enabled": False, "claim_hours": 12, "max_failed": 3, "max_declined": 8, "max_unmerged": 8,
                   "max_running": 0,
                   "daily_output_tokens": 0, "shared_checkout": "", "notify_command": [], "notify_batch": False, "rigs": {}}
RIG_DEFAULTS = {"execution_contracts": False, "wip": 1, "landing": "", "test_companions": False, "items": [], "epics": [], "milestones": [], "lanes": [],
                "test_companion_import_depth": 1,
                "priorities": [], "planner_retry_token": "",
                "planner_items": [], "planner_request_token": "",
                "exclude": [], "review_if_scope_contains": [], "merge": "", "merge_requires": [],
                "main_verification": "push-workflows", "livetest": {}}
# `[rigs.<rig>.livetest]` (ADR-0151). Absent is exactly `enabled = false`, and
# every list is an allowlist: EMPTY MEANS NONE, never "no restriction" — the
# opposite of the rig lists above, because what these admit touches production.
LIVETEST_DEFAULTS = {"enabled": False, "targets": [], "recipes": [], "max_exercise": "observe",
                     "daily_spend_cap": ""}


def log(who, message):
    print("%s %s: %s" % (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), who, message),
          flush=True)


def _read_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_state(path, state):
    """Replace the file by rename, so a reader never sees a torn document."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


@contextlib.contextmanager
def state_lock(city):
    """One short exclusive lock for a `factory-state.json` merge.

    Not `proof/factory.lock`, which protects the owned backlog tree's edits,
    commit and push. This lock covers a single read-merge-rename."""
    proof = os.path.join(city, "proof")
    os.makedirs(proof, exist_ok=True)
    fd = os.open(os.path.join(proof, "factory-state.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _merge_dict(disk, mine, snapshot):
    """Apply this instance's edits onto `disk` without reverting keys it did not change.

    `snapshot` is the section as this instance first saw it. A key it changed or
    added comes from `mine`. A key it deleted is removed. Every other key keeps
    the value now on disk, which may be a newer writer's."""
    out = dict(disk or {})
    snap = snapshot if isinstance(snapshot, dict) else {}
    mine = mine if isinstance(mine, dict) else {}
    for key in snap:
        if key not in mine:
            out.pop(key, None)
        elif mine[key] != snap[key]:
            out[key] = mine[key]
    for key, value in mine.items():
        if key not in snap:
            out[key] = value
    return out


def claim_stale(since, ttl_s, now=None):
    """True when a claim stamped at `since` is at least `ttl_s` seconds old.

    `since` is UTC `%Y-%m-%dT%H:%M:%SZ`, the moment the claim was written. A
    missing or unreadable stamp is stale: a claim that cannot be aged would
    otherwise wait for ever. `now` is there so a test can pin the clock."""
    now = now or datetime.now(timezone.utc)
    try:
        started = datetime.strptime(since or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    return (now - started).total_seconds() >= ttl_s


def rerun_already_going(detail):
    """True when `gh run rerun` refused because that run is already in progress.

    That refusal is the answer: the failed jobs are already running. Any other
    refusal is a real failure a person has to see."""
    text = (detail or "").lower()
    return ("already running" in text or "already in progress" in text
            or "currently running" in text or "currently in progress" in text)


class Standing:
    """What a script said on its last pass, so a condition that has not changed
    is not said again.

    A pass runs every five minutes and most of what it has to say is the same as
    five minutes ago: an item whose scope overlaps a live claim, an item skipped
    for a remote branch, twenty policy tables for rigs this city does not have.
    Two days of that was 604 identical refusals and 195 identical skips in a
    1,965-line log, and the one line that mattered — an item skipped on EVERY
    pass, for ever, that nobody was told about — read exactly like the noise.

    `say(key, message)` logs when the message for that key is new or different.
    A key not said during a pass is forgotten when the pass `close()`s, so a
    condition that clears and comes back is reported again. `since(key)` is when
    it was first said: `gc proof status` prints the standing list with its age."""

    def __init__(self, city, who, keep=True):
        self.city = city
        self.path = os.path.join(city, "proof", "factory-state.json")
        self.who, self.keep, self.touched = who, keep, set()
        self._sections, self._section_orig = {}, {}
        self.state = _read_state(self.path)
        standing = self.state.get("standing")
        if not isinstance(standing, dict):
            standing = {}
            self.state["standing"] = standing
        mine = standing.get(who)
        if not isinstance(mine, dict):
            mine = {}
            standing[who] = mine
        self.mine = mine

    def say(self, key, message):
        self.touched.add(key)
        known = self.mine.get(key)
        if known and known.get("message") == message:
            return False
        self.mine[key] = {"message": message, "since": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        log(self.who, message)
        return True

    def since(self, key):
        return (self.mine.get(key) or {}).get("since", "")

    def section(self, name):
        """A script's own memory in the same file, written by `close()` or
        `transact_section`.

        The file is replaced by rename, so a reader never sees a tear. `close`
        merges under `proof/factory-state.lock`: only this script's standing
        entry and the section keys this instance changed are written back.
        Another writer's section is left as they wrote it. Two writers of the
        same key: the later merge wins, and the next pass reads what landed."""
        body = self.state.get(name)
        if not isinstance(body, dict):
            body = {}
            self.state[name] = body
        if name not in self._sections:
            self._sections[name] = body
            self._section_orig[name] = copy.deepcopy(body)
        return self._sections[name]

    def transact_section(self, name, fn):
        """Re-read `name` under the short state lock, run `fn(body)`, write if it changed.

        `body` is this instance's section dict, the same object the caller is
        already editing. Those edits are merged onto the disk copy before `fn`
        runs, and the write includes them. Replacing `body` from disk alone
        would drop them and reset the snapshot, so `close` would never write
        them back: one rig's `acted` stamp, lost because a later rig claimed a
        judge in the same pass.

        The lock is held only for the read, the mutation and the rename — not
        for whatever the caller does with the result. After it returns, this
        instance's snapshot matches `body`, so a later `close` does not write
        a stale section back over the winner of the race."""
        body = self.section(name)
        with state_lock(self.city):
            fresh = _read_state(self.path)
            disk = fresh.get(name) if isinstance(fresh.get(name), dict) else {}
            snap = self._section_orig.get(name)
            snap = snap if isinstance(snap, dict) else {}
            merged = _merge_dict(disk, body, snap)
            # `setdefault(rig, {})` puts a key on `body` that the snapshot never
            # had. That placeholder is not an edit. Letting it win would hide a
            # claim another pass wrote since this one read the file, and both
            # passes would start a session.
            for key, value in list(merged.items()):
                if key not in snap and value == {} and key in disk:
                    merged[key] = disk[key]
            merged = copy.deepcopy(merged)
            body.clear()
            body.update(merged)
            result = fn(body)
            if body != disk and self.keep:
                fresh[name] = body
                _write_state(self.path, fresh)
            self._section_orig[name] = copy.deepcopy(body)
            return result

    def close(self):
        for key in [k for k in self.mine if k not in self.touched]:
            del self.mine[key]
        if not self.keep:
            return
        with state_lock(self.city):
            fresh = _read_state(self.path)
            standing = fresh.get("standing")
            if not isinstance(standing, dict):
                standing = {}
                fresh["standing"] = standing
            standing[self.who] = self.mine
            for name, body in self._sections.items():
                snap = self._section_orig.get(name)
                if not isinstance(snap, dict) or body == snap:
                    continue
                disk = fresh.get(name) if isinstance(fresh.get(name), dict) else {}
                fresh[name] = _merge_dict(disk, body, snap)
            _write_state(self.path, fresh)


def limit_hold(state):
    """Why nothing new should start, or '' — the watchdog found sessions stopped at
    the provider's usage limit. `state` is a Standing's whole state."""
    held = (state or {}).get("limit") or {}
    if not held.get("sessions"):
        return ""
    return ("%d session%s stopped at the provider's usage limit since %s (%s); a new packet or planning run "
            "would only start another" % (len(held["sessions"]), "" if len(held["sessions"]) == 1 else "s",
                                          held.get("since", "?"), ", ".join(held["sessions"])))


def active_rigs(rigs, states, policy):
    """Automatic responders must respect the operator's rig suspension."""
    return {name: rig for name, rig in rigs.items() if name in policy.get("rigs", {})
            and name in states and not states[name].get("suspended", True)}


def budget_hold(policy, rigs):
    """One budget rule for every model launcher; completion still proceeds."""
    limit = policy.get("daily_output_tokens", 0)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise Refused("daily_output_tokens must be a non-negative integer")
    if limit == 0:
        return ""
    import tokens
    spent = tokens.output_since_midnight([r.get("Path") or "" for r in rigs.values()])
    return ("daily output-token limit reached (%d of %d); no new model work" % (spent, limit)
            if spent >= limit else "")


def repo_slug(clone):
    """`owner/name` of a clone's GitHub origin."""
    url = sh(["git", "-C", clone, "remote", "get-url", "origin"]).stdout.strip()
    return url.split("github.com", 1)[-1].lstrip(":/").removesuffix(".git")


def standing(city):
    """Every standing condition, for `gc proof status`: [(who, key, message, since)]."""
    try:
        with open(os.path.join(city, "proof", "factory-state.json"), encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return []
    return sorted((who, key, entry.get("message", ""), entry.get("since", ""))
                  for who, entries in (state.get("standing") or {}).items() for key, entry in entries.items())


def sh(argv, cwd=None):
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=admit.RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", "`%s` did not finish in %d s and was killed"
                                           % (" ".join(argv[:4]), admit.RUN_TIMEOUT))


def city_dir():
    return admit.city_root()


def load_policy(city):
    """`<city>/proof/policy.toml` is the operator's. Missing or disabled means the
    factory does nothing: there is no default that dispatches."""
    path = os.path.join(city, "proof", "policy.toml")
    policy = dict(POLICY_DEFAULTS)
    if os.path.isfile(path):
        with open(path, "rb") as fh:
            policy.update(tomllib.load(fh))
    policy["rigs"] = {name: {**RIG_DEFAULTS, **cfg} for name, cfg in (policy.get("rigs") or {}).items()}
    for name, cfg in policy['rigs'].items():
        if type(cfg['execution_contracts']) is not bool:
            raise Refused('%s: execution_contracts must be true or false' % name)
        if not isinstance(cfg['test_companions'], bool):
            raise Refused('%s: test_companions must be true or false' % name)
        if type(cfg['test_companion_import_depth']) is not int or cfg['test_companion_import_depth'] not in (1, 2):
            raise Refused('%s: test_companion_import_depth must be 1 or 2' % name)
        cfg["livetest"] = livetest_policy(cfg, name)
    return policy


def livetest_policy(cfg, name="?"):
    """A rig's `[rigs.<rig>.livetest]` table, completed with its defaults and
    checked. A malformed table refuses the pass rather than reading as enabled:
    a typo here must never widen what reaches production."""
    import livetest_contract as lt
    given = (cfg or {}).get("livetest") or {}
    if not isinstance(given, dict):
        raise Refused("%s: livetest must be a table" % name)
    table = {**LIVETEST_DEFAULTS, **given}
    unknown = sorted(set(table) - set(LIVETEST_DEFAULTS))
    if unknown:
        raise Refused("%s: livetest has unknown key(s) %s; it takes %s"
                      % (name, ", ".join(unknown), ", ".join(sorted(LIVETEST_DEFAULTS))))
    if not isinstance(table["enabled"], bool):
        raise Refused("%s: livetest.enabled must be true or false" % name)
    for key in ("targets", "recipes"):
        if not isinstance(table[key], list) or not all(isinstance(v, str) and v for v in table[key]):
            raise Refused("%s: livetest.%s must be a list of names; an empty list admits none" % (name, key))
    if table["max_exercise"] not in lt.EXERCISE_LEVELS:
        raise Refused("%s: livetest.max_exercise must be one of %s" % (name, " | ".join(lt.EXERCISE_LEVELS)))
    if not isinstance(table["daily_spend_cap"], str):
        raise Refused("%s: livetest.daily_spend_cap must be a string" % name)
    return table


def rig_states(city):
    doc = json.loads(admit.gc(city, "rig", "list", "--json").stdout)
    return {r["name"]: r for r in doc.get("rigs", []) if not r.get("hq")}


def packets(city, rig, superseded=False):
    """Every admitted packet in a rig: its work bead and its NEWEST workflow root
    (a re-admitted packet has one root per attempt).

    state: running | passed | failed | declined. `declined` is a failure at
    `load-context` — the worker read the packet and refused it before touching
    anything (already fixed on main, unclear, needs a decision). That is cheap and
    says something about the ITEM; a failure later says something about the
    factory. They are counted apart so stale items cannot trip the fault breaker."""
    out = admit.gc(city, "bd", "--rig", rig, "list", "--all", "--json", "--limit=0", check=False)
    try:
        beads = json.loads(out.stdout or "[]")
    except ValueError:
        beads = []
    return packets_of(beads, superseded)


def packets_of(beads, superseded=False):
    """`packets()` without the store read. With `superseded`, the beads a triage
    retired are returned as well, each with the workflow that ran FOR IT — the
    newest root created before the next bead of the same packet existed. Without
    that pairing a superseded bead would borrow its successor's outcome, which is
    why the live view leaves them out; the report needs them, because a packet
    that was declined and then re-shaped still cost what it cost."""
    roots = {}
    for b in beads:
        md = b.get("metadata") or {}
        pid = md.get("gc.var.packet_id") or ""
        if (md.get("gc.kind") == "workflow" and pid and not flag(md.get("proof.superseded"))
                and md.get("gc.formula_name") != "mol-proof-pr-recovery"):
            # PR recovery owns a separate input, never the original packet outcome.
            roots.setdefault(pid, []).append(b)
    work = {}
    for b in beads:
        pid = (b.get("metadata") or {}).get("proof.packet_id")
        if pid:
            work.setdefault(pid, []).append(b)
    found = []
    for pid, mine in work.items():
        mine.sort(key=lambda b: b.get("created_at") or "")
        theirs = sorted(roots.get(pid, []), key=lambda b: b.get("created_at") or "")
        for n, b in enumerate(mine):
            md = b.get("metadata") or {}
            retired = flag(md.get("proof.superseded"))
            if retired and not superseded:
                continue                  # a superseded bead must not borrow its successor's workflow
            until = (mine[n + 1].get("created_at") or "") if n + 1 < len(mine) else "~"
            # …and never its predecessor's either. Admission creates the work bead
            # first and slings its workflow seconds later; in that window the newest
            # root is the LAST attempt's, and a failed one would stop a live packet.
            since = b.get("created_at") or ""
            ran = [r for r in theirs if since <= (r.get("created_at") or "") < until]
            root = ran[-1] if ran else {}
            outcome = (root.get("metadata") or {}).get("gc.outcome") if root.get("status") == "closed" else None
            state = "running" if outcome is None else ("passed" if outcome == "pass" else "failed")
            if retired and outcome is None:
                state = "failed"          # retired without a finished workflow: it never ran to an outcome
            if state == "failed" and any(
                    (k.get("metadata") or {}).get("gc.root_bead_id") == root.get("id")
                    and "load-context.attempt" in ((k.get("metadata") or {}).get("gc.step_ref") or "")
                    and (k.get("metadata") or {}).get("gc.outcome") == "fail" for k in beads):
                state = "declined"
            # Keep the state vocabulary: a rootless admission already holds a
            # worker slot. Recovery and status need the more precise fact too.
            pending = not root and b.get("status") != "closed" and not retired
            root_md = root.get("metadata") or {}
            record_pending = bool(root and root.get("status") != "closed"
                                  and b.get("status") != "closed" and not retired
                                  and "proof.review" not in root_md and "proof.live_run_id" not in root_md)
            found.append({"id": pid, "bead": b, "md": md, "root": root, "state": state,
                          "superseded": retired, "admission_pending": pending,
                          "record_pending": record_pending})
    found.sort(key=lambda p: p["bead"].get("created_at") or "")
    return found


def admission_retryable(packet):
    """An interrupted implementation admission can reuse its work bead.

    Live-test admission does not reuse a bead and its run already holds the
    city's live slot; a person must inspect that run instead of retrying it.
    """
    return bool((packet.get("admission_pending") or packet.get("record_pending"))
                and not packet["md"].get("proof.live_run_id"))


_BEADS = {}

# The lines a claim, a release or a completion rewrite. They change the FILE
# without changing the WORK: an item is "re-shaped" only when something else did.
_VOLATILE = re.compile(r"(?m)^(status|claimed_by|claimed_at|claim_expires|last_reviewed):.*\n")


def shaped(text):
    """An item's text with the claim-and-status lines removed — what a worker was
    actually asked to do. The first version of this test compared git blobs, and
    the release commit itself changed the blob: every released item read as
    re-shaped and was claimed again within a minute."""
    return _VOLATILE.sub("", text)


# The `gc.failure_reason` a worker records when a packet needs an independent
# reviewer it was not admitted with. An item whose text names it is retaken only
# once something draws that reviewer (`review: required` in its frontmatter, or
# a write-scope path the rig's policy reviews) — never by the reviewer-less path
# that declined it. BKLG-20261008-8to9 was admitted that way twice in one evening.
REVIEW_DECLINE = "required_independent_review_not_admitted"


# The tail of a `stop_reason()` that carries no WORKER's code: the controller
# recorded the failure, or only a check's own words say why. A triage quotes a
# worker's code (`triaged()`); for these, as for no reason at all, any re-shape
# counts — so reporting them changes what status says, never what is retaken.
UNCODED = " [no worker code]"


def stop_code(reason):
    """The bare failure code of a `stop_reason()` string ("<code> at <step>"),
    or "" when it carries no worker's code (it ends with UNCODED)."""
    reason = reason or ""
    if reason.endswith(UNCODED):
        return ""
    return reason.split(" at ", 1)[0].strip()


def triaged(given, today, reason=""):
    """Why a stopped packet's item does NOT yet count as triaged, or "" when it does.

    A re-shape is a change to what the item asks (`shaped()`), and a triage is a
    re-shape that NAMES the code the packet stopped with: the worker's
    `gc.failure_reason` quoted in the item, in a `## Triage` section that says
    what changed for it. An edit that happens to touch the file — a backfill
    moving out, a date, a link — re-armed BKLG-20261008-8to9 on 2026-10-08 and
    it was declined again for the same reason within twenty minutes. The
    dispatcher's re-take and the completer's bead-closing both ask this."""
    if shaped(given) == shaped(today):
        return "the item has not changed since it was declined"
    code = stop_code(reason)
    if code and code not in today:
        return "the item was re-shaped but does not name the stop code %s; a triage quotes every stop code it answers" % code
    return ""


def flag(value):
    """bd stores `--set-metadata k=true` as a JSON boolean, not the string."""
    return str(value).strip().lower() == "true"


def rig_beads(city, rig, fresh=False):
    """Every bead of a rig, read once per process — or again with `fresh`, which
    a responder needs on the publish half: what it read while observing may
    already have been answered by another pass."""
    if fresh:
        _BEADS.pop(rig, None)
    if rig not in _BEADS:
        out = admit.gc(city, "bd", "--rig", rig, "list", "--all", "--json", "--limit=0", check=False)
        try:
            _BEADS[rig] = json.loads(out.stdout or "[]")
        except ValueError:
            _BEADS[rig] = []
    return _BEADS[rig]


PLAN_STEP_ORDER = ("plan", "land", "report")


def plan_state(bead, root):
    """running | landed | no-op | stopped | cleared, for one planning run.

    A planning run is a work bead carrying `proof.plan_for` in the rig that owns
    the backlog, and the workflow it was slung on. It is NOT a packet — it must
    never count against a rig's wip or its failure breaker — so nothing that keys
    on `proof.packet_id` sees it. Until this existed a run that stopped left its
    bead open, which by design blocks the next run for that rig, and told nobody:
    the planner could be off for ever without a line on any screen."""
    if bead.get("status") == "closed":
        # Closed by the run itself only when its workflow passed. Closed over a
        # workflow that failed or never started is a PERSON clearing a stopped run
        # so the planner may try again — which must never read as "landed".
        if (root.get("metadata") or {}).get("gc.outcome") != "pass":
            return "cleared"
        outcome = (root.get("metadata") or {}).get("proof.plan_outcome") or (bead.get("metadata") or {}).get("gc.work_outcome")
        return "no-op" if outcome == "no-op" else "landed"
    if not root:
        return "stopped"                                     # a bead whose workflow never started
    if root.get("status") != "closed":
        return "running"
    # The workflow ended and the run never closed its work bead: it did not finish.
    return "stopped"


def planning_runs(city, home_rig):
    """Every planning run recorded in the rig that owns the backlog, oldest first."""
    beads = rig_beads(city, home_rig)
    by_id = {b["id"]: b for b in beads}
    roots = sorted((b for b in beads if (b.get("metadata") or {}).get("gc.kind") == "workflow"
                    and (b.get("metadata") or {}).get("gc.var.target_rig")), key=lambda b: b.get("created_at") or "")
    runs = []
    for bead in sorted((b for b in beads if (b.get("metadata") or {}).get("proof.plan_for")
                        and not flag((b.get("metadata") or {}).get("proof.superseded"))),
                       key=lambda b: b.get("created_at") or ""):
        md = bead.get("metadata") or {}
        # The trigger records the root it slung; for a run from before it did, the
        # root is the first one for that rig created after the bead.
        root = by_id.get(md.get("proof.plan_root") or "") or next(
            (r for r in roots if (r.get("metadata") or {}).get("gc.var.target_rig") == md.get("proof.plan_for")
             and (r.get("created_at") or "") >= (bead.get("created_at") or "")), {})
        runs.append({"bead": bead, "md": md, "root": root, "target": md.get("proof.plan_for"),
                     "state": plan_state(bead, root)})
    return runs


STEP_ORDER = ("load-context", "workspace", "implement", "review-cycle", "push", "open-pr", "arm-automerge", "land", "report")
LIVE_STEP_ORDER = ("load-context", "preflight", "observe", "exercise", "evidence", "settle", "report")


def current_step(city, rig, packet, order=None, formula="mol-proof-packet"):
    """The runnable agent task, excluding control beads and blocked successors.

    A review scope-check is created with the whole workflow. Its old creation
    time and lack of an agent assignee do not mean that the reviewer is stalled.
    Assigned tasks can remain open, so readiness follows dependency edges too.
    """
    root = (packet.get("root") or {}).get("id")
    if root is None:
        return "admission"
    beads = rig_beads(city, rig)
    states = {b["id"]: b.get("status") for b in beads}
    tasks = [b for b in beads if (b.get("metadata") or {}).get("gc.root_bead_id") == root
             and b.get("status") != "closed"
             and (b.get("metadata") or {}).get("gc.kind") in (None, "", "task")]
    live = [(b.get("metadata") or {}).get("gc.step_ref") or "" for b in tasks
            if all(states.get(d.get("depends_on_id")) == "closed"
                   for d in b.get("dependencies") or []
                   if (d.get("type") or d.get("dep_type")) in ("blocks", "waits-for", "conditional-blocks"))]
    for step in order or STEP_ORDER:
        hits = sorted(ref for ref in live if ("." + step + ".") in ("." + ref + ".")
                      and ("attempt" in ref or "iteration" in ref))
        if hits:
            return re.sub("^" + re.escape(formula) + r"\.", "", hits[0])
    return "controller" if tasks else "finishing"


def last_attempt(md):
    """The last line a control bead's check printed on its final failed attempt
    (`gc.attempt_log`, a JSON list bd may hand back as a string), or ""."""
    log = md.get("gc.attempt_log")
    if isinstance(log, str):
        try:
            log = json.loads(log)
        except ValueError:
            return ""
    for entry in reversed(log if isinstance(log, list) else []):
        lines = [line.strip() for line in str((entry or {}).get("reason") or "").splitlines() if line.strip()]
        if lines:
            return lines[-1][:240]
    return ""


def stop_reason(city, rig, packet):
    """Why a packet's workflow stopped: "<gc.failure_reason> at <step>", e.g.
    `fix_already_on_main_no_authorized_work_remains at load-context.attempt.1`.
    Its full reasoning is a mail to `human` whose subject starts BLOCKED: and
    names the packet.

    A worker records the code on the attempt or iteration it failed; that wins.
    A failure the CONTROLLER records lands on the ralph/retry bead itself, or on
    the root (BKLG-20261004-axhn: `control_dispatch_error` on the review-cycle
    ralph), and the cause a check printed lives only in that bead's
    `gc.attempt_log` — so its last line is added. A check that failed with no
    code at all (BKLG-20261009-vq87's land) is reported by that line alone. Both
    read "no reason recorded" before, and both end UNCODED. A first iteration's
    step_ref has no formula prefix, so the step label is cut at "packet." either way."""
    root = (packet.get("root") or {}).get("id")
    if not root:
        return ""
    mine = [b for b in rig_beads(city, rig)
            if b.get("id") == root or (b.get("metadata") or {}).get("gc.root_bead_id") == root]

    def step_of(bead):
        ref = (bead.get("metadata") or {}).get("gc.step_ref") or ("workflow" if bead.get("id") == root else bead.get("id") or "?")
        return ref.split("packet.")[-1]

    def worker(bead):
        ref = (bead.get("metadata") or {}).get("gc.step_ref") or ""
        return "attempt" in ref or "iteration" in ref

    coded = [b for b in mine if (b.get("metadata") or {}).get("gc.failure_reason")]
    if coded:                                   # a worker's attempt first, then a control bead, the root last
        bead = min(coded, key=lambda b: (not worker(b), b.get("id") == root))
        md = bead["metadata"]
        if worker(bead):
            return "%s at %s" % (md["gc.failure_reason"], step_of(bead))
        said = last_attempt(md)
        return "%s at %s%s%s" % (md["gc.failure_reason"], step_of(bead), "; last attempt: " + said if said else "", UNCODED)
    for bead in mine:
        md = bead.get("metadata") or {}
        said = last_attempt(md) if md.get("gc.outcome") == "fail" else ""
        if said:
            return "%s: %s%s" % (step_of(bead), said, UNCODED)
    return ""


def recorded_pr_url(city, rig, packet):
    """The packet's pull request as recorded: on its work bead (`proof.pr_url`,
    which the completer backfills), else on any bead of its workflow — the
    open-pr worker usually records it on its own step bead, not the work bead
    (BKLG-20261009-gpuk: liskov-rs#1558 on its step beads only)."""
    url = (packet.get("md") or {}).get("proof.pr_url")
    root = (packet.get("root") or {}).get("id")
    if url or not root:
        return url or ""
    for bead in rig_beads(city, rig):
        md = bead.get("metadata") or {}
        if (bead.get("id") == root or md.get("gc.root_bead_id") == root) and md.get("proof.pr_url"):
            return md["proof.pr_url"]
    return ""


def set_bead(city, rig, bead_id, **metadata):
    args = ["bd", "--rig", rig, "update", bead_id]
    for key, value in metadata.items():
        args += ["--set-metadata", "%s=%s" % (key.replace("__", "."), value)]
    return admit.gc(city, *args, check=False)


def unset_bead(city, rig, bead_id, *keys):
    """Remove metadata keys. Writing "" does not clear one, so a claim is dropped this way."""
    args = ["bd", "--rig", rig, "update", bead_id]
    for key in keys:
        args += ["--unset-metadata", key]
    return admit.gc(city, *args, check=False)


def mail(subject, body):
    sent = sh(["gc", "mail", "send", os.environ.get("PROOF_KEEPER_RECIPIENT", "human"), "-s", subject, "-m", body])
    return sent.returncode == 0


def _land_backlog_change(clone, message, add=()):
    """Commit whatever the caller changed under docs/ in the owned orchestrator
    clone, with views regenerated and validation green, and push it to main.

    `add` names NEW files to include, exactly, as repository-relative paths
    under docs/ — the live-result publisher's evidence records. They are staged
    before the view refresh because the generated indexes are rendered from the
    index. Tracked files are staged as before; nothing else untracked is swept in.

    The clone is nobody's working copy, so a failure is undone with a hard reset
    to origin/main — never possible in the shared checkout, always safe here.
    Returns the landed commit, or raises Refused having left the clone clean."""
    def undo(why):
        sh(["git", "-C", clone, "reset", "--quiet", "--hard", "origin/main"])
        raise Refused(why)

    for path in add:
        parts = str(path).split("/")
        if not isinstance(path, str) or parts[0] != "docs" or len(parts) < 2 or "" in parts \
                or ".." in parts or "\\" in path or os.path.islink(os.path.join(clone, path)) \
                or not os.path.isfile(os.path.join(clone, path)):
            undo("refusing to stage %r: new files land only as regular files under docs/" % (path,))
    if add:
        staged = sh(["git", "-C", clone, "add", "--", *add])
        if staged.returncode != 0:
            undo("staging %s failed: %s" % (", ".join(add), (staged.stdout + staged.stderr).strip()[-300:]))
    sh(["git", "-C", clone, "add", "-u", "docs"])
    fresh = sh([sys.executable, "scripts/validate_docs.py", "--refresh-epic-views"], cwd=clone)
    if fresh.returncode != 0:
        undo("view refresh failed: %s" % (fresh.stdout + fresh.stderr).strip()[-600:])
    sh(["git", "-C", clone, "add", "-u", "docs"])
    valid = sh([sys.executable, "scripts/validate_docs.py"], cwd=clone)
    if valid.returncode != 0:
        undo("validation failed: %s" % (valid.stdout + valid.stderr).strip()[-600:])
    commit = sh(["git", "-C", clone, "commit", "--quiet", "-m", message])
    if commit.returncode != 0:
        undo("commit failed: %s" % (commit.stdout + commit.stderr).strip()[-600:])
    push = sh(["git", "-C", clone, "push", "--quiet", "origin", "HEAD:main"])
    if push.returncode != 0:
        sh(["git", "-C", clone, "fetch", "--quiet", "origin"])
        undo("main moved before the push; nothing landed, the next run retries")
    sh(["git", "-C", clone, "fetch", "--quiet", "origin"])
    return sh(["git", "-C", clone, "rev-parse", "HEAD"]).stdout.strip()


def land_backlog_change(clone, message, shared_checkout="", add=()):
    """Land while excluding owned-clone writers; linked worktrees are separate.

    The live-evidence publisher lands from its own detached worktree. Its .git
    file points at a separate index, so it never takes the owned-clone lock.
    """
    def republish():
        # Another working tree: dirty files there belong to its sessions.
        sh(["git", "-C", shared_checkout, "fetch", "--quiet", "origin"])
        sh(["git", "-C", shared_checkout, "merge", "--quiet", "--ff-only", "origin/main"])

    if os.path.isfile(os.path.join(clone, ".git")):
        landed = _land_backlog_change(clone, message, add=add)
        if shared_checkout and os.path.isdir(shared_checkout):
            republish()
        return landed

    city = os.path.realpath(city_dir())
    with backlog_write(city):
        landed = _land_backlog_change(clone, message, add=add)
        if shared_checkout and os.path.isdir(shared_checkout):
            _backlog_writes[(os.getpid(), city)].append(republish)
    return landed
