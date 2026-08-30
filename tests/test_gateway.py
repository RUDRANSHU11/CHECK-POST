"""Gateway behaviour: what gets logged, what moves a counter, and what survives
a restart."""

from __future__ import annotations

from datetime import timedelta

from engine.gateway import Gateway
from engine.ledger import Ledger
from engine.policy import REFUND_HUMAN_THRESHOLD_PAISE
from engine.schema import (
    ActionRequest,
    ActionType,
    AgentName,
    Outcome,
    OutcomeResult,
    Verdict,
    rupees,
)
from tests.conftest import NOON_IST


def rq(n: int, action: ActionType = ActionType.SEND_SMS, **over) -> ActionRequest:
    base = dict(
        request_id=f"rq_{n}",
        agent=AgentName.RECOVERY,
        action=action,
        customer_id="c_ok",
    )
    base.update(over)
    return ActionRequest(**base)


# -- logging --------------------------------------------------------------- #

def test_every_submission_logs_request_then_decision(gateway):
    gateway.submit(rq(1), now=NOON_IST)
    types = [e.entry_type for e in gateway.ledger.entries()]
    assert types == ["request", "decision"]


def test_denied_requests_are_logged_too(gateway):
    d = gateway.submit(rq(1, customer_id="c_stop"), now=NOON_IST)
    assert d.verdict is Verdict.DENY
    # The refusal is the thing worth having on record.
    assert len(gateway.ledger.entries(entry_type="decision")) == 1
    assert gateway.ledger.verify().ok


def test_ledger_stays_verifiable_across_a_batch(gateway):
    for i in range(30):
        gateway.submit(rq(i, invoice_id="i_big"), now=NOON_IST)
    assert gateway.ledger.verify().ok


# -- counters -------------------------------------------------------------- #

def test_contact_cap_enforced_across_successive_calls(gateway):
    assert gateway.submit(rq(1), now=NOON_IST).verdict is Verdict.ALLOW
    assert gateway.submit(rq(2), now=NOON_IST).verdict is Verdict.ALLOW
    third = gateway.submit(rq(3), now=NOON_IST)
    assert third.verdict is Verdict.DENY
    assert any(r.rule_id == "contact_frequency" for r in third.results)


def test_denied_action_does_not_consume_the_quota(gateway):
    # Two denials at night, then two allows in the morning: the night attempts
    # never happened, so they must not have burned the daily allowance.
    night = NOON_IST + timedelta(hours=15)  # 03:00 IST next day
    assert gateway.submit(rq(1), now=night).verdict is Verdict.DENY
    assert gateway.submit(rq(2), now=night).verdict is Verdict.DENY
    later = night + timedelta(hours=8)  # 11:00 IST
    assert gateway.submit(rq(3), now=later).verdict is Verdict.ALLOW
    assert gateway.submit(rq(4), now=later).verdict is Verdict.ALLOW


def test_contact_window_rolls_off_after_24h(gateway):
    gateway.submit(rq(1), now=NOON_IST)
    gateway.submit(rq(2), now=NOON_IST)
    assert gateway.submit(rq(3), now=NOON_IST).verdict is Verdict.DENY
    assert gateway.submit(rq(4), now=NOON_IST + timedelta(hours=25)).verdict is Verdict.ALLOW


def test_idempotency_key_registered_only_on_allow(gateway):
    # A denied request's key must stay reusable, or a bad first attempt would
    # permanently poison a legitimate retry.
    denied = gateway.submit(
        rq(1, customer_id="c_stop", idempotency_key="k1"), now=NOON_IST
    )
    assert denied.verdict is Verdict.DENY
    allowed = gateway.submit(rq(2, idempotency_key="k1"), now=NOON_IST)
    assert allowed.verdict is Verdict.ALLOW
    repeat = gateway.submit(rq(3, idempotency_key="k1"), now=NOON_IST)
    assert repeat.verdict is Verdict.DENY
    assert any(r.rule_id == "duplicate_action" for r in repeat.results)


def test_refunds_accumulate_against_the_payment(gateway):
    first = gateway.submit(
        rq(1, ActionType.ISSUE_REFUND, payment_id="p_nsf", amount_paise=rupees(30)),
        now=NOON_IST,
    )
    assert first.verdict is Verdict.ALLOW
    second = gateway.submit(
        rq(2, ActionType.ISSUE_REFUND, payment_id="p_nsf", amount_paise=rupees(30)),
        now=NOON_IST,
    )
    assert second.verdict is Verdict.DENY
    assert any(r.rule_id == "refund_exceeds_payment" for r in second.results)


