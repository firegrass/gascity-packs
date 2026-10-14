#!/usr/bin/env python3
"""The durable owner of one live-test run, and of the fleet's one live slot.

A live test contacts production. The thing that must survive a crash is
therefore not the result but the *permission*: how many requests this run has
already been allowed to send, and whether it still owns the one slot in which a
live test may run at all. Both live on disk, under the configured city root,
and every transition that changes either is one flock-protected transaction.

  <city>/proof/live-slot          the one global slot record
  <city>/proof/live-slot.lock     the lock taken for each transition, briefly
  <city>/proof/live-runs/<run-id>/manifest.json   what admission froze
  <city>/proof/live-runs/<run-id>/state.json      the journal
  <city>/proof/live-runs/<run-id>/attempt-<n>.body  bounded response evidence

Nothing here is the backlog. Markdown remains the authority for what a packet
is and whether it is done (`docs/backlog/items/`); this is the execution record
the bead links, and `gc proof status` and the completion publisher read it.
That is why a failure here is typed: an error must never be mistaken for an
empty successful run, so `status()` of a run that does not exist *raises* rather
than returning a blank record, and an uncertain slot reports intervention
rather than reading as free.

Three rules the rest of the module exists to keep:

  * **Write-ahead.** An HTTP attempt is persisted before it is sent and its
    result after receipt, so a process killed between send and receipt has
    still spent its request. The budget is two reads for the whole run
    (preflight and observation); a third is a new run, never an invisible
    retry. An attempt with no result makes the run `inconclusive`, and an
    inconclusive run never sends a replacement.
  * **Elapsed time grants nothing.** A slot is released by a durable
    settlement, never by a clock. A held slot whose journal is missing or
    corrupt stays held and reports intervention: uncertain ownership is not
    transferred to the next caller.
  * **Lifecycle is not the verdict.** `lifecycle` is where the run got to;
    `outcome` is what it found. They are separate so a refused or failed run
    still settles and still publishes its evidence.

Standard library only. The clock, the city path and every failure-injection
point are injected, so the tests in `gascity/tests/test_livetest_state.py`
drive real files in a temporary city and kill a real process mid-attempt.

Bounds, states and the manifest shape are the frozen observe V1 contract,
`docs/knowledge/production-validation-observe-contract.md`. The record-shape
constants come from `livetest_contract`, which owns them: a second copy of
`OBSERVE_MAX_AGE_SECONDS` here is the gap in the fence that module exists to
prevent.

The interface the rest of the observe sequence builds on
--------------------------------------------------------

Admission (`BKLG-20260920-n0tn`) freezes the manifest and takes the slot:

    run_id   = new_run_id(item_id, item_blob_sha, recipe, admitted_at, rerun=0)
    manifest = build_manifest(run_id=..., item_id=..., item_blob_sha=...,
                              exercise=..., recipe=..., target=...,
                              expected_build=..., admitted_at=..., expires_at=...,
                              max_age_seconds=..., credential_profile=...,
                              output_paths=[...])        -> dict, with `digest`
    store    = RunStore(city, now=<callable>, hook=None)
    store.acquire(manifest)                              -> RunStatus

The runner's three entrypoints (`BKLG-20260920-3l4m`) drive one run:

    store.recover(run_id)                                -> RunStatus
    store.begin_attempt(run_id, "preflight"|"observation")   -> AttemptReceipt
    store.complete_attempt(run_id, seq, http_status=, body=, error_code="")
                                                         -> AttemptReceipt
    store.record_preflight(run_id, passed=, code="")     -> RunStatus
    store.record_observation(run_id, verdict=, evidence=None) -> RunStatus
    store.settle(run_id)                                 -> RunStatus
    store.release(run_id)                                -> SlotStatus

The publisher (`BKLG-20260922-biok`) reads and closes:

    store.status(run_id)                                 -> RunStatus
    store.slot()                                         -> SlotStatus
    store.record_publication(run_id, commit)             -> RunStatus

Every refusal is `Refused` with `.code` from `FAILURE_CODES` and a bounded
`.detail`. The three returned records are frozen dataclasses:

    AttemptReceipt(seq, phase, status, started_at, completed_at, http_status,
                   body_bytes, body_sha256, error_code)
        status: sent | completed | abandoned
    RunStatus(run_id, item_id, target, exercise, lifecycle, outcome, reported,
              attempts_used, attempts_remaining, expires_at, slot_held,
              cleanup_recorded, published_commit, needs_intervention, detail,
              receipts)
        lifecycle: LIFECYCLE_STATES;  outcome: OUTCOMES or "";
        reported: the evidence vocabulary — pass | feature_failure | refused |
        inconclusive | cleanup_required, or "" while the run is still running
    SlotStatus(held, run_id, item_id, acquired_at, expires_at, released_at,
               needs_intervention, detail)
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import livetest_contract as lt  # noqa: E402  the one parser for the record shape

MANIFEST_KIND = "proof.liskov.livetest-manifest.v1"
STATE_KIND = "proof.liskov.livetest-run.v1"
SLOT_KIND = "proof.liskov.live-slot.v1"

# Two HTTP reads for the entire run: preflight and observation. A third read is
# a new run id, not a retry hidden inside this one.
MAX_REQUESTS = 2
REQUEST_PHASES = ("preflight", "observation")
# The frozen bound on a response body. The transport stops reading there; the
# store refuses to record more, so the bound survives a careless caller.
RESPONSE_BYTES_MAX = 64 * 1024
# Held beyond the run window so cleanup still has a live credential (contract:
# "a 120-second cleanup margin, in addition to the 120-second run timeout").
CLEANUP_MARGIN_SECONDS = 120

# Where the run got to.
LIFECYCLE_STATES = ("admitted", "preflight_passed", "observed", "settled", "published",
                    "cleanup_required")
LIFECYCLE_NEXT = {
    "admitted": ("preflight_passed", "settled", "cleanup_required"),
    "preflight_passed": ("observed", "settled", "cleanup_required"),
    "observed": ("settled", "cleanup_required"),
    "settled": ("published",),
    "published": (),
    "cleanup_required": (),
}
# What the run found. Separate from the lifecycle on purpose: a refused or
# failed run still settles and still publishes.
OUTCOMES = ("pass", "feature_failure", "refused", "inconclusive")

# The recipes whose cleanup is a verified no-op, because the recipe cannot
# create a production resource. Absence is not an oversight: a mutate or spend
# recipe needs its own cleanup, and generalising this no-op to it would leave
# real state behind. A recipe not named here settles into `cleanup_required`.
NO_OP_CLEANUP_RECIPES = ("readyz-build-v1",)

# Trusted config maps a target to its credential profile. A packet names the
# profile; it can never name a file. The token itself is loaded by the check
# entrypoints, never by this module.
CREDENTIAL_PROFILES = {"liskov-api": "observe-liskov-api"}

# Exactly the keys a manifest carries. Anything else refuses, which is what
# keeps a secret, a command or a free-form URL out of it.
MANIFEST_KEYS = ("kind", "run_id", "item_id", "item_blob_sha", "exercise", "recipe",
                 "recipe_version", "recipe_digest", "target", "expected_build",
                 "admitted_at", "expires_at", "max_age_seconds", "credential_profile",
                 "output_paths")

RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
ITEM_ID_RE = re.compile(r"^BKLG-\d{8}-[a-z0-9]{4}$")
# The item's identity at admission: its git blob (forty hex) under the Git work
# authority, its ledger semantic digest (sixty-four hex) under Beads (ADR-0236).
BLOB_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
RECIPE_VERSION_RE = re.compile(r"-v(\d+)$")
INSTANT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# Every refusal this module can make. `gc proof status` and the publisher
# branch on the code, never on the sentence.
FAILURE_CODES = (
    "attempt_outstanding",     # a request was sent and never resolved
    "attempt_unknown",         # no such attempt on this run
    "bad_transition",          # the lifecycle does not allow it
    "body_too_large",          # more than RESPONSE_BYTES_MAX offered as evidence
    "budget_exhausted",        # the two-read budget is spent
    "cleanup_unverified",      # this recipe's cleanup is not a verified no-op
    "deadline_passed",         # the run window closed; observation cannot resume
    "exercise_unauthorized",   # mutate/spend: recognised, not implemented
    "identity_mismatch",       # state and manifest disagree about whose run this is
    "manifest_edited",         # on-disk manifest no longer matches its digest
    "manifest_invalid",        # the manifest is not a well-formed frozen object
    "not_settled",             # the slot is released after settlement, not before
    "publication_recorded",    # already published, under a different commit
    "run_inconclusive",        # an interrupted run never sends a replacement
    "slot_foreign",            # the slot is not this run's to release
    "slot_held",               # another run owns the slot
    "slot_uncertain",          # the holder's journal is missing or corrupt
    "state_corrupt",           # the journal is not readable JSON
    "state_missing",           # there is no such run
    "unsafe_path",             # a symlink or an escape from the runs root
)

DETAIL_MAX = 200
HISTORY_MAX = 64


class Refused(Exception):
    """A typed refusal. `code` is from FAILURE_CODES; `detail` is bounded."""

    def __init__(self, code, detail=""):
        self.code = code
        self.detail = _bounded(detail)
        super().__init__("%s: %s" % (code, self.detail) if self.detail else code)


def _bounded(text, limit=DETAIL_MAX):
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------- canonical form

def canonical_bytes(obj):
    """The one serialization a digest is taken over.

    Sorted keys, no insignificant space, ASCII-escaped and NaN-free, so the
    bytes do not depend on dict order, locale or platform.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def digest(obj):
    """SHA-256 of the canonical form, as forty-plus hex digits (sixty-four)."""
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def recipe_digest(name):
    """The digest of a registered recipe as checked-in code defines it.

    A manifest carries it so an edited recipe cannot be passed off as the one
    that was admitted.
    """
    registered = lt.RECIPES.get(name)
    if registered is None:
        raise Refused("manifest_invalid", "live_recipe %r is not a registered recipe" % (name,))
    return digest({"name": name, "entry": registered})


