#!/usr/bin/env python3
"""Checked evidence for a settled live-test run, and its one publication.

A live test is closed by an evidence record, never by an agent's say-so
(ADR-0151). This module is where the record comes from. It reads a run's
frozen manifest and journal through their owner, `livetest_state.RunStore`,
checks every fact it will state, renders exactly two documents — a raw
evidence record and a work note — and publishes them on the orchestrator's
`main` through the one landing routine, `factory_lib.land_backlog_change`.
The completer's live-result path (`completer.publish_live_results`) is its only
caller in the factory; nothing here contacts production, and a publication
retried any number of times never repeats the production read.

What reaches the committed documents, and what never does
----------------------------------------------------------

Only bounded, validated journal and manifest fields: identities, instants,
the recipe and its digest, the attempt receipts (sequence, phase, status, HTTP
status, byte count, SHA-256 of the body, a typed error code), the observed
`build`, `ready`, bounded `status`, `service`, `processGroup`, the failed
assertion names, the preflight refusal's condition name and the cleanup
record. Never a response body (`attempt-<n>.body` is not opened), never a
component or reason string, never a token, never bead text or anything an
agent wrote. The rendered text is scanned for token-like data as a last fence;
a value that fails its pattern, or text that looks like a credential, refuses
the whole publication rather than being redacted into it.

The five reports are published distinctly:

  pass              the observation met every fixed assertion. The ONLY report
                    that completes an item, and only the manifest's own item.
  feature_failure   the deployed target failed an assertion. The work note is
                    the linked defect record; the item's acceptance stays open.
  refused           preflight refused (a named condition). Evidence about the
                    workflow, never validation of the feature; the item stays
                    open, and no other item is touched.
  inconclusive      an attempt did not resolve or the window closed without a
                    verdict. The work note is an intervention record. No
                    replacement request is made; a rerun is a new run id.
  cleanup_required  cleanup is not a verified no-op. Intervention; the slot
                    stays held until a person settles it. The journal cannot
                    record a publication at this lifecycle, so the remote
                    commit alone makes it idempotent.

Publication, and why it is idempotent
-------------------------------------

The publication commit carries the trailers `Live-Run: <run id>` and
`Live-Evidence: <evidence digest>`. Before any commit, `publish_run` fetches
and looks for that run's trailer on `origin/main`:

  found        the commit landed and its receipt was lost (the process died
               after the push, or the push reply was lost). The journal is
               told the commit that is already there; no second commit.
  not found    the documents are rendered in a disposable detached worktree
               of the owned clone, `<city>/proof/live-publish/<run id>`, cut
               from the fetched `origin/main`, and landed from it. A process
               killed mid-publication leaves only that worktree behind, and
               the next attempt removes it before it starts. The owned clone's
               own working tree is never written, so an interrupted publication
               cannot wedge the completer's other work.

Because the worktree is cut from the same fetched `origin/main` the trailer
search read, any other landing of the same run in between moves `main`, and the
landing routine's push is refused (it retries on the next pass, which then
finds the trailer).

The callable interface
----------------------

    pending(city)                       -> ([run_id, ...], [(name, code, detail), ...])
        settled or cleanup_required runs not yet recorded as published, and
        every run directory whose journal cannot be read — a problem is
        reported, never skipped as if it were an empty result.
    check(store, run_id)                -> Evidence       (raises EvidenceRefused)
    render(evidence, item_path=None)    -> {repo-relative path: text}
        exactly the manifest's two output paths; raises EvidenceRefused
        `token_like` if the rendered text looks like a credential.
    output_paths(item_id, run_id, admitted_at) -> [raw path, work-note path]
        the naming admission (BKLG-20260920-n0tn) freezes into the manifest.
    publish_run(city, clone, run_id, *, now=None, hook=None,
                shared_checkout="", land=None)   -> PublicationReceipt
        raises EvidenceRefused (the evidence cannot be published; a person
        must look) or factory_lib.Refused (the landing did not happen; the
        next pass retries from the journal).

`EvidenceRefused.code` is one of FAILURE_CODES below, or the store's own code
(`livetest_state.FAILURE_CODES`) passed through unchanged when the journal
itself refuses.

The receipt schema (`proof.liskov.livetest-publication.v1`):

    PublicationReceipt(kind, run_id, item_id, report, action, commit,
                       journal_recorded, item_effect, paths, evidence_digest)
      action:       published | reconciled | already_recorded
      item_effect:  closed | left_open | held_last_child | not_ours |
                    identity_changed | missing
      journal_recorded: the store now names `commit` as the publication

A closure happens only for `pass`, when the manifest's item is still `working`
under a Gas City seat, its live fields and its shaped text are the ones that
were admitted (`item_blob_sha`), and completing it would not leave its epic
active with no active child (the completer's own last-child rule). Otherwise
the evidence is still published and the effect says why the item was left.

Standard library only. The clock, the failure-injection hook and the landing
routine are injected, so `gascity/tests/test_livetest_evidence.py` drives real
temporary git repositories and the real run store with no network.
"""
from __future__ import annotations

import glob
import importlib.util
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factory_lib as fl  # noqa: E402  the one landing routine
import livetest_contract as lt  # noqa: E402  the one parser for the record shape
import livetest_runner as lr  # noqa: E402  the recipe's trusted request constants
import livetest_state as ls  # noqa: E402  the one run store
import work_authority as wa  # noqa: E402  Git or Beads work authority (ADR-0236)

