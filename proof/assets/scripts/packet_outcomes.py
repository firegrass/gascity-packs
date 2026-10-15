#!/usr/bin/env python3
"""Per-packet outcome facts, written once when a packet is delivered.

ADR-0238 §10 (stage 1b of the native-work plan): before a cheaper model tier
can be judged, every delivered packet needs the facts the judgement reads —
did the gate pass first time, how many review rounds and findings, how many
attempts and workflow roots, wall time against the item's timebox, and what
the run cost in list-price dollars by model. Nothing wrote these before; the
timeline export records `model-unrecorded` and `provider-unrecorded` on every
attempt because nothing stamps them.

One record per (packet, workflow root), schema `proof.timeline.v1` kind
`outcome`, appended to `<city>/proof/analytics/packet-outcomes.jsonl`, plus a
compact stamp on the work bead (`proof.outcome.*`, `proof.model`,
`proof.provider`) so the daily report and anyone reading the bead get the
figures without re-deriving them. A script: no agent, no model, no tokens, no
work-state writes beyond those stamps.

Sources, all read-only:
  - the rig's beads (work bead, workflow roots, step and control beads);
  - the engine's usage sink `<city>/.gc/usage.jsonl`, joined by the session id
    the step beads carry (`gc.session_id`) — the model there is the id the
    provider reported and the cost is what the engine priced at write time;
  - the work ledger row for the item's `timebox` and `model_floor`;
  - `<city>/proof/deployed-pack.txt` and `gc version` for provenance.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import factory_lib as fl  # noqa: E402
import timeline  # noqa: E402

ANALYTICS = "proof/analytics/packet-outcomes.jsonl"
ROLE_OF_STEP = (("implement", "worker"), ("review", "reviewer"), ("gate", "gater"),
                ("load-context", "worker"), ("workspace", "worker"), ("push", "worker"),
                ("open-pr", "worker"), ("land", "worker"), ("report", "worker"))
STAMPS = {"gate_first_pass": "proof.outcome.gate_first_pass", "attempts": "proof.outcome.attempts",
          "review_rounds": "proof.outcome.review_rounds", "cost_usd": "proof.outcome.cost_usd",
          "wall_hours": "proof.outcome.wall_hours", "timebox_hours_max": "proof.outcome.timebox_hours"}


def _ts(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def timebox_hours(text):
    """The upper bound of a shaping timebox in hours, or None.

    `2-3h` -> 3, `1h` -> 1, `30m` -> 0.5, `2 days` -> 48, `3-4h active work`
    -> 4; prose without a unit is None. The field is free-form in the ledger."""
    if not isinstance(text, str):
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:[-–]|to)\s*(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes|d|day|days)\b"
                  r"|(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes|d|day|days)\b", text, re.I)
    if not m:
        return None
    value, unit = (m.group(2), m.group(3)) if m.group(3) else (m.group(4), m.group(5))
    scale = {"h": 1.0, "m": 1 / 60.0, "d": 24.0}[unit[0].lower()]
    return round(float(value) * scale, 3)


def role_of(step_ref, worker_name=""):
    """The pack role a step or usage fact belongs to."""
    m = re.match(r"(?:[a-z0-9-]+__)?([a-z-]+?)(?:-pr-|-[a-z0-9]{2,}$)", worker_name or "")
    if worker_name.startswith("proof__") and m:
        return m.group(1)
    # The innermost part names the step: `implement.iteration.1.gate` is the gate.
    for part in reversed((step_ref or "").split(".")):
        for key, role in ROLE_OF_STEP:
            if part == key or part.startswith(key + "-"):
                return role
    return "unknown"


def usage_for(city, session_ids):
    """Model and compute facts of the given sessions from the engine's sink,
    read as a stream; the file is append-only and shared by every run."""
    path = Path(city) / ".gc/usage.jsonl"
    wanted, facts, seen = set(session_ids), [], set()
    if not wanted or not path.is_file():
        return facts
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not any(s in line for s in wanted):
                continue
            try:
                fact = json.loads(line)
            except ValueError:
                continue
            if fact.get("session_id") not in wanted:
                continue
            key = fact.get("idempotency_key") or fact.get("upstream_req_id")
            if key in seen:
                continue
            seen.add(key)
            facts.append(fact)
    return facts


def packet_beads(work, beads):
    """(roots, steps_by_root) for every workflow that ran FOR this work bead:
    the roots created in its admission interval, as packets_of pairs them."""
    pid = (work.get("metadata") or {}).get("proof.packet_id")
    siblings = sorted((b for b in beads if (b.get("metadata") or {}).get("proof.packet_id") == pid),
                      key=lambda b: b.get("created_at") or "")
    later = [b.get("created_at") or "" for b in siblings if (b.get("created_at") or "") > (work.get("created_at") or "")]
    until = later[0] if later else "~"
    roots = sorted((b for b in beads if (b.get("metadata") or {}).get("gc.kind") == "workflow"
                    and (b.get("metadata") or {}).get("gc.var.packet_id") == pid
                    and (b.get("metadata") or {}).get("gc.formula_name") != "mol-proof-pr-recovery"
                    and (work.get("created_at") or "") <= (b.get("created_at") or "") < until),
                   key=lambda b: b.get("created_at") or "")
    ids = {r["id"] for r in roots}
    steps = defaultdict(list)
    for b in beads:
        root = (b.get("metadata") or {}).get("gc.root_bead_id")
        if root in ids:
            steps[root].append(b)
    predecessors = sum(1 for b in siblings if (b.get("created_at") or "") < (work.get("created_at") or ""))
    return roots, steps, predecessors


def _attempt_log(md):
    raw = md.get("gc.attempt_log")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return raw if isinstance(raw, list) else []


def outcome_for(rig, work, beads, usage_facts, item=None, provenance=None, now=None):
    """The `outcome` facts of one delivered packet: a dict for timeline.snapshot."""
    md = work.get("metadata") or {}
    pid = md.get("proof.packet_id") or ""
    roots, steps, predecessors = packet_beads(work, beads)
    root = roots[-1] if roots else {}
    rmd = root.get("metadata") or {}
    gaps = []
    if not roots:
        gaps.append("workflow-missing")
    mine = [s for r in roots for s in steps.get(r["id"], [])]
    refs = {s["id"]: ((s.get("metadata") or {}).get("gc.step_ref") or "") for s in mine}
    sessions = {}
    for s in mine:
        sid = (s.get("metadata") or {}).get("gc.session_id")
        if sid:
            sessions[sid] = role_of(refs[s["id"]])
    # Attempts: how many times the pack had to start over for this item.
    load_attempts = sum(1 for s in mine if re.search(r"load-context\.attempt\.\d+$", refs[s["id"]]))
    implement_iters = sum(1 for s in mine if re.search(r"(^|\.)implement\.iteration\.\d+$", refs[s["id"]]))
    review_beads = sorted((s for s in mine if re.search(r"review-cycle\.iteration\.\d+\.review$", refs[s["id"]])),
                          key=lambda s: s.get("created_at") or "")
    # Gate first pass: the gate lane's first iteration bead, else the implement
    # control bead's attempt log (empty when the check passed first time).
    first_gate = next((s for s in mine if re.search(r"implement\.iteration\.1\.gate$", refs[s["id"]])), None)
    control = next((s for s in mine if refs[s["id"]].endswith(".implement") and "gc.max_attempts" in (s.get("metadata") or {})), None)
    if first_gate is not None:
        result = (first_gate.get("metadata") or {}).get("proof.gate_result") or (first_gate.get("metadata") or {}).get("gc.outcome")
        gate_first = result == "pass"
    elif control is not None:
        gate_first = not _attempt_log(control.get("metadata") or {}) and implement_iters <= 1
    else:
        gate_first = None
        gaps.append("gate-unrecorded")
    verdicts = {}
    findings, findings_known = 0, bool(review_beads)
    for n, rb in enumerate(review_beads, 1):
        bmd = rb.get("metadata") or {}
        verdicts["round_%d" % n] = str(bmd.get("proof.review_verdict") or "unrecorded")
        count = bmd.get("proof.review_findings")
        if str(count).isdigit():
            findings += int(count)
        else:
            findings_known = False
    if review_beads and not findings_known:
        gaps.append("review-findings-uncounted")
    # Dollars and models, from the engine's own facts.
    cost, cost_by_model, tokens_by_model, unpriced, active = 0.0, defaultdict(float), defaultdict(int), 0, 0.0
    model_by_role, provider_by_role = {}, {}
    for f in usage_facts:
        if f.get("kind") == "compute":
            active += float(f.get("wall_seconds") or 0)
            continue
        role = role_of("", f.get("worker") or "")
        if role == "unknown":
            role = sessions.get(f.get("session_id"), "unknown")
        model, provider = f.get("model") or "unrecorded", f.get("provider") or "unrecorded"
        model_by_role.setdefault(role, model)
        provider_by_role.setdefault(role, provider)
        tokens_by_model[model] += int(f.get("input_tokens") or 0) + int(f.get("output_tokens") or 0) \
            + int(f.get("cache_read_tokens") or 0) + int(f.get("cache_creation_tokens") or 0)
        if f.get("unpriced") or f.get("cost_usd_estimate") is None:
            unpriced += 1
        else:
            cost += float(f["cost_usd_estimate"])
            cost_by_model[model] += float(f["cost_usd_estimate"])
    if not any(f.get("kind") == "model" for f in usage_facts):
        gaps.append("usage-missing")
    elif unpriced:
        gaps.append("usage-partly-unpriced")
    if "worker" not in model_by_role:
        gaps.append("model-unrecorded")
    created, closed = _ts(work.get("created_at")), _ts(root.get("closed_at")) if root else None
    end = closed or (now or datetime.now(timezone.utc))
    wall = round((end - created).total_seconds() / 3600, 3) if created else None
    fields = (item or {}).get("fields") or {}
    box = fields.get("timebox")
    box_hours = timebox_hours(box)
    if box is None:
        gaps.append("timebox-missing")
    elif box_hours is None:
        gaps.append("timebox-unparsed")
    prov = provenance or {}
    facts = dict(
        packet=timeline.identity(pid), workflow=timeline.identity(root.get("id")),
        state=timeline.identity(rmd.get("gc.outcome")) or "unrecorded",
        landing=timeline.identity(md.get("proof.landing")) or "unrecorded",
        attempts=len(roots) + predecessors, workflow_roots=len(roots), prior_beads=predecessors,
        load_context_attempts=load_attempts, implement_iterations=implement_iters,
        gate_first_pass=gate_first, review_rounds=len(review_beads),
        review_findings=findings if findings_known else None, review_verdicts=verdicts,
        provider_by_role=provider_by_role, model_by_role=model_by_role, sessions=len(sessions),
        wall_hours=wall, active_hours=round(active / 3600, 3),
        timebox=timeline.identity(re.sub(r"\s+", "_", str(box))[:60]) if box is not None else None,
        timebox_hours_max=box_hours,
        wall_over_timebox=round(wall / box_hours, 3) if wall is not None and box_hours else None,
        cost_usd=round(cost, 4), cost_by_model={k: round(v, 4) for k, v in cost_by_model.items()},
        tokens_by_model=dict(tokens_by_model), unpriced_facts=unpriced,
        model_floor=timeline.identity(fields.get("model_floor")),
        landed=timeline.identity(rmd.get("proof.landed_commit")) or timeline.identity(md.get("proof.completed")),
        merge_commit=timeline.identity(md.get("proof.merge_commit")),
        main_green=timeline.identity(md.get("proof.main_green")),
        engine=timeline.identity(prov.get("engine")), pack_commit=timeline.identity(prov.get("pack_commit")),
        created_at=timeline.timestamp(work.get("created_at")), closed_at=timeline.timestamp(root.get("closed_at")) if root else None,
        gaps=sorted(set(gaps)))
    return facts


def provenance(city):
    """Engine commit and deployed pack commit, from receipts; 'unknown' when absent."""
    result = {"engine": None, "pack_commit": None}
    try:
        import admit
        payload = json.loads(admit.gc(city, "version", "--json").stdout)
        if re.fullmatch(r"[0-9a-f]{40}", str(payload.get("commit", ""))):
            result["engine"] = payload["commit"]
    except Exception:  # noqa: BLE001 — provenance is diagnostic, never a gate
        pass
    try:
        for line in (Path(city) / "proof/deployed-pack.txt").read_text().splitlines():
            if re.fullmatch(r"pack_commit=[0-9a-f]{40}", line):
                result["pack_commit"] = line.split("=", 1)[1]
    except OSError:
        pass
    return result


def ledger_row(backlog_clone, packet_id):
    if not backlog_clone or not packet_id:
        return None
    try:
        import work_authority as wa
        return wa.row(backlog_clone, packet_id)
    except Exception:  # noqa: BLE001 — a missing ledger read is a recorded gap, not a stop
        return None


def already_recorded(path, packet, workflow):
    if not Path(path).is_file():
        return False
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            f = row.get("facts") or {}
            if f.get("packet") == packet and f.get("workflow") == workflow:
                return True
    return False


def record(city, rig, work, beads, backlog_clone="", now=None):
    """Write the outcome record and the bead stamps for one delivered packet.
    Returns a one-line summary for the keeper's notice, '' when nothing was
    written (already recorded, or no packet id)."""
    md = work.get("metadata") or {}
    pid = md.get("proof.packet_id") or ""
    if not pid:
        return ""
    roots, steps, _ = packet_beads(work, beads)
    sessions = {(s.get("metadata") or {}).get("gc.session_id") for r in roots for s in steps.get(r["id"], [])}
    facts = outcome_for(rig, work, beads, usage_for(city, {s for s in sessions if s}),
                        item=ledger_row(backlog_clone, pid), provenance=provenance(city), now=now)
    path = Path(city) / ANALYTICS
    if already_recorded(path, facts["packet"], facts["workflow"]):
        return ""
    row = timeline.snapshot("outcome", rig, work["id"], facts, (now or datetime.now(timezone.utc)).isoformat())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
    stamps = []
    for key, name in STAMPS.items():
        value = facts.get(key)
        if value is not None:
            stamps += ["--set-metadata", "%s=%s" % (name, str(value).lower() if isinstance(value, bool) else value)]
    worker_model, worker_provider = facts["model_by_role"].get("worker"), facts["provider_by_role"].get("worker")
    if worker_model and worker_provider:
        stamps += ["--set-metadata", "proof.model=" + worker_model, "--set-metadata", "proof.provider=" + worker_provider]
    if stamps:
        fl.sh(["gc", "bd", "--rig", rig, "update", work["id"]] + stamps)
    return ("\noutcome  gate first pass %s, %d attempt(s), %d review round(s), %s USD, %s h wall / %s h timebox, model %s%s"
            % (facts["gate_first_pass"], facts["attempts"], facts["review_rounds"], facts["cost_usd"],
               facts["wall_hours"], facts["timebox_hours_max"], worker_model or "unrecorded",
               ("  gaps: " + ", ".join(facts["gaps"])) if facts["gaps"] else ""))


def rows(city, since=None):
    """Outcome records on disk, newest last, optionally from an ISO instant."""
    path = Path(city) / ANALYTICS
    out = []
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("kind") == "outcome" and (not since or (row.get("collected_at") or "") >= since):
            out.append(row)
    return out


def _median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def rollup(records):
    """Per worker-model summary of outcome records: what stage 5 compares."""
    by_model = defaultdict(list)
    for row in records:
        f = row["facts"]
        by_model[f["model_by_role"].get("worker", "unrecorded")].append(f)
    table = {}
    for model, facts in sorted(by_model.items()):
        gated = [f["gate_first_pass"] for f in facts if f["gate_first_pass"] is not None]
        table[model] = dict(
            packets=len(facts), rigs=sorted({row["rig"] for row in records if row["facts"]["model_by_role"].get("worker", "unrecorded") == model}),
            gate_first_pass=(sum(gated) / len(gated)) if gated else None,
            attempts_mean=sum(f["attempts"] for f in facts) / len(facts),
            review_rounds_mean=sum(f["review_rounds"] for f in facts) / len(facts),
            findings_median=_median([f["review_findings"] for f in facts]),
            cost_usd_median=_median([f["cost_usd"] for f in facts]), cost_usd_total=round(sum(f["cost_usd"] for f in facts), 2),
            wall_hours_median=_median([f["wall_hours"] for f in facts]),
            wall_over_timebox_median=_median([f["wall_over_timebox"] for f in facts]),
            unpriced_facts=sum(f["unpriced_facts"] for f in facts),
            gaps=sorted({g for f in facts for g in f["gaps"]}))
    return table


def lines(city, since=None):
    """Report lines for the daily mail."""
    records = rows(city, since)
    if not records:
        return ["stage 1b outcomes: no delivered packet recorded%s" % (" since " + since if since else "")]
    out = ["stage 1b outcomes (%d delivered packet%s%s)" % (len(records), "" if len(records) == 1 else "s",
                                                           ", since " + since if since else "")]
    for model, t in rollup(records).items():
        out.append("  %-20s %3d packets  gate-first %s  attempts %.2f  reviews %.2f  findings %s  cost/packet %s USD (total %s)  wall %s h  wall/timebox %s%s"
                   % (model, t["packets"], "%.0f%%" % (100 * t["gate_first_pass"]) if t["gate_first_pass"] is not None else "n/a",
                      t["attempts_mean"], t["review_rounds_mean"],
                      t["findings_median"] if t["findings_median"] is not None else "n/a",
                      "%.2f" % t["cost_usd_median"] if t["cost_usd_median"] is not None else "n/a", t["cost_usd_total"],
                      "%.1f" % t["wall_hours_median"] if t["wall_hours_median"] is not None else "n/a",
                      "%.2f" % t["wall_over_timebox_median"] if t["wall_over_timebox_median"] is not None else "n/a",
                      ("  gaps: " + ", ".join(t["gaps"])) if t["gaps"] else ""))
    return out


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="Per-packet outcome facts (stage 1b).")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("rollup", help="print the per-model rollup of recorded outcomes")
    r.add_argument("--city", default=None)
    r.add_argument("--since", default=None, help="ISO instant; records collected before it are left out")
    r.add_argument("--json", action="store_true")
    s = sub.add_parser("show", help="compute (without writing) the outcome of one work bead")
    s.add_argument("--city", default=None)
    s.add_argument("--rig", required=True)
    s.add_argument("--bead", required=True)
    s.add_argument("--backlog", default="", help="Gas City-owned orchestrator clone, for the ledger row")
    args = p.parse_args(argv)
    city = args.city or fl.city_dir()
    if args.cmd == "rollup":
        if args.json:
            print(json.dumps(rollup(rows(city, args.since)), indent=2, sort_keys=True))
        else:
            print("\n".join(lines(city, args.since)))
        return 0
    beads = fl.rig_beads(city, args.rig)
    work = next((b for b in beads if b.get("id") == args.bead), None)
    if work is None:
        print("no bead %s in rig %s" % (args.bead, args.rig), file=sys.stderr)
        return 2
    roots, steps, _ = packet_beads(work, beads)
    sessions = {(s.get("metadata") or {}).get("gc.session_id") for rt in roots for s in steps.get(rt["id"], [])}
    facts = outcome_for(args.rig, work, beads, usage_for(city, {x for x in sessions if x}),
                        item=ledger_row(args.backlog, (work.get("metadata") or {}).get("proof.packet_id")),
                        provenance=provenance(city))
    print(json.dumps(facts, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