def recipe_version(name):
    """`readyz-build-v1` is version v1. The registry names the version in the
    recipe id, and a recipe without one is not registered."""
    found = RECIPE_VERSION_RE.search(name or "")
    if not found:
        raise Refused("manifest_invalid", "recipe %r does not name a version" % (name,))
    return "v%s" % found.group(1)


def new_run_id(item_id, item_blob_sha, recipe, admitted_at, rerun=0):
    """A deterministic run id: the same admission computes the same id, and an
    explicit rerun — a different `rerun` ordinal — is a different run."""
    return digest({"item": item_id, "blob": item_blob_sha, "recipe": recipe,
                   "admitted_at": admitted_at, "rerun": int(rerun)})[:32]


def instant(when):
    """A datetime as the one instant format the records use."""
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _at(text, what="instant"):
    if not isinstance(text, str) or not INSTANT_RE.match(text):
        raise Refused("manifest_invalid", "%s must be YYYY-MM-DDTHH:MM:SSZ, got %r" % (what, text))
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- the manifest

def manifest_errors(manifest):
    """Every way this object is not the frozen manifest, as messages.

    Pure: no file, clock, network or environment is read.
    """
    errors = []
    if not isinstance(manifest, dict):
        return ["manifest must be an object"]

    body = {k: v for k, v in manifest.items() if k != "digest"}
    unknown = sorted(set(body) - set(MANIFEST_KEYS))
    missing = sorted(set(MANIFEST_KEYS) - set(body))
    if unknown:
        errors.append("manifest carries key(s) it does not own: %s; a secret, a command or a "
                      "free-form URL has no place in a frozen manifest" % ", ".join(unknown))
    if missing:
        errors.append("manifest is missing %s" % ", ".join(missing))
    for key, value in sorted(body.items()):
        for text in (value if isinstance(value, list) else [value]):
            if isinstance(text, str) and "://" in text:
                errors.append("%s carries a URL; the target-to-origin mapping is checked-in "
                              "code, never a value in a record" % key)
    if unknown or missing:
        return errors

    if body["kind"] != MANIFEST_KIND:
        errors.append("kind must be %s, got %r" % (MANIFEST_KIND, body["kind"]))
    if not isinstance(body["run_id"], str) or not RUN_ID_RE.match(body["run_id"]):
        errors.append("run_id must be thirty-two hex digits, got %r" % (body["run_id"],))
    if not isinstance(body["item_id"], str) or not ITEM_ID_RE.match(body["item_id"]):
        errors.append("item_id must be a backlog id, got %r" % (body["item_id"],))
    if not isinstance(body["item_blob_sha"], str) or not BLOB_SHA_RE.match(body["item_blob_sha"]):
        errors.append("item_blob_sha must be a forty-hex git blob or a sixty-four-hex ledger digest, got %r"
                      % (body["item_blob_sha"],))

    level = body["exercise"]
    if level not in lt.EXERCISE_LEVELS:
        errors.append("exercise must be one of %s, got %r" % (" | ".join(lt.EXERCISE_LEVELS), level))
    elif not lt.is_authorized_exercise(level):
        errors.append("exercise %r is recognised but not implemented; observe V1 admits %s only"
                      % (level, " | ".join(lt.AUTHORIZED_EXERCISES)))

    recipe, target = body["recipe"], body["target"]
    registered = lt.RECIPES.get(recipe) if isinstance(recipe, str) else None
    if registered is None:
        errors.append("recipe %r is not a registered recipe (%s)"
                      % (recipe, ", ".join(sorted(lt.RECIPES)) or "none"))
    else:
        if registered["target"] != target:
            errors.append("recipe %r runs against %s, but the manifest target is %r"
                          % (recipe, registered["target"], target))
        if registered["exercise"] != level:
            errors.append("recipe %r is an %s recipe, but the manifest declares %r"
                          % (recipe, registered["exercise"], level))
        try:
            if body["recipe_digest"] != recipe_digest(recipe):
                errors.append("recipe_digest does not match the registered recipe; the recipe "
                              "changed after admission, so this run is not the admitted one")
            if body["recipe_version"] != recipe_version(recipe):
                errors.append("recipe_version must be %s for %r" % (recipe_version(recipe), recipe))
        except Refused as why:
            errors.append(why.detail or why.code)

    if not isinstance(body["expected_build"], str) or not lt.BUILD_RE.match(body["expected_build"]):
        errors.append("expected_build must be a full forty-hex-digit commit, got %r"
                      % (body["expected_build"],))

    try:
        admitted, expires = _at(body["admitted_at"], "admitted_at"), _at(body["expires_at"], "expires_at")
        window = (expires - admitted).total_seconds()
        if window <= 0:
            errors.append("expires_at must be after admitted_at")
        elif window > lt.OBSERVE_TIMEOUT_SECONDS:
            errors.append("the run window is %d seconds; observe V1 fixes it at %d and refuses a "
                          "manifest that widens it" % (window, lt.OBSERVE_TIMEOUT_SECONDS))
    except Refused as why:
        errors.append(why.detail or why.code)

    age = body["max_age_seconds"]
    if not isinstance(age, int) or isinstance(age, bool) or age <= 0:
        errors.append("max_age_seconds must be a positive whole number of seconds, got %r" % (age,))
    elif age > lt.OBSERVE_MAX_AGE_SECONDS:
        errors.append("max_age_seconds is %d; observe V1 fixes it at %d and refuses a manifest "
                      "that widens it" % (age, lt.OBSERVE_MAX_AGE_SECONDS))

    profile = body["credential_profile"]
    if not isinstance(profile, str) or not PROFILE_RE.match(profile):
        errors.append("credential_profile names a machine-local profile, not a path: got %r" % (profile,))
    elif CREDENTIAL_PROFILES.get(target) != profile:
        errors.append("credential_profile for %r is %r, not %r; trusted config maps the profile "
                      "to the target" % (target, CREDENTIAL_PROFILES.get(target), profile))

    paths = body["output_paths"]
    if not isinstance(paths, list) or not paths:
        errors.append("output_paths must list the evidence paths this run may write")
    else:
        for path in paths:
            if not isinstance(path, str) or not path:
                errors.append("output_paths entries are repository-relative paths, got %r" % (path,))
            elif path.startswith("/") or ".." in path.replace("\\", "/").split("/"):
                errors.append("output_path %r escapes the repository" % (path,))
            elif not (path.replace("\\", "/") + "/").startswith(lt.EVIDENCE_SCOPE_PREFIX):
                errors.append("output_path %r is outside %s; a live run writes only evidence"
                              % (path, lt.EVIDENCE_SCOPE_PREFIX))
    return errors


