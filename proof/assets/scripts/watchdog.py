#!/usr/bin/env python3
"""PROOF watchdog: put right the four ways a running packet stalls with nobody
at fault and nobody told. A script — no agent, no model, no tokens.

    watchdog.py [--dry-run]

Run first in every factory pass (`factory.sh`), under the factory lock.

1. **A workflow root held by a worker.** The engine keeps a running workflow's
   root `in_progress` and UNASSIGNED. When the controller restarts it puts every
   running root back to `open`, and an open, unassigned bead routed to the worker
   pool is exactly what the builtin work query offers a polling session. The
   session is handed the root — its own packet's or, worse, another's — it
   rightly refuses to execute it, and from then on `gc hook --claim` answers
   `existing_assignment` with that root for as long as the OTHER packet runs,
   masking the session's own ready steps. On 2026-09-20 one restart reopened
   four roots; BKLG-20260920-tbhd, review already approved, sat 104 of its 119
   minutes behind BKLG-20260913-524k's root, and both of its worker's BLOCKED
   mails went unread. The watchdog restores the engine's own state: `open` goes
   back to `in_progress`, and a session's hold is cleared (`--assignee ""
   --force`, which leaves the status alone — probed on a scratch bead). Neither
   touches a step, an outcome or a control bead. A **scope bead** held by one of
   this pack's sessions is released the same way (`scope_holds`): gc routes the
   scope of a ralph loop's second and later iterations to the worker pool, so a
   worker is handed it the moment the iteration's last step closes (2026-10-03
   to 2026-10-09, nine BLOCKED mails). `claim.py` releases it in the session;
   this is the backstop.

2. **A ready step whose session is sitting idle.** A session that stopped polling
   is woken by the engine only every 40-50 minutes. If a step is ready, assigned
   to a live session, and that session has been quiet for IDLE_MINUTES, it is
   nudged to claim — at most once per NUDGE_EVERY_MINUTES per session, and at
   once when the watchdog has just cleared a root that session held.

3. **Sessions stopped at the provider's usage limit.** A session that reaches the
   account's limit stops at a prompt saying so, and stays there: on 2026-09-20 two
   workers and the planner sat at "You've reached your Fable limit" for two hours,
   two of them on packets admitted the second the limit hit, and nothing said so.
   The watchdog reads the screen of a quiet session that has a ready step, says
   which sessions are stopped and since when, mails `human` once per episode,
   and records it so the dispatcher and the planner's trigger HOLD — a new packet
   would only start a session that stops at the same prompt, holding a claim. It
   keeps nudging a stopped session, slowly: the screen cannot show that the limit
   was lifted, and a nudge that gets through is how the session finds out.

4. **Mail that needs a person, in an inbox nobody is watching.** `human` mail is
   a list inside the city. With `notify_command` set in the policy, each unread
   message that needs a person (BLOCKED, a stopped packet or planning run, a red
   main, an escalation) is pushed through it once. And a message is marked read
   once its matter is settled, so what is left unread is what is still owed: a
   "packet ready" or "packet landed" notice once that packet is done on main; a
   `BLOCKED:`, "packet stopped", "pull request cannot merge" or "CI triage" mail
   once its packet has been triaged on main (done, superseded, re-shaped or
   re-admitted); a "planning run stopped" mail once that rig's run is cleared.
   A `NOTE:` is never pushed: the daily report digests it (BKLG-20260924-fuyh).
   The rule is `human_mail` below; `gc proof status` and the report import it.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import factory_lib as fl
from factory_lib import admit

ME = "watchdog"
ROOT_SETTLE_SECONDS = 120     # a root younger than this is still the engine's to start
IDLE_MINUTES = 10
NUDGE_EVERY_MINUTES = 15
PROBE_EVERY_MINUTES = 30      # a session stopped at the usage limit: a nudge is how it learns the limit lifted
NUDGES_BEFORE_SAYING = 3      # after this many on one step with no progress, a person is told
# Claude Code: "You've reached your … limit", "limit to reset"; Codex: "You've hit your usage limit".
LIMIT = re.compile(r"reached your .{0,40}limit|hit your usage limit|usage limit reached|limit to reset|/usage-credits", re.I)
BLOCKING = ("blocks", "waits-for", "conditional-blocks")
STAMP = "%Y-%m-%dT%H:%M:%SZ"


def when(text):
    """A bead's or a session's timestamp, as an aware datetime; None if unreadable."""
    try:
        stamp = datetime.fromisoformat((text or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def limited(screen):
    """Is this session stopped at the provider's usage limit? Judged from the end
    of its screen only: once a session moves again the notice scrolls away."""
    tail = [line for line in (screen or "").splitlines() if line.strip()][-14:]
    return any(LIMIT.search(line) for line in tail)


def last_nudge(entry):
    """(when, step, count) from a nudge memory entry — a bare timestamp in 0.14.0."""
    if isinstance(entry, dict):
        return when(entry.get("at")), entry.get("step") or "", int(entry.get("count") or 0)
    return when(entry), "", 1 if entry else 0


def root_faults(beads, now):
    """[(root id, "start" | "unassign", holder)] for the running workflow roots
    that are not in the engine's own state — in_progress and unassigned."""
    faults = []
    for bead in beads:
        md = bead.get("metadata") or {}
        if md.get("gc.kind") != "workflow" or bead.get("status") == "closed":
            continue
        born = when(bead.get("created_at"))
        if not born or (now - born).total_seconds() < ROOT_SETTLE_SECONDS:
            continue
        holder = bead.get("assignee") or ""
        if holder:
            faults.append((bead["id"], "unassign", holder))
        elif bead.get("status") == "open":
            faults.append((bead["id"], "start", ""))
    return faults


