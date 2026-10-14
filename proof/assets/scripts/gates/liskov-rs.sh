#!/usr/bin/env bash
# Diff-aware gate for liskov-rs, run by the ORCHESTRATOR in the packet worktree.
#
# With review off by default this is the only check between a worker and the
# repository's own CI, so it is what liskov-rs AGENTS.md selects for a diff, not
# a formatting floor: the repository contracts composite for workflow/scripts;
# fmt for Rust-affecting work; for a Rust-affecting diff, Clippy with warnings
# denied and the tests of every touched crate. The structural guards are the
# repository's pre-commit hook, which runs in the owned clone at commit time.
# An integration target the diff changes directly also runs its ignored
# PostgreSQL tests locally, within the member-owned CI lanes. Ignored operator
# canaries are never authorized by a changed test path.
#
# Two lanes are left to the member's REQUIRED pull-request checks and are not
# run here (owner, 2026-10-07): the PostgreSQL fan-out a shared test-support or
# fixture path causes (`sweep`, pg-suite-sweep.yml, triggers on exactly those
# paths) and the local-harness container lane (`delivery-gate` needs `debug`,
# which runs it whenever the member's classifier selects it). Run here, they
# took 40-75 min on a shared host, past the 60-min check, and the rig's single
# control dispatcher held every other packet meanwhile.
#
# Operator-authored and trusted: it reads the diff's PATHS, never its content,
# and nothing from a bead. Configure per rig as `gate_command`, with `gate_path`
# and `gate_home` naming the toolchain (see gascity/README.md).
set -euo pipefail

BASE="$(git merge-base origin/main HEAD)"
mapfile -t CHANGED < <(git diff --name-only "$BASE" HEAD)
[ "${#CHANGED[@]}" -gt 0 ] || { echo "gate: no changed paths against origin/main" >&2; exit 1; }

export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-8}"   # three workers share this host
contracts=0
workspace=0
declare -A PKGS=() TESTS=() TEST_GROUPS=() WHOLE=() CI_OWNED=()
for path in "${CHANGED[@]}"; do
  case "$path" in
    .github/workflows/*|.github/actions/*|scripts/liskov/*) contracts=1 ;;
  esac
  case "$path" in
    Cargo.toml|Cargo.lock|rust-toolchain*|.cargo/*) workspace=1 ;;
    crates/*/*)
      crate="${path#crates/}"; crate="${crate%%/*}"
      manifest="crates/$crate/Cargo.toml"
      if [ ! -f "$manifest" ]; then workspace=1; continue; fi
      pkg="$(sed -n '/^\[package\]/,/^\[/{s/^name *= *"\([^"]*\)".*/\1/p}' "$manifest" | head -1)"
      [ -n "$pkg" ] || { echo "gate: cannot read the package name from $manifest" >&2; exit 1; }
      PKGS["$pkg"]=1
      case "$path" in
        crates/"$crate"/tests/*.rs)
          t="${path#crates/$crate/tests/}"; t="${t%%/*}"; t="${t%.rs}"
          if [ -f "crates/$crate/tests/$t.rs" ]; then
            TESTS["$pkg/$t"]=1
            relative="${path#crates/$crate/tests/}"
            relative="${relative#*/}"
            if [[ "$relative" == */* ]]; then
              group="${relative%%/*}"
              # A directory can be mounted beneath another module (e.g.
              # pg_runtime/peer_discovery). An anchored group filter is only
              # safe when the target root declares that module directly.
              if grep -Eq "^[[:space:]]*(pub(\([^)]*\))?[[:space:]]+)?mod[[:space:]]+${group}[[:space:]]*;" "crates/$crate/tests/$t.rs"; then
                TEST_GROUPS["$pkg/$t"]+=" $group"
              else
                WHOLE["$pkg/$t"]=1
              fi
            else
              # A root or depth-one helper can affect every sibling group.
              WHOLE["$pkg/$t"]=1
            fi
          elif [[ "$path" == crates/"$crate"/tests/*/*.rs ]]; then
            # Shared support has no target of its own. Its compile coverage is
            # `cargo test -p` below; its ignored PostgreSQL fan-out is CI's sweep.
            CI_OWNED["the PostgreSQL fan-out of a shared test-support change: required check \`sweep\`"]=1
          fi ;;
      esac ;;
  esac
done

if [ "$workspace" = 1 ] || [ "${#PKGS[@]}" -gt 0 ]; then contracts=1; fi
# Record the selected lane before any selected check can fail.
if [ "$workspace" = 1 ]; then
  echo "gate-fact: lane=workspace" >&2
elif [ "${#PKGS[@]}" -gt 0 ]; then
  echo "gate-fact: lane=rust" >&2
else
  echo "gate-fact: lane=contracts" >&2
fi
if [ -f scripts/liskov/scorecard.py ]; then
  python3 scripts/liskov/scorecard.py --base "$BASE" >&2
fi
if [ "$contracts" = 1 ]; then
  python3 "$(dirname -- "${BASH_SOURCE[0]}")/repository_contracts.py" "$BASE"
fi

if [ "$workspace" = 0 ] && [ "${#PKGS[@]}" = 0 ]; then
  echo "gate: no Rust-affecting path changed; selected script/contracts checks complete" >&2
  exit 0