def build_manifest(*, run_id, item_id, item_blob_sha, exercise, recipe, target, expected_build,
                   admitted_at, expires_at, max_age_seconds, credential_profile, output_paths):
    """The frozen manifest, validated and digested. Refuses anything it cannot
    freeze — including an exercise the factory recognises but does not implement."""
    body = {
        "kind": MANIFEST_KIND,
        "run_id": run_id,
        "item_id": item_id,
        "item_blob_sha": item_blob_sha,
        "exercise": exercise,
        "recipe": recipe,
        "recipe_version": recipe_version(recipe) if isinstance(recipe, str) and
        RECIPE_VERSION_RE.search(recipe) else "",
        "recipe_digest": recipe_digest(recipe) if isinstance(recipe, str) and recipe in lt.RECIPES else "",
        "target": target,
        "expected_build": expected_build,
        "admitted_at": admitted_at,
        "expires_at": expires_at,
        "max_age_seconds": max_age_seconds,
        "credential_profile": credential_profile,
        "output_paths": list(output_paths),
    }
    errors = manifest_errors(body)
    if errors:
        code = "exercise_unauthorized" if any("not implemented" in e for e in errors) else "manifest_invalid"
        raise Refused(code, "; ".join(errors))
    body["digest"] = digest(body)
    return body