PACK_SESSION = "proof__"      # session names of this pack's agents; a controller's is core__control-dispatcher-…


def scope_holds(beads, sessions=()):
    """[(scope bead id, holder)] for the open scope beads one of this pack's
    sessions holds.

    gc 1.4.2 routes the scope bead of every second and later iteration of a
    ralph loop to the loop's worker pool (`spawnNextAttempt` skips only spec and
    control kinds when it routes an attempt), so it is claimed the moment its
    last step closes and then masks the worker's real steps. `claim.py` releases
    it in the session; this is the backstop. A scope bead is topology: by gc's own
    rule no agent may hold one, and no controller ever does — the control
    dispatcher closes it unassigned. Only a holder that is one of this pack's
    sessions is released, never a control dispatcher or an unknown name."""
    templates = {s.get("id"): s.get("template") or "" for s in sessions or ()}
    holds = []
    for bead in beads:
        md = bead.get("metadata") or {}
        holder = (bead.get("assignee") or "").strip()
        if md.get("gc.kind") != "scope" or bead.get("status") == "closed" or not holder:
            continue
        if "control-dispatcher" in holder:
            continue
        if holder.startswith(PACK_SESSION) or "/proof." in templates.get(holder, ""):
            holds.append((bead["id"], holder))
    return holds


def ready_steps(beads):
    """{session name: [step bead id]} — open steps, assigned, with nothing left
    blocking them. Control beads and workflow roots are the orchestrator's."""
    status = {b["id"]: b.get("status") for b in beads}
    found = {}
    for bead in beads:
        md = bead.get("metadata") or {}
        ref = md.get("gc.step_ref") or ""
        if bead.get("status") == "closed" or not bead.get("assignee") or md.get("gc.kind") not in (None, "", "task") \
                or not ("attempt" in ref or "iteration" in ref):
            continue
        if ref.endswith((".gate", ".review-gate")):
            continue                              # the gater is a script: quiet while a gate runs, nothing to wake
        waits = [d for d in bead.get("dependencies") or []
                 if (d.get("type") or d.get("dep_type") or "") in BLOCKING
                 and status.get(d.get("depends_on_id"), "closed") != "closed"]
        if not waits:
            found.setdefault(bead["assignee"], []).append(bead["id"])
    return found


