#!/usr/bin/env python3
"""Resolve a live-test spend cap to a production fence, or refuse (ADR-0151 cl. 3).

A cap the tester merely respects is not a cap, because the tester is the thing
that might be wrong. A `spend` packet's declared cap must therefore name a fence
that production already enforces and that the spending actor cannot widen, and
this module is the one place that knows which fences those are. It knows the two
that exist today, cited by name from `docs/knowledge/liskov-supply-and-funds-flow.md`
and ADR-0151 clause 3, and refuses everything else by default:

  LISKOV_TREASURY_ORG_EXPOSURE_ENFORCED    organization and application exposure
                                           caps: 50 ACU per application, 200 ACU
                                           aggregate per organization
  LISKOV_PLATFORM_COVERAGE_ARMED_BUDGET    the platform-coverage budget a cycle is
                                           armed under, enforced in the database at
                                           hold time; no process reads this name
                                           from its environment

Recognising a fence is not authorizing a spend. Observe V1 implements no spend
recipe, so preflight refuses `spend` as not implemented AFTER this module has
had its say: an unknown or missing fence is reported by name first, because that
refusal is the one a future spend packet will need to read. Nothing here reads a
production value to prove the fence is armed; that proof is spend-tier work, and
until it exists no resolution here is sufficient on its own. A policy
`daily_spend_cap` is not a production fence and is not recognised.

A cap is written `<amount> via <FENCE>`, the shape `livetest_contract.SPEND_CAP_RE`
checks on the record. Standard library only; pure.

  livetest_fence.py '<amount> via <FENCE>'   exit 0 and describe the fence, or
                                             exit 2 and print the refusal
  livetest_fence.py --list                   the recognised fences
"""
import os
import re
import sys
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import livetest_contract as lt  # noqa: E402  the one parser for the record shape

# The fences production enforces today. Adding one here is a claim about
# production, so it belongs with the evidence that the fence exists and cannot
# be widened by the spender — never with a packet that wants to spend.
FENCES = {
    "LISKOV_TREASURY_ORG_EXPOSURE_ENFORCED": {
        "name": "organization and application exposure caps",
        "enforces": "50 ACU per application and 200 ACU aggregate per organization, "
                    "enforced in production when LISKOV_TREASURY_ORG_EXPOSURE_ENFORCED = \"true\"; "
                    "treasury inventory does not raise them",
        "unit": "ACU",
        # The tightest bound the fence guarantees to one packet's target: the
        # per-application cap. A declared cap above it is not bounded by it.
        "max_amount": Decimal("50"),
    },
    "LISKOV_PLATFORM_COVERAGE_ARMED_BUDGET": {
        "name": "platform-coverage armed budget",
        "enforces": "every coverage hold must fit the network's active budget revision "
                    "(liskov_platform_coverage_budget_current), which the spending cycle is armed "
                    "under, and the cycle's frozen escrow ceilings, both checked in one "
                    "transaction at hold time (ADR-0182 sections 2 and 5); this names a database "
                    "fence, not an environment variable, so there is nothing to set",
        "unit": "ACU",
        # The bound is the active budget revision the cycle is armed under, which
        # lives in production and is not known here; no amount is accepted or
        # refused on this side.
        "max_amount": None,
    },
}

AMOUNT_RE = re.compile(r"^[0-9]{1,9}(\.[0-9]{1,6})?$")


class FenceRefused(Exception):
    """A typed refusal: `code` for a caller to branch on, `message` for a person."""

    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__("%s: %s" % (code, message))


def recognised():
    """The fence names this module resolves, sorted."""
    return sorted(FENCES)


def resolve(spend_cap):
    """The fence a declared cap resolves to, as a dict, or `FenceRefused`.

    Refusal codes, each with its own message:
      no_cap          nothing was declared
      no_fence_named  the cap is not `<amount> via <FENCE>` (a bare amount is a
                      cap the tester merely respects)
      unknown_fence   the named fence is not one production is known to enforce
      bad_amount      the amount is not a positive decimal
      over_fence      the amount is larger than the fence itself bounds
    """
    text = (spend_cap or "").strip()
    known = ", ".join(recognised())
    if not text:
        raise FenceRefused("no_cap", "no spend_cap is declared; a spend packet must name the "
                           "production fence that bounds it. Looked for '<amount> via <FENCE>' "
                           "naming one of: %s" % known)
    match = lt.SPEND_CAP_RE.match(text)
    if not match:
        raise FenceRefused("no_fence_named", "spend_cap %r names no production fence; a cap the "
                           "tester merely respects is not a cap. Looked for '<amount> via <FENCE>' "
                           "naming one of: %s" % (text, known))
    amount_text, fence = match.group("amount"), match.group("fence")
    if fence not in FENCES:
        raise FenceRefused("unknown_fence", "spend_cap names fence %s, which is not a fence "
                           "production is known to enforce. Looked for %s among the recognised "
                           "fences: %s. No fence, no spend authorization" % (fence, fence, known))
    record = FENCES[fence]
    try:
        amount = Decimal(amount_text) if AMOUNT_RE.match(amount_text) else None
    except InvalidOperation:
        amount = None
    if amount is None or amount <= 0:
        raise FenceRefused("bad_amount", "spend_cap amount %r is not a positive %s amount"
                           % (amount_text, record["unit"]))
    if record["max_amount"] is not None and amount > record["max_amount"]:
        raise FenceRefused("over_fence", "spend_cap %s %s is more than %s bounds (%s %s); the "
                           "declared cap would not be enforced by the fence it names"
                           % (amount, record["unit"], fence, record["max_amount"], record["unit"]))
    return {"fence": fence, "amount": str(amount), "unit": record["unit"],
            "name": record["name"], "enforces": record["enforces"]}


def main(argv):
    if argv[:1] == ["--list"]:
        for fence in recognised():
            print("%s  %s: %s" % (fence, FENCES[fence]["name"], FENCES[fence]["enforces"]))
        return 0
    if len(argv) != 1:
        print("usage: livetest_fence.py '<amount> via <FENCE>' | --list", file=sys.stderr)
        return 64
    try:
        found = resolve(argv[0])
    except FenceRefused as why:
        print("livetest-fence: REFUSE [%s] %s" % (why.code, why.message))
        return 2
    print("livetest-fence: %s %s via %s (%s)" % (found["amount"], found["unit"], found["fence"],
                                                 found["name"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
