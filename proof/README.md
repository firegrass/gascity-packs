# PROOF pack

PROOF agents, formulas, validation commands and deterministic factory scripts.
The existing factory continues to use its Git work authority. Model choices,
permissions and recovery behavior are preserved from the orchestrator pack.

Cities importing the agents declare the `claude`, `codex` and `grok` provider
aliases with `base = "builtin:<name>"`. Credentials and operator-specific
provider patches stay in the city configuration.

The pack owns `assets/scripts/proof_work_policy.py`, the storage-independent
readiness, ordering, capacity and scope rules. The orchestrator retains Markdown
parsing, validation and compatibility imports. Public content contains code and
synthetic fixtures; project records and machine configuration belong elsewhere.

## Checked-work rehearsal

`gc proof work` invokes the Go helper and pinned Python evaluator. Build with
Go >=1.26.5, a C/C++ toolchain and ICU development libraries:

```sh
cd proof/work-helper
go build -mod=readonly -o /path/to/proof-work .
```

The helper reads one JSON request on stdin and prints one JSON result; any
refusal exits 1 with the reason on stderr and commits nothing. Work, operation
receipts and full revision events are native durable versioned Beads records.
All writes touch a coordination bead and re-evaluate policy after serialization
conflicts. Creation dates and legacy resolution are preserved. Import emits no
PROOF creation/completion events and applies no classification hold.

Read-only actions (no `operation_id`, no `actor`):

- `snapshot` — the state, every record and the computed frontier.
- `revision` — `{epoch, mode, revision, source_sha, generated_at}`, answered by
  the helper from the coordination bead without running the evaluator.
- `history {id}` — the record's immutable events, ordered by `at` then
  `operation_id` (each also carries `ledger_revision`), and its receipts.

Mutations carry `epoch`, `operation_id` and `actor`; `gascity/<rig>` is a
factory seat, any other actor is a person. `shadow` and `live` accept
mutations, `frozen` refuses every one:

- `import`, `claim`, `renew`, `release`, `edit` — as before: readiness, scope
  overlap, epic capacity, lease generations and authored revisions.
- `author` — a new open backlog item, epic or question under its legacy root
  (`record`, or `records` to author an epic with its first item atomically).
- `answer` — a question only; sets `answered`, moves the path to
  `docs/questions/answered/` and replaces the body.
- `evidence` — appends deduplicated entries to a working or done item, with
  the caller's receipts kept verbatim on the event.
- `complete` — from `working` by the lease holder, bound to the semantic
  digest the attempt executed. A factory seat needs a merged pull request (or
  merge sha) and a green main sha; a person may complete on acceptance alone
  and is the only actor that may supersede. The last active child of an epic
  needs a person's `close_epic: true`, which closes the epic in the same
  operation; a factory seat can never close an epic. The completion event
  retains the full completed row and evidence and is never overwritten.
- `reopen` — person only; done or superseded back to open (a closed parent
  epic reopens with it). Earlier completion events stay.
- `activate` — `shadow` to `live`, only with reconciled reservations and the
  expected source revision. `freeze` — `live` to `frozen`; thawing is a new
  epoch via a fresh import.

Not installed by this candidate: v2 execution bindings, production client
migration, retention/backup automation and the authority marker switch. A
native receipt observed after an uncertain commit is not an off-host
durability promise. Uncommitted Dolt history refuses mutation and receipt
replay pending reconciliation.

## Work-ledger retention

`gc proof work-maintain {replicate,backup,doctor,status}` and the orders
`proof-work-replicate` (every minute), `proof-work-backup` (hourly) and
`proof-work-doctor` (every five minutes) keep the `proof_work` ledger
recoverable as ADR-0236 asks: `main` is pushed to the orchestrator git remote
under `refs/dolt/liskov-work-beads` (code refs untouched), one verified
full-history Dolt backup an hour is kept 24 times hourly and 30 times daily,
and the doctor writes `<city>/proof/work-retention.json` for admission to
read. The `work-retention` doctor check reports the same. The ledger is not a
rig, so these scripts address the sql-server with explicit `--workspace`
arguments and refuse ambient `BEADS_*`/`BD_*` overrides; the reasons are in
the script header. The operator's city.toml change, including the compactor
allowlist that keeps `gc dolt compact` off the ledger, is
`docs/work-retention-install.md`.

