# gc proof work

The checked transaction rehearsal client. Explicit workspace, compiled helper,
epoch and operation identity are required. Live mode is refused.

`gc proof work --workspace /path/.beads --helper /path/proof-work --request /path/request.json`

The JSON request selects `snapshot`, `import`, `claim`, `renew`, `release` or
`edit`. Use the orchestrator's `scripts/beads_work_import.py` to prepare a
committed snapshot. Completion and live cutover are not installed yet.
