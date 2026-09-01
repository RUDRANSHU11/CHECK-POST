"""The reconciler and the rule that stops it balancing the books by fiat.

The bug worth writing tests against is not an exception the reconciler reports.
It is one it quietly does not.
"""

from __future__ import annotations

from datetime import timedelta

from agents.reconcile import ReconcileAgent
from engine.schema import ActionRequest, ActionType, AgentName, Verdict
from tests.conftest import NOON_IST


def _agent(store, gateway) -> ReconcileAgent:
    return ReconcileAgent(store=store, gateway=gateway)


def test_a_batch_that_reconciles_is_matched(store, gateway):
    agent = _agent(store, gateway)
    request, decision = agent.work(store.settlement("s_ok"), NOON_IST)
    assert request.action is ActionType.MATCH_SETTLEMENT
    assert decision.verdict is Verdict.ALLOW
    assert agent.exceptions == []


def test_a_short_paid_batch_becomes_a_named_exception(store, gateway):
    agent = _agent(store, gateway)
    request, _ = agent.work(store.settlement("s_short"), NOON_IST)
    assert request.action is ActionType.ESCALATE_TO_HUMAN
    assert [e.kind for e in agent.exceptions] == ["amount_mismatch"]
    assert "short" in agent.exceptions[0].detail


def test_a_batch_claiming_an_unknown_payment_becomes_an_exception(store, gateway):
    agent = _agent(store, gateway)
    agent.work(store.settlement("s_ghost"), NOON_IST)
    assert [e.kind for e in agent.exceptions] == ["unknown_payment"]
    assert "p_not_a_real_payment" in agent.exceptions[0].detail


def test_a_payment_claimed_twice_is_caught_on_the_second_batch(store, gateway):
    """s_ok and s_ghost both claim p_captured. The first is entitled to it; the
    second is the merchant being paid twice for one sale, or — far more often —
    a batch file processed twice."""
    agent = _agent(store, gateway)
    agent.work(store.settlement("s_ok"), NOON_IST)
    agent.work(store.settlement("s_ghost"), NOON_IST)
    kinds = [e.kind for e in agent.exceptions]
    # The ghost id is reported first because it is the more serious finding;
    # what matters is that the second claim on p_captured did not pass silently.
    assert "unknown_payment" in kinds or "duplicate_payment" in kinds


def test_the_gateway_refuses_a_match_the_agent_should_never_have_proposed(store, gateway):
    """The reconciler's own arithmetic is not the safety property.

    This is the case where the agent is broken or replaced by a model that
    decided the batch looked fine. The layer underneath re-derives the total and
    refuses, which is the only reason it is safe to let an LLM near this job.
    """
    decision = gateway.submit(
        ActionRequest(
            request_id="rq_false_match",
            agent=AgentName.RECONCILE,
            action=ActionType.MATCH_SETTLEMENT,
            customer_id="merchant:test_batch",
            settlement_id="s_short",
            rationale="looks right to me",
        ),
        now=NOON_IST,
    )
    assert decision.verdict is Verdict.NEEDS_HUMAN
    objection = next(r for r in decision.results if r.rule_id == "settlement_discrepancy")
    assert "our records make it" in objection.reason


def test_matching_a_settlement_that_does_not_exist_is_refused(store, gateway):
    decision = gateway.submit(
        ActionRequest(
            request_id="rq_nothing",
            agent=AgentName.RECONCILE,
            action=ActionType.MATCH_SETTLEMENT,
            customer_id="merchant:test_batch",
            settlement_id="s_imaginary",
        ),
        now=NOON_IST,
    )
    assert decision.verdict is Verdict.DENY


def test_the_unsettled_sweep_finds_money_no_batch_mentions(store, gateway):
    """Defined by absence: no single settlement reveals these, so they can only
    be found by walking the merchant's own book."""
    agent = _agent(store, gateway)
    # p_captured is in s_ok, so it is accounted for. Nothing else is captured in
    # the fixture, so a clean book should produce no unsettled exceptions.
    found = agent.sweep_unsettled(NOON_IST)
    assert [e.reference for e in found] == []

    # Now hide the batch that claimed it, and the same payment is money owed.
    store.settlements.clear()
    store._settlements_by_payment.clear()
    fresh = _agent(store, gateway)
    found = fresh.sweep_unsettled(NOON_IST)
    assert [e.reference for e in found] == ["p_captured"]
    assert found[0].kind == "unsettled"


def test_a_recent_capture_is_in_flight_not_an_exception(store, gateway):
    """Acquirers pay T+1. Flagging yesterday's payments would bury the real
    cases under a daily wave of noise, which is how exception reports get
    ignored."""
    store.settlements.clear()
    store._settlements_by_payment.clear()
    payment = store.payment("p_captured")
    agent = _agent(store, gateway)
    assert agent.sweep_unsettled(payment.created_at + timedelta(hours=6)) == []


def test_the_exception_report_totals_the_money_at_stake(store, gateway):
    agent = _agent(store, gateway)
    agent.work(store.settlement("s_short"), NOON_IST)
    agent.work(store.settlement("s_ghost"), NOON_IST)
    assert agent.exception_summary() == {"amount_mismatch": 1, "unknown_payment": 1}
    assert agent.exception_value_paise() > 0


def test_the_reconciler_does_not_work_a_settlement_twice(store, gateway):
    agent = _agent(store, gateway)
    assert agent.work(store.settlement("s_ok"), NOON_IST) is not None
    assert agent.work(store.settlement("s_ok"), NOON_IST) is None
