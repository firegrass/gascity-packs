#!/usr/bin/env bash
# Tests for the PROOF check scripts, against a real throwaway git repository
# and a fake `bd` that serves bead JSON from files. Run: gascity/tests/test_checks.sh
set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CHECKS="$HERE/../assets/scripts/checks"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok()   { pass=$((pass+1)); printf '  ok    %s\n' "$1"; }
bad()  { fail=$((fail+1)); printf '  FAIL  %s\n' "$1"; }
expect() { # expect <0|nonzero> <name> <cmd...>
  local want="$1" name="$2"; shift 2
  "$@" >"$T/out" 2>&1; local rc=$?
  if { [ "$want" = 0 ] && [ $rc -eq 0 ]; } || { [ "$want" != 0 ] && [ $rc -ne 0 ]; }; then ok "$name"
  else bad "$name (rc=$rc)"; sed 's/^/        /' "$T/out" | tail -5; fi
}

# --- a fake bd: beads are JSON files in $T/beads --------------------------------
mkdir -p "$T/bin" "$T/beads"
cat > "$T/bin/bd" <<'BD'
#!/usr/bin/env bash
store="$BD_FAKE_STORE"
case "$1" in
  show) cat "$store/$2.json" 2>/dev/null || exit 1 ;;
  list) jq -s '[.[][]]' "$store"/*.json ;;
  update)
    id="$2"; shift 2
    if [ -f "$store/fail-key" ]; then
      failed_key="$(cat "$store/fail-key")"
      for arg in "$@"; do
        case "$arg" in "$failed_key="*) echo "injected receipt failure" >&2; exit 1 ;; esac
      done
    fi
    while [ $# -gt 0 ]; do
      if [ "$1" = "--set-metadata" ]; then
        k="${2%%=*}"; v="${2#*=}"
        jq --arg k "$k" --arg v "$v" '.[0].metadata[$k]=$v' "$store/$id.json" > "$store/$id.tmp" && mv "$store/$id.tmp" "$store/$id.json"
        shift 2
      elif [ "$1" = "--status" ]; then
        jq --arg v "$2" '.[0].status=$v' "$store/$id.json" > "$store/$id.tmp" && mv "$store/$id.tmp" "$store/$id.json"
        shift 2
      else shift; fi
    done ;;
esac
BD
chmod +x "$T/bin/bd"
cat > "$T/bin/gc" <<'GC'
#!/usr/bin/env bash
if [ "$1 ${2:-}" = "hook --claim" ]; then
  cat "$BD_FAKE_STORE/claim.json"
  exit
fi
[ "$1" != --city ] || shift 2
[ "$1" = bd ] || exit 2
shift
[ "$1" != --rig ] || shift 2
exec "$(dirname -- "$0")/bd" "$@"
GC
chmod +x "$T/bin/gc"
export PATH="$T/bin:$PATH" BD_FAKE_STORE="$T/beads"

# --- a real repo with an origin and a packet worktree --------------------------
git init -q --bare "$T/origin.git"
git init -q "$T/seed" && cd "$T/seed" && git config user.email t@t && git config user.name t
echo base > f.txt
# A backlog item held by the Gas City seat, and a view with a generated block:
# the two things a direct landing is allowed to change beyond what was reviewed.
ITEM=docs/backlog/items/BKLG-20260101-ab12-a-packet.md
mkdir -p docs/backlog/items
printf -- '---\ntitle: A packet\nstatus: working\npriority: p2\nwrite_scope:\n  - g.txt\nclaimed_by: gascity/rig\nclaimed_at: 2026-01-01T00:00:00Z\nclaim_expires: 2099-01-01T12:00:00Z\n---\n\n# A packet\n\nBody.\n' > "$ITEM"
printf 'Queue, by hand.\n\n<!-- BEGIN GENERATED DELIVERY QUEUE -->\nclaimed 1\n<!-- END GENERATED DELIVERY QUEUE -->\n\nFooter, by hand.\n' > docs/backlog/delivery-queue.md
mkdir -p scripts
cat > scripts/validate_docs.py <<'GENERATOR'
from pathlib import Path
import re
p = Path("docs/backlog/delivery-queue.md")
p.write_text(re.sub(r"(?m)^claimed [0-9]+$", "claimed 0", p.read_text()))
print("validate_docs: refreshed docs/backlog/delivery-queue.md")
GENERATOR
git add f.txt docs scripts && git commit -qm base && git branch -M main
git remote add origin "$T/origin.git" && git push -q origin main
git clone -q "$T/origin.git" "$T/clone" && cd "$T/clone" && git config user.email t@t && git config user.name t
git worktree add -q "$T/wt" -b agent/pkt origin/main
WT="$T/wt"

root() { # root <gate_command> [extra metadata json]
  # `proof.repo` is what admission stamps; `design_scope.py` refuses without it
  # rather than guessing, so every fixture root carries one. `liskov-rs` here
  # because these cases are about the gate, not about the Console. Admission
  # also records a scope; only the missing-scope refusal fixture omits it.
  jq -n --arg wd "$WT" --arg gate "$1" --argjson extra "${2:-{\}}" \
    '[{id:"r-1", metadata: ({"gc.var.work_dir":$wd,"gc.var.branch":"agent/pkt","gc.var.base_branch":"main","gc.var.gate_command":$gate,"proof.repo":"liskov-rs","proof.write_scope":"liskov-rs:g.txt"} + $extra)}]' \
    > "$T/beads/r-1.json"
  echo '[{"id":"c-1","metadata":{"gc.root_bead_id":"r-1"}}]' > "$T/beads/c-1.json"
}
review() { # review <verdict> <tree>
  jq -n --arg v "$1" --arg t "$2" '[{id:"v-1", closed_at:"2026-01-01T00:00:00Z", metadata:{"gc.root_bead_id":"r-1","proof.review_verdict":$v,"proof.reviewed_tree":$t}}]' > "$T/beads/v-1.json"
}
export GC_BEAD_ID=c-1 GC_ITERATION=1
# The rig's review substrings come from the city's policy file (ADR-0159). Point
# the checks at a fixture city, never at whatever city the caller's shell names.
export GC_CITY="$T/city"; unset GC_CITY_PATH
mkdir -p "$GC_CITY/proof"; touch "$GC_CITY/city.toml"
printf '[rigs.liskov-rs]\nreview_if_scope_contains = ["billing"]\n' > "$GC_CITY/proof/policy.toml"

echo "packet-gates.sh"
root "true"
expect 1 "refuses a branch with no commits ahead of base"      "$CHECKS/packet-gates.sh"
( cd "$WT" && echo change > g.txt )
expect 1 "refuses uncommitted work (validated tree != submitted)" "$CHECKS/packet-gates.sh"
( cd "$WT" && git add g.txt && git -c user.email=t@t -c user.name=t commit -qm change )
root "false"
expect 1 "fails when the rig gate command fails"                "$CHECKS/packet-gates.sh"
root "test -f g.txt"
expect 0 "passes when committed and the gate passes"           "$CHECKS/packet-gates.sh"
TREE="$(git -C "$WT" rev-parse 'HEAD^{tree}')"
[ "$(jq -r '.[0].metadata["proof.validated_tree"]' "$T/beads/r-1.json")" = "$TREE" ] \
  && ok "stamps the validated tree on the workflow root" || bad "stamps the validated tree on the workflow root"
# Review is opt-in. Without it the gated tree IS the approved tree, and the record
# says a gate approved it; with it, the gate must leave approval to the reviewer.
[ "$(jq -r '.[0].metadata | [."proof.approved_tree", ."proof.approved_by"] | join(" ")' "$T/beads/r-1.json")" = "$TREE gate" ] \
  && ok "with review off, records the gated tree as approved by the gate" || bad "with review off, records the gated tree as approved by the gate"
# The gate lane (Q-20261007-b7vh): the gater ran the gate on this iteration's
# committed tree. A record counts only for HEAD's exact tree and only from the
# gater session that held the bead; otherwise the gate runs inline as before.
root "true" '{"gc.kind":"workflow","gc.var.packet_id":"BKLG-20260101-ab12","gc.var.recovery_trial":"qualification","gc.var.review":"false"}'
expect 1 "recovery trial cannot get gate-only approval without requesting review" "$CHECKS/packet-gates.sh"
jq -n --arg tree "$TREE" '[{id:"rr-1",status:"closed",metadata:{
  "proof.review_parent":"r-1","proof.review_packet":"BKLG-20260101-ab12",
  "proof.review_candidate_tree":$tree,"proof.reviewed_tree":$tree,"proof.review_verdict":"approve",
  "proof.review_agent":"liskov-rs/proof.reviewer","gc.routed_to":"liskov-rs/proof.reviewer",
  "gc.session_name":"proof__reviewer-qualification","gc.outcome":"pass"}}]' > "$T/beads/rr-1.json"
expect 0 "recovery approval and the real gate qualify the same packet tree" "$CHECKS/packet-gates.sh"
[ "$(jq -r '.[0].metadata["proof.approved_by"]' "$T/beads/r-1.json")" = reviewer ] \
  && ok "recovery records independent review, never gate-only approval" || bad "recovery records independent review, never gate-only approval"
jq '.[0].metadata["proof.reviewed_tree"]="old-tree"' "$T/beads/rr-1.json" > "$T/beads/rr-1.tmp"
mv "$T/beads/rr-1.tmp" "$T/beads/rr-1.json"
expect 1 "recovery gate refuses a stale reviewer receipt" "$CHECKS/packet-gates.sh"
rm -f "$T/beads/rr-1.json"

gate_record() { # gate_record <result> <tree> [session] [iteration] [assignee] [step_ref]
  local session="${3-proof__gater-pr-abc}" it="${4:-1}"
  jq -n --arg r "$1" --arg t "$2" --arg s "$session" --arg a "${5:-$session}" \
    --arg ref "${6:-mol-proof-packet.implement.iteration.$it.gate}" \
    '[{id:"g-1", status:"closed", assignee:$a, created_at:"2026-01-01T00:00:00Z",
       metadata:{"gc.root_bead_id":"r-1","gc.step_ref":$ref,
                 "gc.session_id":"pr-abc","gc.session_name":$s,"gc.run_target":"proof.gater","gc.routed_to":"liskov-rs/proof.gater",
                 "proof.gate_result":$r,"proof.gate_tree":$t,"proof.gate_session":$s,"proof.gate_seconds":"42",
                 "proof.gate_log_tail":"error[E0308]: mismatched types"}}]' > "$T/beads/g-1.json"
}
root "false"
gate_record pass "$TREE"
expect 0 "gate lane: a gater pass on HEAD's tree passes without running the gate inline" "$CHECKS/packet-gates.sh"
grep -q "gate passed on the gate lane" "$T/out" && ok "gate lane: says the lane passed it" || bad "gate lane: says the lane passed it"
[ "$(jq -r '.[0].metadata["proof.validated_tree"]' "$T/beads/r-1.json")" = "$TREE" ] \
  && ok "gate lane: still stamps the validated tree" || bad "gate lane: still stamps the validated tree"
root "true"
gate_record fail "$TREE"
expect 1 "gate lane: a gater fail on HEAD's tree fails, even when an inline gate would pass" "$CHECKS/packet-gates.sh"
grep -q "mismatched types" "$T/out" && ok "gate lane: repeats the failed gate's log tail for the next iteration" || bad "gate lane: repeats the failed gate's log tail for the next iteration"
root "false"
gate_record pass "0000000000000000000000000000000000000000"
expect 1 "gate lane: a pass on another tree is not evidence; the inline gate decides" "$CHECKS/packet-gates.sh"
gate_record pass "$TREE" "proof__worker-pr-xyz"
jq '.[0].metadata["gc.routed_to"]="liskov-rs/proof.worker"' "$T/beads/g-1.json" > "$T/beads/g.tmp" && mv "$T/beads/g.tmp" "$T/beads/g-1.json"
expect 1 "gate lane: a record a worker session wrote is not evidence" "$CHECKS/packet-gates.sh"
gate_record pass "$TREE" "proof__gater-pr-abc" 1 "proof__worker-pr-xyz"
expect 1 "gate lane: a record whose bead another session held is not evidence" "$CHECKS/packet-gates.sh"
gate_record pass "$TREE" "proof__gater-pr-abc" 2
expect 1 "gate lane: another iteration's record is not evidence" "$CHECKS/packet-gates.sh"
# A first iteration's children carry no formula prefix on a live city
# (`implement.iteration.1.gate`); a later iteration's do. Both are the record.
gate_record pass "$TREE" "proof__gater-pr-abc" 1 "" "implement.iteration.1.gate"
expect 0 "gate lane: finds a first iteration's record, whose step_ref has no formula prefix" "$CHECKS/packet-gates.sh"
gate_record pass "$TREE" "proof__gater-pr-abc" 1 "" "review-cycle.iteration.1.review-gate"
expect 1 "gate lane: a review-cycle record is not evidence for the implement check" "$CHECKS/packet-gates.sh"
gate_record error "$TREE"
expect 1 "gate lane: an error record falls back to the inline gate" "$CHECKS/packet-gates.sh"
root "test -f g.txt"
expect 0 "gate lane: ...which passes when the inline gate passes" "$CHECKS/packet-gates.sh"
rm -f "$T/beads/g-1.json"

# Current engine claims use canonical session IDs; old receipts retain names.
root "false" '{"gc.var.gate_lane":"true"}'
gate_record pass "$TREE" "proof__gater-pr-abc" 1 "pr-abc"
jq '.[0].metadata += {"gc.session_id":"pr-abc", "gc.session_name":"proof__gater-pr-abc", "gc.routed_to":"liskov-rs/proof.gater", "gc.run_target":"proof.gater"}' "$T/beads/g-1.json" > "$T/beads/g.tmp"
mv "$T/beads/g.tmp" "$T/beads/g-1.json"
expect 0 "canonical session ID claim accepts an existing name receipt without rebuilding" "$CHECKS/packet-gates.sh"
root "touch $T/unexpected-inline" '{"gc.var.gate_lane":"true"}'
jq '.[0].metadata["gc.session_id"]="foreign-session"' "$T/beads/g-1.json" > "$T/beads/g.tmp"
mv "$T/beads/g.tmp" "$T/beads/g-1.json"
expect 1 "lane refuses a mismatched canonical claim without running inline" "$CHECKS/packet-gates.sh"
[ ! -f "$T/unexpected-inline" ] && ok "invalid lane evidence never runs the inline build" || bad "invalid lane evidence never runs the inline build"
rm -f "$T/beads/g-1.json" "$T/unexpected-inline"
expect 1 "lane refuses missing evidence without rebuilding in the controller" "$CHECKS/packet-gates.sh"
[ ! -f "$T/unexpected-inline" ] && ok "missing lane evidence never runs inline" || bad "missing lane evidence never runs inline"
rm -f "$T/unexpected-inline"
for mutation in \
  '.[0].metadata["proof.gate_tree"]="stale"' \
  '.[0].metadata["proof.gate_session_id"]="foreign"' \
  '.[0].metadata["proof.gate_session"]="foreign-name"' \
  '.[0].metadata["gc.routed_to"]="other/proof.gater"' \
  '.[0].metadata["gc.root_bead_id"]="other-root"' \
  '.[0].metadata["gc.step_ref"]="implement.iteration.2.gate"'; do
  gate_record pass "$TREE" "proof__gater-pr-abc" 1 "pr-abc"
  jq "$mutation" "$T/beads/g-1.json" > "$T/beads/g.tmp" && mv "$T/beads/g.tmp" "$T/beads/g-1.json"
  expect 1 "lane refuses stale or foreign evidence: $mutation" "$CHECKS/packet-gates.sh"
  [ ! -f "$T/unexpected-inline" ] && ok "refusal leaves the controller free" || bad "refusal leaves the controller free"
done
rm -f "$T/beads/g-1.json"

echo "gater.sh"
GATER="$CHECKS/../gater.sh"
gater_bead() { # gater_bead <gate command>
  root "$1"
  jq -n '[{id:"g-2", status:"open", metadata:{"gc.root_bead_id":"r-1","gc.step_ref":"mol-proof-packet.implement.iteration.1.gate"}}]' > "$T/beads/g-2.json"
  echo '{"ok":true,"action":"work","bead_id":"g-2"}' > "$T/beads/claim.json"
}
g2() { jq -r --arg k "$1" '.[0].metadata[$k] // .[0][$k] // empty' "$T/beads/g-2.json"; }
gater_bead "test -f g.txt && test \"\$HOME\" = $T/gatehome && ! env | grep -q GATER_LEAK"
mkdir -p "$T/gatehome"
jq --arg h "$T/gatehome" '.[0].metadata["gc.var.gate_home"]=$h' "$T/beads/r-1.json" > "$T/beads/r.tmp" && mv "$T/beads/r.tmp" "$T/beads/r-1.json"
GATER_LEAK=1 GC_SESSION_ID=pr-t GC_SESSION_NAME=proof__gater-pr-t expect 0 "runs the claimed gate and exits cleanly" "$GATER"
[ "$(g2 proof.gate_result) $(g2 proof.gate_tree) $(g2 proof.gate_session)" = "pass $TREE proof__gater-pr-t" ] \
  && ok "records pass, the gated tree and its session (gate_home as HOME, nothing inherited)" || { bad "records pass, the gated tree and its session"; cat "$T/out"; }
[ "$(g2 proof.gate_session_id)" = pr-t ] && ok "records the canonical gater ID" || bad "records the canonical gater ID"
[ "$(g2 gc.outcome) $(g2 status)" = "pass closed" ] && ok "closes the step gc.outcome=pass" || bad "closes the step gc.outcome=pass"
gater_bead "sleep 30"
jq '.[0].metadata["gc.var.gate_timeout"]="1s"' "$T/beads/r-1.json" > "$T/beads/r.tmp" && mv "$T/beads/r.tmp" "$T/beads/r-1.json"
GC_SESSION_ID=pr-t GC_SESSION_NAME=proof__gater-pr-t expect 0 "a hung command reaches its hard deadline and records failure" "$GATER"
[ "$(g2 proof.gate_result) $(g2 status)" = "fail closed" ] && ok "timeout is terminal gate evidence" || bad "timeout is terminal gate evidence"
gater_bead "echo boom >&2; false"
GC_SESSION_NAME=proof__gater-pr-t expect 0 "a failing gate is recorded, not a failed step" "$GATER"
[ "$(g2 proof.gate_result) $(g2 gc.outcome) $(g2 proof.gate_reused)" = "fail pass false" ] && ok "records fail and still closes pass" || bad "records fail and still closes pass"
case "$(g2 proof.gate_log_tail)" in *boom*) ok "keeps the gate's log tail" ;; *) bad "keeps the gate's log tail" ;; esac
# A review-cycle gate runs on the tree under review; on a first review that is
# the tree the implement check already gated, and building it again buys nothing.
gater_bead "false"
jq --arg t "$TREE" '.[0].metadata["proof.validated_tree"]=$t' "$T/beads/r-1.json" > "$T/beads/r.tmp" && mv "$T/beads/r.tmp" "$T/beads/r-1.json"
GC_SESSION_NAME=proof__gater-pr-t expect 0 "an already-gated tree is not gated again" "$GATER"
[ "$(g2 proof.gate_result) $(g2 proof.gate_reused) $(g2 proof.gate_tree)" = "pass true $TREE" ] \
  && ok "records it pass, reused, for that tree" || bad "records it pass, reused, for that tree"
gater_bead "true"
( cd "$WT" && echo dirty > h.txt )
GC_SESSION_NAME=proof__gater-pr-t expect 0 "a worktree it cannot gate is recorded as an error" "$GATER"
[ "$(g2 proof.gate_result)" = "error" ] && ok "records error for a dirty worktree" || bad "records error for a dirty worktree"
rm -f "$WT/h.txt"
echo '{"ok":true,"action":"drain"}' > "$T/beads/claim.json"
expect 0 "nothing routed: exits cleanly" "$GATER"
rm -f "$T/beads/g-2.json" "$T/beads/claim.json"
root "test -f g.txt" '{"gc.var.review":"true"}'
# The review loop applies the PREVIOUS review's findings first in each iteration
# (BKLG-20261004-axhn: a fix made last was never reviewed). Its first iteration's
# apply-findings has nothing to apply; the implement check closes it, but only
# when the formula marked it — in an older root it runs after the review.
af() { # af <id> <step_ref> [marker]
  jq -n --arg id "$1" --arg ref "$2" --arg m "${3:-}" \
    '[{id:$id, status:"open", metadata:({"gc.root_bead_id":"r-1","gc.step_ref":$ref}
       + (if $m == "" then {} else {"proof.applies":$m} end))}]' > "$T/beads/$1.json"
}
af af-1 "review-cycle.iteration.1.apply-findings" previous-review
af af-old "review-cycle.iteration.1.apply-findings"
af af-2 "mol-proof-packet.review-cycle.iteration.2.apply-findings" previous-review
expect 0 "passes with review on"                               "$CHECKS/packet-gates.sh"
[ "$(jq -r '.[0].metadata["proof.approved_tree"] // "none"' "$T/beads/r-1.json")" = "none" ] \
  && ok "with review on, the gate approves nothing" || bad "with review on, the gate approves nothing"
[ "$(jq -r '.[0] | [.status, .metadata["gc.outcome"], .metadata["gc.work_outcome"]] | join(" ")' "$T/beads/af-1.json")" = "closed pass no-op" ] \
  && ok "with review on, closes the first iteration's apply-findings: no review precedes it" \
  || bad "with review on, closes the first iteration's apply-findings: no review precedes it"
[ "$(jq -r '.[0].status' "$T/beads/af-old.json") $(jq -r '.[0].status' "$T/beads/af-2.json")" = "open open" ] \
  && ok "never touches an unmarked (older-order) or later apply-findings" \
  || bad "never touches an unmarked (older-order) or later apply-findings"
rm -f "$T/beads/af-1.json" "$T/beads/af-old.json" "$T/beads/af-2.json"
# ADR-0150, through the real check script: the design rule stops a render-path
# diff before the gate runs, and the write-scope rule stops a stray path on any
# rig. Both are tested in full in gascity/tests/test_design_scope.py; what is
# pinned here is that `packet-gates.sh` actually runs them, and runs them first.
( cd "$WT" && mkdir -p web-app/src && echo 'export const App = 1' > web-app/src/App.tsx \
  && git add web-app/src/App.tsx && git -c user.email=t@t -c user.name=t commit -qm console )
root "test -f g.txt" '{"proof.repo":"liskov-ui","proof.write_scope":"liskov-ui:web-app/src/**\nliskov-ui:g.txt"}'
expect 1 "refuses a Console change with no agreed design"      "$CHECKS/packet-gates.sh"
[ "$(jq -r '.[0].metadata["proof.validated_tree"] // "none"' "$T/beads/r-1.json")" = "none" ] \
  && ok "  and refuses BEFORE the gate, so it costs no build" || bad "  and refuses BEFORE the gate, so it costs no build"
root "test -f g.txt" '{"proof.repo":"liskov-ui","proof.write_scope":"liskov-ui:web-app/src/**\nliskov-ui:g.txt","proof.design_mockup":"slipway-app-overview@sha256:abc"}'
expect 0 "passes the same change with an agreed design"        "$CHECKS/packet-gates.sh"
[ "$(jq -r '.[0].metadata["proof.design_built"]' "$T/beads/r-1.json")" = "slipway-app-overview@sha256:abc" ] \
  && ok "  and records the design the tree was built to" || bad "  and records the design the tree was built to"
# ADR-0159: outside the declared scope, only a protected path, or a review
# substring with no reviewer, refuses; the rest is allowed and recorded.
expansion() { jq -r '.[0].metadata["proof.scope_expansion"] // "unset"' "$T/beads/r-1.json"; }
commit_file() { ( cd "$WT" && mkdir -p "$(dirname "$1")" && echo x > "$1" && git add "$1" \
  && git -c user.email=t@t -c user.name=t commit -qm "add $1" ); }
drop_file() { ( cd "$WT" && git rm -q "$1" && git -c user.email=t@t -c user.name=t commit -qm "drop $1" ); }
root "test -f g.txt" '{"proof.repo":"liskov-rs","proof.write_scope":"liskov-rs:crates/**"}'
expect 0 "passes a companion outside the write scope, on any rig" "$CHECKS/packet-gates.sh"
[ "$(expansion)" = "$(printf 'liskov-rs:g.txt\nliskov-rs:web-app/src/App.tsx')" ] \
  && ok "  and records it on the root as proof.scope_expansion" || bad "  and records it on the root as proof.scope_expansion ($(expansion))"
root "test -f g.txt" '{"proof.repo":"liskov-rs","proof.write_scope":"liskov-rs:g.txt\nliskov-rs:web-app/**"}'
expect 0 "passes a change its scope declares"                   "$CHECKS/packet-gates.sh"
[ "$(expansion)" = "" ] \
  && ok "  and records an empty expansion, not none" || bad "  and records an empty expansion, not none ($(expansion))"
commit_file .github/workflows/ci.yml
root "test -f g.txt" '{"proof.repo":"liskov-rs","proof.write_scope":"liskov-rs:crates/**"}'
expect 1 "refuses an undeclared workflow file"                  "$CHECKS/packet-gates.sh"
grep -q "ci.yml is protected" "$T/out" && [ "$(expansion)" = "unset" ] \
  && [ "$(jq -r '.[0].metadata["proof.validated_tree"] // "none"' "$T/beads/r-1.json")" = "none" ] \
  && ok "  says why, records nothing, and runs no gate" || bad "  says why, records nothing, and runs no gate"
drop_file .github/workflows/ci.yml
commit_file crates/billing/lib.rs
root "test -f g.txt" '{"proof.repo":"liskov-rs","proof.write_scope":"liskov-rs:g.txt\nliskov-rs:web-app/**"}'
expect 1 "refuses an undeclared review-substring path with no reviewer" "$CHECKS/packet-gates.sh"
grep -q '`billing`' "$T/out" && grep -q -- "--review" "$T/out" \
  && ok "  names the substring and the repair" || bad "  names the substring and the repair"
root "test -f g.txt" '{"proof.repo":"liskov-rs","proof.write_scope":"liskov-rs:g.txt\nliskov-rs:web-app/**","proof.review":"on"}'
expect 0 "passes it when admission recorded a reviewer"         "$CHECKS/packet-gates.sh"
[ "$(expansion)" = "liskov-rs:crates/billing/lib.rs" ] \
  && ok "  and records it as the expansion" || bad "  and records it as the expansion ($(expansion))"
root "test -f g.txt" '{"proof.repo":"liskov-rs","proof.write_scope":"liskov-rs:g.txt\nliskov-rs:web-app/**","gc.var.review":"true"}'
expect 0 "a root from an older pack falls back to the review formula variable" "$CHECKS/packet-gates.sh"
root "test -f g.txt" '{"proof.repo":"liskov-rs","proof.write_scope":"liskov-rs:g.txt\nliskov-rs:web-app/**","gc.var.rig_name":"liskov-ui"}'
expect 0 "the substrings are the packet's rig's, named by rig_name" "$CHECKS/packet-gates.sh"
grep -q "has no \[rigs.liskov-ui\] table" "$T/out" \
  && ok "  and a rig with no table is loud, not a refusal" || bad "  and a rig with no table is loud, not a refusal"
printf 'not toml [\n' > "$GC_CITY/proof/policy.toml"
expect 1 "an unreadable policy refuses rather than dropping the substrings" "$CHECKS/packet-gates.sh"
grep -q "could not read the review substrings" "$T/out" \
  && ok "  and says it could not read them" || bad "  and says it could not read them"
printf '[rigs.liskov-rs]\nreview_if_scope_contains = ["billing"]\n' > "$GC_CITY/proof/policy.toml"
drop_file crates/billing/lib.rs
root "touch gate-ran" '{"proof.repo":"liskov-rs","proof.write_scope":null}'
expect 1 "a workflow with no recorded scope refuses" "$CHECKS/packet-gates.sh"
grep -Fq "this workflow records no write_scope. Admission writes it on the workflow root; a root without it was cut off mid-admission and the dispatcher writes it on its next pass (BKLG-20261011-92r0). The scope rule is not skipped." "$T/out" \
  && [ "$(expansion)" = "unset" ] && [ ! -e "$WT/gate-ran" ] \
  && [ "$(jq -r '.[0].metadata["proof.validated_tree"] // "none"' "$T/beads/r-1.json")" = "none" ] \
  && ok "  names admission, records nothing, and runs no gate" || bad "  names admission, records nothing, and runs no gate"
root "test -f g.txt"
[ "$(jq -r '.[0].metadata["proof.design_built"] // "none"' "$T/beads/r-1.json")" = "none" ] \
  && ok "a packet that renders nothing records no design" || bad "a packet that renders nothing records no design"
( cd "$WT" && git rm -q web-app/src/App.tsx && git -c user.email=t@t -c user.name=t commit -qm revert )

# Gas City runs a check with HOME set to the city and a minimal PATH, which hides
# a toolchain under the operator's home. Reproduce that sandbox: the gate's tool
# is only reachable through the rig's operator-configured gate_path / gate_home.
mkdir -p "$T/tools" "$T/home"
printf '#!/bin/sh\ntest -f "$HOME/.toolchain" && test -f g.txt\n' > "$T/tools/mytool"; chmod +x "$T/tools/mytool"
touch "$T/home/.toolchain"
sandboxed() { env -i HOME="$T/city-home" PATH="$T/bin:/usr/local/bin:/usr/bin:/bin" BD_FAKE_STORE="$BD_FAKE_STORE" GC_BEAD_ID=c-1 GC_ITERATION=1 "$@"; }
root "mytool"
expect 1 "in gc's check sandbox the gate cannot find an operator toolchain" sandboxed "$CHECKS/packet-gates.sh"
root "mytool" "{\"gc.var.gate_path\":\"$T/tools\"}"
expect 1 "gate_path alone is not enough when the tool resolves state from HOME" sandboxed "$CHECKS/packet-gates.sh"
root "mytool" "{\"gc.var.gate_path\":\"$T/tools\",\"gc.var.gate_home\":\"$T/home\"}"
expect 0 "gate_path and gate_home give the gate, and only the gate, the toolchain" sandboxed "$CHECKS/packet-gates.sh"
# The check runs sandboxed: no GC_CITY, HOME and PATH replaced. The deployed pack
# sits at <city>/packs/proof, so the city is reachable from the script's own path.
commit_file crates/billing/lib.rs
mkdir -p "$T/pcity/packs" "$T/pcity/proof"; touch "$T/pcity/city.toml"
ln -s "$(cd -- "$CHECKS/../../.." && pwd)" "$T/pcity/packs/proof"
printf '[rigs.liskov-rs]\nreview_if_scope_contains = ["billing"]\n' > "$T/pcity/proof/policy.toml"
root "test -f g.txt" '{"proof.repo":"liskov-rs","proof.write_scope":"liskov-rs:g.txt\nliskov-rs:web-app/**"}'
expect 1 "in gc's check sandbox the policy is read from the deployed city" \
  sandboxed "$T/pcity/packs/proof/assets/scripts/checks/packet-gates.sh"
grep -q '`billing`' "$T/out" && ok "  and its substrings still refuse" || bad "  and its substrings still refuse"
rm "$T/pcity/proof/policy.toml"
expect 0 "a city with no policy file applies no substring, loudly" \
  sandboxed "$T/pcity/packs/proof/assets/scripts/checks/packet-gates.sh"
grep -q "no policy file at $T/pcity/proof/policy.toml" "$T/out" \
  && ok "  and says which file it looked for" || bad "  and says which file it looked for"
drop_file crates/billing/lib.rs
( cd "$WT" && git checkout -q -b agent/other )
root "true"
expect 1 "refuses a worktree on the wrong branch"              "$CHECKS/packet-gates.sh"
( cd "$WT" && git checkout -q agent/pkt && git branch -qD agent/other )

echo "review-verdict.sh"
root "true" "{\"proof.validated_tree\":\"$TREE\"}"
rm -f "$T/beads/v-1.json"
expect 1 "refuses when no verdict exists"                      "$CHECKS/review-verdict.sh"
review iterate "$TREE"
expect 1 "refuses an 'iterate' verdict"                        "$CHECKS/review-verdict.sh"
review reject "$TREE"
expect 1 "refuses a 'reject' verdict"                          "$CHECKS/review-verdict.sh"
review approve "$TREE"
jq '.[0].metadata["gc.kind"]="task"' "$T/beads/v-1.json" > "$T/beads/v-1.tmp"
mv "$T/beads/v-1.tmp" "$T/beads/v-1.json"
expect 0 "accepts an independent review on an explicit task-kind bead" "$CHECKS/review-verdict.sh"
review approve "0000000000000000000000000000000000000000"
expect 1 "refuses stale review evidence (reviewed tree != HEAD)" "$CHECKS/review-verdict.sh"
review approve "$TREE"
expect 0 "passes on approve of the current, gated tree"        "$CHECKS/review-verdict.sh"
[ "$(jq -r '.[0].metadata["proof.approved_tree"]' "$T/beads/r-1.json")" = "$TREE" ] \
  && ok "stamps the approved tree on the workflow root" || bad "stamps the approved tree on the workflow root"
# Verdict metadata is copied upward onto control and scope beads when an
# iteration closes. A later-closed scope bead carrying an older "approve" must
# not outvote the reviewer's own, newer "iterate".
review iterate "$TREE"
jq -n --arg t "$TREE" '[{id:"s-1", closed_at:"2026-01-02T00:00:00Z", metadata:{"gc.root_bead_id":"r-1","gc.kind":"scope","proof.review_verdict":"approve","proof.reviewed_tree":$t}}]' > "$T/beads/s-1.json"
expect 1 "ignores a verdict copied onto a control or scope bead" "$CHECKS/review-verdict.sh"
rm -f "$T/beads/s-1.json"
review approve "$TREE"
root "false" '{"proof.validated_tree":"stale"}'
expect 1 "re-runs gates when the tree moved, and fails if they fail" "$CHECKS/review-verdict.sh"
# The gate lane in the review loop: the gater gated the tree under review beside
# the review. Only that iteration's review-cycle record counts, for HEAD's tree.
gate_record pass "$TREE" "proof__gater-pr-abc" 1 "" "review-cycle.iteration.1.review-gate"
expect 0 "review gate lane: a gater pass on the moved tree passes without the inline gate" "$CHECKS/review-verdict.sh"
grep -q "gate passed on the gate lane" "$T/out" && ok "review gate lane: says the lane passed it" || bad "review gate lane: says the lane passed it"
[ "$(jq -r '.[0].metadata | [."proof.validated_tree", ."proof.approved_tree"] | join(" ")' "$T/beads/r-1.json")" = "$TREE $TREE" ] \
  && ok "review gate lane: stamps the validated and approved tree" || bad "review gate lane: stamps the validated and approved tree"
root "true" '{"proof.validated_tree":"stale"}'
gate_record fail "$TREE" "proof__gater-pr-abc" 1 "" "mol-proof-packet.review-cycle.iteration.1.review-gate"
expect 1 "review gate lane: a gater fail fails, even when an inline gate would pass" "$CHECKS/review-verdict.sh"
grep -q "mismatched types" "$T/out" && ok "review gate lane: repeats the failed gate's log tail" || bad "review gate lane: repeats the failed gate's log tail"
root "false" '{"proof.validated_tree":"stale"}'
gate_record pass "$TREE" "proof__gater-pr-abc" 1 "" "implement.iteration.1.gate"
expect 1 "review gate lane: an implement record is not evidence for the review check" "$CHECKS/review-verdict.sh"
gate_record pass "$TREE" "proof__gater-pr-abc" 2 "" "review-cycle.iteration.2.review-gate"
expect 1 "review gate lane: another iteration's record is not evidence" "$CHECKS/review-verdict.sh"
root "test -f g.txt" '{"proof.validated_tree":"stale"}'
gate_record pass "0000000000000000000000000000000000000000" "proof__gater-pr-abc" 1 "" "review-cycle.iteration.1.review-gate"
expect 0 "review gate lane: a record for another tree falls back to the inline gate" "$CHECKS/review-verdict.sh"
grep -q "re-running gates" "$T/out" && ok "review gate lane: ...and says it gated inline" || bad "review gate lane: ...and says it gated inline"
rm -f "$T/beads/g-1.json"
# The last review is final (BKLG-20261004-axhn). The loop's budget lives on its
# control bead; on the last iteration a non-approving review stops the packet
# with a reason and the findings on record. Earlier iterations record nothing.
control() { # control <max_attempts>
  jq -n --arg m "$1" '[{id:"rc-1", status:"open", metadata:{"gc.root_bead_id":"r-1","gc.kind":"ralph",
    "gc.step_id":"review-cycle","gc.step_ref":"mol-proof-packet.review-cycle","gc.max_attempts":$m}}]' > "$T/beads/rc-1.json"
}
noted_review() { # noted_review <verdict> <tree>
  jq -n --arg v "$1" --arg t "$2" '[{id:"v-1", status:"closed", closed_at:"2026-01-01T00:00:00Z",
    notes:"1. handle the empty case\n2. test it",
    metadata:{"gc.root_bead_id":"r-1","gc.step_ref":"mol-proof-packet.review-cycle.iteration.3.review",
              "proof.review_verdict":$v,"proof.reviewed_tree":$t}}]' > "$T/beads/v-1.json"
}
md() { jq -r --arg k "$2" '.[0].metadata[$k] // empty' "$T/beads/$1.json"; }
root "true" "{\"proof.validated_tree\":\"$TREE\"}"
control 3
noted_review iterate "$TREE"
GC_ITERATION=2 expect 1 "an 'iterate' before the last review only fails the iteration" "$CHECKS/review-verdict.sh"
[ -z "$(md v-1 gc.failure_reason)$(md r-1 proof.review_failure_reason)" ] && [ "$(md rc-1 gc.max_attempts)" = 3 ] \
  && ok "  and records no stop, leaving the budget alone" || bad "  and records no stop, leaving the budget alone"
GC_ITERATION=3 expect 1 "an 'iterate' on the last review stops the packet" "$CHECKS/review-verdict.sh"
grep -q "review_budget_exhausted" "$T/out" && ok "  and says review_budget_exhausted" || bad "  and says review_budget_exhausted"
[ "$(md v-1 gc.failure_reason)" = review_budget_exhausted ] \
  && ok "  records review_budget_exhausted on the review, where the factory reads a stop" \
  || bad "  records review_budget_exhausted on the review, where the factory reads a stop"
[ "$(md r-1 proof.review_failure_reason) $(md r-1 proof.outstanding_findings_bead)" = "review_budget_exhausted v-1" ] \
  && [ "$(md r-1 proof.outstanding_findings)" = "$(printf '1. handle the empty case\n2. test it')" ] \
  && ok "  records the outstanding findings on the workflow root" || bad "  records the outstanding findings on the workflow root"
root "true" "{\"proof.validated_tree\":\"$TREE\"}"
noted_review reject "$TREE"
GC_ITERATION=1 expect 1 "a 'reject' fails the iteration" "$CHECKS/review-verdict.sh"
[ "$(md rc-1 gc.max_attempts)" = 3 ] \
  && ok "  and leaves controller budgets to the engine" \
  || bad "  and leaves controller budgets to the engine"
control 2
GC_ITERATION=2 expect 1 "a 'reject' on the last review stops the packet" "$CHECKS/review-verdict.sh"
[ "$(md v-1 gc.failure_reason) $(md rc-1 gc.max_attempts)" = "review_rejected 2" ] \
  && ok "  records review_rejected, and never raises the budget" || bad "  records review_rejected, and never raises the budget"
rm -f "$T/beads/rc-1.json" "$T/beads/v-1.json"

echo "land-on-main.sh"
export GC_CITY="$T/city"
mkdir -p "$GC_CITY/proof"
touch "$GC_CITY/city.toml"
LAND="$CHECKS/land-on-main.sh"
commit() { git -C "$WT" -c user.email=t@t -c user.name=t commit -qm "$1"; }
undo()   { git -C "$WT" reset -q --hard "$1"; }
origin_main() { git -C "$T/origin.git" rev-parse refs/heads/main; }
APPROVED="$(git -C "$WT" rev-parse HEAD)"
landing() { # landing <gate> [extra metadata json] — a root admitted with landing 'main'
  local extra="${2:-{\}}"
  root "$1" "$(jq -cn --arg c "$APPROVED" --arg t "$TREE" --arg i "$ITEM" --argjson e "$extra" \
    '{"gc.var.land_main":"true","gc.var.item_path":$i,"proof.approved_commit":$c,"proof.approved_tree":$t} + $e')"
}
root "true" "{\"proof.approved_commit\":\"$APPROVED\",\"proof.approved_tree\":\"$TREE\"}"
expect 1 "refuses a packet that was not admitted with landing 'main'" "$LAND"
root "true" "{\"gc.var.land_main\":\"true\",\"gc.var.item_path\":\"$ITEM\"}"
expect 1 "refuses a packet with no approved commit on record"   "$LAND"
landing "true"
expect 1 "refuses while the backlog item is not completed"      "$LAND"

complete_item() {
  ( cd "$WT" && sed -i -e 's/^status: working$/status: done/' -e '/^claimed_by:/d' -e '/^claimed_at:/d' \
      -e 's|^claim_expires: .*|done_at: 2026-01-01\nevidence:\n  - ../../raw/evidence.md|' "$ITEM" \
    && sed -i 's/^claimed 1$/claimed 0/' docs/backlog/delivery-queue.md \
    && git add "$ITEM" docs/backlog/delivery-queue.md )
}
complete_item && commit "complete the item"
READY="$(git -C "$WT" rev-parse HEAD)"
BEFORE="$(origin_main)"
expect 0 "--verify accepts the completed, approved packet"      "$LAND" --verify
[ "$(origin_main)" = "$BEFORE" ] && ok "--verify pushes nothing" || bad "--verify pushes nothing"

( cd "$WT" && echo extra > unreviewed.txt && git add unreviewed.txt ) && commit "slip in a file"
expect 1 "refuses a path that was never reviewed"               "$LAND"
undo "$READY"
( cd "$WT" && echo later >> g.txt && git add g.txt ) && commit "edit reviewed content"
expect 1 "refuses reviewed content that changed after approval" "$LAND"
undo "$READY"
( cd "$WT" && echo "More body." >> "$ITEM" && git add "$ITEM" ) && commit "edit the item body"
expect 1 "refuses an item whose body changed"                   "$LAND"
undo "$READY"
( cd "$WT" && sed -i 's/^priority: p2$/priority: p0/' "$ITEM" && git add "$ITEM" ) && commit "reprioritise"
expect 1 "refuses an item frontmatter change beyond completion" "$LAND"
undo "$READY"
( cd "$WT" && sed -i 's/^Footer, by hand.$/Footer, edited./' docs/backlog/delivery-queue.md && git add docs ) && commit "edit a view by hand"
expect 1 "refuses a view changed outside its generated block"   "$LAND"
undo "$READY"
ORPHAN="$(git -C "$WT" -c user.email=t@t -c user.name=t commit-tree "$TREE" -m rewritten)"
root "true" "{\"gc.var.land_main\":\"true\",\"gc.var.item_path\":\"$ITEM\",\"proof.approved_commit\":\"$ORPHAN\",\"proof.approved_tree\":\"$TREE\"}"
expect 1 "refuses a HEAD not built on the approved commit (rebase)" "$LAND"
landing "false"
expect 1 "refuses when the gate fails on the landing tree"      "$LAND"
landing "echo dirty > gate-dirty"
expect 1 "refuses a gate that dirties the validated tree"       "$LAND"
rm -f "$WT/gate-dirty"
landing "git -c user.name=t -c user.email=t@t commit --allow-empty -qm gate-mutated-head"
expect 1 "refuses a gate that changes the committed HEAD"       "$LAND"
undo "$READY"

# main moves while the packet is in flight
( cd "$T/seed" && echo concurrent > h.txt && git add h.txt && git commit -qm concurrent && git push -q origin main )
landing "true"
expect 1 "verify reports moved main without integrating it"    "$LAND" --verify
[ "$(origin_main)" = "$(git -C "$T/seed" rev-parse HEAD)" ] && ok "a refused landing leaves main untouched" || bad "a refused landing leaves main untouched"

# A separate process keeps the factory lock until the checked landing finishes.
# A bounded check must integrate, gate and push while that process still holds it.
hold_factory_lock() {
  rm -f "$T/lock-held" "$T/release-lock"
  python3 - "$GC_CITY/proof/factory.lock" "$T/lock-held" "$T/release-lock" <<'HOLDER' &
import fcntl, pathlib, sys, time
lock, ready, release = map(pathlib.Path, sys.argv[1:])
with lock.open('a') as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    ready.touch()
    deadline = time.monotonic() + 30
    while not release.exists():
        if time.monotonic() >= deadline:
            sys.exit('test never released the factory lock')
        time.sleep(.01)
HOLDER
  factory_lock_pid=$!
  for attempt in $(seq 1 100); do [ -f "$T/lock-held" ] && break; sleep .01; done
  [ -f "$T/lock-held" ] && ok "separate process holds the factory lock" || bad "separate process holds the factory lock"
}
release_factory_lock() {
  ! flock -n "$GC_CITY/proof/factory.lock" true && ok "factory lock stayed held through publication" || bad "factory lock stayed held through publication"
  touch "$T/release-lock"
  wait "$factory_lock_pid" || bad "factory lock holder timed out"
  flock -n "$GC_CITY/proof/factory.lock" true && ok "factory lock is released by its holder" || bad "factory lock is released by its holder"
}
hold_factory_lock
( cd "$T/seed" && echo 'another main advance' > j.txt && git add j.txt \
  && git commit -qm 'advance while factory lock is held' && git push -q origin main )

# a shared checkout whose fast-forward is refused by someone else's file
git clone -q -b main "$T/origin.git" "$T/shared"   # at the pre-landing main
echo "another session's work" > "$T/shared/g.txt"
landing "test -f g.txt && test -f h.txt && test -f j.txt && ! flock -n '$GC_CITY/proof/factory.lock' true" "{\"gc.var.shared_checkout\":\"$T/shared\"}"
expect 0 "integrates, gates and pushes while another process holds the factory lock" timeout 20 "$LAND"
release_factory_lock
LANDED="$(git -C "$WT" rev-parse HEAD)"
[ "$(origin_main)" = "$LANDED" ] && ok "main is exactly the gated commit" || bad "main is exactly the gated commit"
[ "$(jq -r '.[0].metadata["proof.validated_commit"]' "$T/beads/r-1.json")" = "$LANDED" ] \
  && ok "validation stamps the refreshed commit, not the pre-integration head" || bad "validation stamps the refreshed commit, not the pre-integration head"

[ "$(jq -r '.[0].metadata["proof.landed_commit"]' "$T/beads/r-1.json")" = "$LANDED" ] \
  && ok "records the landed commit on the workflow root" || bad "records the landed commit on the workflow root"
[ "$(jq -r '.[0].metadata["proof.republished"]' "$T/beads/r-1.json")" = "refused" ] \
  && [ "$(cat "$T/shared/g.txt")" = "another session's work" ] \
  && ok "leaves a shared checkout alone when its fast-forward is refused" || bad "leaves a shared checkout alone when its fast-forward is refused"
rm "$T/shared/g.txt"
expect 0 "passes again once landed (idempotent)"                "$LAND"
[ "$(git -C "$T/shared" rev-parse HEAD)" = "$LANDED" ] && ok "fast-forwards a clean shared checkout" || bad "fast-forwards a clean shared checkout"

echo "land-plan.sh"
# A planning run has no reviewer, so what may land is stated over paths: backlog
# items nobody is holding, open questions, and views changed only inside their
# generated blocks. Everything else about the landing is land-on-main.sh's.
PLAN="$CHECKS/land-plan.sh"
HELD=docs/backlog/items/BKLG-20260101-cd34-held.md
QUESTION=docs/questions/open/Q-20260101-ef56-who-decides.md
NEW_ITEM=docs/backlog/items/BKLG-20260101-gh78-planned.md
( cd "$T/seed" && git fetch -q origin && git merge -q --ff-only origin/main \
  && printf -- '---\ntitle: Held\nstatus: working\nclaimed_by: gascity/rig\n---\n\n# Held\n\nBody.\n' > "$HELD" \
  && printf -- '---\ntitle: Planned\nstatus: open\ndepends_on: []\n---\n\n# Planned\n' > "$NEW_ITEM" \
  && git add "$HELD" "$NEW_ITEM" && git commit -qm "an item a worker is holding" && git push -q origin main )
git -C "$T/clone" fetch -q origin
git -C "$T/clone" worktree add -q "$T/plan" -b agent/proof-planner-rig origin/main
PWT="$T/plan"
git -C "$PWT" config user.email t@t && git -C "$PWT" config user.name t
plan_root() { # plan_root <gate_command> [extra metadata json]
  jq -n --arg wd "$PWT" --arg gate "$1" --argjson extra "${2:-{\}}" \
    '[{id:"r-2", metadata: ({"gc.var.work_dir":$wd,"gc.var.branch":"agent/proof-planner-rig","gc.var.base_branch":"main","gc.var.gate_command":$gate,"gc.var.land_plan":"true","gc.var.assigned_items":"BKLG-20260101-gh78,BKLG-20260101-cd34"} + $extra)}]' \
    > "$T/beads/r-2.json"
  echo '[{"id":"c-2","metadata":{"gc.root_bead_id":"r-2"}}]' > "$T/beads/c-2.json"
}
pcommit() { git -C "$PWT" add -A && git -C "$PWT" commit -qm "$1"; }
pundo()   { git -C "$PWT" reset -q --hard "$1"; }
export GC_BEAD_ID=c-2
BASE_MAIN="$(origin_main)"

plan_root "true" '{"gc.var.land_plan":"false"}'
expect 1 "refuses a workflow not slung as a planning run"       "$PLAN"
plan_root "true"
echo proof.plan_outcome > "$T/beads/fail-key"
expect 1 "a failed no-op receipt is surfaced" "$PLAN"
rm "$T/beads/fail-key"
expect 0 "a run that wrote nothing passes"                      "$PLAN"
[ "$(origin_main)" = "$BASE_MAIN" ] && ok "and lands nothing" || bad "and lands nothing"
[ "$(jq -r '.[0].metadata["proof.plan_outcome"]' "$T/beads/r-2.json")" = "no-op" ] \
  && ok "and records the run as a no-op" || bad "and records the run as a no-op"
PBASE="$(git -C "$PWT" rev-parse HEAD)"

( cd "$PWT" && echo "touched" >> f.txt ) && pcommit "edit a file that is not planning output"
expect 1 "refuses a path that is not an item, a question or a view" "$PLAN"
pundo "$PBASE"
( cd "$PWT" && printf -- '---\ntitle: Held\nstatus: working\nclaimed_by: gascity/rig\n---\n\n# Held\n\nRe-shaped.\n' > "$HELD" ) && pcommit "re-shape an item a worker is holding"
expect 1 "refuses an item that is claimed or working on main"   "$PLAN"
pundo "$PBASE"
( cd "$PWT" && sed -i 's/^Footer, by hand.$/Footer, edited./' docs/backlog/delivery-queue.md ) && pcommit "edit a view by hand"
expect 1 "refuses a view changed outside its generated block"   "$PLAN"
pundo "$PBASE"
( cd "$PWT" && rm f.txt ) && pcommit "delete a file"
expect 1 "refuses a deletion"                                   "$PLAN"
pundo "$PBASE"

( cd "$PWT" && mkdir -p docs/questions/open && printf -- '---\nstatus: open\n---\nUnrelated decision\n' > "$QUESTION" ) && pcommit "unrelated question"
expect 1 "rejects a question unrelated to an assigned item" "$PLAN" --verify
pundo "$PBASE"
( cd "$PWT" && printf -- '---\nstatus: open\n---\nNew work\n' > docs/backlog/items/BKLG-20260101-zz99-invented.md ) && pcommit "invent a child packet"
expect 1 "rejects a newly invented backlog item" "$PLAN" --verify
pundo "$PBASE"
( cd "$PWT" && mkdir -p docs/questions/open \
  && printf -- '---\ntitle: Planned\nstatus: open\ndepends_on: [Q-20260101-ef56]\nwrite_scope:\n  - g.txt\n---\n\n# Planned\n\nShaped by the planner.\n' > "$NEW_ITEM" \
  && printf -- '---\ntitle: Who decides\nstatus: open\n---\n\n# Who decides\n\nAn owner call.\n' > "$QUESTION" \
  && sed -i 's/^claimed 1$/claimed 2/' docs/backlog/delivery-queue.md ) && pcommit "plan two items"
plan_root "true" '{"gc.var.assigned_items":"BKLG-20260101-cd34"}'
expect 1 "rejects editing an unassigned item" "$PLAN" --verify
plan_root "true" '{"gc.var.assigned_items":""}'
expect 1 "rejects legacy broad planning output without assignments" "$PLAN" --verify
plan_root "true"
expect 0 "--verify accepts items, a question and a regenerated view" "$PLAN" --verify
[ "$(origin_main)" = "$BASE_MAIN" ] && ok "--verify pushes nothing" || bad "--verify pushes nothing"
plan_root "false"
expect 1 "refuses when the rig gate fails on the planned tree"  "$PLAN"
PREADY="$(git -C "$PWT" rev-parse HEAD)"
plan_root "git -c user.name=t -c user.email=t@t commit --allow-empty -qm gate-mutated-head"
expect 1 "planning also refuses a gate that changes HEAD"      "$PLAN"
pundo "$PREADY"

( cd "$T/seed" && echo planner-concurrent > i.txt && git add i.txt && git commit -qm concurrent && git push -q origin main )
plan_root "true"
expect 1 "refuses while origin's main is not integrated"        "$PLAN"
[ "$(origin_main)" = "$(git -C "$T/seed" rev-parse HEAD)" ] && ok "a refused planning landing leaves main untouched" || bad "a refused planning landing leaves main untouched"
git -C "$PWT" fetch -q origin && git -C "$PWT" merge -q --no-edit origin/main
hold_factory_lock
plan_root "! flock -n '$GC_CITY/proof/factory.lock' true" "{\"gc.var.shared_checkout\":\"$T/shared\"}"
expect 0 "gates and pushes the integrated plan while the factory lock is held" timeout 20 "$PLAN"
release_factory_lock
PLANNED="$(git -C "$PWT" rev-parse HEAD)"
[ "$(origin_main)" = "$PLANNED" ] && ok "main is exactly the planning commit" || bad "main is exactly the planning commit"
[ "$(jq -r '.[0].metadata | [."proof.landed_commit", ."proof.plan_outcome"] | join(" ")' "$T/beads/r-2.json")" = "$PLANNED landed" ] \
  && ok "records the landed commit and the outcome" || bad "records the landed commit and the outcome"
[ "$(git -C "$T/shared" rev-parse HEAD)" = "$PLANNED" ] && ok "fast-forwards the shared checkout" || bad "fast-forwards the shared checkout"
expect 0 "passes again once landed (idempotent)"                "$PLAN"
[ "$(jq -r '.[0].metadata["proof.plan_outcome"]' "$T/beads/r-2.json")" = landed ] \
  && ok "an idempotent check does not downgrade landed to no-op" || bad "an idempotent check does not downgrade landed to no-op"

jq '.[0].metadata |= del(."proof.landed_commit", ."proof.landed_tree", ."proof.plan_outcome")' "$T/beads/r-2.json" > "$T/beads/lost.tmp"
mv "$T/beads/lost.tmp" "$T/beads/r-2.json"
expect 0 "reconciles a lost planning landing receipt from main and validation" "$PLAN"
[ "$(jq -r '.[0].metadata["proof.plan_outcome"]' "$T/beads/r-2.json")" = landed ] \
  && ok "receipt recovery records landed rather than no-op" || bad "receipt recovery records landed rather than no-op"


printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
