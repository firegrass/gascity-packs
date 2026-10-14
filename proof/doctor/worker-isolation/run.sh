#!/usr/bin/env bash
# gc doctor check: worker-isolation.
#
# Exit 0 = OK, 1 = warning, 2+ = error. The first stdout line is the message.
#
# A rig-scoped agent starts in its rig root. In this workspace the ordinary
# checkouts are shared with other sessions, and a worker that started in one
# with no prompt adopted another session's packet and opened a pull request for
# it. This check makes the three preconditions that would have prevented that
# mechanical, for every rig-scoped agent that is not suspended and is driven by
# a model (agents with prompt_mode = "none", such as the control-dispatcher, run
# a deterministic command and are exempt):
#
#   1. its rig root is a clone marked Gas City-owned (<git-dir>/gascity-owned);
#   2. its prompt template resolves to a file that exists — a missing one makes
#      Gas City start the agent with NO prompt, silently;
#   3. its pre_start runs the PROOF prestart guard. Agents Gas City injects
#      implicitly per provider have no guard and no role prompt, so in a rig
#      they must be suspended.
set -uo pipefail

export PROOF_HEALTH_SCRIPTS="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../assets/scripts" && pwd)"

cfg="$(gc config show --json 2>/dev/null)" || { echo "cannot read city config (gc config show --json failed)"; exit 2; }

report="$(printf '%s' "$cfg" | python3 -c '
import json, os, subprocess, sys
sys.path.insert(0, os.environ["PROOF_HEALTH_SCRIPTS"])
import rig_health
doc = json.load(sys.stdin)
city = doc.get("city_path") or os.getcwd()
cfg = doc["config"]
rigs = {r["Name"]: r.get("Path") or "" for r in cfg.get("Rigs") or []}
problems, checked = [], 0
for a in cfg.get("Agents") or []:
    rig = a.get("Dir") or ""
    if not rig or a.get("Suspended") or (a.get("PromptMode") or "") == "none":
        continue
    checked += 1
    name = "%s/%s" % (rig, a["Name"])
    root = rigs.get(rig, "")
    why = rig_health.problem(root)
    if why:
        problems.append("%s: %s: %s" % (name, root or "<unbound>", why))
    prompt = a.get("PromptTemplate") or ""
    path = prompt if os.path.isabs(prompt) else os.path.join(city, prompt)
    if not prompt or not os.path.isfile(path):
        problems.append("%s: prompt template %r does not exist - the agent would start with no prompt" % (name, prompt))
    if not any("prestart-guard.sh" in (c or "") for c in a.get("PreStart") or []):
        problems.append("%s: no prestart guard - suspend it, or give it the PROOF pre_start" % name)
if problems:
    print("%d isolation problem(s) across %d live rig agent(s)" % (len(problems), checked))
    print("\n".join(problems))
    sys.exit(2)
print("%d live rig agent(s), all in owned clones behind the prestart guard" % checked)
')"
rc=$?
printf '%s\n' "$report"
exit $rc
