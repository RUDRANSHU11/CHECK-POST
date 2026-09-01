"""The fraud signals, and how they add up.

This lives in ``engine/`` rather than in ``agents/`` for one reason: **the
gateway does not take the agent's word for how risky something is.**

An agent that could assert its own risk score would be able to justify any block
it liked — and the LLM version of the risk agent is exactly the component most
likely to do that, sincerely, after reading a memo somebody wrote for it. So the
score travels with the request as a *claim*, and the economics gate recomputes
it here from the merchant's own records before pricing anything. If the two
disagree, the gate's number is the one that decides, and the gap is logged.

Both sides importing the same function is deliberate. The property that matters
is not that two models were written, it is that the number the gate acts on was
derived from the merchant's data rather than supplied by the thing being policed.

The model itself
----------------
Additive weights over named binary signals, capped at 1.0. Not a classifier, not
fitted to anything: a fitted model would score better and could not be read
aloud. Every signal below is a sentence a fraud analyst would recognise, and the
weight next to it is arguable in public, which is worth more in a five-minute
demo than being right to three decimal places.

Leakage
-------
``signals_for`` filters history down to attempts at or before the payment being
scored. The store holds the whole month; a scorer that looked at the rest of it
would be reading the future, would score beautifully, and would be worthless.
The filter lives here rather than at the call site so that no caller can leak
the future by forgetting to apply it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from engine.schema import (
    Customer,
    FailureReason,
    Payment,
    PaymentMethod,
    PaymentStatus,
    fmt,
)

RISK_MODEL_VERSION = "1.0"

# --------------------------------------------------------------------------- #
# Thresholds — the whole model's configuration, in one screen.
# --------------------------------------------------------------------------- #

#: An account younger than this is new enough to be worth noticing on its own.
YOUNG_ACCOUNT_DAYS = 30

#: A burst is this many declines inside this window, on the same account.
BURST_DECLINES = 3
BURST_WINDOW = timedelta(hours=1)

#: Sustained hammering, over a longer window than a single burst.
VELOCITY_ATTEMPTS = 5
VELOCITY_WINDOW = timedelta(hours=24)

#: Declines that mean "the bank knows this is wrong", as opposed to "the customer
#: is out of money". Card testing produces the first kind almost exclusively.
HARD_DECLINES: frozenset[FailureReason] = frozenset(
    {
        FailureReason.DO_NOT_HONOUR,
        FailureReason.LIMIT_EXCEEDED,
        FailureReason.INCORRECT_OTP,
    }
)
HARD_DECLINE_COUNT = 2

#: A ticket this many times the customer's own median, and above the floor, is
#: out of character. The floor stops a customer whose history is three 50-rupee
#: payments from being flagged for buying something at 400.
OUTSIZED_MULTIPLE = 4.0
OUTSIZED_FLOOR_PAISE = 200_000  # 2,000.00

#: Weight per signal. They sum, then cap at 1.0. The burst carries the most
#: because it is the one pattern with no innocent explanation at volume.
WEIGHTS: dict[str, float] = {
    "decline_burst": 0.35,
    "young_account": 0.22,
    "hard_declines": 0.20,
    "outsized_ticket": 0.18,
    "velocity": 0.15,
    "card_on_young_account": 0.10,
}


@dataclass(frozen=True)
class Signal:
    name: str
    weight: float
    #: A sentence with the actual numbers in it, for the audit trail.
    detail: str


@dataclass
class RiskScore:
    score: float
    signals: list[Signal] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return [s.name for s in self.signals]

    def explain(self) -> str:
        if not self.signals:
            return "no risk signals present"
        return "; ".join(s.detail for s in self.signals)


# --------------------------------------------------------------------------- #
# The signals
# --------------------------------------------------------------------------- #


def signals_for(
    payment: Payment,
    customer: Customer | None,
    history: list[Payment],
) -> list[Signal]:
    """Every signal that fires on this payment, each carrying its own evidence."""
    prior = [
        p
        for p in history
        if p.created_at <= payment.created_at and p.payment_id != payment.payment_id
    ]
    out: list[Signal] = []

    # 1. A burst of declines in the hour before this one landed.
    window_start = payment.created_at - BURST_WINDOW
    burst = [
        p
        for p in prior
        if p.status is PaymentStatus.FAILED and p.created_at >= window_start
    ]
    if len(burst) >= BURST_DECLINES:
        out.append(
            Signal(
                "decline_burst",
                WEIGHTS["decline_burst"],
                f"{len(burst)} declined attempts in the hour before this one",
            )
        )

    # 2. Account age at the moment of the payment.
    young = False
    if customer is not None:
        age_days = (payment.created_at - customer.created_at).days
        if age_days <= YOUNG_ACCOUNT_DAYS:
            young = True
            out.append(
                Signal(
                    "young_account",
                    WEIGHTS["young_account"],
                    f"account was {age_days} days old at the time of payment",
                )
            )

    # 3. Hard declines specifically — the bank refusing, not the balance.
    hard = [p for p in prior if p.failure_reason in HARD_DECLINES]
    if len(hard) >= HARD_DECLINE_COUNT:
        kinds = sorted({p.failure_reason.value for p in hard if p.failure_reason})
        out.append(
            Signal(
                "hard_declines",
                WEIGHTS["hard_declines"],
                f"{len(hard)} prior hard declines ({', '.join(kinds)})",
            )
        )

    # 4. Out of character for this account.
    amounts = sorted(p.amount_paise for p in prior)
    if amounts:
        median = amounts[len(amounts) // 2]
        if (
            median > 0
            and payment.amount_paise >= OUTSIZED_FLOOR_PAISE
            and payment.amount_paise >= median * OUTSIZED_MULTIPLE
        ):
            out.append(
                Signal(
                    "outsized_ticket",
                    WEIGHTS["outsized_ticket"],
                    f"{fmt(payment.amount_paise)} against a median of {fmt(median)} "
                    f"across {len(prior)} prior attempts",
                )
            )

    # 5. Sustained volume over a day, which an hour-wide burst check misses.
    day_start = payment.created_at - VELOCITY_WINDOW
    day = [p for p in prior if p.created_at >= day_start]
    if len(day) >= VELOCITY_ATTEMPTS:
        out.append(
            Signal(
                "velocity",
                WEIGHTS["velocity"],
                f"{len(day)} attempts on this account in 24 hours",
            )
        )

    # 6. Cards are the instrument of choice on a fresh account; a young UPI
    #    account is mostly just a new customer.
    if young and payment.method is PaymentMethod.CARD:
        out.append(
            Signal(
                "card_on_young_account",
                WEIGHTS["card_on_young_account"],
                "card payment on an account opened this month",
            )
        )

    return out


def score(
    payment: Payment,
    customer: Customer | None,
    history: list[Payment],
) -> RiskScore:
    """P(this payment is fraudulent), as a number the merchant can argue with."""
    signals = signals_for(payment, customer, history)
    total = min(1.0, sum(s.weight for s in signals))
    return RiskScore(score=round(total, 4), signals=signals)
