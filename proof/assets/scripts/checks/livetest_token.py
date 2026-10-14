#!/usr/bin/env python3
"""Decide whether a Fly token matches a live-test exercise tier (ADR-0152).

Reads `flyctl tokens debug` output (a macaroon's caveats, not its secret) on
stdin and judges it against the tier, the target app and the exercise deadline
the packet was admitted at. This INSPECTS the credential rather than probing it:
a probe cannot tell a read-only token from a narrowly-scoped exec token, because
both refuse an arbitrary command — and a probe that is wrong in the other
direction runs something in production to find out. The caveats say what the
token is, exactly, with no side effects.

Permission is checked in BOTH directions. The credential must cover the actions
the packet will run, and its effective command authority must be *contained*
within what this orchestrator has decided the tier may do against that target. A
token whose allowlist is a bare `liskov_ops` prefix covers the requested read and
is still refused, because it covers everything else as well.

Exit 0 = the token matches the tier. 2 = it does not (the reason is on stdout).
64 = the packet's own arguments are unusable.
"""
import argparse
import json
import sys
import time

# flyctl's default expiry is 175200h — twenty years. A live-test credential that
# outlives the design it belongs to is how an unowned token ends up in a config
# file, which this workspace already has one of.
MAX_LIFETIME_DAYS = 400
# A token about to lapse must refuse at preflight, not mid-exercise: `settle`
# would still run as a teardown, but against a surface it can no longer reach.
# ADR-0152's floor stands underneath the deadline binding below, never instead
# of it — a two-minute exercise still needs a credential worth having.
MIN_REMAINING_MINUTES = 30

# The trusted target descriptor. It is checked-in code, as the observe V1
# contract requires of the target mapping: a packet names a target, never an app
# id, an origin or a permitted command. A target absent from this table cannot be
# live-tested at all.
#
# `app_id` is Fly's immutable numeric id, which is what an Apps caveat carries —
# an app *name* never appears in a macaroon, so a name comparison would check
# nothing. Measured 2026-09-21 against the live fleet and recorded in
# docs/raw/2026-09-21-live-test-credential-tier-measurements.md, the evidence
# ADR-0152 is drawn from.
#
# `permitted_argv` is the containment side of the check: every command the
# credential allows must extend one of these. The catalog is deliberately the
# smallest one the evidence supports; widening it for a real mutate or spend
# recipe is BKLG-20260920-3l4m and BKLG-20260920-n0tn's work, and belongs here in
# trusted code rather than in a packet's text.
TRUSTED_TARGETS = {
    "liskov-api": {
        "app_id": "24628470",
        # Observe V1 fixes the cleanup margin at 120 seconds, on top of the run
        # timeout, so settle can still reach the surface after the exercise.
        "cleanup_margin_seconds": 120,
        # No exercise may declare a deadline beyond this.
        "max_exercise_seconds": 10800,
        "permitted_argv": {
            # The observe tier executes nothing at all: a read-only org token
            # cannot reach a Machine by either route, so any command authority
            # in an observe credential is the wrong credential.
            "observe": [],
            "mutate": [
                ["/usr/local/bin/liskov_ops", "policy-rollback-floor-census"],
            ],
            "spend": [
                ["/usr/local/bin/liskov_ops", "policy-rollback-floor-census"],
                ["/usr/local/bin/liskov_ops", "platform-coverage-funding",
                 "budget-activate"],
            ],
        },
    },
}

# Verbs that ARM the platform: after them it spends asynchronously, and no
# credential can bound the amount (ADR-0152, context). They exist at the spend
# tier and nowhere else. A mutate credential that covers one is refused by name,
# because "a mutate token that can arm" is the failure worth its own message.
ARMING_ARGV = [
    ["/usr/local/bin/liskov_ops", "platform-coverage-funding", "budget-activate"],
]

# Caveat types this judge models. An unmodelled type is refused rather than
# ignored: ignoring it assumes it neither widens nor narrows, and that assumption
# is exactly the optimistic union this check exists to refuse. All six were
# observed in real readonly and machine-exec tokens on 2026-09-21.
MODELLED_CAVEAT_TYPES = {
    "Organization", "ValidityWindow", "IfPresent", "Apps", "Commands", "FeatureSet",
}

