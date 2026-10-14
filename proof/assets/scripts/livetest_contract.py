"""The live-test record shape — one parser for every reader (ADR-0151).

A live-test item declares the production surface it exercises and how far it is
allowed to go, in place of the code write scope an implementation packet
declares. The rules are here, pure and free of I/O, because more than one
caller asks the same question about the same fields:

  - `scripts/validate_docs.py`, the gate every record in this repository
    passes, through its `_check_live_test` adapter;
  - `gascity/shape_check.py`, which answers it for a draft;
  - admission and the runtime checks of the observe V1 contract, which arrive
    in later packets (`BKLG-20260920-n0tn`, `BKLG-20260920-3l4m`).

Competing parsers and per-caller defaults are the failure this module exists to
prevent: a field the validator reads one way and admission another is a fence
with a gap in it.

Recognising a record is not authorizing it. `exercise` accepts the whole
ADR-0151 ladder so a future record validates, while `AUTHORIZED_EXERCISES` says
what is actually implemented today — observe, and only observe. Nothing here
reads `exercise` to permit an action.

Bounds are the frozen observe V1 contract
(`docs/knowledge/production-validation-observe-contract.md`).
"""

import re

# The ADR-0151 ladder, recognised in a record. `spend` implies `mutate`,
# `mutate` implies `observe`.
EXERCISE_LEVELS = ("observe", "mutate", "spend")
# What the factory implements today. Observe V1 refuses the rest everywhere an
# exercise is executed or admitted; a record may still declare one.
AUTHORIZED_EXERCISES = ("observe",)

# Observe V1 freezes both, and refuses a record that widens either.
OBSERVE_MAX_AGE_SECONDS = 120
OBSERVE_TIMEOUT_SECONDS = 120

# The registered recipes. The target-to-origin mapping is trusted checked-in
# code, never a URL taken from a record or a bead.
RECIPES = {
    "readyz-build-v1": {
        "target": "liskov-api",
        "exercise": "observe",
        "description": "read-only GET of the public /readyz build and readiness fields",
    },
}

# Registered on a live-test item, in addition to the work-record fields.
LIVE_TEST_KEYS = (
    "live_target",
    "exercise",
    "live_recipe",
    "live_expected_build",
    "live_max_age_seconds",
    "live_timeout_seconds",
)
# Carried only when `exercise: spend` (ADR-0151 clause 3).
SPEND_ONLY_KEYS = ("spend_cap",)
# Every key the shape owns, for a caller registering them all at once.
ALL_KEYS = LIVE_TEST_KEYS + SPEND_ONLY_KEYS

TARGET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
BUILD_RE = re.compile(r"^[0-9a-f]{40}$")
# An amount and the production fence that enforces it: "<amount> via <FENCE>".
# The fence is named, not enumerated here — preflight is what resolves it to a
# fence production already enforces (ADR-0151 clause 3), and refuses when it
# cannot. This only checks that the record names one at all.
SPEND_CAP_RE = re.compile(r"^(?P<amount>\S.*\S) via (?P<fence>[A-Z][A-Z0-9_]*)$")

# A live test's write scope is always and only documentation: its product is
# evidence. A live test that edits code has become an implementation packet.
EVIDENCE_SCOPE_PREFIX = "docs/"
ORCHESTRATOR_REPO = "liskov-agent-orchestrator"


def _text(fm, key):
    """A frontmatter scalar as a stripped string; '' when absent or not scalar."""
    value = fm.get(key)
    if value is None or isinstance(value, (list, dict)):
        return ""
    return str(value).strip()


def _as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def is_live_test(fm):
    """True when this record declares a production surface to exercise.

    `live_target` is the discriminator, not a second `doc_type`: a live test is
    an ordinary backlog record whose scope is a target rather than a tree.
    """
    return bool(_text(fm, "live_target"))


def exercise(fm):
    """The declared exercise level, or '' — what the record says, not what is
    authorized. Ask `is_authorized_exercise` for that."""
    return _text(fm, "exercise")


def is_authorized_exercise(level):
    return level in AUTHORIZED_EXERCISES


def declared_keys(fm):
    """The live-test keys present on this record, in registration order."""
    return tuple(key for key in ALL_KEYS if _text(fm, key))