## Factory clients and the work authority

The factory clients (`dispatcher.py`, `admit.py`, `completer.py`, `keeper.py`,
`status.py`, `pr_recovery.py`, `livetest_evidence.py`, `execution_contract.py`)
read the orchestrator's committed marker `gascity/work-authority.json` through
`assets/scripts/work_authority.py` (ADR-0236). Absent or `authority: git`, every
client keeps its Markdown-on-main path byte for byte. Under `authority: beads`:

- records come from `python3 scripts/work_client.py snapshot --json` in the
  owned orchestrator clone, never from a Markdown file; mutations (`claim`
  through `next_work.py`, `renew`, `release`, `evidence`, `complete`) go through
  that client with the seat's lease, and no client writes a Markdown work record
  (`factory_lib.land_backlog_change` refuses a staged one);
- admission emits `proof.contract.v2` (`schemas/proof.contract.v2.json`): v1 plus
  a `work` binding of ledger identity, revisions and lease; the verifier accepts
  v1 for historical runs; the lease is stored on the execution bead as
  `proof.lease_id`; `--landing main` is refused, because that packet completes
  its item in Markdown;
- the dispatcher admits nothing unless `<city>/proof/work-retention.json` is
  younger than ten minutes, `ok` and replicated within five minutes, and logs
  one standing reason otherwise;
- the completer completes through the checked completion command with the
  merged pull request, merge and green main commits, the approved tree and the
  attempt, catches the helper's last-active-child refusal and holds that item
  for a person as before, and lands ADR delivery bookkeeping as a docs change;
- the keeper renews an expiring lease itself; the live-result publisher lands
  its two documents, completes a pass with the landing commit as its merge and
  main identity, and only then records the raw record through `evidence` — an
  evidence entry is part of the semantic digest a completion is bound to, so
  it is appended after, never before.

A marker carrying `freeze: true` (either authority) is a write freeze: the
dispatcher admits and claims nothing, the completer completes and renews
nothing, keeper renewals skip; reads stay available.

## Tests

```sh
python3 -m unittest discover -s proof/tests -v
python3 proof/tests/run_native.py
python3 -m unittest proof.tests.test_work_retention -v
bash proof/tests/test_checks.sh
bash proof/tests/test_livetest_credentials.sh
python3 proof/assets/scripts/check_entrypoints.py proof
```

Native qualification requires bd 1.3.1, Dolt 2.3.5, Go and ICU. It starts a
disposable server and never opens a live city. The original seven Go transaction
checks and two CLI controls are retained; checked-command tests additionally
exercise the canonical Python policy, same-seat replacement leases, and the
full completion, authoring, mode-transition and history flow in one isolated
database.

These are rehearsal tests, not authority-switch qualification. Private-corpus
import and compatibility tests remain in the orchestrator.

`test_work_retention.py` is the retention qualification: on a disposable
server it runs the real Gas City compactor with and without the execution
allowlist, replicates to a bare git remote across an observed backing-ref
rollover and clones the result, rotates fake-clock hourly backups across day
boundaries and restores every retained copy, and drives each doctor failure.
It needs a Gas City checkout for the compactor (`GASCITY_CHECKOUT`, or a
sibling `gascity` directory; CI fetches a pinned one) and skips that group
without it.

Factory helpers route execution-ledger operations through Gas City's adapter.
The standalone native qualification harness owns an isolated database and keeps
its administrative bypass controls there. Credential verdicts and argument
errors report policy rules without echoing supplied credential material.
Fork CI uses GitHub-hosted Ubuntu runners; upstream retains Blacksmith runners.
