"""PROOF work policy. Pure functions over records; no storage or document parsing."""
import fnmatch
import heapq
import os
import re
from datetime import datetime, timezone

FRONTIER_SCHEMA = "proof.liskov.delivery-frontier.v1"


PRIORITIES = ("p0", "p1", "p2", "p3")


PRIORITY_RANK = {value: index for index, value in enumerate(PRIORITIES)}


ACTIVE_STATUSES = {"open", "working"}


TERMINAL_STATUSES = {"done", "superseded", "answered"}


WORK_ID_RE = re.compile(r"^(BKLG|EPIC|Q)-(\d{8})-([a-z0-9]{4})$")


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


WRITE_SCOPE_RE = re.compile(r"^([a-z0-9][a-z0-9.-]*):(.+)$")


ORCHESTRATOR_SCOPE_REPO = "liskov-agent-orchestrator"


CLAIM_KEYS = ("claimed_by", "claimed_at", "claim_expires")


MAX_WORKING_EPICS = 25


def work_id_from_filename(filename):
    stem = filename.removesuffix(".md")
    parts = stem.split("-")
    if len(parts) >= 3 and parts[0] in ("BKLG", "EPIC", "Q"):
        candidate = "-".join(parts[:3])
        if WORK_ID_RE.match(candidate):
            return candidate
    return None


def record_id(record):
    """The id other records use to cite this one, or None."""
    if record.doc_type == "milestone":
        value = record.fm.get("milestone_id")
        return value if isinstance(value, str) else None
    return work_id_from_filename(record.filename)


