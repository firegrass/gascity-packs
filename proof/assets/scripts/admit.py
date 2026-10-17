#!/usr/bin/env python3
"""Admit one claimed backlog packet into Gas City for execution.

    gc proof admit BKLG-YYYYMMDD-xxxx --rig <rig>
             --landing <local|push|pr|automerge|main> [--review] [--dry-run]
    gc proof admit BKLG-YYYYMMDD-xxxx --rig <rig>
             [--exercise observe|mutate|spend] [--dry-run]       # a live test

Admission is a bridge, not a second backlog. The Markdown backlog stays the
authority for what work exists, who holds it and whether it is done; Gas City
only executes. So this command never claims and never edits a backlog item. It
*verifies* that the item is already claimed for the Gas City seat on
origin/main, then creates the execution side: a packet worktree, one work bead,
and the `mol-proof-packet` workflow.

Completion is different from the claim, and `--landing main` authorizes it. The
backlog's own procedure has the working session mark its item done in its own
worktree, and that is what a `main` packet does: the worker completes the item
on the packet branch, and the orchestrator's landing check — not the agent —
decides whether the result is still what was reviewed, gates it and pushes it.
That level exists only for the repository that holds the backlog, whose rules
land on main directly; a repository that lands through pull requests keeps
`pr` / `automerge`, and its items are completed by a person who saw the merge.

Why admission does not make the claim itself: a claim is not one file write. It
rewrites the item, possibly its epic and milestone, and the generated
delivery-queue views, and lands on `main` from a checkout other sessions are
writing. That procedure is understood and hazardous; putting it inside new,
unproven code is how a pilot damages the thing it is piloting. Claim first with
the existing tool, land it, then admit:

    python3 scripts/next_work.py --claim <id> --by gascity/<factory>/<rig> --hours 12

A LIVE TEST — an item that declares `live_target` — is a different packet type
(ADR-0151), and admission never runs one as an implementation packet. It is
authorized on the production-action axis, not the git one: `--exercise`,
default `observe`, in place of `--landing`, which it refuses, since a live test
has no code to land. It is admitted only where the rig's
`[rigs.<rig>.livetest]` policy enables it and names its target and recipe;
admission freezes the run's manifest, takes the city's ONE live slot through
`livetest_state.RunStore` (the same owner the factory uses — no second lock),
and slings `mol-proof-livetest` to the tier agent `proof.tester-<exercise>`.
Observe V1 implements `observe` only; `mutate` and `spend` are recognised and
refused as not implemented, whatever the policy says.

Everything read here comes from the Gas City-owned orchestrator clone,
fast-forwarded to origin/main — never from the shared checkout, which goes stale
silently. Backlog text is untrusted data: it reaches `gc` and `git` only as
argv or a file, never through a shell.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ == "__main__":
    sys.modules.setdefault("admit", sys.modules[__name__])

import judge  # noqa: E402  the pack's one judgement helper, next to this file
import livetest_contract as lt  # noqa: E402  the one parser of the live-test record shape
import path_rules  # noqa: E402  the path sets the orchestrator decides, not the agent
import scope_companions  # noqa: E402
import shaping_evidence  # noqa: E402

BACKLOG_REPO = "liskov-agent-orchestrator"  # the repository that holds docs/backlog
# A factory's claim seat is `gascity/<factory>/<rig>` (ADR-0239 decision 4):
# the prefix says "a factory, not a person", the factory is this city's
# workspace name, the rig the clone that runs the packet. `seat()` is the one
# builder; `held_by_us()` is the one test, and it still recognises the
# two-part seat `gascity/<rig>` that claims made before pack 0.44.0 carry, so
# those packets finish without a re-claim. Remove that arm once `gc proof
# status` reports no legacy-held claim.
SEAT_PREFIX = "gascity/"              # a claim held under this prefix is a factory's
LEASE_KEY = "proof.lease_id"          # the seat's ledger lease, on the execution bead (ADR-0236)
WORKER = "proof.worker"
FORMULA = "mol-proof-packet"
LIVE_FORMULA = "mol-proof-livetest"
TESTER = "proof.tester-%s"            # one agent per exercise tier (ADR-0152 clause 1)
DEFAULT_EXERCISE = "observe"
MIN_CLAIM_LEFT = timedelta(hours=1)
LANDING = {
    "local":     {"push": "false", "open_pr": "false", "automerge": "false", "land_main": "false"},
    "push":      {"push": "true",  "open_pr": "false", "automerge": "false", "land_main": "false"},
    "pr":        {"push": "true",  "open_pr": "true",  "automerge": "false", "land_main": "false"},
    "automerge": {"push": "true",  "open_pr": "true",  "automerge": "true",  "land_main": "false"},
    # Direct landing: nothing is pushed but the final, orchestrator-gated commit,
    # and it goes to main. No packet branch is published and no PR is opened.
    "main":      {"push": "false", "open_pr": "false", "automerge": "false", "land_main": "true"},
}
ITEM_PATH_RE = re.compile(r"^docs/backlog/items/BKLG-[0-9]{8}-[a-z0-9]{4}-[A-Za-z0-9._-]+[.]md$")
ID_RE = re.compile(r"^BKLG-[0-9]{8}-[a-z0-9]{4}$")


class Refused(Exception):
    """Admission declined. The message says what to do about it."""


class Dirty(Refused):
    """A recorded packet workspace holds uncommitted changes. Admission archives
    them to a tag (`archive_retained`); a caller that must not change the
    worktree — a dry run, a live test — gets this refusal instead."""

    def __init__(self, message, work_dir, branch):
        super().__init__(message)
        self.work_dir, self.branch = work_dir, branch


class Waits(Refused):
    """Not refused — deferred: the city's one live slot is taken. The item is
    fit, and is admitted once the slot is free, so a caller must not treat
    this as a refusal to remember."""


WAITS_EXIT = 75  # EX_TEMPFAIL: the dispatcher tells a wait from a refusal by it


class Running(Refused):
    """Already admitted or being admitted; preserve the running packet's claim."""


RUNNING_EXIT = 76  # distinct from a refused item, an outage, and the live-slot wait


@contextlib.contextmanager
def admission_lock(city, item_id, dry_run=False):
    """One item's check/create/sling is single-flight; lock files stay in place."""
    if dry_run:
        yield
        return
    directory = os.path.join(city, "proof", "admit-locks")
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, item_id + ".lock"), "a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Running("%s already being admitted" % item_id) from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class Unavailable(Refused):
    """Not about the item — a command admission ran failed or was killed: the
    city's Dolt server down, a `git fetch` refused. Only `run()` raises it; no
    rule check does. The dispatcher holds the item for an hour, not until it
    changes: on 2026-09-26 a Dolt outage held a p1 packet out for three days."""


UNAVAILABLE_EXIT = 69  # EX_UNAVAILABLE: the dispatcher tells an outage from a refusal by it


# No command this pack runs is allowed to wait for ever. On 2026-09-20 a `git
# fetch` stalled on a reset connection and held the factory lock for seventeen
# minutes: nothing completed, nothing dispatched, and a deploy could not take the
# lock. Ten minutes covers the slowest honest call (a worktree of liskov-rs).
RUN_TIMEOUT = 600


def run(argv, cwd=None, check=True, input=None, env=None):  # noqa: A002 - subprocess's own name
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              timeout=RUN_TIMEOUT, input=input, env=env)
    except subprocess.TimeoutExpired:
        raise Unavailable("`%s` did not finish in %d s and was killed" % (" ".join(argv[:4]), RUN_TIMEOUT))
    if check and proc.returncode != 0:
        raise Unavailable("`%s` failed (%d): %s" % (" ".join(argv[:4]), proc.returncode,
                                                (proc.stderr or proc.stdout).strip()[-400:]))
    return proc


def city_root():
    root = os.environ.get("GC_CITY") or os.environ.get("GC_CITY_PATH")
    if root:
        return root
    here = os.getcwd()
    while here != "/":
        if os.path.isfile(os.path.join(here, "city.toml")):
            return here
        here = os.path.dirname(here)
    raise Refused("cannot find the city: set GC_CITY or run inside the city directory")


def factory_name():
    """This factory's name: the city's workspace name, read in gc's own order
    (config.EffectiveCityName) — `GC_CITY_NAME` (what gc gives pack commands),
    else the site binding `.gc/site.toml` `workspace_name`, else `city.toml`
    `[workspace] name`, else the city directory's name. It is one path segment
    of the claim seat and of the Slack adapter URL, so it is refused otherwise."""
    name = (os.environ.get("GC_CITY_NAME") or "").strip()
    if not name:
        import tomllib
        root = city_root()
        for path, read in ((os.path.join(root, ".gc", "site.toml"), lambda d: d.get("workspace_name")),
                           (os.path.join(root, "city.toml"), lambda d: (d.get("workspace") or {}).get("name"))):
            try:
                with open(path, "rb") as fh:
                    name = str(read(tomllib.load(fh)) or "").strip()
            except (OSError, ValueError):
                name = ""
            if name:
                break
        name = name or os.path.basename(os.path.normpath(root))
    if not name or any(c in name for c in "/?#%") or any(c.isspace() for c in name):
        raise Refused("factory name %r is not usable in a URL path or a claim seat (one path segment)" % name)
    return name


def seat(rig):
    """The claim seat this factory holds a packet under: gascity/<factory>/<rig>."""
    return SEAT_PREFIX + factory_name() + "/" + rig


def seat_rig(value):
    """The rig of a factory seat, either form; '' for a person."""
    parts = str(value or "")[len(SEAT_PREFIX):].split("/") if str(value or "").startswith(SEAT_PREFIX) else []
    return parts[-1] if 1 <= len(parts) <= 2 and all(parts) else ""


def seat_factory(value):
    """The factory of a three-part seat; '' for a person or a two-part seat."""
    parts = str(value or "")[len(SEAT_PREFIX):].split("/") if str(value or "").startswith(SEAT_PREFIX) else []
    return parts[0] if len(parts) == 2 and all(parts) else ""


def held_by_us(claimed_by, rig):
    """Whether `claimed_by` is this factory's seat for `rig` — or the two-part
    seat a claim made before pack 0.44.0 still carries (see SEAT_PREFIX)."""
    return claimed_by in (seat(rig), SEAT_PREFIX + rig)