EVIDENCE_KIND = "proof.liskov.livetest-evidence.v1"
PUBLICATION_KIND = "proof.liskov.livetest-publication.v1"
REPORTS = ("pass", "feature_failure", "refused", "inconclusive", "cleanup_required")
ACTIONS = ("published", "reconciled", "already_recorded")
ITEM_EFFECTS = ("closed", "left_open", "held_last_child", "not_ours", "identity_changed", "missing")
RUN_TRAILER = "Live-Run"
EVIDENCE_TRAILER = "Live-Evidence"
SEAT_PREFIX = fl.admit.SEAT_PREFIX       # the claims the factory itself holds
CONTRACT_DOC = "docs/knowledge/production-validation-observe-contract.md"

# Every refusal this module adds to the store's own. The completer and
# `gc proof status` branch on the code, never on the sentence.
FAILURE_CODES = (
    "cleanup_missing",         # settled with no cleanup record, or one that contradicts the lifecycle
    "evidence_mismatch",       # the verdict and the facts it rests on disagree
    "exercise_unauthorized",   # mutate/spend: recognised, never published as implemented
    "foreign_path",            # an output path outside the two evidence documents, or taken on main
    "identity_mismatch",       # item, target, build or recipe of the facts is not the admitted one
    "publication_duplicated",  # more than one commit on main claims this run
    "publication_mismatch",    # a commit claims this run but does not carry its documents
    "receipt_invalid",         # an attempt receipt that is not a well-formed spent read
    "token_like",              # the rendered text looks like it carries a credential
    "unsafe_value",            # a field outside its pattern: it would copy arbitrary text
    "not_settled",             # the run has not settled; it has nothing to publish yet
)