# An IfPresent grants its `ifs` when the request matches and falls back to `else`
# otherwise. A fallback of read (or nothing) narrows; anything else hands out
# authority this judge has not accounted for.
NARROWING_ELSE_MASKS = {"", "r"}


class Ambiguous(Exception):
    """The caveats cannot be read as one unambiguous grant."""


def _require(condition, message):
    if not condition:
        raise Ambiguous(message)


def collect(doc, _depth=0):
    """Read every location's caveats into one effective grant, or refuse.

    Where the previous version flattened alternatives into a union — several
    Commands caveats becoming one long allowlist, several Apps caveats merging
    into one dict — this refuses. Two restrictive caveats of the same kind
    intersect, and an intersection this judge cannot compute must not be guessed
    in the permissive direction.
    """
    found = {
        "commands": None, "apps": None, "org_masks": [],
        "not_after": None, "not_before": None,
    }

    def visit(caveats, inside_if):
        for c in caveats or []:
            _require(isinstance(c, dict), "a caveat is not an object")
            ty, body = c.get("type"), c.get("body")
            _require(
                ty in MODELLED_CAVEAT_TYPES,
                "caveat type %r is not modelled by this check; refusing rather than "
                "assuming what it allows" % (ty,))
            if ty == "Commands":
                _require(found["commands"] is None,
                         "more than one Commands caveat: their intersection is not "
                         "modelled, and their union is not what the token allows")
                _require(isinstance(body, list), "a Commands caveat body is not a list")
                allowed = []
                for entry in body:
                    _require(isinstance(entry, dict),
                             "a Commands entry is not an object")
                    args = entry.get("args")
                    _require(isinstance(args, list) and args
                             and all(isinstance(a, str) for a in args),
                             "a Commands entry carries no readable argv")
                    allowed.append(list(args))
                found["commands"] = allowed
            elif ty == "Apps":
                _require(found["apps"] is None,
                         "more than one Apps caveat: their intersection is not "
                         "modelled, and their union widens the token")
                _require(isinstance(body, dict) and isinstance(body.get("apps"), dict),
                         "an Apps caveat body carries no app map")
                found["apps"] = dict(body["apps"])
            elif ty == "Organization":
                _require(isinstance(body, dict), "an Organization caveat body is not an object")
                found["org_masks"].append(body.get("mask"))
            elif ty == "ValidityWindow":
                _require(isinstance(body, dict), "a ValidityWindow body is not an object")
                na, nb = body.get("not_after"), body.get("not_before")
                if na is not None:
                    _require(isinstance(na, int), "not_after is not an instant")
                    found["not_after"] = na if found["not_after"] is None else min(found["not_after"], na)
                if nb is not None:
                    _require(isinstance(nb, int), "not_before is not an instant")
                    found["not_before"] = nb if found["not_before"] is None else max(found["not_before"], nb)
            elif ty == "IfPresent":
                _require(not inside_if,
                         "a nested IfPresent: this judge does not model a conditional "
                         "grant inside another one")
                _require(isinstance(body, dict), "an IfPresent body is not an object")
                _require(body.get("else") in NARROWING_ELSE_MASKS,
                         "an IfPresent falls back to %r, which is not a read-only "
                         "mask; the grant it makes when its condition does not match "
                         "is wider than this check can account for" % (body.get("else"),))
                visit(body.get("ifs"), True)
            # FeatureSet appears beside Apps inside a real machine-exec token's
            # IfPresent. It scopes builder/wg features, not commands or apps, so
            # it is modelled by being recognised and carrying no authority here.

    for location in doc:
        _require(isinstance(location, dict), "a token location is not an object")
        visit(location.get("caveats"), False)
    return found


def covers(allowed, wanted):
    """True when an allow-listed argv is a prefix of the wanted argv."""
    return len(allowed) <= len(wanted) and list(wanted[:len(allowed)]) == list(allowed)


def contained(allowed, trusted_sets):
    """True when an allow-listed argv is no wider than something the tier permits.

    The mirror of covers(): there the credential must reach the packet's command,
    here the credential may not reach past the orchestrator's. A bare
    ["/usr/local/bin/liskov_ops"] extends nothing, because it is shorter than
    every trusted entry — which is the point: it allows every verb.
    """
    return any(covers(t, allowed) for t in trusted_sets)