def to_nudge(ready, sessions, nudged, now, urgent=(), stopped=()):
    """[(session id, session name, step id)] to wake now.

    ready: `ready_steps()`. sessions: `gc session list --json` rows. nudged:
    {session name: {"at", "step", "count"}}. urgent: session names to wake whatever
    their idle time — the watchdog has just freed them. stopped: names sitting at
    the usage limit, probed every PROBE_EVERY_MINUTES instead."""
    by_name = {s.get("session_name"): s for s in sessions}
    out = []
    for name, steps in sorted(ready.items()):
        session = by_name.get(name)
        if not session or session.get("closed"):
            continue                              # no live session: the engine starts one for the step
        last, _step, _count = last_nudge(nudged.get(name))
        every = PROBE_EVERY_MINUTES if name in stopped else NUDGE_EVERY_MINUTES
        if last and now - last < timedelta(minutes=every):
            continue
        quiet = when(session.get("last_active"))
        if name not in urgent and (not quiet or now - quiet < timedelta(minutes=IDLE_MINUTES)):
            continue
        out.append((session.get("id") or name, name, steps[0]))
    return out


def quiet_candidates(ready, sessions, now):
    """Live sessions with a ready step that have been quiet IDLE_MINUTES — the
    only ones whose screens are worth reading."""
    by_name = {s.get("session_name"): s for s in sessions}
    out = []
    for name in sorted(ready):
        session = by_name.get(name)
        quiet = when((session or {}).get("last_active"))
        if session and not session.get("closed") and quiet and now - quiet >= timedelta(minutes=IDLE_MINUTES):
            out.append((session.get("id") or name, name))
    return out


def unmoved(nudged):
    """[(session name, step, count)] nudged NUDGES_BEFORE_SAYING times on one step."""
    rows = []
    for name, entry in sorted(nudged.items()):
        _at, step, count = last_nudge(entry)
        if step and count >= NUDGES_BEFORE_SAYING:
            rows.append((name, step, count))
    return rows


def heal(city, rig, beads, now, said, dry_run, sessions=()):
    freed = []
    for scope, holder in scope_holds(beads, sessions):
        what = "scope bead %s was held by session %s, masking that session's own steps" % (scope, holder)
        if dry_run:
            fl.log(ME, "rig %s: would release — %s" % (rig, what))
            continue
        done = admit.gc(city, "bd", "--rig", rig, "update", scope, "--assignee", "", "--force", check=False)
        if done.returncode == 0:
            freed.append(holder)
            fl.log(ME, "rig %s: %s; released it (status untouched)" % (rig, what))
        else:
            said.say("scope:" + scope, "rig %s: %s, and releasing it failed: %s"
                     % (rig, what, (done.stderr or done.stdout).strip()[-200:]))
    for root, action, holder in root_faults(beads, now):
        if action == "unassign":
            what = "workflow root %s was held by session %s, masking that session's own steps" % (root, holder)
            args = ["--rig", rig, "update", root, "--assignee", "", "--force"]
            freed.append(holder)
        else:
            what = "workflow root %s had been put back to `open`, where the worker pool's query offers it" % root
            args = ["--rig", rig, "update", root, "--status", "in_progress"]
        if dry_run:
            fl.log(ME, "rig %s: would restore — %s" % (rig, what))
            continue
        done = admit.gc(city, "bd", *args, check=False)
        if done.returncode == 0:
            fl.log(ME, "rig %s: %s; restored to in_progress and unassigned" % (rig, what))
        else:
            said.say("root:" + root, "rig %s: %s, and restoring it failed: %s"
                     % (rig, what, (done.stderr or done.stdout).strip()[-200:]))
    return freed


