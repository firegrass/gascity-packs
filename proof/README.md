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

This candidate supports committed import, snapshot, claim, renewal, release and
revision-checked edits on an explicit shadow workspace. Work, operation receipts
and full revision events are native durable versioned Beads records. All writes
touch a coordination bead and re-evaluate policy after serialization conflicts.
Creation dates and legacy resolution are preserved. Import emits no PROOF
creation/completion events and applies no classification hold.

Live mutation is deliberately refused. Authoring, acceptance/evidence/completion,
v2 execution bindings, production client migration, retention/backup automation,
authority switching and rollback are not installed by this foundation. A native
receipt observed after an uncertain commit is not an off-host durability promise.
Uncommitted Dolt history refuses mutation and receipt replay pending reconciliation.

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
  its two documents, records the raw record through `evidence` and completes a
  pass with the landing commit as its merge and main identity.

A marker carrying `freeze: true` (either authority) is a write freeze: the
dispatcher admits and claims nothing, the completer completes and renews
nothing, keeper renewals skip; reads stay available.

## Tests

```sh
python3 -m unittest discover -s proof/tests -v
python3 proof/tests/run_native.py
bash proof/tests/test_checks.sh
bash proof/tests/test_livetest_credentials.sh
python3 proof/assets/scripts/check_entrypoints.py proof
```

Native qualification requires bd 1.3.1, Dolt 2.3.5, Go and ICU. It starts a
disposable server and never opens a live city. The original seven Go transaction
checks and two CLI controls are retained; checked-command tests additionally
exercise the canonical Python policy and same-seat replacement leases.

These are rehearsal tests, not authority-switch or retention qualification.
Private-corpus import and compatibility tests remain in the orchestrator.

Factory helpers route execution-ledger operations through Gas City's adapter.
The standalone native qualification harness owns an isolated database and keeps
its administrative bypass controls there. Credential verdicts and argument
errors report policy rules without echoing supplied credential material.
Fork CI uses GitHub-hosted Ubuntu runners; upstream retains Blacksmith runners.