def render(argv):
    return " ".join(argv)


def safe_problem(problem):
    """Report the violated rule, never input-bearing credential diagnostics."""
    reasons = (
        ("the token is not valid", "credential is not yet valid"),
        ("no ValidityWindow", "credential has no expiry"),
        ("the token expired", "credential has expired"),
        ("the token has", "credential cannot cover the exercise and cleanup window"),
        ("the token is valid", "credential lifetime exceeds the permitted ceiling"),
        ("the token is scoped", "credential app scope does not match the trusted target (wrong app)"),
        ("no Apps caveat", "credential lacks the required single-app restriction"),
        ("an observe-tier", "observe credentials must not authorize execution"),
        ("no Organization", "credential lacks organization scope"),
        ("the narrowest Organization", "observe credentials require read-only organization scope"),
        ("this is a read-only", "execution requires a restricted machine-exec credential"),
        ("a mutate-tier", "execution credential lacks a command allowlist"),
        ("a spend-tier", "execution credential lacks a command allowlist"),
        ("the packet names", "credential does not cover a requested command"),
        ("the token allows", "credential authorizes commands outside the selected tier"),
    )
    for prefix, diagnostic in reasons:
        if problem.startswith(prefix):
            return diagnostic
    return "credential restrictions do not satisfy the trusted policy"


class QuietArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes the rejected value, which may itself be a
        # credential accidentally supplied in the wrong argument position.
        self.exit(2, "REFUSE - invalid credential-check arguments.\n")


