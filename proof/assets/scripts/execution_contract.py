"""Canonical proof.contract.v1 compiler and verifier (ADR-0235).

Contracts are execution evidence, never a second task/claim owner. Every decision
reads one freshly fetched docs revision; the base is provenance, not a global
Git freshness test. Callers serialize admissions with admit.admission_lock.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import sys
import tempfile
import tomllib
import uuid

import admit

SCHEMA = "proof.contract.v1"
PILOTS = {"liskov-agent-orchestrator", "liskov-rs"}
VOLATILE = {"status", "claimed_by", "claimed_at", "claim_expires", "last_reviewed"}
HEX = re.compile(r"^[0-9a-f]{64}$")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def atomic_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def git(clone, *args):
    return admit.run(["git", "-C", str(clone), *args]).stdout.strip()


def snapshot(clone):
    # No mutable-checkout fallback on lock contention. git show reads this SHA.
    git(clone, "fetch", "origin", "main")
    return git(clone, "rev-parse", "origin/main")


def parse(clone, text):
    sys.path.insert(0, str(Path(clone) / "scripts"))
    import docs_model
    fm, body, errors = docs_model.split_frontmatter(text)
    if errors or not isinstance(fm, dict):
        raise admit.Refused("contract: malformed source document")
    return fm, body


def record(clone, revision, item):
    paths = git(clone, "ls-tree", "-r", "--name-only", revision, "docs").splitlines()
    matches = [p for p in paths if Path(p).name.startswith(item + "-") and p.endswith(".md")]
    if len(matches) != 1:
        raise admit.Refused("contract: expected one authoritative record for " + item)
    path = matches[0]
    fm, body = parse(clone, git(clone, "show", revision + ":" + path))
    return path, fm, body


def semantic(fm, body):
    # Only frontmatter bookkeeping is excluded. Body lines named status, etc.
    # remain task meaning. Lifecycle and ownership are always checked separately.
    return {"fields": {k: v for k, v in fm.items() if k not in VOLATILE}, "body": body}


def claim_identity(item, fm, rig, instant=None):
    try:
        started = admit.parse_ts(fm["claimed_at"])
        expires = admit.parse_ts(fm["claim_expires"])
    except (KeyError, TypeError, ValueError) as exc:
        raise admit.Refused("contract: malformed claim timestamps") from exc
    instant = instant or admit.now()
    if (fm.get("status") != "working" or fm.get("claimed_by") != admit.SEAT_PREFIX + rig
            or expires <= instant or started > instant or expires <= started):
        raise admit.Refused("contract: claim is expired, replaced or no longer working")
    return {"packet": item, "owner": fm["claimed_by"], "claimed_at": started.isoformat()}


def local_path(parent, reference):
    if not isinstance(reference, str) or not reference:
        raise admit.Refused("contract: malformed source reference")
    if re.match(r"^[a-z]+://", reference):
        return None  # The exact URI is bound, never pretend remote content was read.
    path = posixpath.normpath(posixpath.join(posixpath.dirname(parent), reference.split("#")[0]))
    if path.startswith(("../", "/")):
        raise admit.Refused("contract: source escapes repository: " + reference)
    return path


def sources(clone, revision, path, fm):
    identities = {}
    def add(p):
        text = git(clone, "show", revision + ":" + p)
        # Generated epic children/index rows do not change governing prose.
        text = re.sub(r"<!-- BEGIN GENERATED [^>]+ -->.*?<!-- END GENERATED [^>]+ -->",
                      "", text, flags=re.S)
        identities[p] = digest(text)
        return text
    for p in ("AGENTS.md", "docs/meta/documentation-system.md"):
        add(p)
    authority_paths = git(clone, "ls-tree", "-r", "--name-only", revision, "docs/meta/doctrine").splitlines()
    for authority in authority_paths:
        if authority.endswith(".md"):
            add(authority)
    if fm.get("parent_epic"):
        epic_path, epic_fm, _ = record(clone, revision, fm["parent_epic"])
        add(epic_path)
        for ref in epic_fm.get("reads") or []:
            target = local_path(epic_path, ref)
            if target is not None:
                add(target)
    for ref in list(fm.get("reads") or []) + list(fm.get("source_documents") or []):
        p = local_path(path, ref)
        if p is not None:
            add(p)
        else:
            identities[ref] = digest(ref)
    prerequisites = {}
    for dep in fm.get("depends_on") or []:
        p, fields, body = record(clone, revision, dep)
        if fields.get("status") not in ("done", "superseded", "answered"):
            raise admit.Refused("contract: prerequisite is not terminal: " + dep)
        outputs = {}
        for ref in fields.get("evidence") or []:
            target = local_path(p, ref)
            outputs[ref] = digest(ref) if target is None else digest(add(target))
        prerequisites[dep] = {"record": digest({"fields": fields, "body": body}), "outputs": outputs}
    return identities, prerequisites


def recipe(pack, rig_config, review_policy):
    files = {str(p.relative_to(pack)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(Path(pack).rglob("*")) if p.is_file()
             and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    if not files:
        raise admit.Refused("contract: recipe pack is empty")
    return digest({"files": files, "rig": rig_config, "review_policy": review_policy})


def compile_contract(clone, revision, item, rig, repo, base, recipe_digest, landing, review, execution=None, rig_clone=None, rig_revision=None):
    path, fm, body = record(clone, revision, item)
    if repo not in PILOTS or landing not in admit.LANDING or type(review) is not bool:
        raise admit.Refused("contract: unsupported repository or authorization")
    identity = claim_identity(item, fm, rig)
    relevant, prerequisites = sources(clone, revision, path, fm)
    if repo != admit.BACKLOG_REPO:
        if rig_clone is None or rig_revision is None:
            raise admit.Refused("contract: member authority snapshot is required")
        authority_paths = git(rig_clone, "ls-tree", "-r", "--name-only", rig_revision,
                              "AGENTS.md", "docs/agents").splitlines()
        if not authority_paths:
            raise admit.Refused("contract: member governing authority is missing")
        for source in authority_paths:
            if source.endswith(".md"):
                relevant[repo + ":" + source] = digest(git(rig_clone, "show", rig_revision + ":" + source))
    result = {"schema": SCHEMA, "packet": item, "rig": rig, "repository": repo,
              "item_path": path, "task_digest": digest(semantic(fm, body)),
              "sources": relevant, "prerequisites": prerequisites,
              "scope": fm.get("write_scope"), "claim": identity, "execution": execution or {},
              "base": base, "docs_revision": revision, "recipe_digest": recipe_digest,
              "validation": fm.get("validation") or [], "acceptance": body,
              "publication": {"landing": landing, "review": review}}
    result["id"] = digest(result)
    validate(result)
    return result


def validate(contract):
    schema = json.loads((Path(__file__).parent / "schemas/proof.contract.v1.json").read_text())
    if not isinstance(contract, dict) or set(contract) != set(schema["required"]):
        raise admit.Refused("contract: malformed fields")
    types = {"string": str, "object": dict, "array": list}
    for key, spec in schema["properties"].items():
        if type(contract[key]) is not types[spec["type"]]:
            raise admit.Refused("contract: malformed " + key)
    if (contract["schema"] != SCHEMA or contract["repository"] not in PILOTS
            or not admit.ID_RE.fullmatch(contract["packet"])
            or not all(HEX.fullmatch(contract[k]) for k in ("id", "task_digest", "recipe_digest"))
            or not all(re.fullmatch(r"[0-9a-f]{40}", contract[k]) for k in ("base", "docs_revision"))
            or not contract["scope"] or not all(isinstance(x, str) for x in contract["scope"])
            or not all(isinstance(x, str) for x in contract["validation"])
            or set(contract["claim"]) != {"packet", "owner", "claimed_at"}
            or contract["claim"]["packet"] != contract["packet"]
            or contract["claim"]["owner"] != admit.SEAT_PREFIX + contract["rig"]
            or set(contract["publication"]) != {"landing", "review"}
            or contract["publication"]["landing"] not in admit.LANDING
            or type(contract["publication"]["review"]) is not bool
            or contract["id"] != digest({k: v for k, v in contract.items() if k != "id"})):
        raise admit.Refused("contract: invalid shape or content identity")
    return contract


def verify(contract, clone, revision, recipe_digest, landing=None, review=None, rig_clone=None, rig_revision=None):
    validate(contract)
    publication = contract["publication"]
    current = compile_contract(clone, revision, contract["packet"], contract["rig"],
        contract["repository"], contract["base"], recipe_digest,
        publication["landing"] if landing is None else landing,
        publication["review"] if review is None else review, contract["execution"], rig_clone, rig_revision)
    changed = [k for k in contract if k not in ("id", "docs_revision") and contract[k] != current[k]]
    if changed:
        raise admit.Refused("contract: reconciliation required: " + ", ".join(changed))
    return contract


def location(city, identity):
    if not isinstance(identity, str) or not HEX.fullmatch(identity):
        raise admit.Refused("contract: invalid identity")
    return Path(city) / "proof/contracts" / (identity + ".json")


def read(city, identity):
    try:
        result = validate(json.loads(location(city, identity).read_text()))
        if result["id"] != identity:
            raise admit.Refused("contract: identity does not match filename")
        return result
    except (OSError, ValueError, TypeError) as exc:
        raise admit.Refused("contract: missing or corrupt contract " + str(identity)) from exc


def persist(city, contract):
    validate(contract)
    path = location(city, contract["id"])
    if path.exists():
        if read(city, contract["id"]) != contract:
            raise admit.Refused("contract: immutable identity collision")
    else:
        atomic_write(path, contract)


def enabled(city, rig, repo):
    import factory_lib as fl
    value = fl.load_policy(city)["rigs"].get(rig, {}).get("execution_contracts", False)
    if type(value) is not bool or (value and repo not in PILOTS):
        raise admit.Refused("contract: execution_contracts requires a supported pilot rig")
    return value


def execution_vars(rigs, rig, work_dir, branch):
    formula = Path(__file__).resolve().parents[2] / "formulas/mol-proof-packet.toml"
    defaults = {k: str(v.get("default", "")) for k, v in tomllib.loads(formula.read_text())["vars"].items()}
    defaults.update(rigs[rig].get("FormulaVars") or {})
    result = {k: defaults.get(k, "") for k in ("gate_command", "gate_lane", "gate_path", "gate_home",
              "gate_target", "gate_timeout", "review_target", "base_branch", "shared_checkout")}
    result.update(work_dir=work_dir, branch=branch)
    return result


def recipe_for(city, rigs, rig):
    import factory_lib as fl
    # Effective merged config includes formula variables, providers and routing.
    # Exclude only local runtime counts/paths? None: conservative reconciliation.
    cfg = {k: rigs[rig].get(k) for k in ("Name", "DefaultBranch", "DefaultMergeStrategy",
           "FormulaVars", "Includes", "Imports", "Overrides", "RigPatches",
           "DefaultSlingTarget", "DefaultSlingTargets")}
    policy = fl.load_policy(city)["rigs"].get(rig, {})
    return recipe(Path(__file__).resolve().parents[2], cfg,
                  {k: policy.get(k) for k in ("review_if_scope_contains", "landing", "merge", "merge_requires")})


def attempt_path(city, item):
    if not admit.ID_RE.fullmatch(item):
        raise admit.Refused("contract: invalid packet")
    return Path(city) / "proof/contract-admissions" / (item + ".json")


def load_attempt(city, item):
    path = attempt_path(city, item)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
        if (set(value) != {"packet", "contract", "attempt", "state", "bead", "root"}
                or value["packet"] != item or not HEX.fullmatch(value["contract"])
                or not re.fullmatch(r"[0-9a-f]{32}", value["attempt"])
                or value["state"] not in ("prepared", "creating", "launching", "launched")):
            raise ValueError("invalid admission journal")
        return value
    except (OSError, ValueError, TypeError) as exc:
        raise admit.Refused("contract: corrupt admission journal") from exc


def prepare(city, contract):
    persist(city, contract)
    value = {"packet": contract["packet"], "contract": contract["id"],
             "attempt": uuid.uuid4().hex, "state": "prepared", "bead": "", "root": ""}
    save_attempt(city, value)
    return value


def save_attempt(city, attempt):
    atomic_write(attempt_path(city, attempt["packet"]), attempt)


def root_vars(bead):
    md = bead.get("metadata") or {}
    try:
        result = json.loads(md.get("gc.graphv2_vars.v1") or "{}")
    except (ValueError, TypeError) as exc:
        raise admit.Refused("contract: malformed workflow variables") from exc
    result.update({k.removeprefix("gc.var."): v for k, v in md.items() if k.startswith("gc.var.")})
    return result


def reconcile(attempt, beads):
    roots = [b for b in beads if (b.get("metadata") or {}).get("gc.kind") == "workflow"
             and root_vars(b).get("contract_attempt") == attempt["attempt"]]
    if len(roots) > 1:
        raise admit.Refused("contract: duplicate workflow identities; reconciliation required")
    if roots:
        variables = root_vars(roots[0])
        if variables.get("contract_id") != attempt["contract"] or variables.get("packet_id") != attempt["packet"]:
            raise admit.Refused("contract: workflow binding mismatch")
        return roots[0]
    work = [b for b in beads if (b.get("metadata") or {}).get("proof.contract_attempt") == attempt["attempt"]
            and (b.get("metadata") or {}).get("gc.kind") != "workflow"]
    if len(work) > 1 or any((b.get("metadata") or {}).get("proof.contract_id") != attempt["contract"] for b in work):
        raise admit.Refused("contract: duplicate or mismatched admission beads")
    if attempt["state"] == "creating" and work:
        attempt.update(bead=work[0]["id"], state="prepared")
    if attempt["bead"] and (not work or work[0]["id"] != attempt["bead"]):
        raise admit.Refused("contract: recorded work bead is missing; preserve admission")
    if attempt["state"] != "prepared":
        raise admit.Running("contract: uncertain launch " + attempt["attempt"] + "; preserve work and reconcile")
    return None


def verify_workspace(contract, member):
    work = contract["execution"].get("work_dir")
    branch = contract["execution"].get("branch")
    if not isinstance(work, str) or not Path(work).is_dir():
        raise admit.Refused("contract: owned workspace is missing")
    common = git(member, "rev-parse", "--path-format=absolute", "--git-common-dir")
    actual = git(work, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if Path(common).resolve() != Path(actual).resolve() or git(work, "branch", "--show-current") != branch:
        raise admit.Refused("contract: workspace no longer belongs to its recorded repository/branch")
    if admit.run(["git", "-C", work, "merge-base", "--is-ancestor", contract["base"], "HEAD"],
                 check=False).returncode != 0:
        raise admit.Refused("contract: workspace does not descend from its repository base")


def verify_root(city, root):
    variables = root_vars(root)
    identity = variables.get("contract_id", "")
    if not identity and not variables.get("contract_required") == "true":
        return None
    contract = read(city, identity)
    rigs = admit.load_rigs(city)
    clone = admit.backlog_clone_of(rigs)
    if (variables.get("packet_id") != contract["packet"]
            or not variables.get("contract_attempt")
            or variables.get("review") != str(contract["publication"]["review"]).lower()
            or any(variables.get(k) != v for k, v in admit.LANDING[contract["publication"]["landing"]].items())):
        raise admit.Refused("contract: workflow authority differs from contract")
    if not contract["execution"] or any(str(variables.get(k, "")) != str(v)
                                        for k, v in contract["execution"].items()):
        raise admit.Refused("contract: workflow execution variables differ from contract")
    md = root.get("metadata") or {}
    expected_scope = [s if ":" in s else contract["repository"] + ":" + s for s in contract["scope"]]
    if (md.get("proof.repo") != contract["repository"]
            or sorted((md.get("proof.write_scope") or "").splitlines()) != sorted(expected_scope)
            or md.get("proof.review") != ("on" if contract["publication"]["review"] else "off")):
        raise admit.Refused("contract: workflow scope or review metadata differs from contract")
    attempt = load_attempt(city, contract["packet"])
    if (attempt is None or attempt["contract"] != identity
            or attempt["attempt"] != variables["contract_attempt"]
            or (attempt["root"] and attempt["root"] != root["id"])):
        raise admit.Refused("contract: workflow has no matching admission")
    member = admit.owned_clone(rigs, contract["rig"])
    verify_workspace(contract, member)
    return verify(contract, clone, snapshot(clone), recipe_for(city, rigs, contract["rig"]),
                  rig_clone=member, rig_revision=snapshot(member))


def main():
    try:
        root = json.load(sys.stdin)
        verify_root(sys.argv[1], root[0] if isinstance(root, list) else root)
    except (admit.Refused, OSError, ValueError, KeyError) as exc:
        sys.exit("proof-contract: " + str(exc))


if __name__ == "__main__":
    main()