RAW_PATH_RE = re.compile(r"^docs/raw/(\d{4}-\d{2}-\d{2})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
NOTE_PATH_RE = re.compile(
    r"^docs/work-notes/sessions/(\d{4}-\d{2}-\d{2})/\1-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
CODE_RE = re.compile(r"^[a-z][a-z_]{0,63}$")
STATUS_TEXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
ASSERTIONS = ("build_not_full", "build_mismatch", "not_ready")
# Exactly what the runner journals for an observation (livetest_runner.observe).
OBSERVED_KEYS = ("http_status", "build", "expected_build", "ready", "status", "service",
                 "processGroup", "failed_assertions", "received_at")

# The last fence. Every rendered value has already passed a narrow pattern, so a
# hit here means a pattern let something through that should not be public.
TOKEN_PATTERNS = (
    ("fly_macaroon", re.compile(r"FlyV1\s")),
    ("fly_token", re.compile(r"\bfm[12]_[A-Za-z0-9+/=_-]{8,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("secret_assignment", re.compile(
        r"(?i)\b(?:token|secret|password|passwd|api[_-]?key|authorization)\s*[:=]\s*\S{6,}")),
    # A long opaque run mixing upper case, lower case and digits. Digests and
    # builds are lower-case hex and paths are hyphenated, so neither matches.
    ("opaque_run", re.compile(r"(?<![A-Za-z0-9+/=])(?=[A-Za-z0-9+/=]*[A-Z])(?=[A-Za-z0-9+/=]*[a-z])"
                              r"(?=[A-Za-z0-9+/=]*[0-9])[A-Za-z0-9+/=]{32,}")),
)


class EvidenceRefused(Exception):
    """The evidence cannot be published as it stands. `code` is typed."""

    def __init__(self, code, detail=""):
        self.code = code
        self.detail = ls._bounded(detail)
        super().__init__("%s: %s" % (code, self.detail) if self.detail else code)


@dataclass(frozen=True)
class Evidence:
    """The checked facts of one run. `facts` is exactly what is rendered."""
    run_id: str
    item_id: str
    report: str
    lifecycle: str
    published_commit: str
    raw_path: str
    note_path: str
    expected_build: str
    target: str
    exercise: str
    recipe: str
    item_blob_sha: str
    facts: dict
    digest: str

    @property
    def paths(self):
        return (self.raw_path, self.note_path)


@dataclass(frozen=True)
class PublicationReceipt:
    kind: str
    run_id: str
    item_id: str
    report: str
    action: str
    commit: str
    journal_recorded: bool
    item_effect: str
    paths: tuple
    evidence_digest: str

    def as_dict(self):
        return {"kind": self.kind, "run_id": self.run_id, "item_id": self.item_id,
                "report": self.report, "action": self.action, "commit": self.commit,
                "journal_recorded": self.journal_recorded, "item_effect": self.item_effect,
                "paths": list(self.paths), "evidence_digest": self.evidence_digest}


# ---------------------------------------------------------------- naming

def output_paths(item_id, run_id, admitted_at):
    """The two documents a run may write, named from its identity. Admission
    freezes these into the manifest; the publisher renders nothing else."""
    if not ls.ITEM_ID_RE.match(item_id or "") or not ls.RUN_ID_RE.match(run_id or ""):
        raise EvidenceRefused("identity_mismatch", "an item id and a run id name the evidence")
    day = ls.instant(ls._at(admitted_at, "admitted_at"))[:10]
    slug = "livetest-%s-%s" % (item_id.lower(), run_id[:12])
    return ["docs/raw/%s-%s.md" % (day, slug),
            "docs/work-notes/sessions/%s/%s-%s.md" % (day, day, slug)]


def _paths(paths):
    if not isinstance(paths, list) or len(paths) != 2:
        raise EvidenceRefused("foreign_path", "a run writes exactly one raw record and one work note, "
                              "got %r" % (paths,))
    raw = [p for p in paths if isinstance(p, str) and RAW_PATH_RE.match(p)]
    note = [p for p in paths if isinstance(p, str) and NOTE_PATH_RE.match(p)]
    if len(raw) != 1 or len(note) != 1:
        raise EvidenceRefused("foreign_path", "output paths %s are not one docs/raw/ record and one "
                              "docs/work-notes/sessions/ note" % ", ".join(map(str, paths)))
    return raw[0], note[0]


# ---------------------------------------------------------------- checking

def _instant(value, what):
    if not isinstance(value, str) or not ls.INSTANT_RE.match(value):
        raise EvidenceRefused("unsafe_value", "%s must be an instant, got %r" % (what, value))
    return value


def _receipts(receipts):
    if len(receipts) > ls.MAX_REQUESTS:
        raise EvidenceRefused("receipt_invalid", "%d attempts journalled; the budget is %d"
                              % (len(receipts), ls.MAX_REQUESTS))
    out = []
    for n, r in enumerate(receipts, 1):
        where = "attempt %s" % r.seq
        if r.seq != n or r.phase != ls.REQUEST_PHASES[n - 1]:
            raise EvidenceRefused("receipt_invalid", "%s is %s at position %d; reads are preflight "
                                  "then observation" % (where, r.phase, n))
        if r.status not in ("completed", "abandoned"):
            raise EvidenceRefused("receipt_invalid", "%s is %r after settlement" % (where, r.status))
        _instant(r.started_at, where + " started_at")
        _instant(r.completed_at, where + " completed_at")
        if not 0 <= r.http_status <= 599:
            raise EvidenceRefused("receipt_invalid", "%s HTTP status %r" % (where, r.http_status))
        if not 0 <= r.body_bytes <= ls.RESPONSE_BYTES_MAX or \
                (r.body_bytes == 0) != (r.body_sha256 == "") or \
                (r.body_sha256 and not HEX64_RE.match(r.body_sha256)):
            raise EvidenceRefused("receipt_invalid", "%s body receipt is not a byte count and its "
                                  "SHA-256" % where)
        if r.error_code and not CODE_RE.match(r.error_code):
            raise EvidenceRefused("unsafe_value", "%s error code is not a typed code" % where)
        out.append({"seq": r.seq, "phase": r.phase, "status": r.status, "started_at": r.started_at,
                    "completed_at": r.completed_at, "http_status": r.http_status,
                    "body_bytes": r.body_bytes, "body_sha256": r.body_sha256,
                    "error_code": r.error_code})
    return out


def _observation(state, outcome, manifest, request, receipts):
    """The observed fields, re-derived: a verdict the facts do not support refuses."""
    evidence = state.get("evidence")
    observed = [r for r in receipts if r["phase"] == "observation" and r["status"] == "completed"]
    if outcome not in ("pass", "feature_failure"):
        if evidence is not None:
            raise EvidenceRefused("evidence_mismatch", "outcome %r carries an observation; only a "
                                  "judged read does" % (outcome,))
        return None
    if not observed or not isinstance(evidence, dict):
        raise EvidenceRefused("evidence_mismatch", "outcome %s has no completed, judged observation"
                              % outcome)
    if set(evidence) != set(OBSERVED_KEYS):
        raise EvidenceRefused("evidence_mismatch", "the observation carries fields %s"
                              % ", ".join(sorted(set(evidence) ^ set(OBSERVED_KEYS))))
    build, ready, status = evidence["build"], evidence["ready"], evidence["status"]
    if not isinstance(build, str) or (build and not lt.BUILD_RE.match(build)):
        raise EvidenceRefused("unsafe_value", "observed build is not a forty-hex commit")
    if evidence["expected_build"] != manifest["expected_build"]:
        raise EvidenceRefused("identity_mismatch", "the observation was judged against another build")
    if not isinstance(ready, bool):
        raise EvidenceRefused("unsafe_value", "observed ready is not a boolean")
    if not isinstance(status, str) or (status and not STATUS_TEXT_RE.match(status)):
        raise EvidenceRefused("unsafe_value", "observed status is not a bounded status word")
    if evidence["service"] != request["service"] or evidence["processGroup"] != request["process_group"]:
        raise EvidenceRefused("identity_mismatch", "the observation is not from %s/%s"
                              % (request["service"], request["process_group"]))
    if evidence["http_status"] != observed[0]["http_status"]:
        raise EvidenceRefused("evidence_mismatch", "the observation's HTTP status is not its receipt's")
    _instant(evidence["received_at"], "received_at")
    derived = []
    if not build:
        derived.append("build_not_full")
    elif build != manifest["expected_build"]:
        derived.append("build_mismatch")
    if ready is not True:
        derived.append("not_ready")
    if evidence["failed_assertions"] != ",".join(derived):
        raise EvidenceRefused("evidence_mismatch", "failed assertions %r do not follow from the "
                              "observed fields (%r)" % (evidence["failed_assertions"], ",".join(derived)))
    if outcome == "pass" and (derived or not 200 <= evidence["http_status"] < 300):
        raise EvidenceRefused("evidence_mismatch", "a pass whose facts do not pass: %s, HTTP %d"
                              % (",".join(derived) or "no failed assertion", evidence["http_status"]))
    if outcome == "feature_failure" and not derived:
        raise EvidenceRefused("evidence_mismatch", "a feature_failure with no failed assertion")
    return {k: evidence[k] for k in OBSERVED_KEYS}


def check(store, run_id):
    """Every fact the documents will state, checked against the journal and
    the frozen manifest, or a typed refusal. Reads nothing but the store."""
    try:
        # `RunStore` has no public manifest reader; `_load_state` is the one
        # path that verifies manifest, journal and their identities together
        # (the runner reads the manifest the same way).
        state = store._load_state(run_id)
        status = store.status(run_id)
    except ls.Refused as why:
        raise EvidenceRefused(why.code, why.detail)
    manifest = state["_manifest"]
    if state.get("item_id") != manifest["item_id"] or status.item_id != manifest["item_id"]:
        raise EvidenceRefused("identity_mismatch", "journal names item %r, manifest %r"
                              % (state.get("item_id"), manifest["item_id"]))
    if not lt.is_authorized_exercise(manifest["exercise"]):
        raise EvidenceRefused("exercise_unauthorized", "exercise %r is not implemented"
                              % manifest["exercise"])
    request = lr.RECIPE_REQUESTS.get(manifest["recipe"])
    if request is None or request["target"] != manifest["target"]:
        raise EvidenceRefused("identity_mismatch", "recipe %s has no registered request for %s"
                              % (manifest["recipe"], manifest["target"]))
    raw_path, note_path = _paths(manifest["output_paths"])

    lifecycle = status.lifecycle
    if lifecycle not in ("settled", "published", "cleanup_required"):
        raise EvidenceRefused("not_settled", "run %s is at %s" % (run_id, lifecycle))
    report = status.reported
    if report not in REPORTS or (lifecycle == "cleanup_required") != (report == "cleanup_required"):
        raise EvidenceRefused("evidence_mismatch", "run %s at %s reports %r" % (run_id, lifecycle, report))

    cleanup = state.get("cleanup") or {}
    if not isinstance(cleanup, dict) or not cleanup:
        raise EvidenceRefused("cleanup_missing", "run %s has no cleanup record" % run_id)
    verified = cleanup.get("verified_no_op")
    if lifecycle == "cleanup_required":
        if verified is not False:
            raise EvidenceRefused("cleanup_missing", "cleanup_required with a cleanup record that "
                                  "does not say it is unverified")
    elif verified is not True or cleanup.get("recipe") != manifest["recipe"] or \
            manifest["recipe"] not in ls.NO_OP_CLEANUP_RECIPES:
        raise EvidenceRefused("cleanup_missing", "run %s settled without a verified no-op cleanup "
                              "for %s" % (run_id, manifest["recipe"]))
    cleanup_at = _instant(cleanup.get("at"), "cleanup at")

    receipts = _receipts(status.receipts)
    outcome = status.outcome
    observed = _observation(state, outcome, manifest, request, receipts)
    refused_code = ""
    if outcome == "refused":
        refused_code = state.get("refused_code") or ""
        if not CODE_RE.match(refused_code):
            raise EvidenceRefused("unsafe_value", "a refusal names its condition, got %r" % (refused_code,))
    ended = [h for h in state.get("history") or [] if h.get("to") in ("settled", "cleanup_required")]
    if not ended:
        raise EvidenceRefused("not_settled", "run %s has no settlement in its history" % run_id)
    settled_at = _instant(ended[-1].get("at"), "settled at")

    facts = {
        "kind": EVIDENCE_KIND, "run_id": run_id, "item_id": manifest["item_id"],
        "item_blob_sha": manifest["item_blob_sha"], "report": report, "lifecycle":
        "settled" if lifecycle == "published" else lifecycle, "outcome": outcome,
        "exercise": manifest["exercise"], "recipe": manifest["recipe"],
        "recipe_version": manifest["recipe_version"], "recipe_digest": manifest["recipe_digest"],
        "target": manifest["target"], "expected_build": manifest["expected_build"],
        "admitted_at": manifest["admitted_at"], "expires_at": manifest["expires_at"],
        "max_age_seconds": manifest["max_age_seconds"],
        "credential_profile": manifest["credential_profile"],
        "manifest_digest": manifest["digest"], "attempts": receipts,
        "attempts_used": len(receipts), "observation": observed, "refused_code": refused_code,
        "cleanup": {"verified_no_op": verified, "at": cleanup_at}, "settled_at": settled_at,
        "output_paths": [raw_path, note_path],
    }
    return Evidence(run_id=run_id, item_id=manifest["item_id"], report=report, lifecycle=lifecycle,
                    published_commit=status.published_commit, raw_path=raw_path, note_path=note_path,
                    expected_build=manifest["expected_build"], target=manifest["target"],
                    exercise=manifest["exercise"], recipe=manifest["recipe"],
                    item_blob_sha=manifest["item_blob_sha"], facts=facts, digest=ls.digest(facts))


# ---------------------------------------------------------------- rendering

def scan(text):
    """The names of the token patterns this text matches; [] is clean."""
    return [name for name, pattern in TOKEN_PATTERNS if pattern.search(text)]


SUMMARY = {
    "pass": "The observation met every fixed assertion of `{recipe}`: the full build equals the "
            "admitted build, `ready` is true, and the reply is from `{service}`/`{group}`.",
    "feature_failure": "**Defect.** The deployed target failed the fixed assertion(s) `{failed}`. "
                       "This record is the linked defect: the item's acceptance stays open, and a "
                       "repair is an ordinary implementation packet, not part of this run.",
    "refused": "Preflight refused at condition `[{refused}]` before any observation. A correct "
               "refusal is evidence about the workflow, never validation of the feature: the item "
               "stays open and no other item is touched.",
    "inconclusive": "**Intervention.** An attempt did not resolve, or the window closed without a "
                    "verdict. Observe recovery makes no replacement request; a rerun is a new run "
                    "id, admitted explicitly.",
    "cleanup_required": "**Intervention.** Cleanup for `{recipe}` is not a verified no-op, so the "
                        "run holds the city's live slot until a person settles it.",
}


def _rel(target, start_file):
    return os.path.relpath(target, os.path.dirname(start_file)).replace(os.sep, "/")


def _summary(facts, request):
    obs = facts["observation"] or {}
    return SUMMARY[facts["report"]].format(
        recipe=facts["recipe"], service=request["service"], group=request["process_group"],
        failed=obs.get("failed_assertions") or "none", refused=facts["refused_code"] or "none")


def _frontmatter(title, doc_type, status, day, sources):
    lines = ["---", 'title: "%s"' % title, "doc_type: %s" % doc_type, "status: %s" % status,
             "owner: agent", "created: %s" % day, "last_reviewed: %s" % day]
    lines += (["source_documents:"] + ["  - %s" % s for s in sources]) if sources else \
        ["source_documents: []"]
    lines += ["confidence: high", "related_repos: []", "---", "", ""]
    return "\n".join(lines)


def render(evidence, item_path=None):
    """The two documents, from the checked facts alone. Deterministic: the same
    evidence renders the same bytes. `item_path` is the item's repo-relative
    path when it exists in the tree being published, for links."""
    facts = evidence.facts
    request = lr.RECIPE_REQUESTS[facts["recipe"]]
    raw, note = evidence.raw_path, evidence.note_path
    day = facts["admitted_at"][:10]
    item = facts["item_id"]
    item_ref = ("[`%s`](%s)" % (item, _rel(item_path, raw))) if item_path else "`%s`" % item
    note_item_ref = ("[`%s`](%s)" % (item, _rel(item_path, note))) if item_path else "`%s`" % item
    obs = facts["observation"]

    rows = [("Report", facts["report"]), ("Outcome", facts["outcome"] or "none"),
            ("Lifecycle", facts["lifecycle"]), ("Run", facts["run_id"]), ("Item", item),
            ("Exercise", facts["exercise"]), ("Recipe", "%s (%s)" % (facts["recipe"],
                                                                     facts["recipe_version"])),
            ("Recipe digest", facts["recipe_digest"]), ("Target", facts["target"]),
            ("Expected build", facts["expected_build"]), ("Admitted", facts["admitted_at"]),
            ("Window closes", facts["expires_at"]),
            ("Freshness limit", "%d s" % facts["max_age_seconds"]),
            ("Credential profile (reference)", facts["credential_profile"]),
            ("Item blob at admission", facts["item_blob_sha"]),
            ("Manifest digest", facts["manifest_digest"]), ("Settled", facts["settled_at"]),
            ("Reads spent", "%d of %d" % (facts["attempts_used"], ls.MAX_REQUESTS))]
    if facts["refused_code"]:
        rows.append(("Preflight refused at", "[%s]" % facts["refused_code"]))
    body = ["# Live-test evidence — %s, run %s" % (item, facts["run_id"][:12]), "",
            "Rendered by `gascity/packs/proof/assets/scripts/livetest_evidence.py` from the run's "
            "frozen manifest and its journal, for %s. Every value below is a bounded, validated "
            "journal or manifest field. No response body, component or reason string, credential "
            "or agent statement is copied. The contract is `%s`." % (item_ref, CONTRACT_DOC), "",
            "## Result", "", _summary(facts, request), "", "| Field | Value |", "| --- | --- |"]
    body += ["| %s | `%s` |" % (k, v) for k, v in rows]
    body += ["", "## Attempts", "",
             "| Seq | Phase | Status | Started | Completed | HTTP | Bytes | Body SHA-256 | Error |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    body += ["| %d | %s | %s | `%s` | `%s` | %d | %d | `%s` | %s |" % (
        a["seq"], a["phase"], a["status"], a["started_at"], a["completed_at"], a["http_status"],
        a["body_bytes"], a["body_sha256"] or "none", ("`%s`" % a["error_code"]) if a["error_code"]
        else "none") for a in facts["attempts"]] or ["| — | no read was sent | | | | | | | |"]
    if obs:
        body += ["", "## Observation", "", "| Field | Value |", "| --- | --- |"]
        body += ["| %s | `%s` |" % (k, str(obs[k]).lower() if isinstance(obs[k], bool) else
                                    (obs[k] if obs[k] != "" else "none")) for k in OBSERVED_KEYS]
        body += ["", "A `ready: true` reading with a degraded `status` is a build and readiness "
                 "result, not proof that the whole service is healthy."]
    body += ["", "## Cleanup", "",
             ("Verified no-op, recorded %s: the recipe creates no production resource."
              if facts["cleanup"]["verified_no_op"] else
              "NOT a verified no-op, recorded %s: the live slot stays held for a person.")
             % facts["cleanup"]["at"],
             "", "## Receipt", "", "Evidence digest `%s` (SHA-256 of the canonical facts below)."
             % evidence.digest, "", "```json", json.dumps(facts, sort_keys=True, indent=1), "```", ""]
    raw_text = _frontmatter("Live-test evidence for %s run %s" % (item, facts["run_id"][:12]), "raw",
                            "raw", day, [_rel(item_path, raw)] if item_path else []) + \
        "\n".join(body)

    effect = ("A pass completes %s, and nothing else." % note_item_ref if facts["report"] == "pass"
              else "The acceptance of %s stays open; no item is completed by this run." % note_item_ref)
    note_body = ["# Live result `%s` — %s" % (facts["report"], item), "", "## Result", "",
                 _summary(facts, request), "", "## Effect on the backlog", "", effect,
                 "Only a `pass` completes an item, and only the item the run was admitted for.", "",
                 "## Evidence", "",
                 "Run `%s`. The [raw evidence record](%s) holds the manifest, the %d spent read(s), "
                 "%sthe cleanup record and the receipt (evidence digest `%s`)." % (
                     facts["run_id"], _rel(raw, note), facts["attempts_used"],
                     "the observed fields, " if obs else "", evidence.digest), ""]
    note_text = _frontmatter("Live result: %s for %s run %s" % (facts["report"], item,
                                                                facts["run_id"][:12]),
                             "work_note", "done", day, [_rel(raw, note)]) + "\n".join(note_body)

    docs = {raw: raw_text, note: note_text}
    hits = sorted({name for text in docs.values() for name in scan(text)})
    if hits:
        raise EvidenceRefused("token_like", "the rendered evidence matches %s; nothing is published"
                              % ", ".join(hits))
    if set(docs) != set(facts["output_paths"]):
        raise EvidenceRefused("foreign_path", "rendered paths are not the manifest's output paths")
    return docs


# ---------------------------------------------------------------- the store side

def pending(city, *, now=None):
    """Runs with settled evidence not yet recorded as published, and problems.

    A run directory whose journal cannot be read is a problem with its code,
    never an absent result: an error that reads like an empty queue is how a
    live test's evidence is lost without anyone deciding to lose it."""
    store = ls.RunStore(city, now=now)
    if not os.path.isdir(store.runs_root):
        return [], []
    ready, problems = [], []
    for name in sorted(os.listdir(store.runs_root)):
        if name.startswith("."):
            continue
        if not ls.RUN_ID_RE.match(name):
            problems.append((name, "unsafe_path", "not a run directory"))
            continue
        try:
            status = store.status(name)
        except ls.Refused as why:
            problems.append((name, why.code, why.detail))
            continue
        if status.lifecycle in ("settled", "cleanup_required"):
            ready.append(name)
    return ready, problems


# ---------------------------------------------------------------- publication

def _git(where, *args):
    return fl.sh(["git", "-C", where, *args])


def _must(done, what):
    if done.returncode != 0:
        raise fl.Refused("%s failed: %s" % (what, (done.stderr or done.stdout).strip()[-300:]))
    return done.stdout


def reconcile(clone, evidence):
    """The commit on the fetched origin/main that already published this run,
    or ''. Refuses a duplicate, a trailer without its documents, and an output
    path that exists on main although no commit published it for this run."""
    marker = "%s: %s" % (RUN_TRAILER, evidence.run_id)
    found = _must(_git(clone, "log", "origin/main", "--format=%H", "--fixed-strings", "--grep", marker),
                  "searching main for %s" % marker).split()
    if len(found) > 1:
        raise EvidenceRefused("publication_duplicated", "%d commits on main claim run %s: %s"
                              % (len(found), evidence.run_id, ", ".join(c[:10] for c in found)))
    if found:
        for path in evidence.paths:
            shown = _git(clone, "show", "%s:%s" % (found[0], path))
            if shown.returncode != 0 or evidence.run_id not in shown.stdout:
                raise EvidenceRefused("publication_mismatch", "commit %s claims run %s but does not "
                                      "carry %s" % (found[0][:10], evidence.run_id, path))
        return found[0]
    for path in evidence.paths:
        if _git(clone, "cat-file", "-e", "origin/main:%s" % path).returncode == 0:
            raise EvidenceRefused("foreign_path", "%s already exists on main, and no commit published "
                                  "it for run %s" % (path, evidence.run_id))
    return ""


def _drop_worktree(clone, path):
    if os.path.islink(path):
        raise fl.Refused("%s is a symlink; the publish worktree must be a directory" % path)
    if os.path.exists(path):
        _git(clone, "worktree", "remove", "--force", path)
        shutil.rmtree(path, ignore_errors=True)
    _git(clone, "worktree", "prune")


def _docs_model(tree):
    """The tree's own docs model, loaded under a private name so it cannot be
    confused with whichever copy the caller already imported."""
    name = "_livetest_docs_model"
    spec = importlib.util.spec_from_file_location(name, os.path.join(tree, "scripts", "docs_model.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module            # its dataclasses resolve their module by name
    quiet, sys.dont_write_bytecode = sys.dont_write_bytecode, True   # no __pycache__ in the tree
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
        sys.dont_write_bytecode = quiet
    return module


def _ledger_effect(clone, evidence):
    """`_item_effect` under the Beads authority: judged on the snapshot row,
    never a file. "closed" means the item is ours to complete through the
    client once the evidence has landed (`_ledger_record`)."""
    row = wa.row(clone, evidence.item_id, fresh=True)
    if row is None:
        return "missing", None, None
    rel = row.get("path")
    if evidence.report != "pass":
        return "left_open", rel, None
    fm = row.get("fields") or {}
    if fm.get("status") == "done":
        return "closed", rel, None            # an earlier pass completed it; the receipt was lost
    if fm.get("status") != "working" or not str(fm.get("claimed_by") or "").startswith(SEAT_PREFIX) \
            or not row.get("lease_id"):
        return "not_ours", rel, None
    live = {"live_target": evidence.target, "exercise": evidence.exercise,
            "live_recipe": evidence.recipe, "live_expected_build": evidence.expected_build}
    if row.get("semantic_digest") != evidence.item_blob_sha or \
            any(str(fm.get(k) or "").strip() != v for k, v in live.items()):
        return "identity_changed", rel, None
    return "closed", rel, None


def _ledger_record(clone, evidence, commit, effect):
    """After the documents landed as `commit`: for a pass that is ours,
    complete the item through the client with the landing commit as its merge
    and main identity, THEN record the raw evidence entry on it. The order
    matters: `fields.evidence` is part of the semantic digest the evaluator
    binds a completion to, so an entry appended first would change the digest
    the attempt was bound to and the completion would refuse for ever;
    evidence may grow on a done item. The helper's last-child refusal holds the
    item for a person, as the Git path does; any other refusal leaves it open
    and says why. Returns the item effect."""
    seat_row = wa.row(clone, evidence.item_id, fresh=True)
    if seat_row is None or effect in ("missing",):
        return effect
    fm = seat_row.get("fields") or {}
    holder = fm.get("claimed_by") if str(fm.get("claimed_by") or "").startswith(SEAT_PREFIX) else ""
    if effect == "closed" and fm.get("status") == "working":
        if not holder:
            effect = "not_ours"
        else:
            lease = seat_row.get("lease_id") or ""
            try:
                wa.client(clone, "complete", item=evidence.item_id, by=holder, lease=str(lease), request={
                    "expected_semantic_digest": seat_row.get("semantic_digest"), "resolution": "done",
                    "evidence": {"merge_sha": commit, "main_sha": commit,
                                 "attempts": [{"bead_id": evidence.run_id, "rig": fl.admit.seat_rig(holder),
                                               "factory": fl.admit.factory_name(), "outcome": evidence.report}],
                                 "acceptance": {"kind": EVIDENCE_KIND, "run_id": evidence.run_id,
                                                "report": evidence.report, "evidence_digest": evidence.digest,
                                                "raw": evidence.raw_path}},
                    "notes": "Completed by the PROOF live-result publisher: run %s passed; evidence at %s"
                             % (evidence.run_id, commit[:10])})
                wa.forget(clone)
            except fl.Refused as why:
                wa.forget(clone)
                if wa.is_last_child_refusal(why):
                    effect = "held_last_child"
                else:
                    fl.log("completer", "live run %s: the work ledger refused the completion — %s; the item stays open"
                           % (evidence.run_id, why))
                    effect = "left_open"
    elif effect == "closed" and fm.get("status") != "done":
        effect = "not_ours"
    # The raw record is evidence on the item whatever happened to it, as the
    # Git path's `evidence:` entry is — appended after the completion, and
    # only by a seat that holds or held the item (the actor the ledger knows).
    actor = holder or fl.admit.seat("completer")   # a done item has no holder; the publisher signs
    current = wa.row(clone, evidence.item_id, fresh=True) or seat_row
    entry = _rel(evidence.raw_path, current["path"])
    if (current.get("fields") or {}).get("status") in ("working", "done") \
            and entry not in ((current.get("fields") or {}).get("evidence") or []):
        try:
            wa.client(clone, "evidence", item=evidence.item_id, by=actor,
                      request={"entries": [entry], "receipts": {"kind": PUBLICATION_KIND, "run_id": evidence.run_id,
                                                               "report": evidence.report, "commit": commit,
                                                               "evidence_digest": evidence.digest}})
            wa.forget(clone)
        except fl.Refused as why:
            fl.log("completer", "live run %s: the work ledger refused the evidence entry — %s" % (evidence.run_id, why))
    return effect


def _item_effect(tree, evidence, dm, today):
    """(effect, item repo-relative path or None, new text or None) for this run."""
    files = glob.glob(os.path.join(tree, "docs", "backlog", "items", evidence.item_id + "-*.md"))
    if len(files) != 1:
        return "missing", None, None
    rel = os.path.relpath(files[0], tree).replace(os.sep, "/")
    if evidence.report != "pass":
        return "left_open", rel, None
    with open(files[0], encoding="utf-8") as fh:
        text = fh.read()
    fm = dm.split_frontmatter(text)[0]
    if not isinstance(fm, dict) or fm.get("status") != "working" or \
            not str(fm.get("claimed_by") or "").startswith(SEAT_PREFIX):
        return "not_ours", rel, None
    admitted = _git(tree, "cat-file", "blob", evidence.item_blob_sha)
    live = {"live_target": evidence.target, "exercise": evidence.exercise,
            "live_recipe": evidence.recipe, "live_expected_build": evidence.expected_build}
    if admitted.returncode != 0 or fl.shaped(admitted.stdout) != fl.shaped(text) or \
            any(str(fm.get(k) or "").strip() != v for k, v in live.items()):
        return "identity_changed", rel, None
    corpus = dm.Corpus.load(os.path.join(tree, "docs"))
    epic = fm.get("parent_epic")
    if epic and not any(dm.record_id(other) != evidence.item_id and other.fm.get("parent_epic") == epic
                        and other.status in dm.ACTIVE_STATUSES for other in corpus.items):
        return "held_last_child", rel, None
    kept = [e for e in (fm.get("evidence") or []) if isinstance(e, str)]
    raw_ref = _rel(evidence.raw_path, rel)
    new = dm.rewrite_frontmatter_keys(
        text, {"status": "done", "last_reviewed": today, "done_at": today,
               "evidence": kept + [e for e in (raw_ref,) if e not in kept]},
        remove=("claimed_by", "claimed_at", "claim_expires"))
    return "closed", rel, new


def _message(evidence, effect):
    said = {"closed": "It completes %s." % evidence.item_id,
            "held_last_child": "It passes, but %s is the last active item of its epic; completing it "
                               "is held for a person." % evidence.item_id,
            "not_ours": "It passes, but %s is not `working` under a Gas City seat on main; the item "
                        "is left to whoever holds it." % evidence.item_id,
            "identity_changed": "It passes, but %s changed since admission; a pass for the admitted "
                                "item does not complete the re-shaped one." % evidence.item_id,
            "missing": "%s is not on main; nothing is completed." % evidence.item_id,
            "left_open": "The acceptance of %s stays open." % evidence.item_id}[effect]
    return ("Publish live-test evidence for %s: %s\n\n"
            "Rendered by the PROOF completer's live-result path\n"
            "(gascity/packs/proof/assets/scripts/livetest_evidence.py) from run %s's settled\n"
            "journal. %s No production request was made to publish it.\n\n"
            "Refs %s, ADR-0151.\n\n%s: %s\n%s: %s\n"
            % (evidence.item_id, evidence.report, evidence.run_id, said, evidence.item_id,
               RUN_TRAILER, evidence.run_id, EVIDENCE_TRAILER, evidence.digest))


def _receipt(evidence, action, commit, recorded, effect):
    return PublicationReceipt(kind=PUBLICATION_KIND, run_id=evidence.run_id, item_id=evidence.item_id,
                              report=evidence.report, action=action, commit=commit,
                              journal_recorded=recorded, item_effect=effect, paths=evidence.paths,
                              evidence_digest=evidence.digest)


def _effect_of(clone, evidence, commit):
    """What an earlier publication commit did to the item: only a pass that
    completed it touches the item file. Under Beads the ledger is asked, and a
    pass whose receipt was lost before the ledger heard is recorded now."""
    if wa.beads(clone):
        effect, _rel_path, _text = _ledger_effect(clone, evidence)
        return _ledger_record(clone, evidence, commit, effect)
    touched = _git(clone, "diff-tree", "--no-commit-id", "--name-only", "-r", commit).stdout.split()
    return "closed" if any(p.startswith("docs/backlog/items/%s-" % evidence.item_id)
                           for p in touched) else "left_open"


def _record(store, evidence, commit):
    """Tell the journal. A cleanup_required run cannot be moved to published by
    its store, so for it the remote commit is the only record."""
    if evidence.lifecycle == "cleanup_required":
        return False
    try:
        store.record_publication(evidence.run_id, commit)
    except ls.Refused as why:
        raise EvidenceRefused(why.code, why.detail)
    return True


def publish_run(city, clone, run_id, *, now=None, hook=None, shared_checkout="", land=None):
    """Publish one settled run's evidence on main, at most once. See the module
    docstring for the protocol; `hook(event)` is called at "fetched",
    "rendered" and "landed" so a test can stop the process exactly there."""
    now = now or (lambda: datetime.now(timezone.utc))
    land = land or fl.land_backlog_change
    event = hook or (lambda _name: None)
    store = ls.RunStore(city, now=now)
    evidence = check(store, run_id)
    if evidence.published_commit:
        return _receipt(evidence, "already_recorded", evidence.published_commit, True,
                        _effect_of(clone, evidence, evidence.published_commit))

    _must(_git(clone, "fetch", "--quiet", "--prune", "origin"), "fetching the orchestrator")
    event("fetched")
    found = reconcile(clone, evidence)
    if found:
        effect = _effect_of(clone, evidence, found)
        if evidence.lifecycle == "cleanup_required":
            return _receipt(evidence, "already_recorded", found, False, effect)
        return _receipt(evidence, "reconciled", found, _record(store, evidence, found), effect)

    tree = os.path.join(city, "proof", "live-publish", run_id)
    _drop_worktree(clone, tree)           # a killed attempt's leftovers, never built on
    os.makedirs(os.path.dirname(tree), exist_ok=True)
    _must(_git(clone, "worktree", "add", "--quiet", "--detach", tree, "origin/main"),
          "cutting the publish worktree")
    try:
        dm = _docs_model(tree)
        native = wa.beads(clone)
        effect, item_rel, item_text = (_ledger_effect(clone, evidence) if native
                                       else _item_effect(tree, evidence, dm, ls.instant(now())[:10]))
        if native and item_rel is not None and not os.path.isfile(os.path.join(tree, item_rel)):
            item_rel = None                   # a virtual record: nothing on disk to link to
        docs = render(evidence, item_rel)
        for path, text in docs.items():
            target = os.path.join(tree, path)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(text)
        if item_text is not None:
            with open(os.path.join(tree, item_rel), "w", encoding="utf-8") as fh:
                fh.write(item_text)
        changed = {line[3:] for line in _must(_git(tree, "status", "--porcelain", "--untracked-files=all"),
                                               "reading the publish worktree").splitlines() if line.strip()}
        allowed = set(docs) | ({item_rel} if item_text is not None else set())
        if not changed <= allowed:
            raise EvidenceRefused("foreign_path", "publication would change %s"
                                  % ", ".join(sorted(changed - allowed)))
        event("rendered")
        landed = land(tree, _message(evidence, effect), shared_checkout, add=sorted(docs))
        event("landed")
        if native:
            effect = _ledger_record(clone, evidence, landed, effect)
        return _receipt(evidence, "published", landed, _record(store, evidence, landed), effect)
    finally:
        _drop_worktree(clone, tree)