def verify_manifest(manifest):
    """The manifest as it must be to be used: well-formed, and carrying the
    digest of its own body. An edited manifest refuses."""
    errors = manifest_errors(manifest)
    if errors:
        raise Refused("manifest_invalid", "; ".join(errors))
    body = {k: v for k, v in manifest.items() if k != "digest"}
    if manifest.get("digest") != digest(body):
        raise Refused("manifest_edited",
                      "manifest %s does not match its digest" % manifest.get("run_id", "?"))
    return manifest


# ---------------------------------------------------------------- typed records

@dataclass(frozen=True)
class AttemptReceipt:
    """One HTTP read of the run's budget. `status` is sent | completed |
    abandoned; `abandoned` is an attempt whose process died before the receipt."""
    seq: int
    phase: str
    status: str
    started_at: str
    completed_at: str
    http_status: int
    body_bytes: int
    body_sha256: str
    error_code: str


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    item_id: str
    target: str
    exercise: str
    lifecycle: str
    outcome: str
    reported: str
    attempts_used: int
    attempts_remaining: int
    expires_at: str
    slot_held: bool
    cleanup_recorded: bool
    published_commit: str
    needs_intervention: bool
    detail: str
    receipts: tuple


@dataclass(frozen=True)
class SlotStatus:
    """A free slot and an uncertain one are different answers. `held=False` with
    `needs_intervention=True` is never a slot the next run may take."""
    held: bool
    run_id: str
    item_id: str
    acquired_at: str
    expires_at: str
    released_at: str
    needs_intervention: bool
    detail: str


# ---------------------------------------------------------------- the store

def city_root(env=None):
    """The configured city root. Two names, no ambient fallback: a store that
    guessed its city could hold the wrong fleet's slot."""
    env = os.environ if env is None else env
    for name in ("GC_CITY", "GC_CITY_PATH"):
        if env.get(name):
            return env[name]
    raise Refused("unsafe_path", "no city root: set GC_CITY, or pass one to RunStore")


