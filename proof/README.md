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
exercise the canonical Python policy and same-seat replacement leases.

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