fi

echo "gate: cargo fmt" >&2
cargo fmt --all --check

if [ "$workspace" = 1 ]; then
  echo "gate: workspace input changed; clippy --workspace" >&2
  cargo clippy --locked --workspace --all-targets --all-features -- -D warnings
  cargo test --locked --workspace --all-targets --all-features
fi
for pkg in "${!PKGS[@]}"; do
  if [ "$workspace" = 0 ]; then
    echo "gate: clippy -p $pkg" >&2
    cargo clippy --locked -p "$pkg" --all-targets --all-features -- -D warnings
    echo "gate: test -p $pkg (all targets, including inventory guards)" >&2
    cargo test --locked -p "$pkg" --all-features --all-targets
  fi
done
# The member owns the composition coverage and test inventory. Do not copy it.
# #[ignore] also marks operator-only live canaries. Only the member's local PG
# lanes authorize ignored execution; do not maintain a canary deny-list here.
# Keep package predicates from the owner filters (suite names are not unique).
if [ "${#TESTS[@]}" -gt 0 ]; then
  sweep_filter="$(bash scripts/liskov/pg-sweep-targets.sh --nextest-filter)"
  v5_filter="$(bash scripts/liskov/classify-v5-pg-lane.sh --nextest-filter)"
  if [ -z "$sweep_filter" ] || [ -z "$v5_filter" ]; then
    echo "gate: empty member-owned PostgreSQL filter" >&2
    exit 1
  fi
  pg_filter="($sweep_filter) | ($v5_filter)"
fi
# One nextest run per package, as the member's own lanes run these suites
# (pg-suite-sweep.yml, delivery.yml: one invocation, eight test threads); its
# nextest profile reserves the lane for its timing-sensitive proofs.
declare -A PG_TARGETS=()
for key in "${!TESTS[@]}"; do
  if [[ "$key" == liskov-local-harness/* ]]; then
    CI_OWNED["local-harness target ${key#*/}: the local-harness lane, required check \`delivery-gate\`"]=1
    continue
  fi
  PG_TARGETS["${key%%/*}"]+=" ${key#*/}"
done
mapfile -t PG_PKGS < <(printf '%s\n' "${!PG_TARGETS[@]}" | sort)
for pkg in "${PG_PKGS[@]}"; do
  [ -n "$pkg" ] || continue
  mapfile -t targets < <(printf '%s\n' ${PG_TARGETS[$pkg]} | sort -u)
  # Membership comes from the same owners as the execution filter. A target
  # outside both lanes legitimately selects nothing, even for a group edit.
  member_targets="$(bash scripts/liskov/pg-sweep-targets.sh --targets "$pkg")"
  if [ "$pkg" = slipway ]; then
    v5_targets="$(bash scripts/liskov/classify-v5-pg-lane.sh --lane-targets)"
    member_targets+=$'\n'"$v5_targets"
  fi
  selectors=(); binaries=(); group_count=0; no_tests=fail
  for t in "${targets[@]}"; do
    selectors+=(--test "$t")
    if [ "${WHOLE[$pkg/$t]:-0}" = 1 ] || ! grep -qxF "$t" <<<"$member_targets"; then
      binaries+=("binary(=$t)")
      no_tests=pass
    else
      mapfile -t groups < <(printf '%s\n' ${TEST_GROUPS[$pkg/$t]} | sort -u)
      group_tests=()
      for group in "${groups[@]}"; do group_tests+=("test(/^${group}::/)"); done
      group_count=$((group_count + ${#groups[@]}))
      group_filter="$(printf ' | %s' "${group_tests[@]}")"; group_filter="${group_filter# | }"
      binaries+=("binary(=$t) & ($group_filter)")
    fi
  done
  target_filter="$(printf ' | %s' "${binaries[@]}")"; target_filter="${target_filter# | }"
  scope_note=""
  if [ "$group_count" -gt 0 ]; then scope_note=", scoped to $group_count group(s),"; fi
  echo "gate: ignored tests in ${#targets[@]} ${pkg} target(s)$scope_note intersected with member PostgreSQL lanes, in one run; other ignored tests stay unrun" >&2
  cargo nextest run --locked -p "$pkg" --all-features "${selectors[@]}" \
    --run-ignored only --no-tests "$no_tests" --test-threads "${GATE_PG_TEST_THREADS:-8}" \
    -E "($pg_filter) & ($target_filter)"
done
# Say what was left to CI, so neither a reviewer nor a person takes the gate's
# pass for evidence it does not carry.
harness="$(bash scripts/liskov/classify-local-harness-lane.sh --revisions "$BASE" HEAD)"
case "$harness" in
  local_harness_lane_required=true)
    CI_OWNED["the local-harness container lane: required check \`delivery-gate\` (job \`debug\`)"]=1 ;;
  local_harness_lane_required=false) ;;
  *) echo "gate: unrecognized harness classification: $harness" >&2; exit 1 ;;
esac
for owned in "${!CI_OWNED[@]}"; do
  echo "gate: left to liskov-rs pull-request CI, not run here: $owned" >&2
done
echo "gate: passed" >&2
