# gc proof work-maintain

Retention and maintenance for the PROOF work ledger, the `proof_work` Dolt
database that is deliberately not a rig (ADR-0236). Four subcommands, every
one a script:

    gc proof work-maintain replicate   push main to the orchestrator git remote under a custom Dolt ref
    gc proof work-maintain backup      one verified full-history copy into <backup-root>/hourly/<UTC ts>/
    gc proof work-maintain doctor      the retention checks; ok=false with named problems otherwise
    gc proof work-maintain gc          CALL DOLT_GC() through the server, a success only when every
                                       commit and every AS OF row count is unchanged afterwards
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
| `--workspace` | `LISKOV_WORK_WORKSPACE` | the ledger's `.beads` directory; `metadata.json` must say `dolt_database: proof_work` (or `proof_test_*`) and names the server port |
| `--city` | `GC_CITY_PATH` | the city root (Gas City sets it for orders) |
| `--remote` | `LISKOV_WORK_REMOTE` | the orchestrator remote as a Dolt git URL, e.g. `git+ssh://git@github.com/proof-computer/liskov-agent-orchestrator.git` |
| `--ref` | `LISKOV_WORK_REF` | the git ref carrying the Dolt data; default `refs/dolt/liskov-work-beads` |
| `--remote-name` | `LISKOV_WORK_REMOTE_NAME` | the Dolt remote name inside the ledger; default `liskov-work` |
| `--backup-root` | `LISKOV_WORK_BACKUP_ROOT` | directory holding `hourly/` and `daily/` |
| `--compactor-order` | `LISKOV_WORK_COMPACTOR_ORDER` | the compactor order whose allowlist the doctor reads; default `mol-dog-compactor` |
| `--data-dir` | `LISKOV_WORK_DATA_DIR` | the sql-server data dir holding `<database>/.dolt`; default `<city>/.beads/dolt` |
| `--max-journal-bytes` | `LISKOV_WORK_MAX_JOURNAL_BYTES` | `backup` refuses and `doctor` reports `ledger_not_collected` above this noms journal size; default 512 MiB |

`--host`, `--port`, `--user` override what `metadata.json` says (qualification
only). `--now <ISO 8601>` is a fake clock for qualification runs. `gc --full`
runs `CALL DOLT_GC('--full')`, collecting the old generation as well.

## Storage garbage collection

ADR-0236 allows storage GC only with proof that reachable history survives.
`gc` records every commit hash in `dolt_log` and the issue, dependency,
event (`pw-event-*`) and receipt (`pw-op-*`) counts AS OF every commit,
runs `CALL DOLT_GC()` through the server, measures again, and reports
`verified: true` only when both lists are identical; otherwise it exits 1
with `gc_history_changed` (or `gc_failed`). The outcome is written to the
state file under `gc: {last_at, bytes_before, bytes_after, verified,
journal_bytes_before, journal_bytes_after, commits}`; the doctor adds
`journal_bytes`, `noms_bytes` and `journal_measured_at`. Dolt was observed
to let `DOLT_GC` wait for other connections' running statements and to
keep serving afterwards. It is an operator command, not an order: run it
when the doctor or a refused backup reports `ledger_not_collected`.

## Refusals and ambient overrides

The script refuses a workspace whose database is not `proof_work` or an
isolated `proof_test_*`, and every subcommand needs its explicit arguments.
Ambient `BEADS_*` / `BD_*` variables redirect bd and Dolt routing; the city's
controller sets a dozen of them for every exec order, so instead of refusing
(as the work helper does) the script strips them from every dolt/bd
subprocess it runs, exactly as the orchestrator client does before invoking
the helper, and records the names in the state file as
`ambient_overrides_stripped` (informational, never a problem).

## Doctor checks

`ok` is false, with these problem names, when:

- `compactor_allowlist_unset` — the city's effective config gives the
  compactor order no non-empty `GC_DOLT_COMPACT_ONLY_DBS`;
- `compactor_allowlist_includes_project_database:<db>` — the allowlist names
  the ledger;
- `workspace_auto_prune_not_false` — `config.yaml` lacks the flat
  `events-journal-auto-prune: false` (an ambient
  `BD_EVENTS_JOURNAL_AUTO_PRUNE` is stripped and listed, not counted);
- `project_database_registered_as_rig:<rig>` — a `[[rigs]]` entry, a rig
  directory, or the city scope points at the ledger;
- `ephemeral_project_records:<n>`, `no_history_project_records:<n>`,
  `expiring_project_records:<n>`, `compacted_project_records:<n>`,
  `unversioned_project_records:<n>` — read from the ledger;
- `replication_never_pushed`, `replication_lag_exceeded:<s>` (over 300 s
  since the last confirmed push), `replication_remote_missing`,
  `replication_remote_mismatch`;
- `backup_never_verified`, `backup_stale:<m>` (over 90 minutes);
- `ledger_not_collected:<bytes>` — the live noms journal is over
  `--max-journal-bytes`; `backup` refuses with the same name so a copy never
  carries a gigabyte of dead chunks; `data_dir_unreadable:<path>` — the
  database directory is not where `--data-dir` says;
- `ledger_unreachable:<why>`, `workspace_unreadable:<why>`,
  `city_config_unreadable:<why>` — everything fails closed.