def claim_script(city):
    """The claim wrapper a nudged session runs: it releases a topology bead it is
    handed instead of stalling on it (see claim.py)."""
    return os.path.join(city, "packs", "proof", "assets", "scripts", "claim.py")


def wake(city, rig, beads, sessions, freed, memory, now, dry_run):
    """Nudge what should be nudged in one rig; return the sessions found stopped
    at the usage limit."""
    ready = ready_steps(beads)
    stopped = []
    for session_id, name in quiet_candidates(ready, sessions, now):
        screen = admit.gc(city, "session", "peek", session_id, check=False)
        if screen.returncode == 0 and limited(screen.stdout):
            stopped.append(name)
    for session_id, name, step in to_nudge(ready, sessions, memory, now, freed, stopped):
        if dry_run:
            fl.log(ME, "rig %s: would nudge %s — step %s is ready and assigned to it" % (rig, name, step))
            continue
        sent = admit.gc(city, "session", "nudge", session_id,
                        "PROOF watchdog: step %s of your packet is ready and assigned to you. "
                        "Run `python3 %s` and carry on." % (step, claim_script(city)), check=False)
        if sent.returncode == 0:
            _at, before, count = last_nudge(memory.get(name))
            memory[name] = {"at": now.strftime(STAMP), "step": step, "count": count + 1 if before == step else 1}
            if name not in stopped:          # a probe of a stopped session is not news every half hour
                fl.log(ME, "rig %s: nudged %s — step %s was ready and the session quiet" % (rig, name, step))
    return stopped


def hold(city, said, stopped, now, dry_run):
    """Record (or clear) the usage-limit hold the dispatcher and the planner's
    trigger read, say it once, and mail a person once per episode."""
    state = said.section("limit")
    if not stopped:
        if state.get("sessions") and not dry_run:
            fl.log(ME, "no session is stopped at the usage limit any more; dispatch and planning resume")
        state.clear()
        return
    since = state.get("since") or now.strftime(STAMP)
    text = ("%d session%s stopped at the provider's usage limit since %s: %s. Nothing they hold moves until the "
            "limit resets, credits are added, or the account or model changes; they are nudged every %d minutes so "
            "they notice when it does. Dispatch and planning hold meanwhile."
            % (len(stopped), "" if len(stopped) == 1 else "s", since, ", ".join(sorted(stopped)), PROBE_EVERY_MINUTES))
    said.say("limit", text)
    mailed = bool(state.get("mailed"))
    if not mailed and not dry_run:
        mailed = fl.mail("BLOCKED: Gas City sessions are stopped at the provider's usage limit", text)
    state.update({"since": since, "sessions": sorted(stopped), "mailed": mailed})


# --- the human inbox, classified ONE way for every reader (BKLG-20260924-fuyh)
# The watchdog pushes and settles by these rules, and `gc proof status` and the
# daily report import them from here. status.py once carried its own copy of
# NEEDS_A_PERSON and it drifted; there is no second copy now.
PACKET_ID = re.compile(r"BKLG-\d{8}-[a-z0-9]{4}")
NEEDS_A_PERSON = re.compile(r"^(BLOCKED|PROOF (packet stopped|planning run stopped|packet merged|pull request|CI triage|red main|claim expiring)|ESCALATION)")
# A worker is PROCEEDING when it sends one: never pushed, never counted as owed,
# digested by the daily report and marked read once that report is mailed.
NOTE = re.compile(r"^NOTE:")
# Delivered notices: settled once the packet is done on main.
DELIVERED = re.compile(r"^PROOF packet (?:ready|landed): (BKLG-\d{8}-[a-z0-9]{4})\b")
# Stops about one packet: settled once that packet has been TRIAGED on main. A
# worker's BLOCKED often names the packet by its four-character code alone
# ("BLOCKED: xxtj needs …"); that settles only when exactly one item has it.
STOPPED = re.compile(r"^(?:BLOCKED|PROOF (?:packet stopped|pull request cannot merge|CI triage)): "
                     r"(BKLG-\d{8}-[a-z0-9]{4}|[a-z0-9]{4})\b")
