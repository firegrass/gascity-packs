Admit one backlog packet into Gas City.

    gc proof admit BKLG-YYYYMMDD-xxxx --rig <rig> --landing <local|push|pr|automerge|main> [--review] [--dry-run]

Admission verifies and bridges; it never claims or edits a backlog item, and a
packet completes its item only when admitted with `--landing main`. Before
admitting, claim the item for the Gas City seat on the existing
path and land that claim on `main`:

    python3 scripts/next_work.py --claim <id> --by gascity/<factory>/<rig> --hours 12

where `<factory>` is this city's workspace name (`gc proof status` prints the
seat). It refuses unless all of these hold on `origin/main`: the item is on the
pilot allow-list (`<city>/proof/pilot-allowlist.txt`), is claimed by this
factory's seat for `<rig>` with at least an hour left, is not a gate, declares
a `write_scope`, and writes only to the rig's own repository.

`--landing` has no default. It is the packet's whole outward authorization:

    local      nothing leaves the machine
    push       push the packet branch
    pr         push and open a pull request
    automerge  push, open a pull request, arm the repository's auto-merge
    main       complete the item and land on main. The worker prepares the
               commit; the orchestrator checks it is still what was reviewed,
               gates it and pushes it. Only for the repository that holds the
               backlog; publishes no branch and opens no pull request

`--review` adds an independent reviewer before anything outward. It is off by
default: the orchestrator's diff-aware gate is then the only check before the
repository's own CI.

Use `--dry-run` first: it performs every check and prints the plan.