def main():
    ap = QuietArgumentParser(prog="proof-credential-check")
    ap.add_argument("--exercise", required=True, choices=["observe", "mutate", "spend"])
    ap.add_argument("--app", required=True,
                    help="the target the packet was admitted against, by name")
    ap.add_argument("--max-exercise-seconds", type=int, required=True,
                    help="the declared maximum duration of the exercise; the "
                         "credential must outlive it plus the cleanup margin")
    ap.add_argument("--verb", action="append", default=[],
                    help="a command the packet will run, as one shell-ish string")
    ap.add_argument("--now", type=int, default=None, help="override the clock, for tests")
    args = ap.parse_args()

    target = TRUSTED_TARGETS.get(args.app)
    if target is None:
        print("REFUSE - no trusted target descriptor for the requested target.")
        print("A live test runs only against a target this orchestrator carries in "
              "checked-in code, because the app id, the cleanup margin and the "
              "permitted commands are not things a packet may supply. "
              "Check the trusted target catalog.")
        return 2
    if args.max_exercise_seconds <= 0:
        print("REFUSE - --max-exercise-seconds must be positive.")
        return 64
    if args.max_exercise_seconds > target["max_exercise_seconds"]:
        print("REFUSE - the declared exercise exceeds the trusted target's duration ceiling.")
        return 2

    raw = sys.stdin.read()
    try:
        doc = json.loads(raw)
    except Exception as exc:
        print("REFUSE - could not read the credential caveats.")
        print("`flyctl tokens debug` must succeed before a live test may start.")
        return 2
    if not isinstance(doc, list):
        print("REFUSE - unexpected token debug shape; expected a list of locations.")
        return 2

    try:
        f = collect(doc)
    except Ambiguous as exc:
        print("REFUSE - the credential caveats are not unambiguous.")
        print("An unreadable restriction is not an absent one. Re-issue the "
              "credential with the flags ADR-0152 names rather than widening this "
              "check to accept it.")
        return 2

    now = args.now if args.now is not None else int(time.time())
    margin = target["cleanup_margin_seconds"]
    permitted = target["permitted_argv"][args.exercise]
    problems = []

    # --- validity window: applies to every tier ------------------------------
    if f["not_before"] is not None and f["not_before"] > now:
        problems.append(
            "the token is not valid for another %d minutes. A credential that has "
            "not begun cannot be used for an exercise starting now."
            % ((f["not_before"] - now + 59) // 60))

    required = max(MIN_REMAINING_MINUTES * 60, args.max_exercise_seconds + margin)
    if f["not_after"] is None:
        problems.append(
            "no ValidityWindow: the token never expires. flyctl's default expiry is "
            "twenty years; re-issue it with an explicit -x.")
    else:
        remaining = f["not_after"] - now
        if remaining <= 0:
            problems.append("the token expired %d minutes ago." % (-remaining // 60))
        elif remaining < required:
            problems.append(
                "the token has %d minutes left but the exercise needs %d: a declared "
                "%d-second run plus a %d-second cleanup margin, under a %d-minute "
                "floor. A credential that lapses mid-exercise leaves settle unable to "
                "reach the surface."
                % (remaining // 60, (required + 59) // 60, args.max_exercise_seconds,
                   margin, MIN_REMAINING_MINUTES))
        elif remaining > MAX_LIFETIME_DAYS * 86400:
            problems.append(
                "the token is valid for %d days, over the %d-day ceiling. This is flyctl's "
                "twenty-year default; re-issue it with an explicit -x."
                % (remaining // 86400, MAX_LIFETIME_DAYS))

    # --- the target app ------------------------------------------------------
    # An Apps caveat carries immutable ids. A single-app token is not the right
    # token merely by being single: it must be scoped to THIS target.
    if f["apps"] is not None:
        scoped = sorted(f["apps"])
        if len(scoped) != 1:
            problems.append(
                "the token is scoped to %d apps; a live test targets one." % len(scoped))
        elif scoped[0] != target["app_id"]:
            problems.append(
                "the token is scoped to app id %s, but %s is app id %s. A valid token "
                "for the wrong app is still the wrong token."
                % (scoped[0], args.app, target["app_id"]))
    elif args.exercise != "observe":
        problems.append(
            "no Apps caveat: a %s-tier token must be scoped to one app with -a."
            % args.exercise)

    # --- command authority, both directions ----------------------------------
    allowed = f["commands"]
    if args.exercise == "observe":
        if allowed is not None:
            problems.append(
                "an observe-tier token carries a Commands caveat, so it can execute on a "
                "Machine. The observe tier must be a read-only org token, which cannot: "
                "flyctl tokens create readonly -o <org> -n proof-tester-observe -x 720h")
        masks = [m for m in f["org_masks"] if m]
        if not masks:
            problems.append("no Organization caveat: this is not an org-scoped token.")
        elif not any(m == "r" for m in masks):
            problems.append(
                "the narrowest Organization mask is %r, not 'r'. An observe-tier token "
                "must be read-only." % (sorted(masks, key=len)[0],))
    else:
        if allowed is None:
            # Two very different credentials reach here, and an operator needs to
            # be told which one they wired in.
            if f["apps"] is None and any(m == "r" for m in f["org_masks"] if m):
                problems.append(
                    "this is a read-only org token, not a machine-exec token: it cannot "
                    "execute anything, so a %s packet could not run at all. It is the "
                    "observe tier's credential." % args.exercise)
            else:
                problems.append(
                    "a %s-tier token carries NO Commands caveat, so EVERY command is "
                    "allowed. This is flyctl's default when neither -C/--command nor "
                    "-p/--command-prefix is passed, and it produces a token that merely "
                    "looks scoped." % args.exercise)
        else:
            # Direction one: the credential must reach what the packet will run.
            for verb in args.verb:
                wanted = verb.split()
                if not any(covers(a, wanted) for a in allowed):
                    problems.append(
                        "the packet names %r, which no allow-listed command covers. "
                        "Allowed: %s"
                        % (verb, "; ".join(render(a) for a in allowed) or "(none)"))
            # Direction two: and it must reach no further than the tier may.
            for a in allowed:
                if args.exercise == "mutate" and contained(a, ARMING_ARGV):
                    problems.append(
                        "the token allows %r, which arms the platform. Arming belongs to "
                        "the spend tier, where a production fence bounds it; a mutate "
                        "credential must not carry it." % render(a))
                elif not contained(a, permitted):
                    problems.append(
                        "the token allows %r, which is wider than the %s tier may run "
                        "against %s. Permitted: %s"
                        % (render(a), args.exercise, args.app,
                           "; ".join(render(p) for p in permitted) or "(nothing)"))

    if problems:
        print("REFUSE - the credential does not match the packet:")
        for p in problems:
            print("  - " + safe_problem(p))
        return 2

    print("OK - credential matches the requested tier, target and exercise duration.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
