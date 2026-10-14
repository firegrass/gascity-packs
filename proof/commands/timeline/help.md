Export read-only observations from existing owners:

    gc proof timeline --city CITY export --rig RIG --output receipts.jsonl
    gc proof timeline rebuild --input receipts.jsonl --database timeline.sqlite
    gc proof timeline report --database timeline.sqlite [--packet BKLG-...] [--history]

Repeat `--input` to retain multiple snapshots. No report changes work state.
Unknown usage and missing acceptance are explicit; totals are lower bounds.

Exports refuse an existing output with different observations; use a new path for
each collection. `--history` exposes retained snapshots; never sum their cumulative usage.
