"""Deterministic checks for a version 1 ``## Shaping evidence`` JSON receipt.

A receipt records a shaper's assertions; passing does NOT establish that their
meaning covers the packet or that delivery will succeed. The shaping reviewer
owns semantic completeness. No receipt command is executed, no fetch occurs,
and Git is read only. Callers decide whether absent legacy receipts are allowed.

Example (place this JSON alone in a json fence under ## Shaping evidence;
substitute real commit hashes, exact source quotes and Acceptance text):

    {
      "version": 1,
      "sources": [{"id": "owner", "repo": "liskov-rs",
        "commit": "0123456789012345678901234567890123456789",
        "path": "crates/owner.rs", "quote": "pub fn authoritative_owner"}],
      "acceptance": [{"criterion": "Reject absent input", "owner": "owner",
        "proof": "cargo test -p owner", "companions": ["crates/owner_test.rs"]}],
      "prerequisites": [],
      "checks": [{"kind": "doctrine", "condition": "The owner permits this proof",
        "sources": ["owner"], "unresolved": []}],
      "review_required": false
    }

Every depends_on ID needs {"id": ID, "state": "available", "outputs": [source_id]}.
For known nonterminal dependencies, state "pending" permits empty outputs and
returns WAIT. Unresolved check findings likewise name nonterminal Q/BKLG IDs in
depends_on and return WAIT. A shape session may publish waiting work; admission
must refuse WAIT. When an upstream dependency becomes terminal, its old pending
receipt remains WAIT (refresh_required/decision_changed), so completing the
producer never invalidates the global documentation corpus. checks.kind is "stop" or "doctrine"; at least one doctrine
row is required. The reviewer must check that EVERY acceptance and applicable
stop/doctrine condition is covered; arbitrary prose cannot be certified here.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import PurePosixPath
import re
import subprocess
import tempfile

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 128
TERMINAL = {"terminal", "done", "superseded", "answered"}
NONTERMINAL = {"open", "working", "blocked", "proposed", "paused"}
DEPENDENCY = re.compile(r"(?:Q|BKLG|EPIC)-\d{8}-[a-z0-9]{4}\Z")


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str


def section(body, heading):
    match = re.search(r"(?ms)^## " + re.escape(heading) + r"[ \t]*\n(.*?)(?=^## |\Z)", body)
    return match.group(1) if match else ""


def _path(value):
    return (isinstance(value, str) and bool(value) and not value.startswith(("/", "-"))
            and "\\" not in value and not any(ord(c) < 32 for c in value)
            and all(part not in ("", ".", "..") for part in value.split("/"))
            and not PurePosixPath(value).is_absolute())


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _git(root, *args):
    """Bound output on disk and time; bypass replace refs and optional locks."""
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(
                ["git", "--no-replace-objects", "--no-optional-locks", "-C", str(root), *args],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                timeout=5, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("git read failed or timed out") from exc
        if result.returncode:
            raise ValueError("git object/ref is unavailable")
        output.seek(0)
        data = output.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("git evidence exceeds 2 MiB")
        return data


def _blob(root, commit, path):
    # Check size first: a malicious evidence path cannot stream a huge blob.
    spec = commit + ":" + path
    size = int(_git(root, "cat-file", "-s", spec))
    if size > MAX_BYTES:
        raise ValueError("source exceeds 2 MiB")
    if _git(root, "cat-file", "-t", spec).strip() != b"blob":
        raise ValueError("source is not a file blob")
    return _git(root, "cat-file", "blob", spec).decode("utf-8")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def check_evidence(body, *, depends_on=(), dependency_status=None,
                   validation_commands=(), review_required=False,
                   repo_roots=None, current_refs=None, require=False,
                   check_current=True, verify_sources=True):
    """Return Finding(severity='BLOCK'|'WARN'|'WAIT', code, message) values.

    repo_roots maps receipt repo names to local Git roots. dependency_status
    maps depends_on IDs to docs_model's terminal/open/unknown, or actual status.
    Set check_current=False only for historical backtests; normal checks compare
    source content with local origin/main (current_refs may override per repo).
    verify_sources=False is schema-only validation for clean CI clones without
    member repositories; it is not evidence verification or admission clearance.
    Missing receipts warn unless require=True. Present invalid receipts block.
    """
    findings = []

    def report(code, message, severity="BLOCK"):
        findings.append(Finding(severity, code, message))

    headings = re.findall(r"(?m)^## Shaping evidence[ \t]*$", body)
    if not headings:
        report("missing", "no Shaping evidence receipt; reshape with pinned owner/proof evidence",
               "BLOCK" if require else "WARN")
        return findings
    if len(headings) != 1:
        report("receipt", "exactly one Shaping evidence section is allowed")
        return findings
    content = section(body, "Shaping evidence").strip()
    match = re.fullmatch(r"```json[ \t]*\n(.*?)\n```", content, re.S)
    if not match or len(content.encode()) > MAX_BYTES:
        report("receipt", "Shaping evidence must contain only one fenced JSON object (at most 2 MiB)")
        return findings
    try:
        receipt = json.loads(match.group(1), object_pairs_hook=_unique_object,
                             parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    except (ValueError, RecursionError):
        report("receipt", "malformed JSON or duplicate JSON keys")
        return findings
    keys = {"version", "sources", "acceptance", "prerequisites", "checks", "review_required"}
    if not isinstance(receipt, dict) or set(receipt) != keys or type(receipt.get("version")) is not int or receipt["version"] != 1:
        report("schema", "expected version 1 and exactly: " + ", ".join(sorted(keys)))
        return findings
    for name in ("sources", "acceptance", "prerequisites", "checks"):
        if not isinstance(receipt[name], list) or len(receipt[name]) > MAX_ROWS or (name != "prerequisites" and not receipt[name]):
            report("schema", name + " must be a nonempty array of at most 128 rows (prerequisites may be empty)")
    if type(receipt["review_required"]) is not bool:
        report("schema", "review_required must be an explicit boolean")
    elif receipt["review_required"] and not review_required:
        report("review", "receipt requires a reviewer: set frontmatter review: required")
    if any(f.code == "schema" for f in findings):
        return findings
    if (not isinstance(depends_on, (list, tuple, set)) or any(not _text(v) for v in depends_on)
            or not isinstance(validation_commands, (list, tuple, set))
            or any(not _text(v) for v in validation_commands)
            or (dependency_status is not None and (not isinstance(dependency_status, dict)
                or any(not _text(k) or not _text(v) for k, v in dependency_status.items())))):
        report("schema", "caller must supply string dependency/command lists and a string status mapping")
        return findings
    roots = repo_roots or {}
    sources = {}
    for source in receipt["sources"]:
        if not isinstance(source, dict) or set(source) != {"id", "repo", "commit", "path", "quote"}:
            report("source", "source requires exactly id, repo, commit, path, quote")
            continue
        sid = source["id"]
        if not _text(sid) or sid in sources:
            report("source", "source IDs must be unique nonempty strings")
            continue
        sources[sid] = source
        if (not _text(source["repo"]) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", source["repo"]) is None
                or not isinstance(source["commit"], str)
                or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source["commit"]) is None
                or not _path(source["path"]) or not _text(source["quote"])):
            report("source", sid + ": expected full commit SHA, safe relative path and nonempty exact quote")
            continue
        if not verify_sources:
            continue
        root = roots.get(source["repo"])
        if root is None:
            report("source", sid + ": repository has no local resolver: " + source["repo"])
            continue
        try:
            commit = source["commit"]
            # Require a commit, not an arbitrary object with a hex object ID.
            if _git(root, "cat-file", "-t", commit).strip() != b"commit":
                raise ValueError("pin is not a commit")
            text = _blob(root, commit, source["path"])
            if source["quote"] not in text:
                raise ValueError("exact quote is absent at pinned commit")
            if check_current:
                ref = (current_refs or {}).get(source["repo"], "refs/remotes/origin/main")
                if not isinstance(ref, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", ref) is None or ".." in ref:
                    raise ValueError("unsafe current reference")
                current = _git(root, "rev-parse", "--verify", ref + "^{commit}").decode().strip()
                if _git(root, "merge-base", commit, current).decode().strip() != commit:
                    raise ValueError("pinned commit is not an ancestor of the current authority reference")
                try:
                    unchanged = _blob(root, current, source["path"]) == text
                except ValueError:
                    unchanged = False
                if not unchanged:
                    report("evidence_stale", sid + ": source changed at current reference; recheck and repin the evidence")
        except (ValueError, UnicodeError) as exc:
            report("source", sid + ": " + str(exc))

    def refs(values, label, *, nonempty=True):
        if (not isinstance(values, list) or len(values) > MAX_ROWS or (nonempty and not values)
                or any(not _text(v) for v in values)):
            report("reference", label + ": expected source ID array")
        elif len(set(values)) != len(values) or any(v not in sources for v in values):
            report("reference", label + ": duplicate or unknown source ID")

    commands = set(validation_commands)
    seen = set()
    for row in receipt["acceptance"]:
        if not isinstance(row, dict) or set(row) != {"criterion", "owner", "proof", "companions"}:
            report("acceptance", "acceptance row requires exactly criterion, owner, proof, companions")
            continue
        criterion = row["criterion"]
        if not _text(criterion) or criterion in seen:
            report("acceptance", "criterion must be nonempty and unique")
        else:
            seen.add(criterion)
            if criterion not in section(body, "Acceptance"):
                report("acceptance", "criterion must quote text in the packet's Acceptance section: " + criterion)
        refs([row["owner"]], "acceptance owner")
        if not _text(row["proof"]) or row["proof"] not in commands:
            report("proof", "acceptance proof must name an exact declared validation command")
        if not isinstance(row["companions"], list) or len(row["companions"]) > MAX_ROWS or any(not _path(p) for p in row["companions"]):
            report("acceptance", "companions must be safe repo-relative paths (empty is explicit none)")

    dependencies = set(depends_on)
    statuses = dependency_status or {}
    seen = set()
    for row in receipt["prerequisites"]:
        if not isinstance(row, dict) or set(row) != {"id", "state", "outputs"}:
            report("prerequisite", "prerequisite row requires exactly id, state, outputs")
            continue
        pid = row["id"]
        if not _text(pid) or not DEPENDENCY.fullmatch(pid) or pid in seen or pid not in dependencies:
            report("prerequisite", "prerequisite must uniquely name a Q/BKLG/EPIC id in depends_on")
            continue
        seen.add(pid)
        status = statuses.get(pid, "unknown")
        if row["state"] == "pending":
            refs(row["outputs"], pid, nonempty=False)
            if status in TERMINAL:
                report("refresh_required", pid + ": dependency is now terminal; refresh its pending output evidence before admission", "WAIT")
            elif status in NONTERMINAL:
                report("pending", pid + ": output is pending; keep the packet blocked until evidence is refreshed", "WAIT")
            else:
                report("prerequisite", pid + ": pending requires a known dependency")
        elif row["state"] == "available":
            refs(row["outputs"], pid)
            if status not in TERMINAL:
                report("prerequisite", pid + ": available requires a terminal dependency and pinned output evidence")
        else:
            report("prerequisite", pid + ": state must be available or pending")
    for pid in sorted(dependencies - seen):
        report("prerequisite", pid + ": depends_on has no prerequisite output row")

    seen = set()
    doctrine = False
    for row in receipt["checks"]:
        if not isinstance(row, dict) or set(row) != {"kind", "condition", "sources", "unresolved"}:
            report("check", "check row requires exactly kind, condition, sources, unresolved")
            continue
        if row["kind"] not in ("stop", "doctrine"):
            report("check", "check kind must be stop or doctrine")
        doctrine = doctrine or row["kind"] == "doctrine"
        condition = row["condition"]
        if not _text(condition) or condition in seen:
            report("check", "check conditions must be unique nonempty strings")
        else:
            seen.add(condition)
        refs(row["sources"], "condition evidence")
        unresolved = row["unresolved"]
        if not isinstance(unresolved, list) or len(unresolved) > MAX_ROWS or any(not _text(p) for p in unresolved):
            report("check", "unresolved must be a dependency ID array (empty means resolved)")
        else:
            for pid in unresolved:
                if not DEPENDENCY.fullmatch(pid) or pid.startswith("EPIC-") or pid not in dependencies or statuses.get(pid) not in NONTERMINAL | TERMINAL:
                    report("check", "unresolved finding must name a known Q/BKLG in depends_on: " + pid)
                elif statuses[pid] in TERMINAL:
                    report("decision_changed", pid + ": dependency is now terminal; reconcile its unresolved finding before admission", "WAIT")
                else:
                    report("pending", pid + ": unresolved shaping finding keeps the packet blocked", "WAIT")
    if not doctrine:
        report("check", "at least one doctrine check is required")
    return findings
