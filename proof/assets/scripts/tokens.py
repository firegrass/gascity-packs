#!/usr/bin/env python3
"""Token use per packet, read from the Claude Code and Codex transcripts of a rig's
sessions. No API call, no model: the transcripts are already on disk.

    tokens.py <rig-clone-path> <BKLG-id>      # one packet, as JSON

A session started in a rig root writes `~/.claude/projects/<rig path, / and .
as ->/<session>.jsonl`. A conversation is NOT one packet: a pooled worker that
finishes a workflow while another is queued takes it in the same conversation
(the first dispatched batch: one session declined `8wte`, then `524k`). So usage
is attributed message by message to the packet the conversation is currently
serving — the last one a step's own text named ("Execute backlog packet …",
"The work bead is packet …", "Implement packet …", "Review packet …"). Ids an
item merely mentions (`related`, `depends_on`) never switch it. Count every API
message ONCE: a transcript repeats a message's `usage` on each content-block
line, and summing lines overstated the first measured packet 2.6x.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from datetime import datetime, timezone
from functools import lru_cache

FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def transcripts(rig_clone):
    home = os.environ.get("PROOF_OPERATOR_HOME") or os.path.expanduser("~")
    if not os.path.isdir(os.path.join(home, ".claude")):
        import pwd
        home = pwd.getpwuid(os.getuid()).pw_dir      # a check sandbox points HOME at the city
    claude = glob.glob(os.path.join(home, ".claude", "projects", re.sub(r"[/.]", "-", rig_clone.rstrip("/")), "*.jsonl"))
    root = os.path.realpath(rig_clone)
    codex = [path for cwd, path in codex_transcripts(home)
             if cwd == root or cwd.startswith(root + os.sep)]
    return claude + codex


@lru_cache(maxsize=4)
def codex_transcripts(home):
    """Index session headers once per report, without reading unrelated bodies."""
    rows = []
    for path in glob.glob(os.path.join(home, ".codex", "sessions", "**", "*.jsonl"), recursive=True):
        try:
            with open(path, encoding="utf-8") as stream:
                header = json.loads(stream.readline())
            if header.get("type") == "session_meta":
                cwd = (header.get("payload") or {}).get("cwd")
                if cwd:
                    rows.append((os.path.realpath(cwd), path))
        except (OSError, ValueError):
            continue  # Incomplete/unreadable headers cannot establish ownership.
    return rows


SERVING = re.compile(r"(?:Execute backlog packet|work bead is packet|Implement packet|Review packet|"
                     r"land packet|finish packet) (BKLG-[0-9]{8}-[a-z0-9]{4})")


# A planning run serves a RIG, not a packet. Its bead and its first step name it.
PLANNING = re.compile(r"(?:(?:Plan|shape) the top of the |Bounded planning for )([A-Za-z0-9._-]+)(?: (?:delivery )?queue|[: ]|\\n|$)")


def stamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def messages(path, seen=None):
    """Yield unique API messages with the packet active at that message's time."""
    seen = set() if seen is None else seen
    current, codex_id, previous = "", "", {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            payload = entry.get("payload") or {}
            if entry.get("type") == "session_meta":
                codex_id = payload.get("id") or payload.get("session_id") or ""
                continue
            if entry.get("type") == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                total = info.get("total_token_usage")
                if not codex_id or not isinstance(total, dict):
                    continue
                keys = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")
                current_total = {key: int(total.get(key) or 0) for key in keys}
                delta = {key: value - previous.get(key, 0) for key, value in current_total.items()}
                previous = current_total
                gap = any(value < 0 for value in delta.values())
                if gap:
                    last = info.get("last_token_usage") or {}
                    delta = {key: int(last.get(key) or 0) for key in keys}
                if not any(delta.values()) and not gap:
                    continue
                mid = "codex:" + codex_id + ":" + str(entry.get("timestamp")) + ":" + json.dumps(current_total, sort_keys=True)
                if mid in seen:
                    continue
                seen.add(mid)
                # Codex input includes cache hits/writes. Reasoning output is a
                # subset of output_tokens, never another charge to add.
                cached, written = delta["cached_input_tokens"], delta["cache_write_input_tokens"]
                usage = {"input_tokens": max(0, delta["input_tokens"] - cached - written),
                         "cache_read_input_tokens": cached, "cache_creation_input_tokens": written,
                         "output_tokens": delta["output_tokens"], "_coverage_gap": gap}
                yield current, stamp(entry.get("timestamp")), usage, mid
                continue
            if codex_id and ((entry.get("type") == "event_msg" and payload.get("type") != "user_message")
                             or (entry.get("type") == "response_item" and
                                 (payload.get("role") == "assistant" or payload.get("type") == "function_call"))):
                continue
            message = entry.get("message") or {}
            if entry.get("type") != "assistant":
                named, planned = SERVING.findall(line), PLANNING.findall(line)
                if named:
                    current = named[-1]
                elif planned and not current.startswith("BKLG-"):
                    current = "plan:" + planned[-1]
                continue
            usage, mid = message.get("usage"), message.get("id")
            if not usage or not mid or mid in seen:
                continue
            seen.add(mid)
            yield current, stamp(entry.get("timestamp")), usage, mid


def empty_usage():
    return dict.fromkeys(FIELDS + ("messages", "conversations", "unattributed_events"), 0)


def add_usage(total, usage):
    total["messages"] += 1
    total["unattributed_events"] += int(bool(usage.get("_coverage_gap")))
    for field in FIELDS:
        total[field] += int(usage.get(field) or 0)


def read(path, since_iso="", until_iso="", seen=None):
    """Usage in [since, until); packet switches before the window still apply."""
    lower, upper = stamp(since_iso), stamp(until_iso)
    by_packet = {}
    for current, at, usage, _mid in messages(path, seen):
        if lower is not None and (at is None or at < lower):
            continue
        if upper is not None and (at is None or at >= upper):
            continue
        add_usage(by_packet.setdefault(current, empty_usage()), usage)
    return by_packet


def aggregate(rig_clone, key, since_iso="", until_iso=""):
    total, seen = empty_usage(), set()
    for path in sorted(transcripts(rig_clone)):
        usage = read(path, since_iso, until_iso, seen).get(key)
        if usage:
            total["conversations"] += 1
            for field in FIELDS + ("messages", "unattributed_events"):
                total[field] += usage[field]
    return total


def planning_usage(rig_clone, target_rig, since_iso, until_iso=""):
    return aggregate(rig_clone, "plan:" + target_rig, since_iso, until_iso)


def packet_usage(rig_clone, packet_id):
    """Lifetime packet total; never sum this once per admission."""
    return aggregate(rig_clone, packet_id)


def usage_by_admission(rig_clone, beads, until_iso=""):
    """Disjoint admission intervals, including superseded beads.

    An API message belongs to the latest admission of its packet at that time.
    Reopening the same bead remains one admission. Old cumulative stamps are
    differenced only when the transcript for that admission is unavailable.
    """
    from bisect import bisect_right
    ceiling = stamp(until_iso)
    by_packet, result, starts = {}, {}, {}
    for bead in beads:
        pid = (bead.get("metadata") or {}).get("proof.packet_id")
        at = stamp(bead.get("created_at"))
        if pid and at is not None:
            by_packet.setdefault(pid, []).append(bead)
            result[bead["id"]] = empty_usage()
    for pid, rows in by_packet.items():
        rows.sort(key=lambda b: (stamp(b["created_at"]), b["id"]))
        starts[pid] = [stamp(b["created_at"]) for b in rows]
    seen, observed, conversations = set(), set(), {}
    for path in sorted(transcripts(rig_clone)):
        for pid, at, usage, _mid in messages(path, seen):
            if pid not in by_packet or at is None or (ceiling is not None and at >= ceiling):
                continue
            slot = bisect_right(starts[pid], at) - 1
            if slot < 0:
                continue
            bead_id = by_packet[pid][slot]["id"]
            observed.add(bead_id)
            conversations.setdefault(bead_id, set()).add(path)
            add_usage(result[bead_id], usage)
    metadata_fields = {
        "output_tokens": "proof.tokens_output", "input_tokens": "proof.tokens_input",
        "cache_read_input_tokens": "proof.tokens_cache_read",
        "cache_creation_input_tokens": "proof.tokens_cache_write",
    }
    for rows in by_packet.values():
        cumulative = dict.fromkeys(FIELDS, 0)
        for bead in rows:
            md, bead_id = bead.get("metadata") or {}, bead["id"]
            modern = md.get("proof.tokens_accounting") == "admission-v2"
            fallback = empty_usage()
            for field, key in metadata_fields.items():
                recorded = int(md.get(key) or 0)
                fallback[field] = recorded if modern else max(0, recorded - cumulative[field])
                cumulative[field] = cumulative[field] + recorded if modern else max(cumulative[field], recorded)
            if bead_id in observed:
                result[bead_id]["conversations"] = len(conversations[bead_id])
                result[bead_id]["source"] = "transcript"
                result[bead_id]["coverage"] = "partial" if result[bead_id]["unattributed_events"] else "observed"
            else:
                fallback["conversations"] = int(md.get("proof.conversations") or 0)
                fallback["source"] = "stamp" if modern else "legacy-delta"
                fallback["coverage"] = "recorded" if any(fallback[f] for f in FIELDS) else "unknown"
                result[bead_id] = fallback
    return result


def output_since_midnight(rig_clones, now=None):
    """Count messages dated today, including ongoing sessions from earlier days."""
    floor = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    spent, seen = 0, set()
    for clone in sorted(set(rig_clones)):
        for path in sorted(transcripts(clone)) if clone else []:
            for _pid, at, usage, _mid in messages(path, seen):
                if at is not None and at >= floor:
                    spent += int(usage.get("output_tokens") or 0)
    return spent


if __name__ == "__main__":
    print(json.dumps(packet_usage(sys.argv[1], sys.argv[2])))
