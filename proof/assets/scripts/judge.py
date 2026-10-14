#!/usr/bin/env python3
"""The one way anything in the PROOF pack asks a typed judgement.

A TypeSafe System One Noul returns the probability that a yes/no statement
holds about a state. The pack uses one where a rule is real but no glob, regex
or substring test can decide it — the pattern ADR-0144 established for
`scripts/agent_doctrine.py`.

**The rule every judgement in this pack obeys.** A backlog item and a worker's
own text are *untrusted input*. A judgement over them may only ever ADD caution
— raise a doctrine body, add a reviewer, hold an item, annotate a mail — and
never authorizes a landing, a merge, a completion or a skipped review. It fails
open to the behaviour the caller had before it existed, says so where a person
can see it, and never changes an exit code. State reaches the model as JSON
built from fields; item text is never pasted into the instructions.

This is a wrapper, not a second client. `scripts/typesafe_client.py` in the
orchestrator repository is the transport, and it is *found*, never copied: pack
scripts already reach the workspace's own modules by putting the Gas City-owned
orchestrator clone's `scripts/` on `sys.path` (`admit.load_item`), and
`search_dirs` does the same here. No retries, no caching, and no thresholds —
what a probability means is the caller's policy, not the transport's.

Where the key comes from, in the order the client tries it:

* ``TYPESAFE_API_KEY``. For what the SUPERVISOR runs — the factory order, so the
  dispatcher, the completer and the admission they shell out to — this is the
  supervisor's systemd drop-in, inherited by that process tree. **It does not
  reach an agent session.** A session is a child of the city's tmux server, and
  a server started before the drop-in never sees it (measured 2026-09-20: the
  supervisor had the name, all six live sessions did not).
* ``<repo root>/.secret-typesafe-api-key``, where the root is the directory
  above the client's own `scripts/`, or a `key_roots` entry that has the file.
  This is the only form an agent session can use — the planner running
  `shape_check.py` in a worktree of the Gas City-owned clone finds the file in
  that clone's root — and the form `shape_check.py` needs from any worktree:
  the key file is gitignored, so it exists only in a main checkout. Mode 600.

Neither is required. With no key every judgement returns a reason and the
caller keeps its previous behaviour.
"""
from __future__ import annotations

import importlib
import os
import sys
import time

CLIENT = "typesafe_client"
MODEL = "jev-latest"
# No command the factory runs may wait for ever. The client's own default is
# the same 15 s; it is stated here so a caller reading this file knows one
# exists without having to go and look.
TIMEOUT = 15.0
KEY_FILE = ".secret-typesafe-api-key"
# The calibration read each item's body capped at 24 kB. The cap is part of the
# question: a corpus gathered under a different one is not comparable.
BODY_CAP = 24000


def load_client(search_dirs=(), name=CLIENT):
    """The transport module, or (None, reason). Never raises."""
    for attempt in (0, 1):
        try:
            return importlib.import_module(name), None
        except Exception as exc:  # noqa: BLE001 - an advisory caller must not crash
            if attempt:
                return None, "the TypeSafe client (%s) is not importable: %s" % (name, exc)
        for directory in search_dirs:
            if directory and os.path.isdir(directory) and directory not in sys.path:
                sys.path.append(directory)


def key_root(roots=()):
    """The first root that actually holds the key file, else None — in which
    case the client falls back to its own repository root."""
    for root in roots:
        if root and os.path.isfile(os.path.join(root, KEY_FILE)):
            return root
    return None


