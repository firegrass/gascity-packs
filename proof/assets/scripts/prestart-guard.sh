#!/usr/bin/env bash
# pre_start guard for every model-backed PROOF agent.
#
# Gas City starts a rig-scoped agent in the rig root and assumes it owns that
# checkout. This workspace's ordinary checkouts are shared and usually carry
# other sessions' uncommitted work; the first pilot worker started in one and
# adopted a packet that was not its own. A rig must therefore be a dedicated
# clone, marked as such, and this guard refuses to create a session otherwise.
#
# Exit non-zero = Gas City does not create the session.
set -euo pipefail

root="${1:?prestart-guard: rig root argument is required}"

city="${2:-${GC_CITY:-}}"
args=()
if [ -n "$city" ]; then args+=(--startup-city "$city"); fi
exec python3 "$(dirname -- "${BASH_SOURCE[0]}")/rig_health.py" "$root" "${args[@]}"