def parse_timestamp(value):
    if not isinstance(value, str) or not TIMESTAMP_RE.match(value):
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def format_timestamp(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def milestone_sequence(record):
    try:
        return int(record.fm.get("sequence", 10**9))
    except (TypeError, ValueError):
        return 10**9


def as_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def dependency_list(record):
    value = record.fm.get("depends_on", [])
    return value if isinstance(value, list) else []


def dependency_cycles(records):
    """Storage-independent cycle validation shared by imports and docs checks."""
    state = {}
    cycles = []

    def visit(rid, trail):
        if state.get(rid) == 2:
            return
        if state.get(rid) == 1:
            cycles.append(trail + [rid])
            return
        state[rid] = 1
        for dep in dependency_list(records[rid]):
            if dep in records:
                visit(dep, trail + [rid])
        state[rid] = 2

    for rid in sorted(records):
        visit(rid, [])
    return cycles


def question_dependents(corpus, question):
    """Active items and epics whose authoritative edge names this question."""
    qid = record_id(question)
    return [r for r in corpus.items + corpus.epics
            if r.status in ACTIVE_STATUSES and qid in dependency_list(r)]


def dependency_state(corpus, dep_id):
    """'terminal' | 'open' | 'unknown' for one dependency id."""
    target = corpus.by_id.get(dep_id)
    if target is None or target.doc_type not in ("backlog", "epic", "question"):
        return "unknown"
    return "terminal" if target.status in TERMINAL_STATUSES else "open"


def blockers(corpus, record):
    """Ids in depends_on that are not terminal (unknown ids block too)."""
    return [
        dep
        for dep in dependency_list(record)
        if dependency_state(corpus, dep) != "terminal"
    ]


def merged_commit(record):
    """The merge commit a working item's pull request landed as, or None.

    The completer records `merged:<sha>` as an evidence entry the moment it
    sees the pull request MERGED (BKLG-20261016-dk56); the claim itself stays
    until `main` is green and the item is completed (ADR-0115)."""
    entries = record.fm.get("evidence")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        match = MERGED_EVIDENCE_RE.match(entry) if isinstance(entry, str) else None
        if match:
            return match.group(1)
    return None


def blocks_scope(record, now):
    """True while this record's claim excludes other claims from its write scope.

    A live claim blocks until its pull request has merged: after that the code
    is on `main` and a later packet merges it in like any other landing, so
    the claim keeps only its bookkeeping (WIP, awaiting-merge counts, red-main
    ownership) and stops serializing the rig."""
    return claim(record, now)[0] == "live" and merged_commit(record) is None


def claim(record, now):
    """Return (state, detail): state is 'none' | 'live' | 'expired' | 'malformed'."""
    fm = record.fm
    present = [key for key in CLAIM_KEYS if key in fm]
    if not present:
        return "none", None
    if len(present) != len(CLAIM_KEYS):
        return "malformed", f"claim needs all of {', '.join(CLAIM_KEYS)}"
    expires = parse_timestamp(fm.get("claim_expires"))
    claimed_at = parse_timestamp(fm.get("claimed_at"))
    if expires is None or claimed_at is None:
        return "malformed", "claimed_at and claim_expires must be YYYY-MM-DDTHH:MM:SSZ"
    if not isinstance(fm.get("claimed_by"), str) or not fm.get("claimed_by"):
        return "malformed", "claimed_by must be a non-empty string"
    if expires <= now:
        return "expired", f"claim by {fm['claimed_by']} expired {fm['claim_expires']}"
    return "live", f"claimed by {fm['claimed_by']} until {fm['claim_expires']}"


def claim_lifecycle_problems(record, now=None):
    state, detail = claim(record, now or now_utc())
    if record.status == 'working':
        if state == 'none':
            return ['working item must carry a claim (claimed_by, claimed_at, claim_expires); use `python3 scripts/next_work.py --claim <id> --by <name>`']
        if state == 'malformed':
            return [detail]
    elif state != 'none':
        return ['only a working item may carry claim fields (status is %s); release the claim or set working' % record.status]
    return []


def membership_problems(corpus, record):
    parent = record.fm.get('parent_epic')
    epic = corpus.epic(parent) if isinstance(parent, str) else None
    problems = []
    if record.status in ACTIVE_STATUSES:
        if not isinstance(parent, str) or not parent:
            problems.append('active backlog item must declare one parent_epic')
        elif epic is None:
            problems.append('parent_epic references unknown epic: ' + parent)
        elif epic.status == 'done':
            problems.append('active backlog item belongs to done epic: ' + parent)
    elif isinstance(parent, str) and parent and epic is None:
        problems.append('parent_epic references unknown epic: ' + parent)
    if epic is not None and str(record.fm.get('gate')).lower() == 'true':
        if str(epic.fm.get('gate')).lower() != 'true':
            problems.append(f'a gate item needs a gate parent epic ({parent} is not a gate)')
        if record.fm.get('milestone') != epic.fm.get('milestone'):
            problems.append(f"a gate item must share its parent epic's milestone ({epic.fm.get('milestone')})")
    return problems


def epic_lifecycle_problems(corpus, record):
    children = corpus.children(record_id(record))
    problems = []
    if record.status == 'working' and record.fm.get('owner') == 'unassigned':
        problems.append('working epic must have an assigned owner')
    if record.status in ACTIVE_STATUSES and not children:
        problems.append('active epic must have at least one active backlog item')
    if record.status == 'open' and any(child.status == 'working' for child in children):
        problems.append('open epic cannot contain working backlog items; mark the epic working')
    return problems


def epic_wip(corpus, now=None):
    """The portfolio capacity snapshot, shared by claims, validation and views.

    An epic consumes a slot only while one of its items carries a LIVE claim
    (ADR-0166). A `working` epic none of whose items is claimed is dormant: it
    keeps its status and its history, and it holds no capacity. `working` is
    the number of claimed epics, the figure the cap is measured against;
    `dormant` lists the working epics that hold no slot, so a reader can see
    why the two differ.
    """
    now = now or now_utc()
    claimed_parents = set()
    for record in corpus.items:
        if record.status in ACTIVE_STATUSES and claim(record, now)[0] == "live":
            parent = record.fm.get("parent_epic")
            if isinstance(parent, str):
                claimed_parents.add(parent)
    working = sorted(record_id(epic) for epic in corpus.epics if epic.status == "working")
    claimed = [eid for eid in working if eid in claimed_parents]
    return {"working": len(claimed), "limit": MAX_WORKING_EPICS,
            "available": max(0, MAX_WORKING_EPICS - len(claimed)), "epics": claimed,
            "dormant": [eid for eid in working if eid not in claimed_parents]}


def epic_start_blocker(corpus, record, wip=None, now=None):
    """Only starting an open epic consumes capacity; ongoing work may continue,
    including work in a dormant working epic, which is why the claimed count
    may legitimately exceed the cap and nothing validates it against one."""
    parent = record.fm.get("parent_epic")
    epic = corpus.epic(parent) if isinstance(parent, str) else None
    if epic is None or epic.status != "open":
        return None
    wip = wip if wip is not None else epic_wip(corpus, now)
    if wip["available"] == 0:
        return (f"epic WIP capacity exhausted ({wip['working']}/{wip['limit']} epics with a live claim); "
                f"finish or release claimed work before starting {record_id(epic)}")
    return None


def is_ready(corpus, record, now):
    """Ready requires terminal dependencies, no live claim and epic capacity."""
    if record.status not in ACTIVE_STATUSES:
        return False
    state, _detail = claim(record, now)
    if state == "live":
        return False
    return not blockers(corpus, record) and epic_start_blocker(corpus, record, now=now) is None


def sort_key(corpus, record):
    """The one total order: milestone, priority, gate, epic rank, rank, created, id."""
    fm = record.fm
    milestone = corpus.milestone(fm.get("milestone")) if isinstance(fm.get("milestone"), str) else None
    sequence = milestone_sequence(milestone) if milestone is not None else 10**9
    priority = PRIORITY_RANK.get(fm.get("priority"), len(PRIORITIES))
    gate = 0 if str(fm.get("gate")).lower() == "true" else 1
    if record.doc_type == "backlog":
        epic = corpus.epic(fm.get("parent_epic")) if isinstance(fm.get("parent_epic"), str) else None
        epic_rank = as_int(epic.fm.get("rank"), 10**9) if epic is not None else 10**9
        epic_created = epic.fm.get("created", "9999") if epic is not None else "9999"
    else:
        epic_rank = as_int(fm.get("rank"), 10**9)
        epic_created = fm.get("created", "9999")
    rank = as_int(fm.get("rank"), 10**9)
    return (
        sequence,
        priority,
        gate,
        epic_rank,
        epic_created,
        rank,
        fm.get("created", "9999"),
        record_id(record) or record.filename,
    )


def write_scopes(record):
    """Parsed write_scope entries as (repo, glob).

    Bare paths belong to the orchestrator. Wildcards stay in the glob so
    `docs/raw/*-iq59-*.md` is not collapsed to the `docs/raw` directory.
    """
    scopes = []
    value = record.fm.get("write_scope", [])
    if not isinstance(value, list):
        return scopes
    for entry in value:
        m = WRITE_SCOPE_RE.match(str(entry))
        if m is None:
            repo, path = ORCHESTRATOR_SCOPE_REPO, str(entry)
        else:
            repo, path = m.groups()
        scopes.append((repo, path.replace("\\", "/").strip("/")))
    return scopes


def scopes_overlap(a, b):
    for repo_a, glob_a in a:
        for repo_b, glob_b in b:
            if repo_a != repo_b:
                continue
            if _scope_globs_overlap(glob_a, glob_b):
                return True
    return False


# Paths a packet writes but never authors: regenerated from the tree by a
# repository-owned generator and checked by CI, so two packets that both
# regenerate them are not writing the same thing. They still route admission,
# doctrine and reviewers as scope; they do not make two claims exclusive.
# `.sqlx/**` is content-addressed (2026-09-26); the liskov-rs service census
# and Cargo metadata fixture come from `validate-services.py --write-*`, the
# policy-client bundle from `generate-policy-client-bundle.py --write`, and
# `Cargo.lock` from Cargo (2026-10-16, BKLG-20261016-dk56). A merge conflict
# on one is repaired by regenerating it (`pr_recovery.py integrate`).
# `crate-boundaries.toml` is hand-written but, since liskov-rs #1772, no
# packet edits its shared counter lines: a shrink or a stem removal is a
# `[[lowers]]` table in the packet's own raise file, and what a packet still
# touches in the registry is its own package row or its own stem's name,
# distinct lines that merge cleanly (owner decision 2026-10-16). It keeps
# routing the reviewer; it no longer serializes the rig.
CONTENT_ADDRESSED_SCOPE_ROOTS = (
    ".sqlx",
    "Cargo.lock",
    "crates/slipway-policy-client-contract/bundle",
    "scripts/liskov/crate-boundaries.toml",
    "scripts/liskov/services-cargo-metadata.json",
    "scripts/liskov/services-census.json",
)
MERGED_EVIDENCE_RE = re.compile(r"^merged:([0-9a-f]{7,40})$")


def _content_addressed(path):
    return any(path == root or path.startswith(root + "/") for root in CONTENT_ADDRESSED_SCOPE_ROOTS)


def claims_collide(a, b):
    """True if two claims' write scopes may not be held at the same time.

    `scopes_overlap` less content-addressed paths: they still route admission,
    doctrine and reviewers as scope, but they do not make two packets exclusive.
    Declaring `.sqlx/**` otherwise made every SQL-touching liskov-rs packet
    one global lock (2026-09-26: 5 free slots, 0 claimable).
    """
    return scopes_overlap([s for s in a if not _content_addressed(s[1])],
                          [s for s in b if not _content_addressed(s[1])])


def _scope_globs_overlap(a, b):
    """True if two write-scope globs can name the same path.

    The other glob is matched as a literal pathname, so
    `docs/raw/*-iq59-*.md` does not collide with `docs/raw/*-89c2-*.md`.
    A glob with no wildcards is a directory reservation: it covers that
    path and every descendant. A trailing `/**` also covers its directory.
    An empty glob is a whole-repo reservation.
    """
    return _scope_glob_covers(a, b) or _scope_glob_covers(b, a)


def _scope_glob_covers(pattern, path):
    pattern = pattern.replace("\\", "/").strip("/")
    path = path.replace("\\", "/").strip("/")
    if pattern == path or pattern == "":
        return True
    if not any(ch in pattern for ch in "*?["):
        return path.startswith(pattern + "/")
    if fnmatch.fnmatchcase(path, pattern):
        return True
    if pattern.endswith("/**"):
        parent = pattern[:-3]
        return parent == "" or path == parent or path.startswith(parent + "/")
    return False


def longest_remaining_dependency_chain(corpus, ordered):
    """Longest prerequisite-first path of unfinished items, not elapsed time.

    Questions, epics and unknown IDs are external waits, not item vertices.
    Include claimed items: their acceptance is still unfinished. A cycle (or
    a dependent of one) makes the result incomplete rather than a false zero.
    """
    records = {record_id(record): record for record in ordered}
    internal = {}
    external = {}
    dependents = {rid: [] for rid in records}
    for rid, record in records.items():
        waiting = set(blockers(corpus, record))
        internal[rid] = waiting & records.keys()
        external[rid] = waiting - records.keys()
        for dep in internal[rid]:
            dependents[dep].append(rid)
    pending = {rid: len(deps) for rid, deps in internal.items()}
    ready = [rid for rid, count in pending.items() if count == 0]
    heapq.heapify(ready)
    paths = {}
    while ready:
        rid = heapq.heappop(ready)
        prior = min((paths[dep] for dep in internal[rid]),
                    key=lambda path: (-len(path), path), default=())
        paths[rid] = prior + (rid,)
        for child in dependents[rid]:
            pending[child] -= 1
            if pending[child] == 0:
                heapq.heappush(ready, child)
    unresolved = sorted(records.keys() - paths.keys())
    path = min(paths.values(), key=lambda value: (-len(value), value), default=())
    return {
        "complete": not unresolved,
        "length": None if unresolved else len(path),
        "items": [] if unresolved else list(path),
        "externalBlockers": [] if unresolved else sorted({dep for rid in path for dep in external[rid]}),
        "unresolvedItems": unresolved,
    }


def frontier(corpus, now=None):
    """Compute the delivery frontier. Pure function of the corpus and `now`."""
    now = now or now_utc()
    active = [r for r in corpus.items if r.status in ACTIVE_STATUSES]
    ordered = sorted(active, key=lambda r: sort_key(corpus, r))
    wip = epic_wip(corpus, now)

    # Kahn layering over active items. Layer 0 has no item prerequisites;
    # claims and epic capacity still govern whether it is ready now. An
    # open question or epic is an external wait (layer None, listed separately).
    layer = {}
    changed = True
    ids = {record_id(r): r for r in ordered}
    guard = 0
    while changed and guard < 10_000:
        changed = False
        guard += 1
        for r in ordered:
            rid = record_id(r)
            if rid in layer:
                continue
            deps = blockers(corpus, r)
            if not deps:
                layer[rid] = 0
                changed = True
                continue
            item_deps = [d for d in deps if d in ids]
            external = [d for d in deps if d not in ids]
            if external:
                continue  # waits on a question, epic, or unknown id
            if all(d in layer for d in item_deps):
                layer[rid] = 1 + max(layer[d] for d in item_deps)
                changed = True

    ready, blocked, claimed, external_waits, capacity_blocked = [], [], [], [], []
    for r in ordered:
        rid = record_id(r)
        fm = r.fm
        state, detail = claim(r, now)
        row = {
            "id": rid,
            "path": os.path.relpath(r.path, corpus.root),
            "title": fm.get("title", ""),
            "status": r.status,
            "milestone": fm.get("milestone"),
            "lane": fm.get("lane"),
            "gate": str(fm.get("gate")).lower() == "true",
            "priority": fm.get("priority"),
            "parentEpic": fm.get("parent_epic"),
            "rank": as_int(fm.get("rank")),
            "packet": "timebox" in fm,
            "parallelGroup": fm.get("parallel_group"),
            "writeScope": fm.get("write_scope", []) if isinstance(fm.get("write_scope"), list) else [],
            "review": str(fm.get("review") or "").strip().lower() == "required",
            "relatedRepos": fm.get("related_repos", []) if isinstance(fm.get("related_repos"), list) else [],
            "dependsOn": dependency_list(r),
            "blockers": blockers(corpus, r),
            "claim": state,
            "claimDetail": detail,
            "claimedBy": fm.get("claimed_by"),
            "factory": fm.get("factory"),
            "capacityBlocker": epic_start_blocker(corpus, r, wip),
            "wave": layer.get(rid),
        }
        if state == "live":
            claimed.append(row)
        elif row["blockers"]:
            external = [d for d in row["blockers"] if d not in ids]
            (external_waits if external else blocked).append(row)
        elif row["capacityBlocker"]:
            capacity_blocked.append(row)
        else:
            ready.append(row)

    # Write-scope conflicts among ready items: serialize, do not parallelize.
    conflicts = []
    for i, a in enumerate(ready):
        sa = write_scopes(ids[a["id"]])
        if not sa:
            continue
        for b in ready[i + 1 :]:
            sb = write_scopes(ids[b["id"]])
            if sb and scopes_overlap(sa, sb):
                conflicts.append({"a": a["id"], "b": b["id"]})

    # Owner items: open questions that block active work, stale claims,
    # questions past their due date.
    blocking_questions = {}
    for r in ordered:
        for dep in blockers(corpus, r):
            target = corpus.by_id.get(dep)
            if target is not None and target.doc_type == "question":
                blocking_questions.setdefault(dep, []).append(record_id(r))
    owner_items = []
    for qid, blocked_ids in sorted(blocking_questions.items()):
        q = corpus.by_id[qid]
        owner_items.append(
            {
                "kind": "question",
                "id": qid,
                "path": os.path.relpath(q.path, corpus.root),
                "title": q.fm.get("title", ""),
                "askedOf": q.fm.get("asked_of"),
                "due": q.fm.get("due"),
                "blocks": sorted(blocked_ids),
            }
        )
    for q in sorted(corpus.questions, key=lambda r: record_id(r)):
        due = q.fm.get("due")
        if q.status == "open" and isinstance(due, str) and DATE_RE.match(due) and due < now.strftime("%Y-%m-%d"):
            if record_id(q) not in blocking_questions:
                owner_items.append(
                    {
                        "kind": "question",
                        "id": record_id(q),
                        "path": os.path.relpath(q.path, corpus.root),
                        "title": q.fm.get("title", ""),
                        "askedOf": q.fm.get("asked_of"),
                        "due": due,
                        "blocks": [],
                        "overdue": True,
                    }
                )
    stale_claims = [
        {"id": record_id(r), "path": os.path.relpath(r.path, corpus.root), "detail": claim(r, now)[1]}
        for r in ordered
        if claim(r, now)[0] == "expired"
    ]

    return {
        "schemaVersion": FRONTIER_SCHEMA,
        "generatedAt": format_timestamp(now),
        "sortKey": "(milestone.sequence, priority, gate, epic.rank, epic.created, rank, created, id)",
        "longestDependencyChain": longest_remaining_dependency_chain(corpus, ordered),
        "currentMilestone": ordered[0].fm.get("milestone") if ordered else None,
        "epicWip": wip,
        "counts": {
            "active": len(ordered),
            "ready": len(ready),
            "blocked": len(blocked),
            "externalWaits": len(external_waits),
            "capacityBlocked": len(capacity_blocked),
            "claimed": len(claimed),
            "staleClaims": len(stale_claims),
        },
        "ready": ready,
        "blocked": blocked,
        "externalWaits": external_waits,
        "capacityBlocked": capacity_blocked,
        "claimed": claimed,
        "staleClaims": stale_claims,
        "conflicts": conflicts,
        "ownerItems": owner_items,
    }


def next_plan(corpus, now=None, result=None):
    """Non-dispatching plan: ready work first, prerequisites before dependents.

    Only depends_on creates graph edges. A prerequisite inherits its most
    urgent descendant's existing delivery sort key. Claims, external waits and
    capacity stay factual; ordering never implies that pending work is ready.
    Cycles and their dependent tail are labeled and retained at the end.
    """
    result = frontier(corpus, now) if result is None else result
    groups = (('ready', 'ready'), ('claimed', 'working'), ('blocked', 'blocked'),
              ('externalWaits', 'external_wait'), ('capacityBlocked', 'capacity_wait'))
    rows = {r['id']: dict(r, nextState=state) for group, state in groups for r in result[group]}
    keys = {rid: sort_key(corpus, corpus.by_id[rid]) for rid in rows}
    effective = dict(keys)
    prerequisites = {rid: {dep for dep in dependency_list(corpus.by_id[rid]) if dep in rows} for rid in rows}
    children = {rid: set() for rid in rows}
    for rid, deps in prerequisites.items():
        for dep in deps:
            children[dep].add(rid)
    # Monotone propagation also converges for cyclic input, without recursion.
    for _ in range(len(rows)):
        changed = False
        for rid in sorted(rows):
            inherited = min([effective[rid]] + [effective[child] for child in children[rid]])
            if inherited < effective[rid]:
                effective[rid] = inherited
                changed = True
        if not changed:
            break
    def plan_key(rid):
        return (rows[rid]['nextState'] != 'ready', effective[rid], keys[rid], rid)
    remaining = {rid: len(deps) for rid, deps in prerequisites.items()}
    queue = [(plan_key(rid), rid) for rid, count in remaining.items() if count == 0]
    heapq.heapify(queue)
    ordered = []
    while queue:
        _, rid = heapq.heappop(queue)
        ordered.append(rid)
        for child in sorted(children[rid]):
            remaining[child] -= 1
            if remaining[child] == 0:
                heapq.heappush(queue, (plan_key(child), child))
    unresolved = sorted(set(rows) - set(ordered), key=plan_key)
    for rid in unresolved:
        rows[rid]['nextState'] = 'cycle_wait'
    for rid in ordered + unresolved:
        rank = effective[rid][1]
        rows[rid]['effectivePriority'] = PRIORITIES[rank] if rank < len(PRIORITIES) else rows[rid]['priority']
        rows[rid]['dependents'] = sorted(children[rid])
    return [rows[rid] for rid in ordered + unresolved]


def delivery_planning(corpus, result=None, plan=None):
    """Read-only milestone scopes and explicit dependency graph for explorers.

    Reuse frontier/next_plan decisions; scope never promotes a waiting row to
    ready. Cross-milestone unfinished prerequisites remain in scoped Next.
    This does not express automation suitability or dispatch authorization.
    """
    result = frontier(corpus) if result is None else result
    plan = next_plan(corpus, result=result) if plan is None else plan
    planned = {row['id']: row for row in plan}
    records = sorted((r for r in corpus.by_id.values()
                      if r.doc_type in ('backlog', 'epic', 'question')),
                     key=lambda r: sort_key(corpus, r))
    nodes = {}
    for record in records:
        rid = record_id(record)
        state = dependency_state(corpus, rid)
        nodes[rid] = {'id': rid, 'path': os.path.join('docs', os.path.relpath(record.path, corpus.root)), 'title': record.fm.get('title', ''),
                      'docType': record.doc_type, 'status': record.status,
                      'milestone': record.fm.get('milestone'),
                      'priority': record.fm.get('priority'), 'dependencyState': state,
                      'state': planned[rid]['nextState'] if rid in planned else
                               ('satisfied' if state == 'terminal' else 'external_wait'),
                      'dependsOn': dependency_list(record), 'blockers': blockers(corpus, record)}
    missing = sorted({dep for node in nodes.values() for dep in node['dependsOn']} - nodes.keys())
    for rid in missing:
        nodes[rid] = {'id': rid, 'title': 'Unresolved dependency', 'path': '',
                      'docType': 'unknown', 'status': 'unknown', 'milestone': None,
                      'priority': None, 'dependencyState': 'unknown', 'state': 'unknown',
                      'dependsOn': [], 'blockers': []}
    milestones = []
    for milestone in corpus.milestones:
        mid = record_id(milestone)
        items = [r for r in corpus.items if r.fm.get('milestone') == mid]
        local = {record_id(r) for r in items}
        closure = set(local & planned.keys())
        pending = list(closure)
        while pending:
            for dep in planned[pending.pop()]['dependsOn']:
                if dep in planned and dep not in closure:
                    closure.add(dep)
                    pending.append(dep)
        ordered = [row['id'] for row in plan if row['id'] in closure]
        milestones.append({'id': mid, 'title': milestone.fm.get('title', ''),
                           'status': milestone.status, 'sequence': milestone_sequence(milestone),
                           'counts': {'total': len(items),
                                      'active': len(local & planned.keys()),
                                      'ready': sum(r['id'] in local and r['nextState'] == 'ready' for r in plan),
                                      'working': sum(r['id'] in local and r['nextState'] == 'working' for r in plan),
                                      'waiting': sum(r['id'] in local and r['nextState'] not in ('ready', 'working') for r in plan),
                                      'terminal': sum(r.status in TERMINAL_STATUSES for r in items)},
                           'nextIds': ordered,
                           'crossMilestonePrerequisites': [rid for rid in ordered if rid not in local],
                           'longestDependencyChain': longest_remaining_dependency_chain(
                               corpus, [corpus.by_id[rid] for rid in ordered])})
    return {'schema': 'proof.baton.planning.v1', 'edgeDirection': 'dependent_to_prerequisite',
            'milestones': milestones, 'nodes': nodes,
            'order': [rid for rid in nodes if rid not in planned] + [row['id'] for row in plan]}
