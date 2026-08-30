"""The money check: is this attempt worth making?"""

from __future__ import annotations

from datetime import timedelta

import pytest

from engine import economics
from engine.economics import MARGIN, MAX_SPEND_FRACTION, assess, probability
from engine.policy import PolicyContext
from engine.schema import ActionRequest, ActionType, AgentName, Verdict, rupees
from tests.conftest import NOON_IST


def req(action: ActionType, amount: int = 0, invoice_id: str = "i_small") -> ActionRequest:
    return ActionRequest(
        request_id="rq_1",
        agent=AgentName.RECOVERY,
        action=action,
        customer_id="c_ok",
        invoice_id=invoice_id,
        amount_paise=amount,
    )


def ctx(store, invoice_id: str = "i_small", **over) -> PolicyContext:
    base = dict(
        now=NOON_IST, customer=store.customer("c_ok"), invoice=store.invoice(invoice_id)
    )
    base.update(over)
    return PolicyContext(**base)


# -- the headline refusal --------------------------------------------------- #

def test_escalating_a_tiny_invoice_is_refused(store):
    # 50 of human time to chase 40. This is the case the project exists for.
    a = assess(req(ActionType.ESCALATE_TO_HUMAN), ctx(store))
    assert a.verdict is Verdict.DENY
    assert "not worth it" in a.reason
    assert a.cost_paise > a.expected_recovery_paise


def test_escalating_a_large_invoice_is_fine(store):
    a = assess(req(ActionType.ESCALATE_TO_HUMAN, invoice_id="i_big"), ctx(store, "i_big"))
    assert a.verdict is Verdict.ALLOW


def test_cheap_channels_on_a_tiny_invoice_are_still_worth_it(store):
    # The gate is not a blanket ban on small invoices — a 25-paise SMS chasing
    # 40 rupees is good business, and refusing it would be its own bug.
    for action in (ActionType.SEND_EMAIL, ActionType.SEND_SMS, ActionType.SEND_WHATSAPP):
        assert assess(req(action), ctx(store)).verdict is Verdict.ALLOW, action


# -- diminishing returns ---------------------------------------------------- #

def test_repeated_attempts_eventually_stop_being_worth_it(store):
    verdicts = [
        assess(req(ActionType.PLACE_CALL), ctx(store, attempts_on_invoice=n)).verdict
        for n in range(5)
    ]
    assert verdicts[0] is Verdict.ALLOW
    assert verdicts[-1] is Verdict.DENY
    # Once it flips to deny it must stay denied — probability only decays.
    first_deny = verdicts.index(Verdict.DENY)
    assert all(v is Verdict.DENY for v in verdicts[first_deny:])


def test_probability_decays_monotonically(store):
    ps = [probability(req(ActionType.SEND_SMS), ctx(store, attempts_on_invoice=n)) for n in range(6)]
    assert ps == sorted(ps, reverse=True)
    assert all(0.0 <= p <= 1.0 for p in ps)


def test_staleness_lowers_the_estimate(store):
    fresh = ctx(store, "i_big")
    stale = ctx(store, "i_big", now=NOON_IST + timedelta(days=120))
    assert probability(req(ActionType.SEND_SMS, invoice_id="i_big"), stale) < probability(
        req(ActionType.SEND_SMS, invoice_id="i_big"), fresh
    )


# -- the budget cap --------------------------------------------------------- #

def test_cumulative_spend_cap(store):
    invoice = store.invoice("i_small")
    budget = int(invoice.amount_paise * MAX_SPEND_FRACTION)
    a = assess(req(ActionType.PLACE_CALL), ctx(store, spent_on_invoice=budget))
    assert a.verdict is Verdict.DENY
    assert "budget" in a.reason


def test_budget_cap_bites_even_when_the_single_attempt_looks_cheap(store):
    # This is the slow bleed: every individual nudge is justifiable, the total
    # is not.
    invoice = store.invoice("i_small")
    budget = int(invoice.amount_paise * MAX_SPEND_FRACTION)
    a = assess(req(ActionType.SEND_SMS), ctx(store, spent_on_invoice=budget))
    assert a.verdict is Verdict.DENY
    assert a.expected_recovery_paise > a.cost_paise, "the attempt itself was fine"


