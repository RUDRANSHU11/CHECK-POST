"""The risk scorer.

Looks at a payment about to take money, decides whether it smells like fraud,
and asks the gateway for permission to block it or send it to an analyst. Like
the recovery agent, it never acts — it proposes.

Thin on purpose
---------------
The scoring model is not in this file. It lives in ``engine/risk_model.py``
because the gateway recomputes the score before pricing anything, and a model
the agent could keep to itself would be a model the layer cannot check. What is
left here is the part that is genuinely the agent's job: choosing, given a
score, whether this is a block, a review, or nothing at all.

That leaves the agent looking almost trivial, and that is the finding rather
than a shortcut. Once the score is computed somewhere auditable and the
thresholds are stated out loud, an autonomous fraud blocker has very little left
in it — and the reason companies do not ship one is not the model, it is that
nobody wants to be the person who let software turn customers away unsupervised.
Which is the layer underneath, not the agent.

What the agent is *not* allowed to know
---------------------------------------
``ground_truth.json`` knows which payments are fraudulent. Nothing in this
module can open it, and the score is computed only from attempts at or before
the payment being scored — a scorer allowed to see the rest of the month would
be reading the future.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from engine import risk_model
from engine.gateway import Gateway
from engine.schema import (
    ActionRequest,
    ActionType,
    AgentName,
    Decision,
    Payment,
)
from engine.store import DataStore

#: Block outright at or above this. Chosen where the model's precision on the
#: synthetic month is ~0.97: at this level almost everything stopped is really
#: fraud, and the handful of genuine customers caught are the cost that gets
#: printed on the scorecard rather than hidden in it.
BLOCK_THRESHOLD = 0.70

#: Between here and the block line, a person looks. This band is where the
#: model is right about two times in three — good enough to be worth an
#: analyst's fifteen rupees, nowhere near good enough to turn a customer away.
FLAG_THRESHOLD = 0.45


class Scorer(Protocol):
    """Turns a payment and its history into a score and its supporting signals."""

    name: str

    def score(
        self, payment: Payment, store: DataStore
    ) -> risk_model.RiskScore: ...


class SignalScorer:
    """The deterministic scorer. Delegates to the shared model in engine/."""

    name = "signals"

    def score(self, payment: Payment, store: DataStore) -> risk_model.RiskScore:
        return risk_model.score(
            payment,
            store.customer(payment.customer_id),
            store.payments_by_customer(payment.customer_id),
        )


@dataclass
class RiskAgent:
    store: DataStore
    gateway: Gateway
    scorer: Scorer = field(default_factory=SignalScorer)
    #: Overridable so a run can be repeated at a different appetite. Moving this
    #: line is the whole fraud policy of most companies, made once in a meeting
    #: and never priced. ``harness.replay --block-threshold`` prices it.
    block_threshold: float = BLOCK_THRESHOLD
    flag_threshold: float = FLAG_THRESHOLD
    #: Payments already judged, so a replay that revisits a day does not
    #: double-count. The gateway would refuse the duplicate anyway; this just
    #: avoids spending a request to be told so.
    seen: set[str] = field(default_factory=set)

    def work(
        self, payment: Payment, now: datetime
    ) -> tuple[ActionRequest, Decision] | None:
        if payment.payment_id in self.seen:
            return None
        self.seen.add(payment.payment_id)

        assessment = self.scorer.score(payment, self.store)
        if assessment.score >= self.block_threshold:
            action = ActionType.BLOCK_ORDER
            verb = "blocking"
        elif assessment.score >= self.flag_threshold:
            action = ActionType.FLAG_FOR_REVIEW
            verb = "sending for review"
        else:
            return None  # nothing to say; most payments are fine

        request = ActionRequest(
            request_id="rq_" + uuid.uuid4().hex[:12],
            agent=AgentName.RISK,
            action=action,
            customer_id=payment.customer_id,
            invoice_id=payment.invoice_id,
            payment_id=payment.payment_id,
            #: The value at stake. Not a cost the merchant pays to act — a cost
            #: it pays when the action is wrong, which is priced separately.
            amount_paise=payment.amount_paise,
            rationale=(
                f"{verb} {payment.payment_id}: risk {assessment.score:.2f} — "
                f"{assessment.explain()}"
            ),
            # The claim, offered as evidence rather than as fact. The gateway
            # recomputes the score and refuses the request if this ran ahead of
            # what the merchant's own records support.
            evidence={
                "claimed_score": assessment.score,
                "signals": assessment.names,
                "model": risk_model.RISK_MODEL_VERSION,
            },
            idempotency_key=f"{payment.payment_id}:{action.value}",
        )

        return request, self.gateway.submit(request, now=now)

    def run(
        self, payments: list[Payment], now: datetime
    ) -> list[tuple[ActionRequest, Decision]]:
        out = []
        for payment in payments:
            attempt = self.work(payment, now)
            if attempt is not None:
                out.append(attempt)
        return out