def gc(city, *args, cwd=None, check=True):
    return run(["gc", "--city", city, *args], cwd=cwd, check=check)


def load_rigs(city):
    doc = json.loads(gc(city, "config", "show", "--json").stdout)
    return {r["Name"]: r for r in doc["config"].get("Rigs") or []}


def owned_clone(rigs, rig):
    if rig not in rigs:
        raise Refused("no rig named %r in this city (have: %s)" % (rig, ", ".join(sorted(rigs))))
    path = rigs[rig].get("Path") or ""
    git_dir = run(["git", "-C", path, "rev-parse", "--absolute-git-dir"], check=False).stdout.strip()
    if not git_dir or not os.path.isfile(os.path.join(git_dir, "gascity-owned")):
        raise Refused("rig %r at %s is not a Gas City-owned clone" % (rig, path or "<unbound>"))
    return path


def healthy_clone(rigs, rig):
    """Launchers need a usable worktree; read-only reports must still see broken rigs."""
    path = owned_clone(rigs, rig)
    import rig_health
    why = rig_health.problem(path)
    if why:
        raise Refused("rig %r at %s: %s" % (rig, path, why))
    return path


def repo_name(clone):
    """The repository a clone belongs to, from its origin URL — the name a backlog
    item's write_scope uses. Rig names are labels an operator may change; the
    repository is the fact."""
    url = run(["git", "-C", clone, "remote", "get-url", "origin"]).stdout.strip()
    name = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return name[:-4] if name.endswith(".git") else name


def backlog_clone_of(rigs):
    """The owned clone of the repository that holds the backlog, whatever its rig
    is called."""
    for rig in sorted(rigs):
        try:
            clone = owned_clone(rigs, rig)
        except Refused:
            continue
        if repo_name(clone) == BACKLOG_REPO:
            return clone
    raise Refused("no rig in this city is a Gas City-owned clone of %s" % BACKLOG_REPO)