# A stopped planning run blocks its rig's next run until a person clears it.
PLANNING_STOPPED = re.compile(r"^PROOF planning run stopped: (\S+)")


def inbox(city, recipient="human"):
    """Unread mail for `recipient`, as gc lists it: id, from, subject, created_at — no body."""
    out = admit.gc(city, "mail", "inbox", recipient, "--json", check=False)
    try:
        found = json.loads(out.stdout or "[]")
    except ValueError:
        return []
    if isinstance(found, dict):                      # gc wraps the list: {"messages": [...]} or {"items": [...]}
        found = next((v for v in found.values() if isinstance(v, list)), [])
    return found


def mail_body(city, mail_id):
    """A message's body, read WITHOUT marking it read; "" when unreadable."""
    out = admit.gc(city, "mail", "peek", mail_id, "--json", check=False)
    try:
        doc = json.loads(out.stdout or "{}")
    except ValueError:
        return ""
    return ((doc.get("message") or doc) if isinstance(doc, dict) else {}).get("body") or ""


def completed(packets):
    """Ids of the packets the completer has marked done on main."""
    return {p["id"] for p in packets if p["md"].get("proof.completed") not in (None, "", "not-ours")}


def _frontmatter(text):
    """The top-level `key: value` lines of an item's frontmatter; lists are skipped."""
    block = re.match(r"---\n(.*?)\n---\n", text, re.S)
    return {key: value.strip("'\"") for key, value in
            re.findall(r"(?m)^([a-z_]+):[ \t]*(\S.*?)[ \t]*$", block.group(1))} if block else {}


def triage_reader(backlog):
    """`triaged(packet_id, mail_created_at)` over the backlog clone: has the item
    been triaged on main since that mail was sent? It has when it is `done` or
    `superseded`, when it is claimed again after the mail (re-admitted), or when
    what it asks for — its SHAPED text, as the completer's `close_triaged` and
    the dispatcher judge it — differs from main's copy at the time of the mail
    (re-shaped, or given a question to wait on). `last_reviewed` alone is not
    evidence: the release commit rewrites it without anyone triaging anything."""
    at_time, cache = {}, {}

    def triaged(packet_id, stamp):
        sent = when(stamp)
        full = PACKET_ID.fullmatch(packet_id)
        files = glob.glob(os.path.join(backlog, "docs/backlog/items",
                                       (packet_id if full else "BKLG-" + "[0-9]" * 8 + "-" + packet_id) + "-*.md"))
        if sent is None or len(files) != 1:
            return False
        key = (packet_id, stamp)
        if key in cache:
            return cache[key]
        with open(files[0], encoding="utf-8") as fh:
            today = fh.read()
        fm = _frontmatter(today)
        answer = fm.get("status") in ("done", "superseded")
        if not answer and fm.get("claimed_at"):
            claimed = when(fm["claimed_at"])
            answer = claimed is not None and claimed > sent
        if not answer:
            if stamp not in at_time:
                at_time[stamp] = fl.sh(["git", "-C", backlog, "rev-list", "-1", "--first-parent",
                                        "--before=" + sent.strftime("%Y-%m-%dT%H:%M:%SZ"), "HEAD"]).stdout.strip()
            then = fl.sh(["git", "-C", backlog, "show", "%s:%s" % (at_time[stamp], os.path.relpath(files[0], backlog))]) \
                if at_time[stamp] else None
            answer = bool(then and then.returncode == 0 and fl.shaped(then.stdout) != fl.shaped(today))
        cache[key] = answer
        return answer
    return triaged


