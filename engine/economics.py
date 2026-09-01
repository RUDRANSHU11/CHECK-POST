"""The money check.

The rulebook answers "are we allowed to?". This answers "is it worth it?" — and
it can refuse an action the rulebook was perfectly happy with. Spending ₹50 of
human attention to chase a ₹40 invoice is not a policy violation. It is just
stupid, and nobody builds the layer that says so.

How the estimate is made
------------------------
    expected recovery = P(this action gets us paid) × what is still owed
    verdict           = allow if expected recovery > cost of the attempt

``P`` comes from a small explicit model: a base response rate for the channel,
multiplied by modifiers for how the payment failed, how many times we have
already tried, and how stale the invoice is. Every number is a named constant in
this file.

Two things this model is deliberately *not*:

* **It is not learned.** A fitted model would be better and is the obvious next
  step, but it would also be unauditable in a five-minute demo. Every number
  here can be pointed at and argued with, which is worth more right now than
  being right to three decimal places.
* **It does not read ground truth.** ``data/ground_truth.json`` knows exactly
  which invoices would have paid. Using it here would make the economics gate
  clairvoyant and every number downstream a lie. The model sees only what a real
  integration would see.

The gate is a separate step rather than a fourteenth rule in policy.py, because
it needs inputs no other rule needs and returns a *quantity* — the expected
value — that the ledger records alongside the verdict. It still emits an
ordinary ``RuleResult`` so a denial from here reads identically to a denial from
the rulebook in the audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from engine.policy import PolicyContext
from engine.schema import (
    ACTION_COST_PAISE,
    CONTACT_ACTIONS,
    RISK_ACTIONS,
    ActionRequest,
    ActionType,
    FailureReason,
    RuleResult,
    Verdict,
    fmt,
)

ECONOMICS_VERSION = "1.1"

# --------------------------------------------------------------------------- #
# The model — every number is here, and every number is arguable.
# --------------------------------------------------------------------------- #

#: Probability that a single message on this channel results in payment, for a
#: fresh overdue invoice on a customer we have not chased yet. Loosely shaped on
#: published dunning benchmarks: the more intrusive the channel, the better it
#: works, which is exactly why the cost side has to be weighed against it.
CHANNEL_BASE_RATE: dict[ActionType, float] = {
    ActionType.SEND_EMAIL: 0.06,
    ActionType.SEND_SMS: 0.11,
    ActionType.SEND_WHATSAPP: 0.18,
    ActionType.PLACE_CALL: 0.28,
}

#: Probability a retry succeeds, given why the first attempt failed. Hard
#: declines are absent because r08 in the rulebook refuses them outright — this
#: table only ever sees failures worth retrying.
RETRY_SUCCESS_RATE: dict[FailureReason, float] = {
    FailureReason.NETWORK_TIMEOUT: 0.62,
    FailureReason.BANK_DOWN: 0.55,
    FailureReason.INCORRECT_OTP: 0.40,
    FailureReason.INSUFFICIENT_FUNDS: 0.22,
}

#: A discount converts noticeably better than a plain reminder — that is what we
#: are buying with the margin.
DISCOUNT_BASE_RATE = 0.42

#: A human working the account beats any automated channel. It also costs ₹50,
#: which is the whole point of making the comparison.
ESCALATION_SUCCESS_RATE = 0.50

#: Each prior attempt multiplies the odds of the next one. Diminishing returns
#: is the mechanism that makes chasing eventually uneconomic rather than merely
#: capped by r07.
ATTEMPT_DECAY = 0.55

#: Staleness. An invoice nobody has paid in two months is a different animal
#: from one that went overdue on Tuesday.
AGE_MODIFIERS: tuple[tuple[int, float], ...] = (
    (30, 1.00),
    (60, 0.70),
    (90, 0.45),
    (10_000, 0.25),
)

#: Require the expected return to clear the cost by this much before spending.
#: 1.0 would approve a coin-flip that breaks exactly even; the margin buys room
#: for the model being wrong, which it will be.
MARGIN = 1.25

#: Total spent chasing one invoice may never exceed this fraction of it, however
#: good each individual attempt looked. Guards the slow bleed where twenty
#: individually-justified nudges add up to more than the debt.
MAX_SPEND_FRACTION = 0.20

# --------------------------------------------------------------------------- #
# The risk side of the ledger
# --------------------------------------------------------------------------- #
# A block is not free just because it costs no postage. Turning away a genuine
# customer costs the sale and, usually, the customer. That number never appears
# on an invoice, which is exactly why a fraud system left to its own devices
# blocks too much: every catch is visible and every false positive is somebody
# else's problem. Pricing it here is what stops that drift.

#: What a wrongly blocked sale costs, as a multiple of the sale itself. Above 1.0
#: because the customer does not come back and does tell people. This is the most
#: arguable number in the file, and it should be argued about.
LOST_SALE_MULTIPLIER = 1.6

#: Probability an analyst reaches the right answer on a flagged payment. Review
#: is good, not perfect, and pricing it at 1.0 would make flagging look free.
REVIEW_CATCH_RATE = 0.80

#: Actions with no economic dimension the gate can model. Refunds and write-offs
#: are settled by the rulebook's ceilings, and matching a settlement moves no
#: money on its own.
NO_OPINION: frozenset[ActionType] = frozenset(
    {
        ActionType.MATCH_SETTLEMENT,
        ActionType.WRITE_OFF,
        ActionType.ISSUE_REFUND,
    }
)


@dataclass
class Assessment:
    """What the gate concluded, and the arithmetic that got it there."""

    applicable: bool
    probability: float = 0.0
    outstanding_paise: int = 0
    expected_recovery_paise: int = 0
    cost_paise: int = 0
    verdict: Verdict = Verdict.ALLOW
    reason: str = ""

    def to_rule_result(self) -> RuleResult:
        return RuleResult(rule_id="economics", verdict=self.verdict, reason=self.reason)


# --------------------------------------------------------------------------- #
# Probability
# --------------------------------------------------------------------------- #


def _age_modifier(invoice_due: datetime, now: datetime) -> float:
    days = max(0, (now - invoice_due).days)
    for limit, modifier in AGE_MODIFIERS:
        if days <= limit:
            return modifier
    return AGE_MODIFIERS[-1][1]


def base_rate(req: ActionRequest, ctx: PolicyContext) -> float:
    """Probability before decay, from the action and how the payment failed."""
    if req.action in CHANNEL_BASE_RATE:
        return CHANNEL_BASE_RATE[req.action]
    if req.action is ActionType.RETRY_CHARGE:
        reason = ctx.payment.failure_reason if ctx.payment else None
        # An unknown or hard decline gets the floor, not a generous default. The
        # rulebook usually blocks these first; if one reaches here, guess low.
        return RETRY_SUCCESS_RATE.get(reason, 0.05)
    if req.action is ActionType.OFFER_DISCOUNT:
        return DISCOUNT_BASE_RATE
    if req.action is ActionType.ESCALATE_TO_HUMAN:
        return ESCALATION_SUCCESS_RATE
    return 0.0


def probability(req: ActionRequest, ctx: PolicyContext) -> float:
    """P(this action gets us paid), after decay and staleness."""
    p = base_rate(req, ctx)
    if p <= 0:
        return 0.0
    p *= ATTEMPT_DECAY**ctx.attempts_on_invoice
    if ctx.invoice is not None:
        p *= _age_modifier(ctx.invoice.due_at, ctx.now)
    return min(p, 1.0)


def cost_of(req: ActionRequest) -> int:
    """What this attempt costs the merchant, in paise.

    A discount is priced at the margin given away, not at zero — it is the most
    expensive "free" action in the catalogue and the easiest one for an agent to
    reach for.
    """
    if req.action is ActionType.OFFER_DISCOUNT:
        return req.amount_paise
    return ACTION_COST_PAISE[req.action]


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


def assess_risk(req: ActionRequest, ctx: PolicyContext) -> Assessment:
    """Is blocking or reviewing this payment worth what it costs to be wrong?

    The probability used here is ``ctx.risk_score`` — the number the *gateway*
    recomputed from the merchant's records. The agent's own estimate is on
    ``req.evidence`` and is deliberately not read: an action priced from the
    proposer's own confidence is not gated at all.

    ``expected_recovery_paise`` carries the expected loss prevented, and
    ``cost_paise`` the expected cost of being wrong. Same two columns as the
    recovery side, so a denial from here reads the same way in the ledger.
    """
    payment = ctx.payment
    if payment is None:
        return Assessment(applicable=False)

    p = ctx.risk_score if ctx.risk_score is not None else 0.0
    value = payment.amount_paise

    if req.action is ActionType.BLOCK_ORDER:
        prevented = int(p * value)
        lost_sale = int((1.0 - p) * value * LOST_SALE_MULTIPLIER)
        a = Assessment(
            applicable=True,
            probability=p,
            outstanding_paise=value,
            expected_recovery_paise=prevented,
            cost_paise=lost_sale,
        )
        if prevented <= lost_sale:
            a.verdict = Verdict.DENY
            a.reason = (
                f"not worth blocking: {p:.0%} risk on {fmt(value)} is {fmt(prevented)} "
                f"of expected fraud prevented, against {fmt(lost_sale)} of expected "
                f"lost sale from turning away a probably-genuine customer"
            )
            return a
        a.reason = (
            f"worth blocking: {p:.0%} risk on {fmt(value)} is {fmt(prevented)} of "
            f"expected fraud prevented against {fmt(lost_sale)} of expected lost sale"
        )
        return a

    # flag for review
    caught = int(p * value * REVIEW_CATCH_RATE)
    cost = ACTION_COST_PAISE[ActionType.FLAG_FOR_REVIEW]
    a = Assessment(
        applicable=True,
        probability=p,
        outstanding_paise=value,
        expected_recovery_paise=caught,
        cost_paise=cost,
    )
    if caught < cost * MARGIN:
        a.verdict = Verdict.DENY
        a.reason = (
            f"not worth reviewing: {fmt(cost)} of analyst time to examine "
            f"{fmt(value)} at {p:.0%} risk, worth {fmt(caught)} expected"
        )
        return a
    a.reason = (
        f"worth reviewing: {fmt(caught)} expected ({p:.0%} of {fmt(value)}, "
        f"caught {REVIEW_CATCH_RATE:.0%} of the time) against {fmt(cost)} of analyst time"
    )
    return a


def assess(req: ActionRequest, ctx: PolicyContext) -> Assessment:
    if req.action in RISK_ACTIONS:
        return assess_risk(req, ctx)
    if req.action in NO_OPINION or ctx.invoice is None:
        return Assessment(applicable=False)

    outstanding = ctx.invoice.amount_paise
    p = probability(req, ctx)
    cost = cost_of(req)

    # A discount only recovers what is left after the discount.
    recoverable = outstanding - req.amount_paise if req.action is ActionType.OFFER_DISCOUNT else outstanding
    expected = int(p * recoverable)

    a = Assessment(
        applicable=True,
        probability=p,
        outstanding_paise=outstanding,
        expected_recovery_paise=expected,
        cost_paise=cost,
    )

    # Expected value first. It explains *this* attempt, which is the more direct
    # answer whenever both checks would fail — an early version reported the
    # budget cap on a first attempt with nothing yet spent, which read as
    # "already cost 0.00" and buried the real reason.
    if expected < cost * MARGIN:
        a.verdict = Verdict.DENY
        a.reason = (
            f"not worth it: {fmt(cost)} to attempt, expected recovery only "
            f"{fmt(expected)} ({p:.0%} of {fmt(recoverable)})"
        )
        return a

    # The budget cap second. It explains accumulation: each nudge was justifiable
    # on its own and the total still is not.
    budget = int(outstanding * MAX_SPEND_FRACTION)
    if ctx.spent_on_invoice + cost > budget:
        a.verdict = Verdict.DENY
        if ctx.spent_on_invoice:
            a.reason = (
                f"chasing this invoice has already cost {fmt(ctx.spent_on_invoice)}; "
                f"another {fmt(cost)} would exceed the {MAX_SPEND_FRACTION:.0%} budget "
                f"of {fmt(budget)} on a {fmt(outstanding)} invoice"
            )
        else:
            a.reason = (
                f"{fmt(cost)} on its own exceeds the {MAX_SPEND_FRACTION:.0%} budget "
                f"of {fmt(budget)} for a {fmt(outstanding)} invoice"
            )
        return a

    a.reason = (
        f"worth it: expected {fmt(expected)} ({p:.0%} of {fmt(recoverable)}) "
        f"against {fmt(cost)} of cost"
    )
    return a
