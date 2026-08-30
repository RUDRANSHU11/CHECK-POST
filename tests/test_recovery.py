"""The recovery agent: what it proposes, and what it hands to the gateway."""

from __future__ import annotations

from datetime import timedelta

import pytest

from agents.recovery import (
    CALL_WORTH_IT_PAISE,
    HIGH_VALUE_PAISE,
    RecoveryAgent,
    RulePlanner,
    build_planner,
)
from engine.schema import ActionType, InvoiceStatus, Verdict
from tests.conftest import NOON_IST


@pytest.fixture
def planner() -> RulePlanner:
    return RulePlanner()


def plan(planner, store, invoice_id: str, tried: int = 0, now=NOON_IST):
    invoice = store.invoice(invoice_id)
    return planner.plan(invoice, store.payments_for(invoice_id), tried, now)


# -- when the agent should do nothing at all -------------------------------- #

@pytest.mark.parametrize("invoice_id", ["i_paid", "i_disputed"])
def test_planner_leaves_settled_and_disputed_invoices_alone(planner, store, invoice_id):
    assert plan(planner, store, invoice_id) is None


def test_planner_leaves_invoices_that_are_not_due_yet(planner, store):
    invoice = store.invoice("i_big")
    invoice.due_at = NOON_IST + timedelta(days=5)
    assert plan(planner, store, "i_big") is None


# -- the ladder ------------------------------------------------------------- #

def test_transient_decline_is_retried_before_anything_is_spent(planner, store):
    # i_small's payment failed on insufficient funds — the cheapest money on the
    # table, because the customer already intended to pay.
    action, amount, rationale = plan(planner, store, "i_small")
    assert action is ActionType.RETRY_CHARGE
    assert "insufficient_funds" in rationale


def test_hard_decline_is_not_retried(planner, store):
    # i_big's card is expired. Retrying is guaranteed to fail, so the planner
    # should go straight to outreach.
    action, _, _ = plan(planner, store, "i_big")
    assert action is not ActionType.RETRY_CHARGE


def test_high_value_invoice_skips_the_email_round(planner, store):
    assert store.invoice("i_big").amount_paise >= HIGH_VALUE_PAISE
    action, _, _ = plan(planner, store, "i_big")
    assert action is ActionType.SEND_SMS


def test_ladder_escalates_with_each_attempt(planner, store):
    seen = [plan(planner, store, "i_big", tried=n)[0] for n in range(4)]
    assert seen[0] is ActionType.SEND_SMS
    assert seen[1] is ActionType.SEND_WHATSAPP
    assert seen[2] is ActionType.PLACE_CALL
    assert seen[3] is ActionType.ESCALATE_TO_HUMAN


def test_small_invoice_never_gets_a_phone_call(planner, store):
    assert store.invoice("i_small").amount_paise < CALL_WORTH_IT_PAISE
    for n in range(6):
        action, _, _ = plan(planner, store, "i_small", tried=n)
        assert action is not ActionType.PLACE_CALL


def test_exhausted_ladder_escalates(planner, store):
    action, _, rationale = plan(planner, store, "i_big", tried=9)
    assert action is ActionType.ESCALATE_TO_HUMAN
    assert "overdue" in rationale


# -- the agent -------------------------------------------------------------- #

def test_agent_submits_and_gets_a_verdict(store, gateway):
    agent = RecoveryAgent(store=store, gateway=gateway)
    result = agent.work(store.invoice("i_big"), NOON_IST)
    assert result is not None
    request, decision = result
    assert request.agent.value == "recovery"
    assert decision.verdict in (Verdict.ALLOW, Verdict.DENY, Verdict.NEEDS_HUMAN)
    assert decision.results


def test_agent_carries_the_memo_so_the_injection_guard_can_see_it(store, gateway):
    # An agent that summarised the memo away would hide the attack from the
    # layer built to catch it.
    agent = RecoveryAgent(store=store, gateway=gateway)
    request, decision = agent.work(store.invoice("i_poisoned"), NOON_IST)
    assert "memo" in request.evidence
    assert decision.verdict is Verdict.DENY
    assert any(r.rule_id == "prompt_injection" for r in decision.results)


def test_agent_counts_a_refusal_as_a_turn_taken(store, gateway):
    agent = RecoveryAgent(store=store, gateway=gateway)
    agent.work(store.invoice("i_stop"), NOON_IST)
    assert agent.tried["i_stop"] == 1


def test_agent_view_may_drift_from_the_gateways(store, gateway):
    # The agent's tally counts attempts; the gateway's counts allowed actions.
    # They are supposed to be able to disagree — that divergence is the thing
    # the checkpoint exists to catch.
    agent = RecoveryAgent(store=store, gateway=gateway)
    agent.work(store.invoice("i_stop"), NOON_IST)  # denied: customer opted out
    assert agent.tried["i_stop"] == 1
    assert gateway._attempts.get("i_stop", 0) == 0


def test_agent_skips_invoices_with_nothing_to_do(store, gateway):
    agent = RecoveryAgent(store=store, gateway=gateway)
    assert agent.work(store.invoice("i_paid"), NOON_IST) is None
    assert len(gateway.ledger) == 0


def test_run_returns_one_attempt_per_actionable_invoice(store, gateway):
    agent = RecoveryAgent(store=store, gateway=gateway)
    attempts = agent.run(list(store.invoices.values()), NOON_IST)
    # i_paid and i_disputed are skipped; the other four are worked.
    assert len(attempts) == 4


def test_idempotency_key_is_stable_for_the_same_attempt(store, gateway):
    agent = RecoveryAgent(store=store, gateway=gateway)
    request, _ = agent.work(store.invoice("i_big"), NOON_IST)
    assert request.idempotency_key.startswith("i_big:0:")


# -- planner selection ------------------------------------------------------ #

def test_no_key_means_the_rule_planner(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert isinstance(build_planner(), RulePlanner)


def test_blank_key_means_the_rule_planner(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    assert isinstance(build_planner(), RulePlanner)