def settled(mail, done, triaged=None, planning_stopped=None):
    """Ids of unread mails whose matter is settled, to be marked read — never deleted.

    `done`: ids complete on main. `triaged(id, created_at)`: see `triage_reader`;
    None when the backlog cannot be read, and then no stop is settled.
    `planning_stopped`: the rigs whose latest planning run is still stopped; None
    when unknown, and then no planning mail is settled."""
    out = []
    for m in mail:
        subject = m.get("subject") or ""
        if (hit := DELIVERED.match(subject)):
            over = hit.group(1) in done
        elif (hit := STOPPED.match(subject)):
            pid = hit.group(1)
            finished = pid in done if PACKET_ID.fullmatch(pid) else sum(d.endswith("-" + pid) for d in done) == 1
            over = finished or bool(triaged and triaged(pid, m.get("created_at")))
        elif (hit := PLANNING_STOPPED.match(subject)):
            over = planning_stopped is not None and hit.group(1) not in planning_stopped
        else:
            over = False
        if over:
            out.append(m["id"])
    return out


def needs_a_person(mail, done=(), settled=()):
    """The unread mails a person still owes an answer: a NEEDS_A_PERSON subject,
    not settled, and not about packets that are all done on main. Never a NOTE."""
    out = []
    for m in mail:
        subject = m.get("subject") or ""
        ids = set(PACKET_ID.findall(subject))
        if NEEDS_A_PERSON.match(subject) and m["id"] not in set(settled) and not (ids and ids <= set(done)):
            out.append(m)
    return out


def notes(mail):
    return [m for m in mail if NOTE.match(m.get("subject") or "")]


def stopped_planning(city, rigs):
    """The rigs whose most recent planning run is stopped; None when it cannot be read."""
    try:
        backlog = admit.backlog_clone_of(rigs)
        home = next(r for r in rigs if os.path.realpath(rigs[r].get("Path") or "") == os.path.realpath(backlog))
    except (fl.Refused, StopIteration):
        return None
    latest = {}
    for run in fl.planning_runs(city, home):
        latest[run["target"]] = run["state"]
    return {target for target, state in latest.items() if state == "stopped"}


def human_mail(city, rigs, mail, done):
    """The inbox as every reader sees it: {"settled": [ids], "need": [mails],
    "notes": [mails]}. `need` and `notes` never include a settled mail."""
    try:
        triaged = triage_reader(admit.backlog_clone_of(rigs))
    except fl.Refused:
        triaged = None
    gone = settled(mail, done, triaged, stopped_planning(city, rigs))
    left = [m for m in mail if m["id"] not in set(gone)]
    return {"settled": gone, "need": needs_a_person(left, done), "notes": notes(left)}


def to_push(mail, pushed, done=(), settled=()):
    """What still needs a person and has not been pushed yet: once each."""
    return [m for m in needs_a_person(mail, done, settled) if m["id"] not in pushed]


