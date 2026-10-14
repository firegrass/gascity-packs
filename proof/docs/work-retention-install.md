# Installing work-ledger retention in a city

The PROOF work ledger is the Dolt database `proof_work` on the city's existing
sql-server, reached through its own `.beads` workspace and deliberately **not a
rig** (ADR-0236). Nothing in the pack names a machine path; the city does, in
`city.toml`. This is the whole operator change.

## 1. Keep the compactor off the ledger

`gc dolt compact` (the dolt pack's `mol-dog-compactor` order) discovers every
database on disk under the data dir and flattens its history. The only thing
that keeps it off `proof_work` is an explicit allowlist of execution
databases. Add to `city.toml`:

```toml
# The compactor flattens history; the work ledger's history is the product.
# Name every EXECUTION database and nothing else: a newly onboarded rig must
# be added here, or its database is never compacted (the safe failure).
[[orders.overrides]]
name = "mol-dog-compactor"
[orders.overrides.env]
GC_DOLT_COMPACT_ONLY_DBS = "bat,doc,hq,lbj,lcs,ldc,ldg,lex,lga,lii,lma,lmo,lnp,lrc,lri,lrj,lrs,lsb,lsk,lui,orc,pcl,pcm,pct,pcu"
```

The list above is the live server's execution databases on 2026-10-14 (the
city scope `hq` and the 24 rig prefixes). Check it against
`ls <city>/.beads/dolt` before applying, and add every rig onboarded since.
The retention doctor refuses (`compactor_allowlist_unset`) while the override
is missing and (`compactor_allowlist_includes_project_database`) if
`proof_work` ever appears in it.

## 2. Give the three orders their arguments

```toml
[[orders.overrides]]
name = "proof-work-replicate"
[orders.overrides.env]
LISKOV_WORK_WORKSPACE = "/home/<user>/projects/gascity/proof-work/.beads"
LISKOV_WORK_REMOTE = "git+ssh://git@github.com/proof-computer/liskov-agent-orchestrator.git"
LISKOV_WORK_REF = "refs/dolt/liskov-work-beads"

[[orders.overrides]]
name = "proof-work-backup"
[orders.overrides.env]
LISKOV_WORK_WORKSPACE = "/home/<user>/projects/gascity/proof-work/.beads"
LISKOV_WORK_BACKUP_ROOT = "/home/<user>/projects/gascity/proof-work-backups"
# LISKOV_WORK_DATA_DIR defaults to <city>/.beads/dolt, the managed data dir;
# LISKOV_WORK_MAX_JOURNAL_BYTES defaults to 536870912 (512 MiB).

[[orders.overrides]]
name = "proof-work-doctor"
[orders.overrides.env]
LISKOV_WORK_WORKSPACE = "/home/<user>/projects/gascity/proof-work/.beads"
LISKOV_WORK_REMOTE = "git+ssh://git@github.com/proof-computer/liskov-agent-orchestrator.git"
LISKOV_WORK_REF = "refs/dolt/liskov-work-beads"
LISKOV_WORK_BACKUP_ROOT = "/home/<user>/projects/gascity/proof-work-backups"
```

`GC_CITY_PATH` is set by Gas City for every order. The `gc doctor` check
`work-retention` reads the `proof-work-doctor` override for the same values.

Requirements on the host:

- the workspace's `metadata.json` says `dolt_database: proof_work` and names
  the server port; its `config.yaml` carries the flat line
  `events-journal-auto-prune: false` (the work helper refuses otherwise);
- the sql-server process can push to the remote: `git+ssh://` uses the SSH
  identity of the user running `dolt sql-server`, and the push happens inside
  the server (`CALL DOLT_PUSH`), not in the order's shell;
- the backup root is on local disk the server can write (`file://` backups are
  written by the server process) and has room for 54 full copies;
- the noms journal (`<data dir>/proof_work/.dolt/noms/vvvv…`) is under 512 MiB.
  One shadow import left a 1.3 GB journal (per-row prolly-tree rewrites in one
  transaction; the same corpus measured 253 MB on a disposable server).
  `backup` refuses with `ledger_not_collected:<bytes>` and the doctor reports
  it until `gc proof work-maintain gc` has run: it calls `DOLT_GC()` through
  the server and reports success only after proving every commit hash and
  every AS OF row count unchanged. Run it once after the import, before the
  first backup, and whenever the doctor names `ledger_not_collected`.

## 3. What to expect on the remote

Replication writes **one** ref, `refs/dolt/liskov-work-beads`, and never
touches a branch you made. Dolt's git remote also pushes a marker branch,
`refs/heads/__dolt_remote_info__`, holding a single `DOLT_REMOTE.md`; it is
not code and never changes `main`. It can be suppressed by setting
`DOLT_REMOTE_INFO_BRANCH=` (empty) in the **sql-server's** environment, which
the pack does not control.

The data ref keeps at most 64 parented git commits before Dolt writes a
parentless one (a "rollover"); the Dolt history is complete at every tip, and
`proof/tests/test_work_retention.py` clones across an observed rollover.

## 4. Check

```sh
gc proof work-maintain status        # state file plus the ledger's head and remotes
gc doctor                            # work-retention is an error until the orders have run
```

Admission reads `<city>/proof/work-retention.json` and refuses new packets
unless it is under ten minutes old, `ok` is true and the replication lag is at
most 300 s.
