# PROOF work authority contract (proof.work-request.v1)

Fixed interface for the Beads-native work authority. Builders implemented
different sides of it in parallel against this text; a change here is a contract
change for every party. Everything below is the Stage B/C deliverable for
ADR-0236 (orchestrator `docs/decisions/adr/0236-typed-work-has-a-separate-dolt-authority.md`).

## Parties and files

- **Pack** (`repos/gascity-packs`, dir `proof/`): `work-helper/main.go` (Go, Beads 1.3.1 public API, the ONLY
  transaction adapter), `assets/scripts/proof_work_evaluate.py` (policy inside the transaction),
  `proof_work_records.py` (record encoding), `proof_work_policy.py` (readiness/order/capacity/scope).
- **Orchestrator** (`liskov-agent-orchestrator`): committed marker `gascity/work-authority.json`, thin client
  `scripts/work_client.py`, legacy CLIs `scripts/next_work.py`, `scripts/docs_model.py`, browser adapter
  `scripts/backlog_web.py`, Rust browser `backlog-web/`.
- **Factory clients** in the pack: `dispatcher.py`, `admit.py`, `completer.py`, `keeper.py`, `status.py`,
  `pr_recovery.py`, `livetest_evidence.py`, `execution_contract.py`.
- **Live machine facts** (never touched by builders; tests use disposable servers only). Since 2026-10-16
  (ADR-0239 M1) the ledger `proof_work` is served by its own `dolt sql-server` on the owner's workstation,
  tailnet address `100.84.172.0:3307` (no TLS; Tailscale encrypts in transit), one SQL user per engineer and
  machine (`proof_work_p620` on the host itself), data dir `~/projects/gascity/proof-work-ledger/data`, bd
  prefix `pw`; it is NOT a rig and is never inside a city's data dir. Each factory's workspace dir
  (`~/projects/gascity/proof-work/.beads` on the host) names the host, port and user in `metadata.json`.
  The city's managed Dolt server (`127.0.0.1:17494`, user root, the execution DBs with 3-letter prefixes hq,
  lrs, orc, doc, …) is a different process and is never exposed. The orchestrator git remote is
  `git@github.com:proof-computer/liskov-agent-orchestrator.git`; replication ref `refs/dolt/liskov-work-beads`.

## Helper invocation

`proof-work --workspace <.beads dir> --evaluator <proof_work_evaluate.py>` reads ONE JSON request on stdin,
prints ONE JSON result on stdout, exit 0. Any refusal: exit 1, one-line reason on stderr, no changes committed.
Ambient `BEADS_*`/`BD_*` env refused. Workspace `metadata.json.dolt_database` must be `proof_work` or `proof_test_*`.
Workspace `config.yaml` must contain `events-journal-auto-prune: false`. Uncommitted Dolt history refuses mutation.

Coordination bead `pw-coordination` metadata (the "state"):
```
{schema:"proof.work.v1", epoch, mode:"shadow"|"live"|"frozen", revision:int, source_sha,
 reservations_reconciled:bool, reservation_digest, project_ids:[legacy ids...],
 activated_at?, activated_by?, frozen_at?, frozen_by?, freeze_reason?}
```
Every request carries `epoch` (string) and is refused on mismatch (except `import` on an empty workspace).
Every mutation carries `operation_id` (1–128 bytes, unique per logical operation; replay with the same
request digest returns the stored receipt; same id with a different request is refused) and `actor` (string;
`gascity/<factory>/<rig>` is a factory seat — `<factory>` the city's workspace name, `<rig>` the clone that
runs the packet (ADR-0239 decision 4) — anything not under `gascity/` is a person). A claim by a two-part
`gascity/<rig>` seat (pack releases before 0.44.0) is refused; claims that already carry one finish as they are,
and the pack's clients treat such a holder as their own for `<rig>` (`admit.held_by_us`).

