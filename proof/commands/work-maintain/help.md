# gc proof work-maintain

Retention and maintenance for the PROOF work ledger, the `proof_work` Dolt
database that is deliberately not a rig (ADR-0236). Four subcommands, every
one a script:

    gc proof work-maintain replicate   push main to the orchestrator git remote under a custom Dolt ref
    gc proof work-maintain backup      one verified full-history copy into <backup-root>/hourly/<UTC ts>/
    gc proof work-maintain doctor      the retention checks; ok=false with named problems otherwise
    gc proof work-maintain status      print the state file and what the ledger says

The orders `proof-work-replicate` (every minute), `proof-work-backup` (hourly)
and `proof-work-doctor` (every five minutes) run the same script. The state
file admission reads is `<city>/proof/work-retention.json`
(`proof.work-retention.v1`).

## Arguments and their environment names

Machine paths never live in the pack. Each argument has an environment
variable the city supplies through `[[orders.overrides]]` (see
`proof/docs/work-retention-install.md`); a flag on the command line wins.

| Flag | Environment | Meaning |
| --- | --- | --- |
| `--workspace` | `PROOF_WORK_WORKSPACE` | the ledger's `.beads` directory; `metadata.json` must say `dolt_database: proof_work` (or `proof_test_*`) and names the server port |
| `--city` | `GC_CITY_PATH` | the city root (Gas City sets it for orders) |
| `--remote` | `PROOF_WORK_REMOTE` | the orchestrator remote as a Dolt git URL, e.g. `git+ssh://git@github.com/proof-computer/liskov-agent-orchestrator.git` |
| `--ref` | `PROOF_WORK_REF` | the git ref carrying the Dolt data; default `refs/dolt/liskov-work-beads` |
| `--remote-name` | `PROOF_WORK_REMOTE_NAME` | the Dolt remote name inside the ledger; default `liskov-work` |
| `--backup-root` | `PROOF_WORK_BACKUP_ROOT` | directory holding `hourly/` and `daily/` |
| `--compactor-order` | `PROOF_WORK_COMPACTOR_ORDER` | the compactor order whose allowlist the doctor reads; default `mol-dog-compactor` |

`--host`, `--port`, `--user` override what `metadata.json` says (qualification
only). `--now <ISO 8601>` is a fake clock for qualification runs.

## Refusals

The script refuses to start when any ambient `BEADS_*` or `BD_*` variable is
set, exactly like the work helper: those redirect bd and Dolt routing, and a
redirected run would maintain the wrong database while reporting success for
the right one. It also refuses a workspace whose database is not `proof_work`
or an isolated `proof_test_*`.

## Doctor checks

`ok` is false, with these problem names, when:

- `compactor_allowlist_unset` — the city's effective config gives the
  compactor order no non-empty `GC_DOLT_COMPACT_ONLY_DBS`;
- `compactor_allowlist_includes_project_database:<db>` — the allowlist names
  the ledger;
- `workspace_auto_prune_not_false` — `config.yaml` lacks the flat
  `events-journal-auto-prune: false`; `ambient_override:<NAME>` — a `BEADS_*`
  / `BD_*` variable (such as `BD_EVENTS_JOURNAL_AUTO_PRUNE`) is set;
- `project_database_registered_as_rig:<rig>` — a `[[rigs]]` entry, a rig
  directory, or the city scope points at the ledger;
- `ephemeral_project_records:<n>`, `no_history_project_records:<n>`,
  `expiring_project_records:<n>`, `compacted_project_records:<n>`,
  `unversioned_project_records:<n>` — read from the ledger;
- `replication_never_pushed`, `replication_lag_exceeded:<s>` (over 300 s
  since the last confirmed push), `replication_remote_missing`,
  `replication_remote_mismatch`;
- `backup_never_verified`, `backup_stale:<m>` (over 90 minutes);
- `ledger_unreachable:<why>`, `workspace_unreadable:<why>`,
  `city_config_unreadable:<why>` — everything fails closed.