def test_escalation_does_not_count_as_a_recovery_attempt(gateway):
    for i in range(4):
        gateway.submit(rq(i, invoice_id="i_big"), now=NOON_IST + timedelta(days=i))
    assert gateway._attempts["i_big"] == 4
    gateway.submit(rq(9, ActionType.ESCALATE_TO_HUMAN, invoice_id="i_big"), now=NOON_IST)
    assert gateway._attempts["i_big"] == 4


# -- human approval -------------------------------------------------------- #

def test_human_approval_flow(gateway):
    big = rq(
        1,
        ActionType.ISSUE_REFUND,
        payment_id="p_expired",
        amount_paise=REFUND_HUMAN_THRESHOLD_PAISE + rupees(1),
    )
    first = gateway.submit(big, now=NOON_IST)
    assert first.verdict is Verdict.NEEDS_HUMAN

    gateway.approve("rq_1", approver="ops@merchant.in", note="verified by phone")

    second = gateway.submit(big, now=NOON_IST)
    assert second.verdict is Verdict.ALLOW
    assert [e.entry_type for e in gateway.ledger.entries()].count("human_approval") == 1


def test_approval_does_not_bypass_a_hard_denial(gateway):
    gateway.approve("rq_1", approver="ops@merchant.in")
    d = gateway.submit(rq(1, customer_id="c_stop"), now=NOON_IST)
    assert d.verdict is Verdict.DENY


# -- restart --------------------------------------------------------------- #

def test_counters_rebuild_from_the_ledger_after_restart(store, tmp_path):
    db = tmp_path / "restart.db"
    gw1 = Gateway(store, Ledger(db))
    gw1.submit(rq(1, idempotency_key="k1"), now=NOON_IST)
    gw1.submit(rq(2, invoice_id="i_big"), now=NOON_IST)
    entries_before = len(gw1.ledger)
    gw1.ledger.close()

    gw2 = Gateway(store, Ledger(db))
    assert len(gw2.ledger) == entries_before
    # The quota, the attempt count and the used key all came back from the log.
    assert gw2._contacts_in_window("c_ok", NOON_IST) == 2
    assert gw2._attempts["i_big"] == 1
    assert "k1" in gw2._used_keys
    assert gw2.submit(rq(3), now=NOON_IST).verdict is Verdict.DENY
    gw2.ledger.close()


def test_restart_preserves_human_approvals(store, tmp_path):
    db = tmp_path / "restart2.db"
    gw1 = Gateway(store, Ledger(db))
    gw1.approve("rq_1", approver="ops@merchant.in")
    gw1.ledger.close()

    gw2 = Gateway(store, Ledger(db))
    assert "rq_1" in gw2._approved_requests
    gw2.ledger.close()


# -- reporting ------------------------------------------------------------- #

def test_stats_and_denial_breakdown(gateway):
    gateway.submit(rq(1), now=NOON_IST)
    gateway.submit(rq(2, customer_id="c_stop"), now=NOON_IST)
    gateway.submit(rq(3, invoice_id="i_disputed"), now=NOON_IST)

    s = gateway.stats()
    assert s["actions_requested"] == 3
    assert s["allowed"] == 1
    assert s["denied"] == 2
    assert s["ledger_entries"] == 6

    breakdown = gateway.denial_breakdown()
    assert breakdown["opt_out"] == 1
    assert breakdown["disputed_invoice"] == 1


def test_spend_only_counts_allowed_actions(gateway):
    from engine.schema import ACTION_COST_PAISE

    gateway.submit(rq(1), now=NOON_IST)
    gateway.submit(rq(2, customer_id="c_stop"), now=NOON_IST)
    assert gateway.stats()["spend_paise"] == ACTION_COST_PAISE[ActionType.SEND_SMS]


def test_outcomes_are_recorded(gateway):
    gateway.submit(rq(1), now=NOON_IST)
    gateway.report_outcome(
        Outcome(
            outcome_id="out_1",
            request_id="rq_1",
            result=OutcomeResult.RECOVERED,
            amount_recovered_paise=rupees(40),
        )
    )
    outcomes = gateway.ledger.entries(entry_type="outcome")
    assert len(outcomes) == 1
    assert outcomes[0].payload["amount_recovered_paise"] == rupees(40)
    assert gateway.ledger.verify().ok