**Portability (ADR-0239 decision 5, pack 0.44.0).** The ledger holds portable facts only. Every mutation except
`import` and `activate` is walked before any policy runs and refused, committing nothing, when it carries: a
string containing `/home/` (an absolute machine path); a string matching `[a-z0-9-]+__[a-z][a-z0-9-]*` (a Gas
City session name such as `proof__worker-pr-x` — person actors such as `claude-83o6` pass); a dict key starting
`gc.` at any depth (an execution key); a top-level request key outside the action's set below; or, on `author`,
`edit` and `answer`, a `fields` key outside the record-type registry (`proof_work_records.REGISTERED_FIELDS`,
the orchestrator's `docs_model.ALLOWED_KEYS` plus `factory`). Bodies are prose and are not walked (`body` on
edit/answer, `record(s)[].body` on author). A receipt replay (same operation id, same request) is answered by the
helper without the evaluator; it can only replay a write that passed.

Per-action request keys (besides `action`, `epoch`, `operation_id`, `actor`): claim `id, hours?, repos?`;
renew `id, lease_id, hours?`; release `id, lease_id`; edit `id, expected_authored_revision, fields, body?`;
answer `id, expected_authored_revision, body, fields?`; evidence `id, entries, receipts?`; complete `id,
lease_id, expected_semantic_digest, resolution, evidence, close_epic?, notes?`; reopen `id, reason`; author
`record | records`; freeze `reason`.

**Routing (ADR-0239 decision 4).** A backlog item may carry `factory: <name>` (one path segment: letters,
digits, `.`, `_`, `-`). A claim by a factory seat whose factory differs is refused (`pinned to factory <name>`);
a person is not routed. `factory` is outside the semantic digest; `edit` may add it to an unclaimed item (it is
the one field `edit` may add). The frontier rows carry `factory` and `claimedBy`; the dispatcher skips a record
pinned elsewhere before claiming.

Mutation results always include:
`{id, bead_id, status, lease_id|null, authored_revision, operational_revision, semantic_digest, epoch,
  ledger_revision, operation_id}`. Mode rules: `shadow` and `live` accept mutations; `frozen` refuses every
mutation; `activate`/`freeze` change mode.

Project record row (`proof.work.v1`, unchanged from the foundation):
`{schema, id, path, fields, body, authored_revision, operational_revision, semantic_digest, source_sha,
  source_text, reserved_scopes?, lease_id?}`. `fields` is the legacy frontmatter (str or list[str] values).
`path` is the legacy docs path (virtual after cutover; new records get `docs/backlog/items/<id>-<slug>.md`,
`docs/backlog/epics/<id>-<slug>.md`, `docs/questions/open/<id>-<slug>.md`; answered questions move to
`docs/questions/answered/`).

## Actions

Read-only (no operation_id, no actor):
- `snapshot {epoch}` → `{schema, epoch, mode, revision, source_sha, reservations_reconciled, …,
  generated_at, records:[rows], frontier, native_records}` (existing).
- `revision {epoch}` → `{epoch, mode, revision, source_sha, generated_at}` — answered by the helper from the
  coordination bead WITHOUT running the Python evaluator (cheap; the browser polls it every 2 s).
- `history {epoch, id}` → `{id, events:[event…] (ordered by at, then operation_id), receipts:[{operation_id,
  digest, result}]}`. Every event dict carries `id` (legacy id), `kind`, `actor`, `at`, `operation_id`, and
  `after` (full row), plus `before` where it exists.

Mutations (existing): `import`, `claim {id, actor, hours?, repos?}`, `renew {id, actor, lease_id, hours?}`,
`release {id, actor, lease_id}`, `edit {id, actor, expected_authored_revision, fields, body?}`.

New mutations:
- `author {actor, record:{id, path, fields, body}}` — create a backlog item, epic or question. Refuse: id not
  matching the legacy id grammar for its path, id already present, path outside the three roots, status not
  `open`, missing required scalar fields, invalid corpus after insertion (dependency cycle, unknown
  prerequisite, lane not in milestone). Sets `created` and `last_reviewed` to today if absent,
  `authored_revision=1`, `operational_revision=0`. Event kind `authored`.