def record_errors(fm, scopes=()):
    """Every way this record breaks the live-test shape, as messages.

    `scopes` is the parsed write scope as `(repo, glob)` pairs — what
    `docs_model.write_scopes()` returns. An empty list is a live test that
    names no evidence path yet, which is allowed: the scope a live test is
    exempt from is the *code* one.

    Pure: no file, clock, network or environment is read.
    """
    errors = []
    target = _text(fm, "live_target")
    if "live_target" in fm and not target:
        errors.append("live_target must be a non-empty production surface (e.g. liskov-api); "
                      "remove the field or name the target (ADR-0151 clause 2)")
    if not is_live_test(fm):
        for key in ALL_KEYS:
            if _text(fm, key):
                errors.append(f"{key} belongs to a live-test item, which declares live_target; "
                              "add live_target or drop the field (ADR-0151 clause 2)")
        return errors

    if not TARGET_RE.match(target):
        errors.append(f"live_target must name one deployed surface as a lowercase slug, got {target!r}")

    level = exercise(fm)
    if not level:
        errors.append("a live_target item must declare exercise: "
                      f"{' | '.join(EXERCISE_LEVELS)} (ADR-0151 clause 1)")
    elif level not in EXERCISE_LEVELS:
        errors.append(f"exercise must be one of {' | '.join(EXERCISE_LEVELS)}, got {level!r}")

    cap = _text(fm, "spend_cap")
    if level == "spend":
        if not cap:
            errors.append("exercise: spend requires spend_cap naming the amount and the production "
                          "fence that enforces it, e.g. `40000000050 planck via "
                          "LISKOV_PLATFORM_COVERAGE_ARMED_BUDGET` (ADR-0151 clause 3)")
        elif not SPEND_CAP_RE.match(cap):
            errors.append(f"spend_cap must read `<amount> via <FENCE>`, got {cap!r}: a cap the tester "
                          "merely respects is not a cap, so it names the production fence preflight "
                          "resolves (ADR-0151 clause 3)")
    elif cap:
        errors.append(f"spend_cap belongs only to exercise: spend, and this item declares {level or 'no exercise'}")

    recipe = _text(fm, "live_recipe")
    if recipe:
        registered = RECIPES.get(recipe)
        if registered is None:
            errors.append(f"live_recipe {recipe!r} is not a registered recipe "
                          f"({', '.join(sorted(RECIPES)) or 'none'}); a recipe is trusted checked-in code")
        elif registered["target"] != target:
            errors.append(f"live_recipe {recipe!r} runs against {registered['target']}, "
                          f"but live_target is {target!r}")

    build = _text(fm, "live_expected_build")
    if build and not BUILD_RE.match(build):
        errors.append(f"live_expected_build must be a full forty-hex-digit commit, got {build!r}")

    for key, ceiling in (("live_max_age_seconds", OBSERVE_MAX_AGE_SECONDS),
                         ("live_timeout_seconds", OBSERVE_TIMEOUT_SECONDS)):
        raw = _text(fm, key)
        if not raw:
            continue
        seconds = _as_int(raw)
        if seconds is None or seconds <= 0:
            errors.append(f"{key} must be a positive whole number of seconds, got {raw!r}")
        elif seconds > ceiling:
            errors.append(f"{key} is {seconds}; observe V1 fixes it at {ceiling} seconds and refuses a "
                          "record that widens it")

    for repo, glob in scopes:
        path = str(glob).replace("\\", "/").strip("/")
        if repo != ORCHESTRATOR_REPO or not (path + "/").startswith(EVIDENCE_SCOPE_PREFIX):
            entry = path if repo == ORCHESTRATOR_REPO else f"{repo}:{path}"
            errors.append(f"a live_target item writes only evidence, so its write_scope holds "
                          f"{EVIDENCE_SCOPE_PREFIX} paths of this repository; {entry!r} is outside it. "
                          "A live test that edits code is an implementation packet (ADR-0151 clause 2)")
    return errors


def describe(fm):
    """One line about what this record would exercise, for a draft-time reader."""
    if not is_live_test(fm):
        return "not a live test"
    level = exercise(fm) or "no exercise"
    recipe = _text(fm, "live_recipe")
    where = f" via {recipe}" if recipe else ""
    return f"live test against {_text(fm, 'live_target')} at --exercise {level}{where}"
