"""Bounded planner policy and assignments. Beads retain the attempted events.

This policy is separate from the dispatcher's rig filters. An empty queue is
never an assignment; a released stop or an explicit owner request is.
"""
import hashlib
import json
import re
from datetime import datetime, timezone

import factory_lib as fl
import stall_recovery

MAX_BATCH = 3
ITEM_ID = re.compile(r"BKLG-[0-9]{8}-[a-z0-9]{4}")


def enabled(policy):
    mode = policy.get("planner_mode", "off")
    if mode not in ("off", "bounded"):
        raise fl.Refused("planner_mode must be off or bounded")
    auto = policy.get("planner_auto_triage", False)
    if not isinstance(auto, bool):
        raise fl.Refused("planner_auto_triage must be a boolean")
    return mode == "bounded"


def requested(cfg):
    ids = cfg.get("planner_items", [])
    token = cfg.get("planner_request_token", "")
    if (not isinstance(ids, list) or any(not isinstance(i, str) or not ITEM_ID.fullmatch(i) for i in ids)
            or len(set(ids)) != len(ids) or not isinstance(token, str)):
        raise fl.Refused("planner_items must be unique BKLG ids; planner_request_token must be a string")
    return ids, token


def event(pid, kind, identity):
    key = hashlib.sha256(json.dumps([pid, kind, identity], separators=(",", ":")).encode()).hexdigest()
    return {"id": pid, "kind": kind, "key": key, "source": identity}


def attempted(beads):
    """Fail closed on damaged receipts; never silently forget spent work."""
    keys = set()
    for bead in beads:
        receipt = (bead.get("metadata") or {}).get("proof.plan_assignments")
        if receipt is None:
            continue  # historical broad planning runs
        try:
            rows = json.loads(receipt) if isinstance(receipt, str) else receipt
            if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_BATCH:
                raise ValueError("expected one to three assignments")
            for row in rows:
                if not isinstance(row, dict) or not re.fullmatch(r"[0-9a-f]{64}", row.get("key", "")):
                    raise ValueError("invalid assignment key")
                keys.add(row["key"])
                aliases = row.get("also_attempted", [])
                if not isinstance(aliases, list) or any(not isinstance(k, str) or not re.fullmatch(r"[0-9a-f]{64}", k) for k in aliases):
                    raise ValueError("invalid assignment alias")
                keys.update(aliases)
        except (ValueError, TypeError) as exc:
            raise fl.Refused("invalid planner assignment receipt on %s: %s" % (bead.get("id"), exc))
    return keys


def recovery_evidence(rig, packet, beads, cfg):
    """A work marker alone cannot authorize repair of a later workflow.

    Released stops have closed roots, so their incident is terminal and needs
    no live session census. Diagnosis beads belong to the target rig, whereas
    planner attempt receipts belong to the home rig.
    """
    md = packet['md']
    root = packet.get('root') or {}
    state = md.get('proof.recovery_state')
    if (beads is None or cfg is None or root.get('status') != 'closed'
            or state not in ('repair', 'owner')
            or md.get('proof.recovery_parent') != root.get('id')):
        return None
    current = stall_recovery.incident(packet, beads, [], cfg, datetime.now(timezone.utc))
    if not current or md.get('proof.recovery_key') != current['key']:
        return None
    tasks = [b for b in beads if b.get('id') == md.get('proof.recovery_task')]
    if len(tasks) != 1 or tasks[0].get('status') != 'closed':
        return None
    task = tasks[0]
    tier = (task.get('metadata') or {}).get('proof.recovery_tier')
    if tier not in ('0', '1'):
        return None
    # Ambiguous duplicate spend reservations cannot supply a trusted receipt.
    if sum((b.get('metadata') or {}).get('proof.recovery_key') == current['key']
           and (b.get('metadata') or {}).get('proof.recovery_tier') == tier for b in beads) != 1:
        return None
    try:
        verdict = stall_recovery.receipt(task, current, rig, int(tier))
    except fl.Refused:
        return None
    if verdict is None or (state == 'repair' and verdict[0] != 'repair'):
        return None
    if state == 'owner' and (tier != '1' or verdict[0] not in ('owner', 'unclear', 'wait', 'nudge')):
        return None
    return {'state': state, 'reason': md.get('proof.recovery_reason', ''),
            'task': task['id'], 'parent': current['parent'], 'key': current['key']}


def candidates(rig, cfg, packets, beads, auto_triage, require_recovery=False,
               recovery_beads=None, recovery_cfg=None):
    ids, token = requested(cfg)
    seen = attempted(beads)
    rows = []
    if auto_triage:
        for p in sorted(packets, key=lambda p: (p["bead"].get("created_at", ""), p["bead"]["id"])):
            if (p["state"] not in ("failed", "declined") or not p["md"].get("proof.released")
                    or p["bead"].get("status") == "closed" or p.get("superseded")):
                continue
            recovered = recovery_evidence(rig, p, recovery_beads, recovery_cfg) if require_recovery else None
            if require_recovery and recovered is None:
                continue
            root = (p.get("root") or {}).get("id")
            if not root:
                raise fl.Refused("stopped packet %s has no workflow root; a person must inspect it" % p["id"])
            row = event(p["id"], "repair", [rig, p["bead"]["id"], root])
            if require_recovery:
                row['recovery'] = recovered
            rows.append(row)
    rows += [event(pid, "request", [rig, token]) for pid in ids]
    selected, used = [], {}
    for row in rows:
        if row["key"] in seen:
            continue
        if row["id"] in used:
            used[row["id"]].setdefault("also_attempted", []).append(row["key"])
        else:
            selected.append(row)
            used[row["id"]] = row
    return selected


def assigned_ids(value):
    ids = value.split(",")
    if not 1 <= len(ids) <= MAX_BATCH or len(set(ids)) != len(ids) or any(not ITEM_ID.fullmatch(i) for i in ids):
        raise fl.Refused("planning run needs one to three distinct assigned_items BKLG ids")
    return ids