- `answer {id, actor, expected_authored_revision, body, fields?}` — question only; sets `status: answered`,
  `last_reviewed`, moves `path` to `docs/questions/answered/`, replaces body. Event kind `answered`.
- `evidence {id, actor, entries:[str…], receipts?:{…json…}}` — appends new entries to `fields.evidence`
  (dedup, order kept), operational revision +1, event kind `evidence` carrying `receipts` verbatim. Allowed on
  working or done items (a done item's evidence may grow; nothing else changes).
- `complete {id, actor, lease_id, expected_semantic_digest, resolution:"done"|"superseded",
  evidence:{pull_request?, merge_sha?, main_sha?, approved_tree?, attempts:[{bead_id, rig, factory?, root?,
  outcome?}…], acceptance:{…}}, close_epic?:bool, notes?:str}` (an attempt never names a session or a path;
  the portability guard refuses one that does)
  - backlog items and questions-as-work are completed only from `working` by the claim holder with a live
    lease (`lease_id` must match; expired lease refuses); `expected_semantic_digest` must equal the row's
    current semantic digest (the contract the attempt executed).
  - `resolution: "done"` by a factory seat requires `evidence.pull_request` AND `evidence.main_sha`
    (the completer supplies merged PR + green main) or `evidence.merge_sha` + `main_sha`; a person may complete
    with `evidence.acceptance` alone. `superseded` is person-only.
  - Epic last-active-child rule: if this is the last active child of an open/working epic, refuse unless
    `close_epic: true` AND the actor is a person; then the epic is set `done` in the same operation (its own
    event). A factory seat can never close an epic.
  - Writes: `status`, `done_at` (today), `last_reviewed`, `evidence += [pull_request]`; removes claim keys,
    `lease_id`, `reserved_scopes`. Event kind `completion` with `resolution`, `evidence`, `notes`, and `after`
    (the full completed row). Completion events are never overwritten.
- `reopen {id, actor, reason}` — person only; done/superseded → open; removes `done_at`; event kind
  `reopened` with `reason`. Earlier completion events stay.
- `activate {actor, expected_source_sha}` — `shadow` → `live`. Refuse unless `reservations_reconciled` is
  true and `state.source_sha == expected_source_sha`. Sets `activated_at/by`. Event kind `activated`.
- `freeze {actor, reason}` — `live` → `frozen` (read-only). Event kind `frozen`. (Rollback or re-activation is
  a new epoch via a fresh import, never a thaw.)

Storage of events/receipts is unchanged: `pw-event-<ophash>-<i>` and `pw-op-<ophash>` durable versioned issues.

## Orchestrator authority marker (committed) `gascity/work-authority.json`
```
{"schema":"proof.work-authority.v1","authority":"git"|"beads","epoch":string|null,
 "source_sha":string|null,"switched_at":string|null,"workspace":string|null,"helper":string|null}
```
`git` is the default until CUTOVER 1. Clients MUST refuse Beads operations when `authority != "beads"` and
MUST refuse legacy Markdown work-record writes when `authority == "beads"`. `workspace` and `helper` may be
overridden on one machine by env `LISKOV_WORK_WORKSPACE` / `LISKOV_WORK_HELPER`. Helper default:
`<repo>/.cache/proof-pack/current/bin/proof-work`, built on demand from
`<repo>/gascity/packs/proof/work-helper` with `/usr/local/go/bin/go build -mod=readonly` when missing.

## Orchestrator thin client `scripts/work_client.py`

Python module + CLI. Module API: `marker()`, `active()`, `snapshot()` (cached per process; returns
`(state, rows, frontier)`), `corpus()` → `docs_model.Corpus` built from rows (Record path =
`REPO_ROOT/<row.path>`), `history(id)`, `revision()`, and mutations `claim(id, by, hours, repos)`,
`renew(id, by, lease_id, hours)`, `release(id, by, lease_id)`, `edit(...)`, `author(record, by)`,
`answer(...)`, `evidence(...)`, `complete(...)`, `reopen(...)`. Each mutation generates a uuid4 operation id,
appends `{operation_id, request, at}` to the journal `<git-common-dir>/proof-work-ops.jsonl` BEFORE invoking the
helper, and on an indeterminate result re-submits the identical request once (receipt replay) before raising.
CLI: `python3 scripts/work_client.py <action> [--id ID] [--by ACTOR] [--hours H] [--repos a,b]
[--lease LEASE] [--request-file FILE] --json`; prints the result JSON; exit 1 + stderr on refusal;
`python3 scripts/work_client.py status --json` prints the marker and (if reachable) `revision`.

Lease ids are returned by `claim` and must be carried by the holder (factory: in the execution bead metadata
`proof.lease_id`; people: printed by next_work.py and re-read from the snapshot row `lease_id` when the holder
matches `--by`).

## Execution contract v2 (`proof.contract.v2`)

Same as v1 plus a `work` object: `{bead_id, legacy_id, epoch, authored_revision, operational_revision,
semantic_digest, lease_id, ledger_revision}`. The verifier accepts v1 (historical runs) and v2. v2 is emitted
by admission only when the authority marker is `beads`.

## Retention and maintenance (pack-owned, city-local state)

State file read by admission: `<city>/proof/work-retention.json`
`{schema:"proof.work-retention.v1", ok:bool, checked_at, problems:[str…],
  replication:{pushed_revision, pushed_at, lag_seconds}, backup:{last_hourly_at, last_daily_at, hourly:int, daily:int}}`.
When the authority is `beads`, the dispatcher refuses new admissions unless this file exists, is younger than
10 minutes, `ok` is true and `replication.lag_seconds <= 300`.

## Addenda recorded at integration (2026-10-14)

- The marker may carry `"freeze": true`; while true, every Markdown work-record write refuses under either
  authority, the dispatcher admits and claims nothing, the completer and keeper skip their passes; reads stay.
- Natively authored rows have `source_sha: null` and `source_text: ""`. `author` also accepts `records: [...]`
  (an epic and its first item atomically) and then returns `ids: [...]`. `reopen` of an item whose parent epic
  is terminal reopens the epic in the same operation (person only).
- `activate`/`freeze` results carry `status` = the new mode plus `mode`, with the row fields null; their
  events have `id: null` and `before`/`after` = the coordination state. `activate` requires `shadow`,
  `freeze` requires `live`. Every event also carries `ledger_revision`, the total order for `history`.
- `evidence` is allowed only on backlog items in `working`/`done`.
- `evidence` is outside the semantic digest (pack 0.43.1, 2026-10-16): the digest covers the task's fields and
  body minus `status`, the claim keys, `last_reviewed` and `evidence`. A row stamped under the earlier formula
  (with `evidence`) is accepted on read and served under the current one; its stored stamp is rewritten on its next
  mutation. Before this, the completer's own `merged:<sha>` evidence write (0.43.0) changed the digest it later
  presented, and every completion after a merge was refused.
- Thin-client CLI: structured fields travel in `--request-file`; `--id/--by/--hours/--repos/--lease` flags
  win over the file; the client fills `epoch` and `operation_id` when absent. `next_work.py --claim` under
  Beads prints one line containing `lease <lease_id>`.
- Retention state adds `replication.last_attempt_at/last_error` and `backup.last_attempt_at/last_error`;
  before the first doctor run the file reads `ok: false`, `problems: ["doctor_never_ran"]`. `lag_seconds` is the
  age of the last confirmed push. Maintenance environment names are `LISKOV_WORK_*` (never `PROOF_*`).
- Execution contract v2 verification ignores the volatile `lease_id`, `operational_revision` and
  `ledger_revision`; `proof.lease_id` is stored on the execution bead; `proof.claim_commit` is `ledger:<rev>`.
- Not yet covered (follow-ups before the orchestrator rig resumes): `--landing main` admission is refused
  under Beads until land-on-main.sh completes the item through the ledger; `watchdog.py` and `report.py`
  still read item files.