def push(command, message):
    """Hand one message to the operator's notifier: subject and id in the
    environment, body on stdin. Its exit status is the delivery."""
    env = dict(os.environ, PROOF_SUBJECT=message.get("subject") or "", PROOF_MAIL_ID=message["id"],
               PROOF_FROM=message.get("from") or "")
    try:
        done = subprocess.run(list(command), input=message.get("body") or "", env=env, text=True,
                              capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as why:
        return False, str(why)
    return done.returncode == 0, (done.stderr or done.stdout).strip()[-200:]


def notification_batches(messages, grouped=False):
    if not grouped or len(messages) <= 1:
        return [(message, [message["id"]]) for message in messages]
    subjects = [m.get("subject") or "(no subject)" for m in messages]
    return [({"id": "batch", "subject": "Gas City: %d alerts need attention" % len(messages),
              "body": "\n".join(subjects[:12]) +
                      ("\n…and %d more." % (len(messages) - 12) if len(messages) > 12 else "") +
                      "\n\nRun gc proof status, then gc mail inbox for details."},
             [m["id"] for m in messages])]


def tell(city, rigs, policy, said, done, now, dry_run):
    """Settle what is settled, then push what still needs a person, once each.
    Returns the classified inbox, as `gc proof status` counts it."""
    mail = inbox(city)
    view = human_mail(city, rigs, mail, done)
    for mail_id in view["settled"]:
        if dry_run:
            fl.log(ME, "would mark %s read: what it is about has been settled on main" % mail_id)
        else:
            admit.gc(city, "mail", "mark-read", mail_id, check=False)
    command = policy.get("notify_command") or []
    owed = to_push(mail, set(said.section("pushed")), done, view["settled"])
    if owed and not command:
        said.say("no-notifier", "%d message%s for `human` need a person and nothing pushes them: set "
                 "`notify_command` in proof/policy.toml, or read them with `gc proof status`"
                 % (len(owed), "" if len(owed) == 1 else "s"))
    pushed = said.section("pushed")
    for message, mail_ids in notification_batches(owed, policy.get("notify_batch", False)) if command else []:
        if dry_run:
            fl.log(ME, "would push %s: %s" % (message["id"], (message.get("subject") or "")[:100]))
            continue
        ok, why = push(command, message)
        if ok:
            for mail_id in mail_ids:
                pushed[mail_id] = now.strftime(STAMP)
            fl.log(ME, "pushed %s: %s" % (message["id"], (message.get("subject") or "")[:100]))
        else:
            said.say("push:" + message["id"], "could not push %s through notify_command: %s" % (message["id"], why))
    for mail_id in sorted(pushed, key=pushed.get)[:-500]:
        del pushed[mail_id]
    return view


def main(argv):
    dry_run = "--dry-run" in argv
    city = fl.city_dir()
    policy = fl.load_policy(city)
    said = fl.Standing(city, ME, keep=not dry_run)
    try:
        return watch(city, policy, said, dry_run)
    finally:
        said.close()


def watch(city, policy, said, dry_run):
    import json
    now = datetime.now(timezone.utc)
    rigs = admit.load_rigs(city)
    out = admit.gc(city, "session", "list", "--json", check=False)
    try:
        sessions = json.loads(out.stdout or "[]")
    except ValueError:
        sessions = []
    if isinstance(sessions, dict):
        sessions = next((v for v in sessions.values() if isinstance(v, list)), [])
    memory = said.section("nudged")
    ids = {s.get("session_name"): s.get("id") for s in sessions}
    done, stopped = set(), []
    health_notified = said.section("health_notified")
    for rig in sorted(rigs):
        try:
            admit.healthy_clone(rigs, rig)
        except fl.Refused as exc:
            said.say("health:" + rig, "rig %s is unhealthy: %s" % (rig, exc))
            if not dry_run and health_notified.get(rig) != str(exc):
                if fl.mail("BLOCKED: Gas City rig %s cannot start workers" % rig, str(exc)):
                    health_notified[rig] = str(exc)
                else:
                    said.say("health-mail:" + rig, "could not mail the rig-health failure; retry next pass")
            continue
        health_notified.pop(rig, None)
        beads = fl.rig_beads(city, rig)
        freed = heal(city, rig, beads, now, said, dry_run, sessions)
        stopped += ["%s (%s)" % (name, rig) for name in wake(city, rig, beads, sessions, freed, memory, now, dry_run)]
        done |= completed(fl.packets_of(beads))
    for name in [n for n, entry in memory.items()
                 if not last_nudge(entry)[0] or now - last_nudge(entry)[0] > timedelta(days=1)]:
        del memory[name]
    for name, step, count in unmoved({n: e for n, e in memory.items() if not any(s.startswith(n + " ") for s in stopped)}):
        said.say("unmoved:" + name, "%s has been nudged %d times for step %s and has not moved — look at it: "
                                    "gc session peek %s" % (name, count, step, ids.get(name) or name))
    hold(city, said, stopped, now, dry_run)

    tell(city, rigs, policy, said, done, now, dry_run)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except fl.Refused as why:
        fl.log(ME, "stopped — %s" % why)
        sys.exit(1)