# -- discounts -------------------------------------------------------------- #

def test_discount_is_priced_at_the_margin_given_away(store):
    modest = assess(
        req(ActionType.OFFER_DISCOUNT, rupees(500), "i_big"), ctx(store, "i_big")
    )
    reckless = assess(
        req(ActionType.OFFER_DISCOUNT, rupees(8_100), "i_big"), ctx(store, "i_big")
    )
    assert modest.verdict is Verdict.ALLOW
    assert reckless.verdict is Verdict.DENY
    assert reckless.cost_paise == rupees(8_100)


def test_discount_recovers_only_what_is_left_after_it(store):
    a = assess(req(ActionType.OFFER_DISCOUNT, rupees(1_000), "i_big"), ctx(store, "i_big"))
    invoice = store.invoice("i_big")
    assert a.expected_recovery_paise < int(a.probability * invoice.amount_paise)


# -- scope ------------------------------------------------------------------ #

@pytest.mark.parametrize(
    "action", [ActionType.ISSUE_REFUND, ActionType.BLOCK_ORDER, ActionType.FLAG_FOR_REVIEW]
)
def test_gate_has_no_opinion_on_non_recovery_actions(store, action):
    assert assess(req(action), ctx(store)).applicable is False


def test_gate_has_no_opinion_without_an_invoice(store):
    a = assess(req(ActionType.SEND_SMS), PolicyContext(now=NOON_IST, customer=store.customer("c_ok")))
    assert a.applicable is False


def test_unknown_failure_reason_gets_the_floor_not_a_generous_default(store):
    # A retry whose failure reason we cannot read should be assumed near-useless,
    # not assumed average.
    p = probability(req(ActionType.RETRY_CHARGE), ctx(store))
    assert p <= 0.05


# -- integration with the gateway ------------------------------------------- #

def test_economics_can_refuse_what_the_rulebook_allowed(gateway):
    # Nothing in policy.py objects to escalating a 40-rupee invoice.
    from engine.policy import decide

    r = req(ActionType.ESCALATE_TO_HUMAN)
    verdict, _ = decide(r, gateway.build_context(r, NOON_IST))
    assert verdict is Verdict.ALLOW

    decision = gateway.submit(r, now=NOON_IST)
    assert decision.verdict is Verdict.DENY
    assert any(x.rule_id == "economics" for x in decision.results)


def test_decision_records_the_expected_recovery(gateway):
    decision = gateway.submit(req(ActionType.SEND_SMS, invoice_id="i_big"), now=NOON_IST)
    assert decision.verdict is Verdict.ALLOW
    assert decision.expected_recovery_paise > 0


def test_gate_is_skipped_once_the_rulebook_has_refused(gateway):
    # Pricing an action we will never take would put a misleading expected
    # return in the audit trail.
    decision = gateway.submit(
        req(ActionType.SEND_SMS, invoice_id="i_disputed"), now=NOON_IST
    )
    assert decision.verdict is Verdict.DENY
    assert not any(x.rule_id == "economics" for x in decision.results)
    assert decision.expected_recovery_paise == 0


def test_spend_accumulates_per_invoice_across_calls(gateway):
    gateway.submit(req(ActionType.SEND_WHATSAPP, invoice_id="i_big"), now=NOON_IST)
    assert gateway._spent["i_big"] > 0
    before = gateway._spent["i_big"]
    gateway.submit(req(ActionType.SEND_WHATSAPP, invoice_id="i_big"), now=NOON_IST)
    assert gateway._spent["i_big"] > before


def test_spend_survives_a_restart(store, tmp_path):
    from engine.gateway import Gateway
    from engine.ledger import Ledger

    db = tmp_path / "spend.db"
    gw1 = Gateway(store, Ledger(db))
    gw1.submit(req(ActionType.SEND_WHATSAPP, invoice_id="i_big"), now=NOON_IST)
    spent = gw1._spent["i_big"]
    gw1.ledger.close()

    gw2 = Gateway(store, Ledger(db))
    assert gw2._spent["i_big"] == spent
    gw2.ledger.close()
