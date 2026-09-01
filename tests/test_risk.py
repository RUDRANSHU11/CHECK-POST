"""The risk agent, and the two rules plus the economics that gate it.

The theme running through this file: the agent proposes, and nothing it says
about itself is taken as evidence. Several of these tests are written from the
point of view of an agent that has been compromised or has simply gone wrong,
because that is the case the layer exists for.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from agents.risk import RiskAgent
from engine import economics
from engine.gateway import Gateway
from engine.ledger import Ledger
from engine.schema import (
    ActionRequest,
    ActionType,
    AgentName,
    Customer,
    FailureReason,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Verdict,
    rupees,
)
from engine.store import DataStore
from tests.conftest import NOON_IST


def build_store() -> DataStore:
    """A fraud ring and an ordinary customer, side by side."""

    def cust(cid, age_days):
        return Customer(
            customer_id=cid,
            name="Test Person",
            email=f"{cid}@example.com",
            phone="+919800000000",
            created_at=NOON_IST - timedelta(days=age_days),
        ).model_dump(mode="json")

    def inv(iid, cid, amount):
        return Invoice(
            invoice_id=iid,
            customer_id=cid,
            amount_paise=amount,
            issued_at=NOON_IST - timedelta(days=20),
            due_at=NOON_IST - timedelta(days=5),
            status=InvoiceStatus.OVERDUE,
        ).model_dump(mode="json")

    def pay(pid, iid, cid, amount, **over):
        base = dict(
            payment_id=pid,
            invoice_id=iid,
            customer_id=cid,
            amount_paise=amount,
            method=PaymentMethod.UPI,
            status=PaymentStatus.CAPTURED,
            created_at=NOON_IST - timedelta(minutes=1),
            failure_reason=None,
        )
        base.update(over)
        return Payment(**base).model_dump(mode="json")

    payments = [
        # The ring: two-day-old account, four hard declines, then a large capture.
        pay("p_fraud", "i_fraud", "c_ring", rupees(9_000), method=PaymentMethod.CARD),
        # An ordinary customer with an unremarkable payment.
        pay("p_ok", "i_ok", "c_ok", rupees(1_200)),
        # Small enough that an analyst's time is worth more than the exposure:
        # 15.00 of review at a 1.25 margin needs ~23.44 of exposure to justify
        # itself, even when the payment is certainly fraudulent.
        pay("p_trivial", "i_trivial", "c_ring", rupees(20), method=PaymentMethod.CARD),
    ]
    for n in range(4):
        payments.append(
            pay(
                f"p_dec_{n}",
                "i_fraud",
                "c_ring",
                rupees(9_000),
                method=PaymentMethod.CARD,
                status=PaymentStatus.FAILED,
                failure_reason=FailureReason.DO_NOT_HONOUR,
                created_at=NOON_IST - timedelta(minutes=30 - n * 5),
            )
        )

    return DataStore(
        {
            "batch_id": "risk_test",
            "seed": 0,
            "customers": [cust("c_ring", 2), cust("c_ok", 500)],
            "invoices": [
                inv("i_fraud", "c_ring", rupees(9_000)),
                inv("i_ok", "c_ok", rupees(1_200)),
                inv("i_trivial", "c_ring", rupees(20)),
            ],
            "payments": payments,
        }
    )


@pytest.fixture
def risk_store() -> DataStore:
    return build_store()


@pytest.fixture
def risk_gateway(risk_store, tmp_path) -> Gateway:
    lg = Ledger(tmp_path / "risk.db")
    yield Gateway(risk_store, lg)
    lg.close()


def submit(gateway, **kw):
    kw.setdefault("request_id", "rq_test")
    kw.setdefault("agent", AgentName.RISK)
    return gateway.submit(ActionRequest(**kw), now=NOON_IST)


# -- the agent -------------------------------------------------------------- #


def test_the_agent_blocks_the_ring_and_leaves_the_ordinary_customer_alone(
    risk_store, risk_gateway
):
    agent = RiskAgent(store=risk_store, gateway=risk_gateway)
    results = agent.run(risk_store.captured_payments(), NOON_IST)
    by_payment = {req.payment_id: (req.action, dec.verdict) for req, dec in results}

    assert by_payment["p_fraud"][0] is ActionType.BLOCK_ORDER
    assert by_payment["p_fraud"][1] is Verdict.ALLOW
    assert "p_ok" not in by_payment, "an ordinary payment should draw no action at all"


def test_the_agent_does_not_judge_the_same_payment_twice(risk_store, risk_gateway):
    agent = RiskAgent(store=risk_store, gateway=risk_gateway)
    first = agent.run(risk_store.captured_payments(), NOON_IST)
    second = agent.run(risk_store.captured_payments(), NOON_IST)
    assert first and not second


def test_the_agents_claim_travels_as_evidence_not_as_fact(risk_store, risk_gateway):
    agent = RiskAgent(store=risk_store, gateway=risk_gateway)
    request, decision = next(
        (r, d)
        for r, d in agent.run(risk_store.captured_payments(), NOON_IST)
        if r.payment_id == "p_fraud"
    )
    assert "claimed_score" in request.evidence
    # ...and the gateway recorded its own number next to the verdict.
    assert decision.risk_score is not None
    assert decision.risk_score == pytest.approx(request.evidence["claimed_score"])


def test_a_low_value_flag_is_refused_as_not_worth_an_analyst(risk_store, risk_gateway):
    """20 rupees of exposure is not worth 15 rupees of a person's time,
    however certain the model is that it is fraud."""
    # Above 1.0 so nothing can reach the block path and every scored payment
    # is routed to review, which is the branch under test.
    agent = RiskAgent(store=risk_store, gateway=risk_gateway, block_threshold=1.01)
    results = {r.payment_id: d for r, d in agent.run(risk_store.captured_payments(), NOON_IST)}
    decision = results["p_trivial"]
    assert decision.verdict is Verdict.DENY
    assert any(r.rule_id == "economics" for r in decision.results)


# -- r15: the agent may not assert its way past the evidence ---------------- #


def test_an_inflated_claim_is_refused(risk_gateway):
    decision = submit(
        risk_gateway,
        action=ActionType.FLAG_FOR_REVIEW,
        customer_id="c_ok",
        payment_id="p_ok",
        amount_paise=rupees(1_200),
        evidence={"claimed_score": 0.99},
    )
    assert decision.verdict is Verdict.DENY
    objection = next(r for r in decision.results if r.rule_id == "risk_evidence")
    assert "0.99" in objection.reason and "records support" in objection.reason


def test_a_block_with_no_signals_is_refused(risk_gateway):
    decision = submit(
        risk_gateway,
        action=ActionType.BLOCK_ORDER,
        customer_id="c_ok",
        payment_id="p_ok",
        amount_paise=rupees(1_200),
    )
    assert decision.verdict is Verdict.DENY
    assert any(
        r.rule_id == "risk_evidence" and "no fraud signal" in r.reason
        for r in decision.results
    )


def test_a_risk_action_naming_no_payment_is_refused(risk_gateway):
    decision = submit(
        risk_gateway,
        action=ActionType.BLOCK_ORDER,
        customer_id="c_ok",
        amount_paise=rupees(1_200),
    )
    assert decision.verdict is Verdict.DENY


def test_a_claim_inside_the_tolerance_is_allowed(risk_gateway):
    """Small gaps are model drift, not dishonesty. The rule must not fire on
    every rounding difference or it would be turned off within a week."""
    from engine.policy import RISK_CLAIM_TOLERANCE

    computed = risk_gateway.build_context(
        ActionRequest(
            request_id="rq",
            agent=AgentName.RISK,
            action=ActionType.BLOCK_ORDER,
            customer_id="c_ring",
            payment_id="p_fraud",
        ),
        NOON_IST,
    ).risk_score
    decision = submit(
        risk_gateway,
        action=ActionType.BLOCK_ORDER,
        customer_id="c_ring",
        payment_id="p_fraud",
        amount_paise=rupees(9_000),
        evidence={"claimed_score": computed + RISK_CLAIM_TOLERANCE - 0.01},
    )
    assert decision.verdict is Verdict.ALLOW


# -- r14: the ceiling ------------------------------------------------------- #


def test_a_large_block_needs_a_person(risk_gateway):
    from engine.policy import BLOCK_HUMAN_THRESHOLD_PAISE

    decision = submit(
        risk_gateway,
        action=ActionType.BLOCK_ORDER,
        customer_id="c_ring",
        payment_id="p_fraud",
        amount_paise=BLOCK_HUMAN_THRESHOLD_PAISE + 1,
    )
    assert decision.verdict is Verdict.NEEDS_HUMAN
    assert any(r.rule_id == "block_ceiling" for r in decision.results)


def test_a_signed_off_large_block_goes_through(risk_gateway):
    from engine.policy import BLOCK_HUMAN_THRESHOLD_PAISE

    request = ActionRequest(
        request_id="rq_big",
        agent=AgentName.RISK,
        action=ActionType.BLOCK_ORDER,
        customer_id="c_ring",
        payment_id="p_fraud",
        amount_paise=BLOCK_HUMAN_THRESHOLD_PAISE + 1,
    )
    assert risk_gateway.submit(request, now=NOON_IST).verdict is Verdict.NEEDS_HUMAN

    risk_gateway.approve("rq_big", approver="analyst")
    # The agent resubmits; the rulebook runs again with the signature in hand.
    assert risk_gateway.submit(request, now=NOON_IST).verdict is Verdict.ALLOW


# -- the economics of blocking --------------------------------------------- #


def test_blocking_is_refused_when_the_expected_lost_sale_exceeds_the_fraud():
    """The arithmetic on its own, without a gateway around it."""
    from engine.policy import PolicyContext

    payment = Payment(
        payment_id="p",
        invoice_id="i",
        customer_id="c",
        amount_paise=rupees(10_000),
        method=PaymentMethod.CARD,
        status=PaymentStatus.CAPTURED,
        created_at=NOON_IST,
    )
    request = ActionRequest(
        request_id="rq",
        agent=AgentName.RISK,
        action=ActionType.BLOCK_ORDER,
        customer_id="c",
        payment_id="p",
        amount_paise=rupees(10_000),
    )

    # Half-confident is not confident enough to turn a customer away, because a
    # wrong block costs more than the sale it stops.
    weak = economics.assess_risk(
        request, PolicyContext(now=NOON_IST, payment=payment, risk_score=0.5)
    )
    assert weak.verdict is Verdict.DENY
    assert "lost sale" in weak.reason

    strong = economics.assess_risk(
        request, PolicyContext(now=NOON_IST, payment=payment, risk_score=0.9)
    )
    assert strong.verdict is Verdict.ALLOW


def test_the_block_break_even_follows_from_the_lost_sale_multiplier():
    """Where the line sits is a consequence of one stated constant, not a
    threshold someone typed in. Moving LOST_SALE_MULTIPLIER must move it."""
    from engine.policy import PolicyContext

    payment = Payment(
        payment_id="p",
        invoice_id="i",
        customer_id="c",
        amount_paise=rupees(10_000),
        method=PaymentMethod.CARD,
        status=PaymentStatus.CAPTURED,
        created_at=NOON_IST,
    )
    request = ActionRequest(
        request_id="rq",
        agent=AgentName.RISK,
        action=ActionType.BLOCK_ORDER,
        customer_id="c",
        payment_id="p",
        amount_paise=rupees(10_000),
    )
    m = economics.LOST_SALE_MULTIPLIER
    break_even = m / (1.0 + m)

    just_under = economics.assess_risk(
        request, PolicyContext(now=NOON_IST, payment=payment, risk_score=break_even - 0.02)
    )
    just_over = economics.assess_risk(
        request, PolicyContext(now=NOON_IST, payment=payment, risk_score=break_even + 0.02)
    )
    assert just_under.verdict is Verdict.DENY
    assert just_over.verdict is Verdict.ALLOW