def ask(state, questions, search_dirs=(), key_roots=(), key=None, opener=None,
        model=MODEL, timeout=TIMEOUT, client_name=CLIENT):
    """Evaluate `questions` against `state`. Returns (values, reason, elapsed_ms).

    `values` maps each question id to a float and is **None whenever any answer
    is unusable** — a partial result is never returned, because a caller that
    acted on three of four answers would be acting on a judgement nobody made.
    Exactly one of `values` and `reason` is ever set.

    This never raises: not when the backlog clone is missing, not when the
    client module cannot be imported, not when there is no key, and not when
    the transport itself does something unexpected.
    """
    started = time.monotonic()

    def elapsed():
        return int(round((time.monotonic() - started) * 1000))

    try:
        if not questions:
            return {}, None, elapsed()
        client, why = load_client(search_dirs, name=client_name)
        if client is None:
            return None, why, elapsed()
        result = client.ask(state, questions, key=key, model=model, timeout=timeout,
                            opener=opener, repo_root=key_root(key_roots))
        if result.error:
            return None, result.error, result.elapsed_ms
        values = {}
        for qid in questions:
            value = client.noul_value(result.answers.get(qid))
            if value is None:
                return None, "the answer for %r was not a noul" % qid, result.elapsed_ms
            values[qid] = value
        return values, None, result.elapsed_ms
    except Exception as exc:  # noqa: BLE001 - the whole point is that callers never crash
        return None, "judgement failed: %s" % (exc,), elapsed()


# --- the deliverability questions -------------------------------------------
#
# Calibrated over the 17 items of the first factory batch, whose outcomes are
# known: docs/raw/2026-09-20-gascity-deliverability-noul-calibration-17-items.md.
# One of the four separates. `prod` scored >= 0.51 on 4 of 4 production-gated
# items and <= 0.11 on 8 of 11 delivered ones; `scope`, `decision` and `big` do
# not separate as framed and are asked ONLY so that a labelled corpus builds
# itself on every admission. Nothing reads them yet, and n = 17 with one framing
# and no held-out set is not a threshold to block with.
#
# Framing changed for ADR-0159 on 2026-10-14. Historical scores from the
# strict-scope frame are not calibration for this frame; all remain advisory.
# They are defined here rather than in a caller because `admit.py` and
# `shape_check.py` must ask the identical question: a corpus assembled from two
# wordings is not one corpus.

FRAME = (
    "`item` is a work order for an unattended worker in one repository. "
    "Its write_scope declares intent; necessary unprotected companion edits "
    "are allowed under ADR-0159, but protected changes need their existing authority. "
    "The worker can run local builds and "
    "tests. It can never read or change production, deploy, spend money, wait "
    "for live traffic, or ask anyone a question. Judge what THIS item requires "
    "to meet its own acceptance. Background, history, and anything the item "
    "leaves to a person under Handoff do not count."
)


def _noul(judge, true, false):
    """One question, in the shape `typesafe_client.noul` builds.

    Written out rather than built through the client so the question set can be
    read, diffed and tested without the transport being importable. That the
    two shapes agree is pinned by `gascity/tests/test_judge.py`.
    """
    return {"type": "noul",
            "instructions": {"judge": judge, "frame": FRAME},
            "criteria": {"true": true, "false": false}}


DELIVERABILITY = {
    "prod": _noul(
        "Does meeting this item's acceptance require evidence from, or a change "
        "to, a live production system?",
        "Acceptance cannot be reached without reading production state, "
        "changing a production system, or waiting for something only production "
        "produces.",
        "Acceptance can be reached from the repository alone: local builds, "
        "local tests, fixtures and evidence already written down."),
    "scope": _noul(
        "Does meeting acceptance require another repository or a protected change "
        "for which this packet lacks authority?",
        "Acceptance needs another repository or an unauthorized protected change.",
        "The change stays in one repository and needs only declared work or "
        "necessary unprotected companions, with authority for protected edits."),
    "decision": _noul(
        "Does this item leave open a decision that belongs to an owner rather "
        "than to the worker delivering it?",
        "A choice the item does not settle has to be made before the work can "
        "be finished, and it is not the worker's to make.",
        "Every choice left open is one a competent worker may make from the "
        "item, the code and the repository's own rules."),
    "big": _noul(
        "Is this more than one focused pull request of about an hour?",
        "Delivering the whole acceptance is substantially more than an hour of "
        "focused work, or is naturally several separate changes.",
        "The whole acceptance is one focused change of about an hour."),
}


def item_state(title, write_scope, body, cap=BODY_CAP):
    """The state the deliverability questions are asked over.

    Every field is data. The item's own text never becomes part of an
    instruction, so nothing written in a backlog item can restate the question.
    """
    return {"item": {
        "title": str(title or ""),
        "write_scope": [str(pattern) for pattern in (write_scope or [])],
        "body": (body or "")[:cap],
    }}