def refresh_backlog_clone(clone):
    """Fast-forward the owned orchestrator clone to origin/main. It is nobody's
    working copy, so it must be clean and on main; anything else is a fault."""
    from factory_lib import backlog_write, BacklogBusy  # factory_lib imports admit
    run(["git", "-C", clone, "fetch", "--quiet", "--prune", "origin"])
    try:
        with backlog_write():
            if run(["git", "-C", clone, "status", "--porcelain", "--untracked-files=no"]).stdout.strip():
                raise Unavailable("the owned orchestrator clone %s has modified tracked files" % clone)
            branch = run(["git", "-C", clone, "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
            if branch != "main":
                raise Unavailable("the owned orchestrator clone is on %r, expected main" % branch)
            run(["git", "-C", clone, "merge", "--quiet", "--ff-only", "origin/main"])
            return run(["git", "-C", clone, "rev-parse", "HEAD"]).stdout.strip()
    except BacklogBusy:
        # The helper logged the bounded wait. No merge or failed-pass receipt:
        # readers use the clone's current tree until the next pass refreshes it.
        return run(["git", "-C", clone, "rev-parse", "HEAD"]).stdout.strip()


def load_item(clone, item_id):
    """(path, frontmatter, body, docs_model) for one backlog item.

    Under the Git authority the item is its Markdown file on origin/main in the
    owned clone. Under the Beads authority (ADR-0236) it is the client
    snapshot's row — no Markdown file is read — and the path is the row's
    legacy path under the clone, virtual after cutover. `docs_model` is the
    workspace's own parser and scope model either way."""
    import work_authority as wa
    if wa.beads(clone):
        row = wa.row(clone, item_id)
        if row is None:
            raise Refused("the work ledger has no record for %s" % item_id)
        fm, body = row.get("fields"), row.get("body")
        if not isinstance(fm, dict) or not isinstance(body, str) or not isinstance(row.get("path"), str):
            raise Refused("the work ledger record for %s is malformed" % item_id)
        if fm.get("doc_type") != "backlog":
            raise Refused("%s is not a backlog item" % item_id)
        sys.path.insert(0, os.path.join(clone, "scripts"))
        import docs_model as dm  # the workspace's own parser and scope model
        return os.path.join(clone, row["path"]), dict(fm), body, dm
    matches = glob.glob(os.path.join(clone, "docs/backlog/items", item_id + "-*.md"))
    if len(matches) != 1:
        raise Refused("expected exactly one backlog item file for %s on origin/main, found %d"
                      % (item_id, len(matches)))
    sys.path.insert(0, os.path.join(clone, "scripts"))
    import docs_model as dm  # the workspace's own parser and scope model
    with open(matches[0], encoding="utf-8") as source:
        text = source.read()
    fm, body, _problems = dm.split_frontmatter(text)
    if not isinstance(fm, dict):
        raise Refused("%s has no readable frontmatter" % matches[0])
    return matches[0], fm, body, dm


def item_identity(clone, rel_item, item_id):
    """(packet digest, claim commit) recorded on a work bead.

    Git: the item's blob at HEAD and the last commit that touched it. Beads:
    the ledger row's semantic digest and the ledger revision as
    `ledger:<revision>` — git may no longer hold the file at all."""
    import work_authority as wa
    if wa.beads(clone):
        row = wa.row(clone, item_id)
        if row is None:
            raise Refused("the work ledger has no record for %s" % item_id)
        return str(row.get("semantic_digest") or ""), "ledger:%s" % wa.ledger_revision(clone)
    digest = run(["git", "-C", clone, "rev-parse", "HEAD:" + rel_item]).stdout.strip()
    claim_commit = run(["git", "-C", clone, "log", "-1", "--format=%H", "--", rel_item]).stdout.strip()
    return digest, claim_commit


def parse_ts(value):
    text = str(value).strip().replace("Z", "+00:00")
    stamp = datetime.fromisoformat(text)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def check_eligible(item_id, fm, dm, rig, want, allowlist_path, clone=None):
    allowed = set()
    if os.path.isfile(allowlist_path):
        for line in open(allowlist_path, encoding="utf-8"):
            line = line.split("#", 1)[0].strip()
            if line:
                allowed.add(line)
    if item_id not in allowed:
        raise Refused("%s is not on the pilot allow-list (%s). The pilot runs only packets a "
                      "human has listed there." % (item_id, allowlist_path))
    check_kind(item_id, fm)

    ours = seat(rig)
    if fm.get("status") != "working" or not held_by_us(fm.get("claimed_by"), rig):
        raise Refused(
            "%s is not claimed for %s on origin/main (status=%s, claimed_by=%s).\n"
            "  Claim it on the existing path, land that on main, then admit again:\n"
            "    python3 scripts/next_work.py --claim %s --by %s --hours 12"
            % (item_id, ours, fm.get("status"), fm.get("claimed_by"), item_id, ours))
    holder = fm["claimed_by"]
    expires = fm.get("claim_expires")
    if not expires:
        raise Refused("%s carries no claim_expires" % item_id)
    left = parse_ts(expires) - datetime.now(timezone.utc)
    if left < MIN_CLAIM_LEFT:
        raise Refused("the claim on %s expires in %s; renew it before admitting "
                      "(next_work.py --renew %s --by %s)" % (item_id, left, item_id, holder))
    if clone is not None:
        check_lease(clone, item_id, holder)
    return check_scopes(item_id, fm, dm, want)


def check_lease(clone, item_id, holder):
    """Under the Beads authority a claim is a lease: the snapshot row must carry
    one for the holder (the seat string the row names — this factory's, or the
    two-part one of an older claim), or admission has nothing to bind the run
    to. Returns the lease id, or '' under Git."""
    import work_authority as wa
    if not wa.beads(clone):
        return ""
    lease = wa.lease_of(clone, item_id, holder)
    if not lease:
        raise Refused("%s carries no live lease for %s in the work ledger; claim it on the existing path "
                      "(next_work.py --claim) and admit again" % (item_id, holder))
    return lease


def check_kind(item_id, fm):
    if fm.get("doc_type") != "backlog":
        raise Refused("%s is not a backlog item" % item_id)
    if fm.get("gate") is True or str(fm.get("gate")).lower() == "true":
        raise Refused("%s is a gate item; gates are decided by people, not executed" % item_id)


def check_scopes(item_id, fm, dm, want):
    class _Rec:  # the owner model reads scopes off a record's frontmatter
        pass
    rec = _Rec()
    rec.fm = fm
    scopes = dm.write_scopes(rec)
    if not scopes:
        raise Refused("%s declares no write_scope; unscoped packets stay out of unattended "
                      "dispatch until a person has scoped them" % item_id)
    foreign = sorted({repo for repo, _ in scopes if repo != want})
    if foreign:
        raise Refused("%s writes outside %s (%s). Cross-repository packets are not admitted "
                      "in this version of the pilot." % (item_id, want, ", ".join(foreign)))
    # Reserved paths are refused, never filtered out: an item whose scope names
    # one was shaped wrong, and admitting the rest of it starts a worker on a job
    # its author did not mean.
    for repo, glob in scopes:
        why = path_rules.reserved_scope(repo, glob)
        if why:
            raise Refused("%s declares a write_scope no unattended packet may hold: %s"
                          % (item_id, why))
    return scopes


DESIGN_MOCKUP_RE = re.compile(r"^conforms:([A-Za-z0-9][A-Za-z0-9._-]*)$")


def check_design(item_id, fm, scopes, clone):
    """ADR-0150: a packet that can change what a Console operator sees builds to
    a mockup the owner agreed, and the agreement is still live.

    Returns "<page>@<digest>" for the check that runs later on the diff, or ""
    when nothing renders. This is the cheap refusal — before a worktree, before
    a bead, before a token. The same condition is refused again on the diff the
    worker produced, because a write scope is what a packet MAY touch and the
    diff is what it DID.

    Admission is the only place that can answer it: the item, the agreement
    registry and the packet are all visible here, and none of them is visible
    from a liskov-ui worktree.
    """
    if not any(path_rules.scope_renders(repo, glob) for repo, glob in scopes):
        return ""
    doctrine = "docs/meta/doctrine/ui-design-mockup.md"
    value = str(fm.get("design_mockup") or "").strip()
    if not value or value == "none":
        raise Refused(
            "%s can change what a Console operator sees, and declares design_mockup=%s. A material "
            "Console change builds to an owner-agreed mockup (ADR-0150, %s). `none` is not available "
            "to a rendering scope. Narrow the write_scope, or name the agreed page:\n"
            "    design_mockup: conforms:<page>"
            % (item_id, value or "nothing", doctrine))
    match = DESIGN_MOCKUP_RE.match(value)
    if not match:
        raise Refused("%s declares design_mockup=%r; expected conforms:<page> or none" % (item_id, value))
    page = match.group(1)

    sys.path.insert(0, os.path.join(clone, "scripts"))
    try:
        import importlib
        catalog = importlib.import_module("build_mockup_catalog")
        agreements = catalog.load_agreements()
    except Exception as exc:  # noqa: BLE001 - an unreadable registry refuses; it never allows
        raise Refused("%s builds to mockup %r, and the agreement registry could not be read (%s). "
                      "This check fails closed." % (item_id, page, exc))
    entry = agreements.get(page)
    if not entry:
        raise Refused(
            "%s builds to mockup %r, which the owner has not agreed. Only the owner adds an entry to "
            "mockups/liskov-console/agreed.toml, in a human-led session — nothing unattended can write "
            "that path (ADR-0150, %s)." % (item_id, page, doctrine))
    page_file = os.path.join(clone, "mockups", "liskov-console", "current", page + ".html")
    if not os.path.isfile(page_file):
        raise Refused("%s builds to mockup %r, which is not a page under mockups/liskov-console/current/"
                      % (item_id, page))
    try:
        actual = catalog.page_digest(catalog.Path(page_file))
    except Exception as exc:  # noqa: BLE001 - same rule: unreadable refuses
        raise Refused("%s builds to mockup %r, whose digest could not be computed (%s)" % (item_id, page, exc))
    if actual != str(entry.get("digest") or "").strip():
        raise Refused(
            "%s builds to mockup %r, whose design has CHANGED since the owner agreed it, so the "
            "agreement has lapsed. An agreement is for a design, not for a filename. The owner renews "
            "it with:\n    python3 scripts/build_mockup_catalog.py --digest %s"
            % (item_id, page, page))
    return "%s@%s" % (page, actual)


def preflight(item_id, fm, dm, want, backlog_clone):
    """Every refusal admission can make BEFORE a claim exists: the item's kind,
    its write scope, and the design it builds to. None of them reads the claim.

    The dispatcher claims on main and only then admits, so a refusal it could
    have had for free became a stranded claim: the item sat `working` for its
    whole claim with no worker, was refused again on every pass, and its write
    scope held its neighbours out of the queue (BKLG-20260907-gtys held
    BKLG-20260830-f2xd, 2026-09-20). The dispatcher asks this first, and claims
    only what passes. `main()` still runs the same checks after the claim: main
    can move between the two, and admission is where the refusal is final."""
    check_kind(item_id, fm)
    scopes = check_scopes(item_id, fm, dm, want)
    check_design(item_id, fm, scopes, backlog_clone)
    return scopes


def check_shaping_evidence(item_id, fm, body, dm, backlog_clone, *, rigs=None,
                           repo_roots=None, review=False, refreshed=None, refresh=True):
    """Verify present implementation receipts before claim and again at admission.

    Legacy packets remain admissible; shaping reports their missing receipt.
    Roots come only from configured owned rigs (or an explicit resolver in a
    test), never a path in the receipt. Fetches update remote refs only, are
    bounded, and are cached only by the caller's current dispatcher pass.
    """
    if lt.is_live_test(fm) or not re.search(r"(?m)^## Shaping evidence[ \t]*$", body):
        return
    corpus = dm.Corpus.load(os.path.join(backlog_clone, "docs"))
    options = dict(depends_on=fm.get("depends_on") or [],
                   dependency_status={key: dm.dependency_state(corpus, key)
                                      for key in corpus.by_id},
                   validation_commands=fm.get("validation") or [],
                   review_required=str(fm.get("review") or "").lower() == "required",
                   require=True)

    def refuse(findings):
        failures = [f for f in findings if f.severity in ("BLOCK", "WAIT")]
        if failures:
            raise Refused("%s shaping evidence: %s" % (item_id, "; ".join(
                "[%s] %s" % (f.code, f.message) for f in failures[:8])))

    # Validate before extracting repository names. No malformed receipt can
    # cause a fetch, and a correctly recorded pending dependency stays waiting.
    refuse(shaping_evidence.check_evidence(body, verify_sources=False, **options))
    content = shaping_evidence.section(body, "Shaping evidence").strip()
    receipt = json.loads(content.split("\n", 1)[1].rsplit("\n", 1)[0])
    if (receipt["review_required"] or options["review_required"]) and not review:
        raise Refused("%s shaping evidence requires an independent reviewer; admit with --review" % item_id)
    needed = {source["repo"] for source in receipt["sources"]}
    roots = dict(repo_roots or {})
    roots[BACKLOG_REPO] = backlog_clone
    if needed - roots.keys():
        for rig in sorted(rigs or {}):
            try:
                clone = owned_clone(rigs, rig)
                name = repo_name(clone)
            except Refused:
                continue
            if name in needed and name not in roots:
                roots[name] = clone
    missing = needed - roots.keys()
    if missing:
        raise Refused("%s shaping evidence has no configured owned repository: %s" %
                      (item_id, ", ".join(sorted(missing))))
    fetched = refreshed if refreshed is not None else set()
    if refresh:
        for name in sorted(needed):
            clone = roots[name]
            key = os.path.realpath(clone)
            if key in fetched:
                continue
            try:
                result = subprocess.run(
                    ["git", "-C", str(clone), "fetch", "--quiet", "--no-tags", "origin",
                     "refs/heads/main:refs/remotes/origin/main"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=30, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise Refused("%s shaping evidence source refresh unavailable for %s" % (item_id, name)) from exc
            if result.returncode:
                raise Refused("%s shaping evidence source refresh failed for %s" % (item_id, name))
            fetched.add(key)
    refuse(shaping_evidence.check_evidence(body, repo_roots=roots, **options))


def check_landing(landing, rig, rig_repo, rel_item, clone=None):
    """`main` lands and completes in one commit, so the item must live in the
    repository the packet works in — and that repository's own rules must land
    on main directly. Both are true of exactly one repository — and of one
    authority: that completing commit is a Markdown work-record write, which
    the Beads authority refuses (ADR-0236), so `main` is refused under it."""
    if landing != "main":
        return
    if clone is not None:
        import work_authority as wa
        wa.refuse_markdown_write(clone, "--landing main (the packet completes its item on the branch)")
    if rig_repo != BACKLOG_REPO:
        raise Refused("--landing main is only for the %s repository, which lands on main "
                      "directly and holds the backlog item the landing completes. Rig %r is %s: "
                      "use that repository's own path (pr or automerge)."
                      % (BACKLOG_REPO, rig, rig_repo or "an unidentified repository"))
    if not ITEM_PATH_RE.match(rel_item):
        raise Refused("item path %r is not one the landing check can be given safely" % rel_item)


def _rung(level):
    return lt.EXERCISE_LEVELS.index(level) if level in lt.EXERCISE_LEVELS else -1


def check_live(item_id, fm, scopes, livetest, exercise, landing=None, rig="this rig"):
    """Every refusal of a live test that needs no claim, the slot or the clock.
    Returns the recipe it would run.

    `livetest` is the rig's `[rigs.<rig>.livetest]` table as the policy loader
    completed it, or None when the rig has none — which is `enabled = false`.
    Each refusal names the condition it broke, and they run in an order that
    lets each be reached: the operator's request first, then the policy, then
    the record, and only then what observe V1 does not implement.
    """
    if landing is not None:
        raise Refused("%s is a live test (live_target %s), and --landing %s is refused: a live test has "
                      "no code to land. Its only writes are evidence, published by the completer; its "
                      "authorization is --exercise (ADR-0151 clause 1)"
                      % (item_id, lt._text(fm, "live_target"), landing))
    if exercise not in lt.EXERCISE_LEVELS:
        raise Refused("--exercise must be one of %s, got %r" % (" | ".join(lt.EXERCISE_LEVELS), exercise))
    if exercise == "spend" and not lt._text(fm, "spend_cap"):
        raise Refused("--exercise spend is refused: %s declares no spend_cap, and a spend is admitted only "
                      "under a cap that resolves to a fence production enforces (ADR-0151 clause 3)" % item_id)
    declared = lt.exercise(fm)
    if _rung(declared) < _rung(exercise):
        raise Refused("%s declares exercise %s, below the requested --exercise %s: a live test is admitted "
                      "at most at the level its item authorizes" % (item_id, declared or "nothing", exercise))
    if livetest is None:
        raise Refused("live testing is not enabled for %s: the rig is not in the policy at all" % rig)
    if livetest.get("enabled") is not True:
        # The loader completes an absent table with its defaults, so absent
        # and `enabled = false` arrive here identical — as they must behave.
        raise Refused("live testing is not enabled for %s: its [rigs.%s.livetest] table is absent or says "
                      "enabled = false. A deployed pack exercises nothing until an operator says so" % (rig, rig))
    target = lt._text(fm, "live_target")
    targets = livetest.get("targets") or []
    if not targets:
        raise Refused("%s's livetest.targets is EMPTY, which admits no target at all — an empty list here "
                      "means none, not \"no restriction\". Name %s there to admit it" % (rig, target))
    if target not in targets:
        raise Refused("%s targets %s, which %s's livetest.targets does not list (%s)"
                      % (item_id, target, rig, ", ".join(targets)))
    ceiling = livetest.get("max_exercise") or DEFAULT_EXERCISE
    if _rung(exercise) > _rung(ceiling):
        raise Refused("--exercise %s is above %s's livetest.max_exercise %s" % (exercise, rig, ceiling))
    errors = lt.record_errors(fm, scopes)
    if errors:
        raise Refused("%s is not a well-formed live test: %s" % (item_id, "; ".join(errors)))
    if not lt.is_authorized_exercise(exercise):
        raise Refused("--exercise %s is recognised but not implemented: observe V1 admits %s only, whatever "
                      "the policy allows. Mutate and spend also wait on actor attribution (ADR-0152)"
                      % (exercise, " | ".join(lt.AUTHORIZED_EXERCISES)))
    recipe = lt._text(fm, "live_recipe")
    if not recipe:
        raise Refused("%s names no live_recipe; only a registered recipe is ever executed" % item_id)
    recipes = livetest.get("recipes") or []
    if not recipes:
        raise Refused("%s's livetest.recipes is EMPTY, which admits no recipe — an empty list here means "
                      "none, not \"no restriction\". Name %s there to admit it" % (rig, recipe))
    if recipe not in recipes:
        raise Refused("%s runs recipe %s, which %s's livetest.recipes does not list (%s)"
                      % (item_id, recipe, rig, ", ".join(recipes)))
    if not lt._text(fm, "live_expected_build"):
        raise Refused("%s names no live_expected_build; preflight has nothing to hold the deployed build to"
                      % item_id)
    if not scopes:
        raise Refused("%s declares no write_scope; a live test declares the docs/ evidence scope its run "
                      "writes (ADR-0151 clause 2)" % item_id)
    return recipe


def live_manifest(item_id, fm, digest, recipe, scopes, now):
    """The run this admission freezes: manifest (with its digest) and run id.
    Everything in it comes from the item, trusted checked-in code, and `now`."""
    import livetest_evidence as le
    import livetest_state as ls
    target = lt._text(fm, "live_target")
    admitted = now.replace(microsecond=0)
    admitted_at = ls.instant(admitted)
    timeout = int(lt._text(fm, "live_timeout_seconds") or lt.OBSERVE_TIMEOUT_SECONDS)
    max_age = int(lt._text(fm, "live_max_age_seconds") or lt.OBSERVE_MAX_AGE_SECONDS)
    profile = ls.CREDENTIAL_PROFILES.get(target)
    if not profile:
        raise Refused("no credential profile is configured for target %s in trusted code" % target)
    try:
        run_id = ls.new_run_id(item_id, digest, recipe, admitted_at)
        paths = le.output_paths(item_id, run_id, admitted_at)
        manifest = ls.build_manifest(
            run_id=run_id, item_id=item_id, item_blob_sha=digest, exercise=lt.exercise(fm), recipe=recipe,
            target=target, expected_build=lt._text(fm, "live_expected_build"), admitted_at=admitted_at,
            expires_at=ls.instant(admitted + timedelta(seconds=timeout)), max_age_seconds=max_age,
            credential_profile=profile, output_paths=paths)
    except (ls.Refused, le.EvidenceRefused) as why:
        raise Refused("the run manifest for %s cannot be frozen — %s" % (item_id, why))
    outside = [p for p in paths if not _covered(p, scopes)]
    if outside:
        raise Refused("%s's write_scope does not cover the evidence this run writes (%s); its claim must "
                      "hold what its run produces" % (item_id, ", ".join(outside)))
    return manifest


def _covered(path, scopes):
    import docs_model as dm
    return dm.scopes_overlap([(lt.ORCHESTRATOR_REPO, path)], list(scopes))


def live_slot(city):
    """(free, why not) for the city's one live slot, read without writing
    anything: the lock file only ever comes into being with the slot record, by
    a transition, so a dry run's look creates no file. An unreadable or
    uncertain slot is never free."""
    import livetest_state as ls
    store = ls.RunStore(city)
    if not os.path.exists(store.slot_path):
        return True, ""
    try:
        slot = store.slot()
    except ls.Refused as why:
        return False, "the live slot record cannot be read (%s: %s), so it counts as held" % (why.code, why.detail)
    if slot.needs_intervention:
        return False, "the live slot NEEDS INTERVENTION — run %s (%s) holds it: %s" % (
            slot.run_id, slot.item_id or "?", slot.detail)
    if slot.held:
        return False, "held by %s (run %s) since %s" % (slot.item_id or "?", slot.run_id, slot.acquired_at)
    return True, ""


def slot_line(city):
    free, why = live_slot(city)
    return "free" if free else why


# A member repository's own doctrine router, where it has one. The orchestrator's
# is scripts/agent_doctrine.py and is always consulted.
MEMBER_ROUTERS = ("scripts/liskov/doctrine-router.py",)
MAX_ROUTED_PATHS = 1500
# Log-only, like `proof.judge.*`: one key per judged clause, so the corpus of
# intent judgements builds itself and can be scored later against what the
# merged diff of the same packet raised.
DOCTRINE_META = "proof.doctrine."


def scope_paths(clone, patterns):
    """Concrete paths a write_scope covers: the files it matches at the clone's
    HEAD, plus each pattern made literal for a file that does not exist yet. One
    representative per (directory, extension) — a router matches globs, so more
    than one file of a kind in a directory tells it nothing new. Patterns are
    backlog text: they reach git only as argv, behind `--`."""
    paths = []
    for pattern in patterns:
        out = run(["git", "-C", clone, "ls-files", "-z", "--", ":(glob)" + pattern], check=False)
        paths += [p for p in out.stdout.split("\0") if p]
        paths.append(re.sub(r"[*]+", "x", pattern).replace("?", "x"))
    seen, keep = set(), []
    for path in paths:
        key = (os.path.dirname(path), os.path.splitext(path)[1])
        if key not in seen:
            seen.add(key)
            keep.append(path)
    return keep[:MAX_ROUTED_PATHS]


def judged_reading(backlog_clone, doc, meta):
    """The clauses no path can decide, as they reach the bead.

    Three shapes, and only the first is new. A judgement that was made lists the
    bodies it RAISED as required reading with their probability, and names the
    rest on one line so a reader can still see what was considered. A judgement
    that could not be made prints exactly the list this bead carried before —
    all four clauses and "read the body if the trigger describes your change" —
    plus the reason. That list is the behaviour ADR-0144 replaced for a diff and
    ADR-0149 replaced here; it stays as the fail-open path, and it is the shape a
    bead must still get when there is no key or no route to the model.
    """
    judged = doc.get("judged") or []
    judgement = doc.get("judgement") or {}
    if not judged:
        return ["Workspace doctrine no path can decide: every judgement body was already "
                "raised by a path."]

    def where(entry):
        return os.path.join(backlog_clone, entry["target"])

    if not judgement.get("evaluated"):
        reason = judgement.get("reason") or "no reason recorded"
        meta[DOCTRINE_META + "error"] = reason
        return (["Workspace doctrine no path can decide — NOT evaluated (%s). Read the body if the "
                 "trigger" % reason, "describes your change:"]
                + ["  - `%s` — %s: %s" % (where(e), e["title"], e["trigger"]) for e in judged])

    meta[DOCTRINE_META + "evidence"] = str(judgement.get("evidence") or "")
    meta[DOCTRINE_META + "model"] = str(judgement.get("model") or "")
    meta[DOCTRINE_META + "ms"] = str(judgement.get("elapsedMs") or 0)
    for entry in judged:
        if isinstance(entry.get("noul"), (int, float)):
            meta[DOCTRINE_META + question_id(entry["target"])] = "%.2f" % entry["noul"]

    raised = [e for e in judged if e.get("raised")]
    rest = [e for e in judged if not e.get("raised")]
    lines = ["Workspace doctrine no path can decide, judged against THIS item's work order "
             "(raise at %.2f)" % (judgement.get("threshold") or 0),
             "— read each BEFORE changing anything:"]
    lines += ["  - `%s` — %s (p %.2f): %s" % (where(e), e["title"], e["noul"], e["trigger"])
              for e in raised] or ["  - none"]
    if rest:
        lines += ["", "  judged not to apply (p < %.2f): %s"
                  % (judgement.get("threshold") or 0,
                     ", ".join("%s %.2f" % (question_id(e["target"]), e["noul"]) for e in rest))]
    lines += ["", "  A probability is not permission. It chose what to put in front of you; a body it "
              "raised", "  governs this work, and one it did not is still yours to read if you know "
              "better."]
    return lines


def question_id(target):
    """The stem the router asks each clause under — the bead's metadata key too."""
    return os.path.splitext(os.path.basename(target))[0]


def required_reading(backlog_clone, rig_clone, repo, scopes, same_repo, item=None):
    """The conditional doctrine this packet's write_scope selects, chosen by the
    routers — not left to a worker remembering to run them. Returns (text, metadata).

    A worker in a rig is not in the workspace root: nothing of the constitution
    is on its path, and in the first dispatched batch no worker opened a single
    workspace doctrine body. So the bodies are chosen here, two ways. By path,
    as before. And for the clauses no path can decide, by judging what this item
    says the work will DO once delivered — admission has no diff, the item is all
    there is, and printing all four clauses on every bead re-created the very
    behaviour ADR-0144 replaced (ADR-0149).

    The metadata returned is log-only. A router that fails never blocks
    admission; the bead then says so and carries the list it always carried.
    """
    paths = scope_paths(rig_clone, [pattern for _repo, pattern in scopes])
    lines = ["## Required reading — chosen by script from this packet's write_scope", ""]
    meta = {}
    sys.path.insert(0, os.path.join(backlog_clone, "scripts"))
    import docs_model as dm
    companions = scope_companions.inspect(rig_clone,
                                          lambda p: dm.scopes_overlap([(repo, p)], scopes))
    if companions:
        lines += ["Scope companion preflight — resolve during load-context, BEFORE implementing:",
                  *("  - " + hint for hint in companions), ""]
    routed = [p if same_repo else "repos/%s/%s" % (repo, p) for p in paths]
    argv = [sys.executable, "scripts/agent_doctrine.py", "--json", "--for", *routed]
    intent = None
    if item is not None:
        # The item is untrusted text and travels as JSON state built from
        # fields; the router never pastes it into a question. It goes in on
        # stdin rather than argv: a packet body is not an argument list.
        intent = json.dumps({"title": str(item.get("title") or ""),
                             "write_scope": [str(p) for p in (item.get("write_scope") or [])],
                             "body": item.get("body") or ""})
        argv += ["--intent", "-"]
    out = run(argv, cwd=backlog_clone, check=False, input=intent)
    try:
        doc = json.loads(out.stdout) if out.returncode == 0 else None
    except ValueError:
        doc = None
    if doc is None:
        meta[DOCTRINE_META + "error"] = "the workspace doctrine router failed at admission"
        lines += ["The workspace doctrine router FAILED at admission. Read the `Conditional doctrine` table in",
                  "`%s` yourself and every body whose trigger your change matches." % os.path.join(backlog_clone, "AGENTS.md"), ""]
    else:
        lines.append("Workspace doctrine whose path trigger this write_scope matches — read each BEFORE changing anything:")
        lines += ["  - `%s` — %s (matched `%s`)" % (os.path.join(backlog_clone, body["target"]), body["title"],
                                                   (body.get("matched") or ["?"])[0])
                  for body in doc.get("applied") or []] or ["  - none"]
        lines += [""] + judged_reading(backlog_clone, doc, meta) + [""]
    own_routers = [] if same_repo else [r for r in MEMBER_ROUTERS if os.path.isfile(os.path.join(rig_clone, r))]
    for router in own_routers:
        own = run([sys.executable, router, "--for", *paths], cwd=rig_clone, check=False)
        text = (own.stdout if own.returncode == 0 else own.stdout + own.stderr).strip().splitlines()
        lines += ["This repository's own router (`%s`), for the same paths — its bodies are relative to" % router,
                  "your worktree:", "", *("    " + line for line in text[:60]), ""]
    lines += ["Those bodies were chosen from the scope this item DECLARES and from what it says the work will",
              "do. Before you close `implement`, ask the same question about what you ACTUALLY changed — and",
              "give the router the diff, because without one it can only list the clauses again:",
              "",
              "    git diff origin/main...HEAD | python3 %s \\" % os.path.join(backlog_clone, "scripts/agent_doctrine.py"),
              "        --for <the paths you actually changed> --diff -",
              "",
              "Read anything new it names."]
    if own_routers:
        lines += ["", "This repository's own router takes `--for <paths>` only; run it again the same way."]
    elif not same_repo:
        # Most member repositories have no router of their own; saying "this
        # repository's own router" to one of them sent a worker hunting for a
        # file that never existed (BKLG-20261004-cowc).
        lines += ["", "This repository has no doctrine router of its own, and none is expected: the workspace",
                  "router above, run from the orchestrator clone, is the only one. Do not look for it, or for",
                  "`scripts/agent_doctrine.py`, in your worktree."]
    if not same_repo and not os.path.isfile(os.path.join(rig_clone, "AGENTS.md")):
        lines += ["", "This repository has no `AGENTS.md`, and that is expected — it is not a missing rule set and",
                  "not a reason to stop. Its rules are the workspace rules in `%s`, this bead,"
                  % os.path.join(os.path.dirname(os.path.normpath(rig_clone)), "CLAUDE.md"),
                  "the item, and the doctrine above; its validation is the item's `validation` and the gate."]
    return "\n".join(lines), meta


def bead_body(item_id, item_path, rel_item, main_sha, work_dir, branch, landing, same_repo, reading="",
              archive="", default_scope=()):
    """What the worker and the reviewer are told. The item is named by ABSOLUTE
    path: a packet for any repository but the orchestrator runs in a clone that
    does not contain the backlog, and a relative path there points at nothing."""
    where = ("It is `%s` in this repository, as of %s." % (rel_item, main_sha[:10]) if same_repo else
             "It lives in the orchestrator repository, not in the one you are changing. Read it from "
             "the Gas City-owned orchestrator clone, which is at %s and is READ-ONLY for this packet — "
             "its `reads` and links resolve relative to that file." % main_sha[:10])
    completion = ("When the workflow's `land` step tells you to, complete the backlog item on the "
                  "packet branch; the orchestrator, not you, pushes to main."
                  if landing == "main" else
                  "Never edit the backlog item or mark it done: completion waits for a person who "
                  "has seen the change land.")
    retained = ("\n\nAn earlier attempt left uncommitted changes in this worktree. Admission archived "
                "them, untracked files included, as the tag `%s` — a commit off the branch, never part "
                "of it — and restored the worktree to its last commit; `git show %s` reads it."
                % (archive, archive) if archive else "")
    views = ("\n\nIn scope by default, whatever the item's `write_scope` lists: the generated views "
             "`python3 scripts/validate_docs.py --refresh-epic-views` rewrites (%s). After staging your "
             "records, run the refresh and stage exactly the views it reports; they are never a reason "
             "to stop. The default covers the refresh output only: edit hand-written text in them only "
             "when the item asks for it."
             % ", ".join("`%s`" % glob for _repo, glob in default_scope) if default_scope else "")
    return ("Execute backlog packet %s.\n\n"
            "The backlog item is the authority for what to do and how it is accepted:\n\n"
            "    %s\n\n"
            "%s\n\n"
            "Read it, every document in its `reads`, and the capsule its epic names, before "
            "changing anything. Work only inside the packet worktree `%s` on branch `%s`.%s\n\n"
            "Landing authorization for this packet: **%s**. Anything beyond that is not yours "
            "to do. %s%s\n%s" % (item_id, item_path, where, work_dir, branch, views, landing, completion,
                                 retained, "\n" + reading + "\n" if reading else ""))


def judge_item(fm, body, backlog_clone, opener=None):
    """The four deliverability Nouls about one item. Returns (metadata, line).

    LOG ONLY. Nothing here or anywhere else reads these values back to decide
    something: admission's refusals, the dispatcher's order and this command's
    exit code are exactly what they were. They are stamped so that in a few
    weeks there is a labelled corpus an order of magnitude larger than the
    seventeen items a threshold cannot honestly be set from today — the one
    question that calibrated, and the three that did not, are in
    `judge.DELIVERABILITY`.

    The item is untrusted text and reaches the model as data, never as part of
    a question. A judgement that could not be made is a reason on the bead and
    a line in the printed plan; it never refuses and never raises.
    """
    values, reason, elapsed = judge.ask(
        judge.item_state(fm.get("title"), fm.get("write_scope"), body),
        judge.DELIVERABILITY,
        # The client lives in the Gas City-owned orchestrator clone, and so
        # does the key file, when the key is not in the environment.
        search_dirs=[os.path.join(backlog_clone, "scripts")],
        key_roots=[backlog_clone],
        opener=opener,
    )
    meta = {"proof.judge.ms": str(elapsed)}
    if values is None:
        meta["proof.judge.error"] = reason
        return meta, "not evaluated — %s" % reason
    meta["proof.judge.model"] = judge.MODEL
    for qid, value in values.items():
        meta["proof.judge." + qid] = "%.2f" % value
    return meta, "%s  (%s, %d ms)" % (
        "  ".join("%s %.2f" % (qid, values[qid]) for qid in judge.DELIVERABILITY if qid in values),
        judge.MODEL, elapsed)


def prior_work(work_dir, base="origin/main"):
    """HEAD of an existing packet worktree when it is ahead of base, else ''."""
    if not os.path.isdir(work_dir):
        return ""
    ahead = run(["git", "-C", work_dir, "rev-list", "--count", base + "..HEAD"])
    if ahead.stdout.strip() == "0":
        return ""
    head = run(["git", "-C", work_dir, "rev-parse", "HEAD"]).stdout.strip()
    return head if re.fullmatch(r"[0-9a-f]{40}", head) else ""


def slugify(title, limit=36):
    slug = re.sub(r"[^a-z0-9]+", "-", str(title).lower()).strip("-")
    if len(slug) > limit:
        cut = slug[:limit]
        # end on a whole word when that costs little, not mid-word
        slug = cut[:cut.rfind("-")] if cut.rfind("-") >= limit - 12 else cut
    return slug.strip("-") or "packet"


def existing_beads(city, rig, item_id, include_superseded=False, include_workflows=False):
    """Packet provenance; optionally include workflow roots from the same snapshot."""
    out = gc(city, "bd", "--rig", rig, "list", "--all", "--json", "--limit=0")
    try:
        beads = json.loads(out.stdout)
    except ValueError as exc:
        raise Refused("cannot read prior packet provenance") from exc
    if not isinstance(beads, list):
        raise Refused("prior packet provenance is not a bead list")
    # Superseded executions still own retained work. They do not prevent a new
    # admission, but their recorded identity must survive a changed item title.
    return [b for b in beads
            if ((b.get("external_ref") or "") == item_id
                or (b.get("metadata") or {}).get("proof.packet_id") == item_id
                or (include_workflows and (b.get("metadata") or {}).get("gc.kind") == "workflow"
                    and (b.get("metadata") or {}).get("gc.var.packet_id") == item_id))
            and (include_superseded or str((b.get("metadata") or {}).get("proof.superseded")).lower() != "true")]  # bd stores a boolean


RETAINED_TAG = "proof/retained/%s/%s"
# The archive is admission's commit, not a worker's, and must not wait on a
# signing agent or a hook: plumbing only, under a fixed identity.
ARCHIVE_GIT = ["-c", "user.name=Gas City admission", "-c", "user.email=admission@gascity.invalid"]


def retained_tag(item_id, instant):
    """The archive's tag: deterministic from the packet and the admission instant."""
    return RETAINED_TAG % (item_id, instant.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))


def archive_retained(work_dir, tag):
    """Archive a dirty worktree to `tag`, then return it to its committed HEAD.

    The commit holds the working tree as it is, untracked files included, and
    is built in a scratch index with `commit-tree`: HEAD, the branch ref, the
    worktree's own index and the shared stash are never written, so the
    branch tip afterwards is the last thing a worker committed. Nothing is
    removed until the tag resolves to that commit."""
    git = ["git", "-C", work_dir]
    head = run(git + ["rev-parse", "HEAD"]).stdout.strip()
    with tempfile.TemporaryDirectory() as scratch:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(scratch, "index"))
        run(git + ["read-tree", head], env=env)
        run(git + ["add", "-A"], env=env)
        tree = run(git + ["write-tree"], env=env).stdout.strip()
    commit = run(git + ARCHIVE_GIT + ["commit-tree", "--no-gpg-sign", tree, "-p", head, "-m",
                                      "WIP: uncommitted work retained in %s, archived by admission"
                                      % os.path.basename(work_dir)]).stdout.strip()
    run(git + ["tag", tag, commit])
    if run(git + ["rev-parse", "--verify", "refs/tags/%s^{commit}" % tag]).stdout.strip() != commit:
        raise Refused("the retained archive %s does not resolve to %s; the worktree is left as it was"
                      % (tag, commit[:12]))
    # `reset --hard HEAD` rewrites the working tree, not the ref, and `clean`
    # without -x leaves ignored files, which `status` never counted as dirt.
    run(git + ["reset", "-q", "--hard", "HEAD"])
    run(git + ["clean", "-fdq"])
    if run(git + ["rev-parse", "HEAD"]).stdout.strip() != head:
        raise Refused("the retained worktree's HEAD moved while it was archived as %s" % tag)
    left = run(git + ["status", "--porcelain", "--untracked-files=all"]).stdout.strip()
    if left:
        raise Refused("archived the retained worktree as %s, but it is still not clean: %s"
                      % (tag, left.splitlines()[0]))


def packet_workspace(rig_clone, history, live, work_dir, branch, archive_as=None):
    """Recover only a recorded packet workspace; never adopt unrelated commits.

    A recorded worktree with uncommitted changes is archived to the tag
    `archive_as` and recovered from its last commit; without `archive_as` it
    is refused as `Dirty`, and nothing in it is changed."""
    candidates = live or [b for b in history if os.path.isdir(
        (b.get("metadata") or {}).get("work_dir") or "")]
    identities = {(m.get("work_dir"), m.get("proof.branch"))
                  for b in candidates for m in [b.get("metadata") or {}]
                  if m.get("work_dir") and m.get("proof.branch")}
    recorded = bool(identities)
    if candidates and not recorded:
        raise Refused("prior packet has no complete workspace identity")
    if len(identities) > 1:
        raise Refused("multiple retained packet workspaces; reconcile them before admission")
    if identities:
        work_dir, branch = identities.pop()
    if not os.path.exists(work_dir):
        return work_dir, branch, ""
    root = os.path.realpath(os.path.join(rig_clone, ".worktrees"))
    if os.path.dirname(os.path.realpath(work_dir)) != root:
        raise Refused("recorded packet workspace is outside this rig's .worktrees")
    actual = run(["git", "-C", work_dir, "branch", "--show-current"]).stdout.strip()
    if actual != branch:
        raise Refused("recorded packet branch does not match its worktree")
    common = run(["git", "-C", work_dir, "rev-parse", "--path-format=absolute", "--git-common-dir"]).stdout.strip()
    owner = run(["git", "-C", rig_clone, "rev-parse", "--path-format=absolute", "--git-common-dir"]).stdout.strip()
    if os.path.realpath(common) != os.path.realpath(owner):
        raise Refused("recorded packet workspace belongs to another repository")
    if run(["git", "-C", work_dir, "status", "--porcelain", "--untracked-files=all"]).stdout.strip():
        if not recorded:
            raise Refused("unrecorded packet workspace has uncommitted changes; reconcile its ownership first")
        if not archive_as:
            raise Dirty("retained packet workspace has uncommitted changes; preserve and reconcile them",
                        work_dir, branch)
        archive_retained(work_dir, archive_as)
    commit = prior_work(work_dir)
    if commit and not recorded:
        raise Refused("unrecorded packet workspace carries commits; reconcile its ownership first")
    return work_dir, branch, commit


def now():
    """The admission instant. A seam, so a test freezes a manifest without a clock."""
    return datetime.now(timezone.utc)


def rig_livetest(city, rig):
    """The rig's `[rigs.<rig>.livetest]` table, completed by the one policy
    loader, or None when the rig is not in the policy at all."""
    try:
        import factory_lib as fl
        policy = fl.load_policy(city)
    except Exception as exc:  # noqa: BLE001 - an unreadable policy refuses; it never enables
        raise Refused("the factory policy could not be read (%s); no live test is admitted" % exc)
    cfg = policy["rigs"].get(rig)
    return None if cfg is None else cfg["livetest"]


def live_bead_body(item_id, item_path, rel_item, main_sha, work_dir, manifest, city, tester):
    """What the tester is told. It names the run, its manifest digest and its
    journal, so nothing about the run is the tester's to invent."""
    journal = os.path.join(city, "proof", "live-runs", manifest["run_id"])
    return ("Execute live-test packet %s against `%s` at --exercise %s, as `%s`.\n\n"
            "The backlog item is the authority for what is tested and how it is accepted:\n\n"
            "    %s\n\n"
            "It is `%s` in this repository, as of %s.\n\n"
            "Admitted run `%s`, recipe `%s`, expected build `%s`.\n"
            "Manifest digest `%s`; its journal is `%s`. The orchestrator's checks read both — an edited\n"
            "manifest refuses. The run window closes at %s.\n\n"
            "This is a live test (ADR-0151): there is no code to change, nothing to commit and nothing to "
            "push. The orchestrator runs preflight, the observation and settle; your prose marks nothing "
            "passed. The only documents this run produces are its evidence, published by the completer:\n\n"
            "%s\n\n"
            "Work only inside `%s`. On an unexpected reading, capture it and stop — a tester reports; it "
            "does not fix.\n"
            % (item_id, manifest["target"], manifest["exercise"], tester, item_path, rel_item, main_sha[:10],
               manifest["run_id"], manifest["recipe"], manifest["expected_build"], manifest["digest"], journal,
               manifest["expires_at"], "\n".join("    " + p for p in manifest["output_paths"]), work_dir))


SLOT_TAKEN = ("slot_held", "slot_uncertain")  # acquire's refusals that mean "wait", not "never"
ADMISSION_MARK = "admission"


def first_admission(store, run_id):
    """True for exactly one admission of `run_id`.

    A run id is derived from the admission second, so two admissions of the
    same item in the same second freeze the SAME run, and `acquire` — for which
    re-acquiring one's own run is idempotent recovery — lets both through. The
    tie is broken here, after the slot is ours, by an exclusive create in the
    run's own directory: atomic on any local filesystem, with no second lock.
    """
    path = os.path.join(store.runs_root, run_id, ADMISSION_MARK)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return False
    try:
        os.write(fd, ("%s pid %d\n" % (datetime.now(timezone.utc).isoformat(), os.getpid())).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def _stranded(run_id, what, why):
    return Refused("%s failed after run %s took the live slot — %s. Nothing contacted production, and the slot "
                   "stays HELD until a person settles or retires that run: elapsed time never frees it. "
                   "`gc proof status` names the holder." % (what, run_id, why))


def admit_live(args, city, rig_clone, backlog_clone, main_sha, item_path, rel_item, fm, dm):
    """Admission of a live test: `mol-proof-livetest`, never `mol-proof-packet`.

    The slot is taken before anything names the run, and it is the one atomic
    guard against a duplicate: two admissions of live tests — of the same item
    or of two — race inside `RunStore.acquire`'s lock, and exactly one wins."""
    import livetest_state as ls
    level = args.exercise or DEFAULT_EXERCISE
    scopes = check_eligible(args.item, fm, dm, args.rig, repo_name(rig_clone),
                            os.path.join(city, "proof", "pilot-allowlist.txt"), backlog_clone)
    recipe = check_live(args.item, fm, scopes, rig_livetest(city, args.rig), level, args.landing, args.rig)
    if args.review:
        raise Refused("--review is for an implementation packet; a live test has no diff to review")
    digest, claim_commit = item_identity(backlog_clone, rel_item, args.item)
    prior = existing_beads(city, args.rig, args.item)
    if prior:
        raise Refused("%s was already admitted as %s. A live test is never re-admitted onto an earlier run: a "
                      "rerun is a new run id, admitted explicitly once a person has retired that bead"
                      % (args.item, ", ".join(b["id"] for b in prior)))
    manifest = live_manifest(args.item, fm, digest, recipe, scopes, now())
    tester = TESTER % level
    name = "%s-%s" % (args.item.lower(), slugify(fm.get("title", "")))
    work_dir, branch, prior_commit = packet_workspace(rig_clone, [], [], os.path.join(rig_clone, ".worktrees", name),
                                                      "agent/BKLG-" + name.removeprefix("bklg-"))
    if prior_commit:
        raise Refused("the packet worktree %s carries commits, and a live test changes no code" % work_dir)

    print("\n".join([
        "packet      %s  (%s)" % (args.item, fm.get("title", "")),
        "type        live test — %s, never %s" % (LIVE_FORMULA, FORMULA),
        "item        %s @ %s (blob %s)" % (rel_item, main_sha[:10], digest[:10]),
        "claim       %s until %s (commit %s)" % (fm.get("claimed_by"), fm.get("claim_expires"), claim_commit[:10]),
        "rig         %s -> %s" % (args.rig, rig_clone),
        "worktree    %s" % work_dir,
        "exercise    %s  (tester %s)" % (level, tester),
        "target      %s  recipe %s  expected build %s" % (manifest["target"], recipe, manifest["expected_build"]),
        "run         %s  window %s .. %s" % (manifest["run_id"], manifest["admitted_at"], manifest["expires_at"]),
        "evidence    %s" % ", ".join(manifest["output_paths"]),
        "live slot   %s" % slot_line(city),
    ]))
    if args.dry_run:
        print("\ndry run: nothing was changed — no slot taken, no run recorded")
        return 0

    if not os.path.isdir(work_dir):
        run([os.path.join(backlog_clone, "scripts", "new-agent-worktree.sh"),
             "--repo", rig_clone, "--name", name, "--branch", branch])
    if not os.path.isdir(work_dir):
        raise Refused("the worktree script did not create %s" % work_dir)

    store = ls.RunStore(city)
    try:
        store.acquire(manifest)
    except ls.Refused as why:
        if why.code not in SLOT_TAKEN:
            raise Refused("the live slot refused run %s — %s. Nothing was created" % (manifest["run_id"], why))
        raise Waits("%s waits: the city's one live slot is not free (%s). Nothing was created"
                    % (args.item, why))
    run_id = manifest["run_id"]
    if not first_admission(store, run_id):
        raise Refused("run %s of %s is already being admitted: another admission of the same item in the same "
                      "second froze the same run and holds the slot for it. This one created nothing"
                      % (run_id, args.item))
    print("slot        taken by run %s" % run_id)

    body = live_bead_body(args.item, item_path, rel_item, main_sha, work_dir, manifest, city, tester)
    meta = {"proof.packet_id": args.item, "proof.packet_digest": digest, "proof.claim_commit": claim_commit,
            "proof.branch": branch, "proof.exercise": level, "proof.live_target": manifest["target"],
            "proof.live_run_id": run_id, "proof.live_manifest_digest": manifest["digest"], "work_dir": work_dir}
    lease = check_lease(backlog_clone, args.item, fm["claimed_by"])
    if lease:
        meta[LEASE_KEY] = lease
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(body)
    try:
        out = gc(city, "bd", "--rig", args.rig, "create", str(fm.get("title", args.item)),
                 "--external-ref", args.item, "--body-file", fh.name, "--metadata", json.dumps(meta), "--json")
        created = json.loads(out.stdout)
        bead_id = (created[0] if isinstance(created, list) else created)["id"]
    except (Refused, ValueError, KeyError, IndexError, TypeError) as why:
        raise _stranded(run_id, "creating the work bead", why)
    finally:
        os.unlink(fh.name)
    print("created     %s" % bead_id)

    sling = ["sling", "%s/%s" % (args.rig, tester), bead_id, "--on", LIVE_FORMULA,
             "--var", "packet_id=" + args.item, "--var", "work_dir=" + work_dir,
             "--var", "live_target=" + manifest["target"], "--var", "tester_target=" + tester,
             "--var", "mutate=" + ("true" if _rung(level) >= _rung("mutate") else "false"),
             "--var", "spend=" + ("true" if level == "spend" else "false")]
    try:
        out = gc(city, *sling)
    except Refused as why:
        raise _stranded(run_id, "slinging %s" % LIVE_FORMULA, why)
    print(out.stdout.strip())
    root = re.search(r"workflow (\S+)", out.stdout or "")
    if not root:
        raise _stranded(run_id, "finding the workflow root", "sling printed no workflow id")
    # The ONE place the orchestrator's checks look for the run (livetest_runner
    # ROOT_RUN_KEY). Checked, not best-effort: a root that names no run settles
    # nothing, and its run would hold the slot unseen.
    try:
        gc(city, "bd", "--rig", args.rig, "update", root.group(1),
           "--set-metadata", "proof.live_run_id=" + run_id,
           "--set-metadata", "gc.work_dir=" + work_dir,
           "--set-metadata", "proof.repo=" + (repo_name(rig_clone) or ""),
           "--set-metadata", "proof.exercise=" + level,
           "--set-metadata", "proof.write_scope=" + "\n".join("%s:%s" % s for s in scopes))
    except Refused as why:
        raise _stranded(run_id, "naming the run on workflow %s" % root.group(1), why)
    return 0


def write_admission_record(city, rig, root_id, work_dir, branch, repo, design, review, scopes):
    """One root-record writer, used after sling and when a pass repairs that write.

    A workflow already exists: a failed write is visible and retryable on the
    next pass, without changing admission's running/success exit code.
    """
    try:
        out = gc(city, "bd", "--rig", rig, "update", root_id,
                 "--set-metadata", "gc.work_dir=" + work_dir,
                 "--set-metadata", "gc.work_branch=" + branch,
                 "--set-metadata", "proof.repo=" + (repo or ""),
                 "--set-metadata", "proof.design_mockup=" + design,
                 "--set-metadata", "proof.review=" + ("on" if review else "off"),
                 "--set-metadata", "proof.write_scope=" + "\n".join(
                     "%s:%s" % (owner, glob) for owner, glob in scopes), check=False)
        written = out.returncode == 0
    except (Unavailable, OSError):
        written = False
    if not written:
        print("gc proof admit: workflow %s admission record write failed; the next pass retries it"
              % root_id, file=sys.stderr)
    return written


def main(argv):
    ap = argparse.ArgumentParser(prog="gc proof admit", description=__doc__.split("\n\n")[0])
    ap.add_argument("item", help="backlog item id, e.g. BKLG-20260918-ab12")
    ap.add_argument("--rig", required=True, help="rig that will execute the packet")
    ap.add_argument("--landing", choices=sorted(LANDING),
                    help="what an implementation packet is authorized to do once approved; required for "
                         "one, and there is no default. A live test takes none")
    ap.add_argument("--exercise", choices=lt.EXERCISE_LEVELS,
                    help="a live test only: how far into production it may go (ADR-0151); default %s"
                         % DEFAULT_EXERCISE)
    ap.add_argument("--review", action="store_true",
                    help="add an independent review before anything outward; off by default")
    ap.add_argument("--dry-run", action="store_true", help="verify and print the plan; change nothing")
    # gc forwards its own global flags into a pack command's argv, so
    # `gc --city DIR proof admit ...` arrives here as `--city DIR ...`. Without
    # this, argparse takes DIR as the item id.
    ap.add_argument("--city", help="city directory (also accepted from gc's global --city flag)")
    args = ap.parse_args(argv)

    if not ID_RE.match(args.item):
        raise Refused("%r is not a backlog item id" % args.item)
    city = args.city or city_root()
    if not os.path.isfile(os.path.join(city, "city.toml")):
        raise Refused("%s is not a city directory (no city.toml)" % city)
    rigs = load_rigs(city)
    rig_clone = healthy_clone(rigs, args.rig)
    backlog_clone = backlog_clone_of(rigs)

    main_sha = refresh_backlog_clone(backlog_clone)
    item_path, fm, item_body, dm = load_item(backlog_clone, args.item)
    rel_item = os.path.relpath(item_path, backlog_clone)
    check_shaping_evidence(args.item, fm, item_body, dm, backlog_clone, rigs=rigs,
                           review=args.review, refreshed={os.path.realpath(backlog_clone)})
    if lt.is_live_test(fm):
        # Never an implementation packet, whatever else was asked for.
        return admit_live(args, city, rig_clone, backlog_clone, main_sha, item_path, rel_item, fm, dm)
    if args.exercise is not None:
        raise Refused("--exercise is for a live test, and %s declares no live_target" % args.item)
    if args.landing is None:
        raise Refused("--landing is required for an implementation packet; there is no default")
    scopes = check_eligible(args.item, fm, dm, args.rig, repo_name(rig_clone),
                            os.path.join(city, "proof", "pilot-allowlist.txt"), backlog_clone)
    check_landing(args.landing, args.rig, repo_name(rig_clone), rel_item, backlog_clone)
    design = check_design(args.item, fm, scopes, backlog_clone)
    digest, claim_commit = item_identity(backlog_clone, rel_item, args.item)

    name = "%s-%s" % (args.item.lower(), slugify(fm.get("title", "")))
    branch = "agent/BKLG-" + name.removeprefix("bklg-")
    work_dir = os.path.join(rig_clone, ".worktrees", name)

    with admission_lock(city, args.item, args.dry_run):
        import execution_contract as ec
        contract = attempt = None
        journal = ec.load_attempt(city, args.item)
        # One store read supplies both the work history and its workflow roots.
        beads = existing_beads(city, args.rig, args.item, include_superseded=True, include_workflows=True)
        history = [b for b in beads if (b.get("metadata") or {}).get("gc.kind") != "workflow"]
        import recovery_review
        try:
            recovery_trial = recovery_review.trial_for(city, args.rig, args.item, history)
        except recovery_review.admit.Refused as why:
            # This file can also be __main__; keep the CLI's own refusal type.
            raise Refused(str(why)) from why
        # Existing legacy executions stay legacy even after policy enablement.
        existing_execution = any(str((b.get("metadata") or {}).get("proof.superseded")).lower() != "true"
                                 for b in history)
        contracted = journal is not None or (not existing_execution and ec.enabled(city, args.rig, repo_name(rig_clone)))
        if contracted:
            revision = ec.snapshot(backlog_clone)
            rel_item, fm, item_body = ec.record(backlog_clone, revision, args.item)
            item_path = os.path.join(backlog_clone, rel_item)
            scopes = check_eligible(args.item, fm, dm, args.rig, repo_name(rig_clone),
                                    os.path.join(city, "proof", "pilot-allowlist.txt"), backlog_clone)
            design = check_design(args.item, fm, scopes, backlog_clone)
            recipe_digest = ec.recipe_for(city, rigs, args.rig)
            base = ec.snapshot(rig_clone)
            if journal is not None:
                contract = ec.verify(ec.read(city, journal["contract"]), backlog_clone, revision,
                                     recipe_digest, args.landing, args.review, rig_clone, base)
                attempt = journal
                recovered_root = ec.reconcile(attempt, beads)
                if not args.dry_run:
                    ec.save_attempt(city, attempt)
                if recovered_root is not None:
                    if not args.dry_run:
                        attempt.update(state="launched", root=recovered_root["id"])
                        ec.save_attempt(city, attempt)
                        md = next((b.get("metadata") or {} for b in history if b["id"] == attempt["bead"]), {})
                        if not md.get("work_dir") or not md.get("proof.branch"):
                            raise Refused("contract: missing admission workspace identity")
                        write_admission_record(city, args.rig, recovered_root["id"], md["work_dir"],
                                               md["proof.branch"], contract["repository"], design,
                                               contract["publication"]["review"], scopes)
                    raise Running("contract %s attempt %s reconciled to workflow %s" %
                                  (contract["id"], attempt["attempt"], recovered_root["id"]))
            else:
                try:
                    work_dir, branch, _ = packet_workspace(rig_clone, history, [], work_dir, branch)
                except Dirty as dirty:
                    work_dir, branch = dirty.work_dir, dirty.branch
                contract = ec.compile_contract(backlog_clone, revision, args.item, args.rig,
                    repo_name(rig_clone), base, recipe_digest, args.landing, args.review,
                    ec.execution_vars(rigs, args.rig, work_dir, branch), rig_clone, base)
                if not args.dry_run:
                    attempt = ec.prepare(city, contract)
            main_sha = revision
            if contract and contract.get("work"):
                digest, claim_commit = contract["work"]["semantic_digest"], "ledger:%s" % contract["work"]["ledger_revision"]
            else:
                digest = ec.git(backlog_clone, "rev-parse", revision + ":" + rel_item)
                claim_commit = ec.git(backlog_clone, "log", "-1", "--format=%H", revision, "--", rel_item)
        elif any((b.get("metadata") or {}).get("proof.contract_id") for b in history):
            raise Refused("contract: missing admission journal; cannot downgrade to legacy")
        prior = [b for b in history if str((b.get("metadata") or {}).get("proof.superseded")).lower() != "true"]
        live = [b for b in prior if b.get("status") != "closed"]
        if len(live) > 1:
            raise Refused("%d open beads already reference %s; reconcile them first" % (len(live), args.item))
        if live and not contracted and (live[0].get("metadata") or {}).get("proof.packet_digest") != digest:
            raise Refused("%s changed on main since bead %s was admitted; reconcile that bead before "
                          "admitting again" % (args.item, live[0]["id"]))
        if prior and not live:
            raise Refused("%s was already executed as %s. Re-running a finished packet is a decision, "
                          "not a retry." % (args.item, ", ".join(b["id"] for b in prior)))
        if live:
            import factory_lib  # local: factory_lib imports admit
            packet = next((p for p in factory_lib.packets_of(beads)
                           if p["bead"]["id"] == live[0]["id"]), None)
            # A rootless bead is recoverable admission, not a second workflow.
            if packet is not None and packet["root"] and packet["state"] == "running":
                if packet.get("record_pending") and not args.dry_run:
                    # The slung workflow uses the bead's recorded workspace,
                    # which can predate the item's current title. Do not recover
                    # or archive a worktree while its worker is still running.
                    md = live[0].get("metadata") or {}
                    recorded_dir, recorded_branch = md.get("work_dir"), md.get("proof.branch")
                    if not recorded_dir or not recorded_branch:
                        raise Refused("prior packet has no complete workspace identity")
                    write_admission_record(city, args.rig, packet["root"]["id"], recorded_dir, recorded_branch,
                                           repo_name(rig_clone), design, args.review, scopes)
                raise Running("%s already runs as workflow %s on bead %s" %
                              (args.item, packet["root"]["id"], live[0]["id"]))
        # A dirty retained worktree is archived to this tag, never on the branch.
        # A dry run changes nothing: it says what admission would archive.
        archive, would_archive = retained_tag(args.item, now()), False
        try:
            work_dir, branch, prior_commit = packet_workspace(rig_clone, history, live, work_dir, branch,
                                                              archive_as=None if args.dry_run else archive)
        except Dirty as dirty:
            if not args.dry_run:
                raise
            work_dir, branch, prior_commit, would_archive = (dirty.work_dir, dirty.branch,
                                                             prior_work(dirty.work_dir), True)
        if not would_archive and not (os.path.isdir(work_dir) and run(
                ["git", "-C", work_dir, "tag", "--list", archive]).stdout.strip()):
            archive = ""
        name = os.path.basename(work_dir)
        # Asked once, for a new bead only: a re-admission keeps the judgement its
        # own admission recorded, so the corpus has one row per execution.
        judged, judgement = (({}, "not asked — bead %s reuses an earlier admission" % live[0]["id"])
                             if live else judge_item(fm, item_body, backlog_clone))

        plan = [
            "packet      %s  (%s)" % (args.item, fm.get("title", "")),
            "item        %s @ %s (blob %s)" % (rel_item, main_sha[:10], digest[:10]),
            "claim       %s until %s (commit %s)" % (fm.get("claimed_by"), fm.get("claim_expires"), claim_commit[:10]),
            "rig         %s -> %s" % (args.rig, rig_clone),
            "worktree    %s" % work_dir,
            "branch      %s" % branch,
            "landing     %s %s" % (args.landing, LANDING[args.landing]),
            "review      %s" % ("independent reviewer" if args.review else "off — the orchestrator's gate only"),
            "bead        %s" % ("reuse " + live[0]["id"] if live else "create"),
            "judgement   %s" % judgement,
            "design      %s" % (design or "nothing in this scope renders"),
        ]
        if contract:
            plan.append("contract    %s attempt %s — compatible" %
                        (contract["id"], attempt["attempt"] if attempt else "new (dry run)"))
        if prior_commit:
            plan.append("resume      %s — left on the branch by an earlier attempt; verified, not redone"
                        % prior_commit[:12])
        if archive:
            plan.append("retained    %s — uncommitted work an earlier attempt left, %s" % (
                archive, "would be archived there, off the branch" if would_archive
                else "archived there, off the branch; the worktree is at its last commit"))
        print("\n".join(plan))
        if args.dry_run:
            print("\ndry run: nothing was changed")
            return 0

        if not os.path.isdir(work_dir):
            run([os.path.join(backlog_clone, "scripts", "new-agent-worktree.sh"),
                 "--repo", rig_clone, "--name", name, "--branch", branch])
        if not os.path.isdir(work_dir):
            raise Refused("the worktree script did not create %s" % work_dir)

        if live:
            bead_id = live[0]["id"]
            if archive:
                gc(city, "bd", "--rig", args.rig, "update", bead_id,
                   "--set-metadata", "proof.retained_archive=" + archive)
        else:
            same_repo = os.path.realpath(rig_clone) == os.path.realpath(backlog_clone)
            # Asked once, for a new bead only — the rule `judge_item` follows, so the
            # corpus keeps one row per execution and a retry re-reads the bead it has.
            reading, doctrine = required_reading(
                backlog_clone, rig_clone, repo_name(rig_clone), scopes, same_repo,
                item={"title": fm.get("title"), "write_scope": fm.get("write_scope"),
                      "body": item_body})
            body = bead_body(args.item, item_path, rel_item, main_sha, work_dir, branch, args.landing, same_repo,
                             reading=reading, archive=archive,
                             default_scope=path_rules.default_scope(repo_name(rig_clone)))
            with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
                fh.write(body)
            meta = {"proof.packet_id": args.item, "proof.packet_digest": digest,
                    "proof.claim_commit": claim_commit, "proof.branch": branch,
                    "proof.landing": args.landing, "work_dir": work_dir}
            if contract:
                meta.update({"proof.contract_id": contract["id"], "proof.contract_attempt": attempt["attempt"]})
            # The lease the seat holds in the ledger travels on the execution
            # bead, so the completer, the keeper and a release can renew or
            # settle it without re-reading the snapshot for the holder.
            lease = (contract.get("work") or {}).get("lease_id") if contract else ""
            lease = lease or check_lease(backlog_clone, args.item, fm["claimed_by"])
            if lease:
                meta[LEASE_KEY] = lease
            meta.update(recovery_review.provenance(city))
            if recovery_trial:
                meta["proof.recovery_trial"] = recovery_trial
                meta["proof.recovery_interventions"] = "[]"
            if archive:
                meta["proof.retained_archive"] = archive
            meta.update(judged)     # log-only; absent when the judgement could not be made
            meta.update(doctrine)   # log-only; the clauses this item's work order raised
            if contract:
                attempt["state"] = "creating"
                ec.save_attempt(city, attempt)
            try:
                out = gc(city, "bd", "--rig", args.rig, "create", str(fm.get("title", args.item)),
                         "--external-ref", args.item, "--body-file", fh.name,
                         "--metadata", json.dumps(meta), "--json")
            finally:
                os.unlink(fh.name)
            created = json.loads(out.stdout)
            bead_id = (created[0] if isinstance(created, list) else created)["id"]
            if contract:
                attempt.update(bead=bead_id, state="prepared")
                ec.save_attempt(city, attempt)
            print("created     %s" % bead_id)
            print("doctrine    %s" % (doctrine.get(DOCTRINE_META + "error")
                                      or "  ".join("%s %s" % (key[len(DOCTRINE_META):], value)
                                                   for key, value in sorted(doctrine.items())
                                                   if key[len(DOCTRINE_META):] not in
                                                   ("evidence", "model", "ms")) or "nothing to judge"))

        sling = ["sling", "%s/%s" % (args.rig, WORKER), bead_id, "--on", FORMULA,
                 "--var", "packet_id=" + args.item, "--var", "work_dir=" + work_dir,
                 "--var", "branch=" + branch, "--var", "review=" + ("true" if args.review else "false")]
        trial = recovery_trial or next(((b.get("metadata") or {}).get("proof.recovery_trial", "")
                                         for b in live if b.get("id") == bead_id), "")
        if trial:
            sling += ["--var", "recovery_trial=" + trial]
        for key, value in LANDING[args.landing].items():
            sling += ["--var", "%s=%s" % (key, value)]
        if args.landing == "main":
            sling += ["--var", "item_path=" + rel_item]
        if prior_commit:
            sling += ["--var", "prior_commit=" + prior_commit]
        if contract:
            # Write ahead of the non-idempotent engine call. A lost response never
            # authorizes another sling merely because no root is visible yet.
            attempt.update(bead=bead_id, state="launching")
            ec.save_attempt(city, attempt)
            sling += ["--var", "contract_required=true", "--var", "contract_id=" + contract["id"],
                      "--var", "contract_attempt=" + attempt["attempt"]]
        out = gc(city, *sling)
        print(out.stdout.strip())
        # Point the dashboard's run view at the packet worktree rather than the rig
        # root. (Its DIFF tab also needs the path under an allowed root: the
        # supervisor's RUN_CWD_ALLOWED_ROOTS must include the rigs directory, or the
        # tab reads "invalid execution path". That is an allowlist, not this key.)
        root = re.search(r"workflow (\S+)", out.stdout or "")
        if root:
            if contract:
                attempt.update(root=root.group(1), state="launched")
                ec.save_attempt(city, attempt)
            # `design_scope.py` runs in the packet worktree, where neither the item
            # nor the agreement registry is visible. What it needs is decided here,
            # where both are, and travels on the workflow root: the agreed design
            # this packet may build to, the scope it declared, and whether it has an
            # independent reviewer — which decides whether a path matching a review
            # substring may change outside that scope (ADR-0159). Metadata rather
            # than formula variables, so no authorization shape changes.
            write_admission_record(city, args.rig, root.group(1), work_dir, branch,
                                   repo_name(rig_clone), design, args.review, scopes)
        return 0


def cli(argv):
    """The process's exit code, one per outcome the dispatcher tells apart: 0
    admitted, RUNNING_EXIT already running, WAITS_EXIT deferred,
    UNAVAILABLE_EXIT an outage, 1 refused."""
    try:
        return main(argv)
    except Running as why:
        print("gc proof admit: running — %s" % why, file=sys.stderr)
        return RUNNING_EXIT
    except Waits as why:
        print("gc proof admit: waits — %s" % why, file=sys.stderr)
        return WAITS_EXIT
    except Unavailable as why:
        print("gc proof admit: unavailable — %s" % why, file=sys.stderr)
        return UNAVAILABLE_EXIT
    except Refused as why:
        print("gc proof admit: refused — %s" % why, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
