"""The fraud signals.

The tests that matter here are the two that constrain what the model is allowed
to *see*, not the ones that check a weight adds up. A scorer that reads the
future scores beautifully and is worth nothing.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from engine import risk_model
from engine.schema import (
    Customer,
    FailureReason,
    Payment,
    PaymentMethod,
    PaymentStatus,
    rupees,
)
from tests.conftest import NOON_IST


def _customer(age_days: int = 400) -> Customer:
    return Customer(
        customer_id="c",
        name="Test Person",
        email="c@example.com",
        phone="+919800000000",
        created_at=NOON_IST - timedelta(days=age_days),
    )


def _payment(
    pid: str = "p",
    amount: int = rupees(1_000),
    minutes_ago: int = 0,
    status: PaymentStatus = PaymentStatus.CAPTURED,
    reason: FailureReason | None = None,
    method: PaymentMethod = PaymentMethod.UPI,
) -> Payment:
    return Payment(
        payment_id=pid,
        invoice_id="i",
        customer_id="c",
        amount_paise=amount,
        method=method,
        status=status,
        created_at=NOON_IST - timedelta(minutes=minutes_ago),
        failure_reason=reason,
    )


def test_an_ordinary_payment_fires_nothing():
    target = _payment()
    assert risk_model.score(target, _customer(), [target]).signals == []


def test_a_burst_of_declines_fires():
    target = _payment("p_now")
    history = [
        _payment(
            f"p_{n}",
            minutes_ago=10 + n,
            status=PaymentStatus.FAILED,
            reason=FailureReason.DO_NOT_HONOUR,
        )
        for n in range(4)
    ]
    result = risk_model.score(target, _customer(), history + [target])
    assert "decline_burst" in result.names
    assert "hard_declines" in result.names
    assert result.score > 0.5


def test_the_model_cannot_see_the_future():
    """The whole month is in the store. Only the past may reach the score.

    Without this filter the scorer would be told about declines that happen
    *after* the payment it is judging, which is information no live system has.
    It would score superbly on the replay and be useless in production, and
    nothing in the scorecard would reveal it.
    """
    target = _payment("p_now", minutes_ago=60)
    later_declines = [
        _payment(
            f"p_future_{n}",
            minutes_ago=0,  # after the target
            status=PaymentStatus.FAILED,
            reason=FailureReason.DO_NOT_HONOUR,
        )
        for n in range(5)
    ]
    result = risk_model.score(target, _customer(), [target] + later_declines)
    assert result.signals == [], "a signal fired on evidence from after the payment"


def test_a_young_account_on_a_card_fires_two_signals():
    target = _payment(method=PaymentMethod.CARD)
    result = risk_model.score(target, _customer(age_days=3), [target])
    assert result.names == ["young_account", "card_on_young_account"]


def test_an_outsized_ticket_needs_both_the_multiple_and_the_floor():
    history = [_payment(f"p_{n}", amount=rupees(100), minutes_ago=100 + n) for n in range(3)]

    # 10x the median, but under the floor: a small customer buying something
    # slightly larger is not a fraud signal.
    small = _payment("p_small", amount=rupees(1_000))
    assert "outsized_ticket" not in risk_model.score(small, _customer(), history + [small]).names

    big = _payment("p_big", amount=rupees(9_000))
    assert "outsized_ticket" in risk_model.score(big, _customer(), history + [big]).names


def test_the_score_is_capped_at_one():
    target = _payment("p_now", amount=rupees(90_000), method=PaymentMethod.CARD)
    history = [
        _payment(
            f"p_{n}",
            amount=rupees(50),
            minutes_ago=5 + n,
            status=PaymentStatus.FAILED,
            reason=FailureReason.DO_NOT_HONOUR,
        )
        for n in range(8)
    ]
    result = risk_model.score(target, _customer(age_days=1), history + [target])
    assert len(result.signals) == len(risk_model.WEIGHTS), "expected every signal"
    assert result.score == 1.0


def test_every_signal_explains_itself_with_numbers():
    """A reason a judge can read aloud, per the house rule for RuleResult."""
    target = _payment("p_now", amount=rupees(90_000), method=PaymentMethod.CARD)
    history = [
        _payment(f"p_{n}", amount=rupees(50), minutes_ago=5 + n,
                 status=PaymentStatus.FAILED, reason=FailureReason.DO_NOT_HONOUR)
        for n in range(8)
    ]
    result = risk_model.score(target, _customer(age_days=1), history + [target])
    for signal in result.signals:
        assert signal.detail, f"{signal.name} has no explanation"
        assert signal.name not in signal.detail, "the detail is a rule id, not a sentence"


@pytest.mark.parametrize("name", sorted(risk_model.WEIGHTS))
def test_every_weight_belongs_to_a_signal_that_can_fire(name):
    """A weight with no signal behind it is dead configuration that reads as
    policy. Kept honest by construction rather than by review."""
    target = _payment("p_now", amount=rupees(90_000), method=PaymentMethod.CARD)
    history = [
        _payment(f"p_{n}", amount=rupees(50), minutes_ago=5 + n,
                 status=PaymentStatus.FAILED, reason=FailureReason.DO_NOT_HONOUR)
        for n in range(8)
    ]
    fired = risk_model.score(target, _customer(age_days=1), history + [target]).names
    assert name in fired