class RunStore:
    """Every durable transition of one city's live testing.

    `now` is a callable returning an aware datetime; `hook` is called with an
    event name and a path at each injectable failure point — `write:`, `fsync:`,
    `replace:`, `dirsync:` around an atomic write, and `transition:<from>-><to>`
    before a lifecycle change — so a test can stop the process exactly there.
    Both default to the real thing.
    """

    def __init__(self, city, *, now=None, hook=None):
        self.city = str(city)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._hook = hook
        self.proof_dir = os.path.join(self.city, "proof")
        self.runs_root = os.path.join(self.proof_dir, "live-runs")
        self.slot_path = os.path.join(self.proof_dir, "live-slot")
        # NOT proof/factory.lock: the factory pass lock is held for a pass, and
        # a live run lasts minutes. Holding it for a run would stop dispatch.
        self.lock_path = os.path.join(self.proof_dir, "live-slot.lock")

    # -- plumbing ---------------------------------------------------------

    def _event(self, name, path):
        if self._hook is not None:
            self._hook(name, str(path))

    @contextlib.contextmanager
    def _transaction(self):
        """The one lock, held for one transition and no longer."""
        os.makedirs(self.proof_dir, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _safe(self, path):
        if os.path.islink(path):
            raise Refused("unsafe_path", "%s is a symlink" % path)
        return path

    def _run_dir(self, run_id):
        if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
            raise Refused("unsafe_path", "run id must be thirty-two hex digits, got %r" % (run_id,))
        root = os.path.realpath(self.runs_root)
        path = os.path.join(self.runs_root, run_id)
        self._safe(path)
        if os.path.exists(path) and not os.path.realpath(path).startswith(root + os.sep):
            raise Refused("unsafe_path", "run directory %s escapes %s" % (path, self.runs_root))
        return path

    def _write(self, path, payload):
        """Same-directory temp, fsync, atomic replace, then fsync the directory.

        Interrupted anywhere, the reader still sees either the whole previous
        file or the whole new one — never a torn one.
        """
        self._safe(path)
        name = os.path.basename(path)
        parent = os.path.dirname(path)
        os.makedirs(parent, exist_ok=True)
        tmp = os.path.join(parent, ".%s.tmp" % name)
        self._event("write:%s" % name, path)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload if isinstance(payload, str)
                     else json.dumps(payload, sort_keys=True, indent=1) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._event("fsync:%s" % name, path)
        os.replace(tmp, path)
        self._event("replace:%s" % name, path)
        fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self._event("dirsync:%s" % name, path)

    def _read(self, path, *, code):
        self._safe(path)
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
        except FileNotFoundError:
            raise Refused(code, "%s is absent" % path)
        except OSError as why:
            raise Refused(code, "%s cannot be read: %s" % (path, why))
        except ValueError as why:
            raise Refused("state_corrupt" if code == "state_missing" else code,
                          "%s is not readable JSON: %s" % (path, why))
        if not isinstance(loaded, dict):
            raise Refused("state_corrupt", "%s is not an object" % path)
        return loaded

    # -- the slot ---------------------------------------------------------

    def _read_slot(self):
        if not os.path.exists(self.slot_path):
            return {}
        return self._read(self.slot_path, code="state_corrupt")

    def _slot_status(self, record):
        run_id = (record or {}).get("run_id") or ""
        if not run_id:
            return SlotStatus(held=False, run_id="", item_id="", acquired_at="",
                              expires_at="", released_at=(record or {}).get("released_at", ""),
                              needs_intervention=False, detail="")
        try:
            state = self._load_state(run_id)
        except Refused as why:
            # Uncertain ownership holds the slot. The next run does not get it
            # because this one's journal went missing.
            return SlotStatus(held=True, run_id=run_id, item_id=record.get("item_id", ""),
                              acquired_at=record.get("acquired_at", ""),
                              expires_at=record.get("expires_at", ""), released_at="",
                              needs_intervention=True,
                              detail="%s: %s" % (why.code, why.detail))
        return SlotStatus(held=True, run_id=run_id, item_id=state.get("item_id", ""),
                          acquired_at=record.get("acquired_at", ""),
                          expires_at=record.get("expires_at", ""), released_at="",
                          needs_intervention=False, detail="")

    def slot(self):
        """Who owns live testing in this city right now."""
        with self._transaction():
            return self._slot_status(self._read_slot())

    # -- the journal ------------------------------------------------------

    def _load_state(self, run_id):
        run_dir = self._run_dir(run_id)
        state = self._read(os.path.join(run_dir, "state.json"), code="state_missing")
        manifest = verify_manifest(self._read(os.path.join(run_dir, "manifest.json"),
                                              code="state_missing"))
        if state.get("kind") != STATE_KIND:
            raise Refused("state_corrupt", "journal for %s is not a %s" % (run_id, STATE_KIND))
        if state.get("run_id") != run_id or manifest["run_id"] != run_id:
            raise Refused("identity_mismatch",
                          "journal and manifest under %s claim %r and %r"
                          % (run_id, state.get("run_id"), manifest["run_id"]))
        if state.get("manifest_digest") != manifest["digest"]:
            raise Refused("identity_mismatch",
                          "journal for %s was written against another manifest" % run_id)
        if state.get("lifecycle") not in LIFECYCLE_STATES:
            raise Refused("state_corrupt", "lifecycle %r is not a state" % (state.get("lifecycle"),))
        state["_manifest"] = manifest
        return state

    def _save_state(self, state):
        run_id = state["run_id"]
        body = {k: v for k, v in state.items() if not k.startswith("_")}
        self._write(os.path.join(self._run_dir(run_id), "state.json"), body)

    def _transition(self, state, to):
        frm = state["lifecycle"]
        if to == frm:
            return state
        if to not in LIFECYCLE_NEXT.get(frm, ()):
            raise Refused("bad_transition", "a run at %s does not go to %s" % (frm, to))
        self._event("transition:%s->%s" % (frm, to), state["run_id"])
        state["lifecycle"] = to
        state.setdefault("history", []).append(
            {"at": instant(self._now()), "from": frm, "to": to})
        state["history"] = state["history"][-HISTORY_MAX:]
        return state

    # -- acquisition ------------------------------------------------------

    def acquire(self, manifest):
        """Take the one slot for this run, and lay down its durable record.

        One flock-protected transaction, shared by direct admission and factory
        dispatch across every rig in the city. Re-acquiring the same run is the
        recovery path and is idempotent; a slot held by another run refuses,
        and a slot whose holder's journal is unreadable refuses without
        transferring ownership.
        """
        manifest = verify_manifest(manifest)
        run_id = manifest["run_id"]
        with self._transaction():
            slot = self._read_slot()
            holder = slot.get("run_id") or ""
            if holder and holder != run_id:
                status = self._slot_status(slot)
                if status.needs_intervention:
                    raise Refused("slot_uncertain",
                                  "the slot is held by %s, whose journal cannot be read (%s); "
                                  "it is not released by time" % (holder, status.detail))
                raise Refused("slot_held", "run %s (%s) holds the live slot"
                              % (holder, status.item_id or "?"))

            run_dir = self._run_dir(run_id)
            existing = os.path.exists(os.path.join(run_dir, "state.json"))
            if existing:
                # Either a re-acquire by the holder, or a record written by an
                # attempt that died before it could take the slot. Both are
                # this run's own; neither may be rebuilt over.
                state = self._load_state(run_id)
                if state["_manifest"]["digest"] != manifest["digest"]:
                    raise Refused("identity_mismatch",
                                  "run %s already exists under another manifest" % run_id)
            else:
                self._write(os.path.join(run_dir, "manifest.json"), manifest)
                state = {"kind": STATE_KIND, "run_id": run_id, "item_id": manifest["item_id"],
                         "manifest_digest": manifest["digest"], "lifecycle": "admitted",
                         "outcome": "", "attempts": [], "cleanup": {}, "publication": {},
                         "history": [{"at": instant(self._now()), "from": "", "to": "admitted"}]}
                self._save_state(state)
                state["_manifest"] = manifest

            if not holder:
                # The slot record is the commit point: until it names this run,
                # the run directory owns nothing.
                self._write(self.slot_path, {
                    "kind": SLOT_KIND, "run_id": run_id, "item_id": manifest["item_id"],
                    "acquired_at": instant(self._now()), "expires_at": manifest["expires_at"]})
            return self._status(state, slot_run=run_id)

    def release(self, run_id):
        """Give the slot back — only once the run is durably settled.

        Releasing before settlement is how a live test leaves something behind;
        releasing twice is a recorded no-op, because a crash after the release
        must not strand the fleet.
        """
        with self._transaction():
            state = self._load_state(run_id)
            if state["lifecycle"] not in ("settled", "published"):
                raise Refused("not_settled",
                              "run %s is at %s; the slot is released after durable settlement"
                              % (run_id, state["lifecycle"]))
            slot = self._read_slot()
            holder = slot.get("run_id") or ""
            if holder and holder != run_id:
                raise Refused("slot_foreign", "the slot is held by %s, not %s" % (holder, run_id))
            if holder:
                self._write(self.slot_path, {
                    "kind": SLOT_KIND, "run_id": "", "item_id": "", "acquired_at": "",
                    "expires_at": "", "released_at": instant(self._now()),
                    "released_run": run_id})
            return self._slot_status(self._read_slot())

    # -- the two-read budget ----------------------------------------------

    def begin_attempt(self, run_id, phase):
        """Persist an HTTP attempt *before* it is sent, and return its receipt.

        Refuses when the budget is spent, when an earlier attempt never
        resolved, when the run is inconclusive, and when the run window has
        closed. A caller that crashes after this call and before
        `complete_attempt` has still spent the request — which is the point.
        """
        if phase not in REQUEST_PHASES:
            raise Refused("bad_transition", "an attempt is %s, got %r"
                          % (" or ".join(REQUEST_PHASES), phase))
        with self._transaction():
            state = self._load_state(run_id)
            manifest = state["_manifest"]
            expected = {"preflight": "admitted", "observation": "preflight_passed"}[phase]
            if state["lifecycle"] != expected:
                raise Refused("bad_transition", "a %s attempt is made at %s, not at %s"
                              % (phase, expected, state["lifecycle"]))
            unresolved = [a for a in state["attempts"] if a.get("status") != "completed"]
            if unresolved:
                raise Refused("attempt_outstanding" if unresolved[0].get("status") == "sent"
                              else "run_inconclusive",
                              "attempt %s (%s) never resolved; this run does not send another"
                              % (unresolved[0].get("seq"), unresolved[0].get("phase")))
            # The deadline is named before the state it produces: "the window
            # closed" is something an operator can act on, where "this run is
            # inconclusive" only says what that already made it.
            if self._now() > _at(manifest["expires_at"], "expires_at"):
                raise Refused("deadline_passed",
                              "the window for %s closed at %s" % (run_id, manifest["expires_at"]))
            if len(state["attempts"]) >= MAX_REQUESTS:
                raise Refused("budget_exhausted",
                              "run %s has spent its %d reads; a third read is a new run"
                              % (run_id, MAX_REQUESTS))
            if self._derive_outcome(state) == "inconclusive":
                raise Refused("run_inconclusive", "run %s is inconclusive" % run_id)

            attempt = {"seq": len(state["attempts"]) + 1, "phase": phase, "status": "sent",
                       "started_at": instant(self._now()), "completed_at": "", "http_status": 0,
                       "body_bytes": 0, "body_sha256": "", "error_code": ""}
            state["attempts"].append(attempt)
            self._save_state(state)
            return _receipt(attempt)

    def complete_attempt(self, run_id, seq, *, http_status=0, body=b"", error_code=""):
        """Record the receipt of an attempt that was persisted before it was sent."""
        if len(body) > RESPONSE_BYTES_MAX:
            raise Refused("body_too_large", "a response body is bounded at %d bytes, got %d"
                          % (RESPONSE_BYTES_MAX, len(body)))
        with self._transaction():
            state = self._load_state(run_id)
            found = [a for a in state["attempts"] if a.get("seq") == seq]
            if not found:
                raise Refused("attempt_unknown", "run %s has no attempt %r" % (run_id, seq))
            attempt = found[0]
            if attempt.get("status") != "sent":
                raise Refused("bad_transition", "attempt %s is already %s"
                              % (seq, attempt.get("status")))
            if body:
                self._write(os.path.join(self._run_dir(run_id), "attempt-%d.body" % seq),
                            body.decode("utf-8", "replace"))
            attempt.update({"status": "completed", "completed_at": instant(self._now()),
                            "http_status": int(http_status), "body_bytes": len(body),
                            "body_sha256": hashlib.sha256(body).hexdigest() if body else "",
                            "error_code": _bounded(error_code, 64)})
            self._save_state(state)
            return _receipt(attempt)

    def recover(self, run_id):
        """Reconcile a run whose process died, and persist what that makes it.

        An attempt that was sent and never resolved is abandoned — it stays
        spent, and the run becomes inconclusive, so observe recovery never
        sends a replacement request. A window that closed without a verdict is
        inconclusive too. Nothing here infers success.
        """
        with self._transaction():
            state = self._load_state(run_id)
            changed = False
            for attempt in state["attempts"]:
                if attempt.get("status") == "sent":
                    attempt["status"] = "abandoned"
                    attempt["error_code"] = "interrupted"
                    attempt["completed_at"] = instant(self._now())
                    changed = True
            derived = self._derive_outcome(state)
            if derived and state["outcome"] != derived:
                state["outcome"] = derived
                changed = True
            if changed:
                self._save_state(state)
            return self._status(state)

    # -- verdicts ---------------------------------------------------------

    def record_preflight(self, run_id, *, passed, code=""):
        """Preflight's verdict. A refusal keeps the lifecycle where it is and
        records the outcome: a refused run still settles and still publishes."""
        with self._transaction():
            state = self._load_state(run_id)
            if passed:
                self._transition(state, "preflight_passed")
            else:
                state["outcome"] = "refused"
                state["refused_code"] = _bounded(code, 64)
            self._save_state(state)
            return self._status(state)

    def record_observation(self, run_id, *, verdict, evidence=None):
        """The observation's verdict, separate from the lifecycle it advances."""
        if verdict not in ("pass", "feature_failure", "refused"):
            raise Refused("bad_transition", "an observation is pass | feature_failure | refused, "
                          "got %r" % (verdict,))
        with self._transaction():
            state = self._load_state(run_id)
            if not [a for a in state["attempts"]
                    if a.get("phase") == "observation" and a.get("status") == "completed"]:
                raise Refused("bad_transition",
                              "run %s has no completed observation to judge" % run_id)
            self._transition(state, "observed")
            state["outcome"] = verdict
            if evidence is not None:
                state["evidence"] = {k: _bounded(v, 512) if isinstance(v, str) else v
                                     for k, v in dict(evidence).items()}
            self._save_state(state)
            return self._status(state)

    def settle(self, run_id):
        """Run cleanup and settle, idempotently, whatever the run found.

        For a recipe that cannot create a production resource, cleanup is a
        recorded no-op — recorded, so that settlement is durable evidence
        rather than an assumption. A recipe whose cleanup is not a verified
        no-op settles into `cleanup_required` and keeps the slot.
        """
        with self._transaction():
            state = self._load_state(run_id)
            if state["lifecycle"] in ("settled", "published"):
                return self._status(state)          # idempotent: already settled
            if state["lifecycle"] == "cleanup_required":
                raise Refused("cleanup_unverified",
                              "run %s needs intervention before it can settle" % run_id)
            for attempt in state["attempts"]:
                if attempt.get("status") == "sent":
                    attempt["status"] = "abandoned"
                    attempt["error_code"] = "interrupted"
                    attempt["completed_at"] = instant(self._now())
            recipe = state["_manifest"]["recipe"]
            if recipe not in NO_OP_CLEANUP_RECIPES:
                state["cleanup"] = {"verified_no_op": False, "recipe": recipe,
                                    "at": instant(self._now()),
                                    "detail": "cleanup for %s is not a verified no-op" % recipe}
                self._transition(state, "cleanup_required")
                self._save_state(state)
                return self._status(state)
            if not state["cleanup"]:
                state["cleanup"] = {"verified_no_op": True, "recipe": recipe,
                                    "at": instant(self._now()),
                                    "detail": "the recipe creates no production resource"}
            if not state["outcome"]:
                # Never an invented pass: a run that settles without a verdict
                # is inconclusive.
                state["outcome"] = self._derive_outcome(state) or "inconclusive"
            self._transition(state, "settled")
            self._save_state(state)
            return self._status(state)

    def record_publication(self, run_id, commit):
        """Evidence has landed. Needs no slot: publication may retry after the
        slot is released, and never repeats the production read."""
        with self._transaction():
            state = self._load_state(run_id)
            recorded = (state.get("publication") or {}).get("commit") or ""
            if recorded:
                if recorded != commit:
                    raise Refused("publication_recorded",
                                  "run %s already published as %s; reconcile before committing "
                                  "again" % (run_id, recorded))
                return self._status(state)
            state["publication"] = {"commit": commit, "at": instant(self._now())}
            self._transition(state, "published")
            self._save_state(state)
            return self._status(state)

    # -- reading ----------------------------------------------------------

    def status(self, run_id):
        """This run, from its journal alone.

        A run that does not exist raises `state_missing`. It never comes back
        as an empty status, because an error that reads like a quiet success is
        how a live test gets believed without having run.
        """
        return self._status(self._load_state(run_id))

    def _derive_outcome(self, state):
        """What the journal says the run found, computed the one way.

        `status()` reports it and `recover()`/`settle()` persist it, so a
        reader and a writer cannot drift apart.
        """
        if state["lifecycle"] in ("settled", "published"):
            return state.get("outcome") or ""
        if [a for a in state["attempts"] if a.get("status") in ("sent", "abandoned")]:
            return "inconclusive"
        if state.get("outcome"):
            return state["outcome"]
        try:
            closed = self._now() > _at(state["_manifest"]["expires_at"], "expires_at")
        except Refused:
            closed = False
        if closed and state["lifecycle"] in ("admitted", "preflight_passed"):
            return "inconclusive"
        return ""

    def _status(self, state, slot_run=None):
        manifest = state["_manifest"]
        if slot_run is None:
            slot_run = (self._read_slot().get("run_id") or "")
        outcome = self._derive_outcome(state)
        lifecycle = state["lifecycle"]
        used = len(state["attempts"])
        intervention = lifecycle == "cleanup_required" or outcome == "inconclusive"
        detail = ""
        if lifecycle == "cleanup_required":
            detail = (state.get("cleanup") or {}).get("detail", "cleanup is not verified")
        elif outcome == "inconclusive":
            detail = "an attempt did not resolve, or the window closed without a verdict"
        elif outcome == "refused":
            detail = state.get("refused_code", "")
        return RunStatus(
            run_id=state["run_id"], item_id=state.get("item_id", ""),
            target=manifest["target"], exercise=manifest["exercise"],
            lifecycle=lifecycle, outcome=outcome,
            reported="cleanup_required" if lifecycle == "cleanup_required" else outcome,
            attempts_used=used, attempts_remaining=max(0, MAX_REQUESTS - used),
            expires_at=manifest["expires_at"], slot_held=(slot_run == state["run_id"]),
            cleanup_recorded=bool(state.get("cleanup")),
            published_commit=(state.get("publication") or {}).get("commit", ""),
            needs_intervention=intervention, detail=_bounded(detail),
            receipts=tuple(_receipt(a) for a in state["attempts"]))


def _receipt(attempt):
    return AttemptReceipt(
        seq=int(attempt.get("seq", 0)), phase=attempt.get("phase", ""),
        status=attempt.get("status", ""), started_at=attempt.get("started_at", ""),
        completed_at=attempt.get("completed_at", ""), http_status=int(attempt.get("http_status", 0)),
        body_bytes=int(attempt.get("body_bytes", 0)), body_sha256=attempt.get("body_sha256", ""),
        error_code=_bounded(attempt.get("error_code", ""), 64))
