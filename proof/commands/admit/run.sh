#!/usr/bin/env bash
# gc proof admit <BKLG-id> --rig <rig> --landing <local|push|pr|automerge> [--dry-run]
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$here/../../assets/scripts/admit.py" "$@"
