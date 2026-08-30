"""The outcome simulator, and the attribution it produces.

These tests matter more than they look: an error here does not crash anything,
it just makes the headline number wrong. The first version of this module
settled the whole book while only subtracting the batch's share, which inflated
attributable recovery by 3.6x without a single test failing.
"""

from __future__ import annotations

import json

import pytest

from engine.schema import (
    ActionRequest,
    ActionType,
    AgentName,
    Decision,
    InvoiceStatus,
    OutcomeResult,
    Verdict,
    rupees,
)
from harness.outcomes import OutcomeSimulator
from tests.conftest import NOON_IST


@pytest.fixture
def truth() -> dict:
    return {
        "seed": 1,
        "invoices": {
            "i_small": {
                "would_pay_anyway": False,
                "pays_if_contacted": True,
                "contacts_needed": 2,
                "poisoned": False,
            },
            "i_big": {
                "would_pay_anyway": True,
                "pays_if_contacted": False,
                "contacts_needed": 1,
                "poisoned": False,
            },
            "i_poisoned": {
                "would_pay_anyway": False,
                "pays_if_contacted": False,
                "contacts_needed": 1,
                "poisoned": True,
            },
        },
    }


@pytest.fixture
def sim(store, truth) -> OutcomeSimulator:
    return OutcomeSimulator(store=store, truth=truth)


def allowed(action: ActionType, invoice_id: str) -> tuple[ActionRequest, Decision]:
    req = ActionRequest(
        request_id="rq_1",
        agent=AgentName.RECOVERY,
        action=action,
        customer_id="c_ok",
        invoice_id=invoice_id,
    )
    dec = Decision(decision_id="d_1", request_id="rq_1", verdict=Verdict.ALLOW)
    return req, dec


def denied(action: ActionType, invoice_id: str) -> tuple[ActionRequest, Decision]:
    req, dec = allowed(action, invoice_id)
    return req, dec.model_copy(update={"verdict": Verdict.DENY})


# -- refusals cost nothing and produce nothing ------------------------------ #

def test_a_refused_action_has_no_outcome(sim):
    req, dec = denied(ActionType.SEND_SMS, "i_small")
    assert sim.observe(req, dec, NOON_IST) is None
    assert sim.total_paise() == 0


# -- contact-driven recovery ------------------------------------------------ #

def test_customer_pays_only_after_enough_contacts(sim, store):
    req, dec = allowed(ActionType.SEND_SMS, "i_small")

    first = sim.observe(req, dec, NOON_IST)
    assert first.result is OutcomeResult.NO_RESPONSE
    assert sim.total_paise() == 0

    second = sim.observe(req, dec, NOON_IST)
    assert second.result is OutcomeResult.RECOVERED
    assert second.amount_recovered_paise == store.invoice("i_small").amount_paise


def test_settling_marks_the_invoice_paid_so_the_rulebook_stops_chasing(sim, store):
    req, dec = allowed(ActionType.SEND_SMS, "i_small")
    sim.observe(req, dec, NOON_IST)
    sim.observe(req, dec, NOON_IST)
    assert store.invoice("i_small").status is InvoiceStatus.PAID


def test_an_invoice_never_settles_twice(sim):
    req, dec = allowed(ActionType.SEND_SMS, "i_small")
    sim.observe(req, dec, NOON_IST)
    sim.observe(req, dec, NOON_IST)
    total = sim.total_paise()
    assert sim.observe(req, dec, NOON_IST) is None
    assert sim.total_paise() == total


def test_a_customer_who_will_never_pay_never_pays(sim):
    req, dec = allowed(ActionType.SEND_SMS, "i_poisoned")
    for _ in range(10):
        outcome = sim.observe(req, dec, NOON_IST)
        assert outcome.amount_recovered_paise == 0
    assert sim.total_paise() == 0


# -- attribution ------------------------------------------------------------ #

@pytest.fixture
def always_settles(monkeypatch):
    """Pin the eligibility draw so attribution can be tested without depending
    on which side of 0.55 a particular invoice id happens to fall."""
    monkeypatch.setattr("harness.outcomes.SPONTANEOUS_RATE", 1.0)


def test_spontaneous_settlement_is_attributed_as_unearned(sim, store, always_settles):
    sim.settle_spontaneously(NOON_IST)
    assert sim.total_paise("spontaneous") > 0
    assert sim.total_paise("chased") == 0
    # i_big would have paid anyway; nothing was spent on it.
    assert "i_big" in sim.recovered
    assert sim.recovered["i_big"][1] == "spontaneous"


def test_the_rate_actually_gates_settlement(sim, monkeypatch):
    monkeypatch.setattr("harness.outcomes.SPONTANEOUS_RATE", 0.0)
    sim.settle_spontaneously(NOON_IST)
    assert sim.recovered == {}


def test_spontaneous_payments_spread_across_the_window(sim, always_settles):
    # All on day one would mean the agent never chases somebody who was going to
    # pay anyway — and that wasted spend is the point of the exercise.
    assert sim.settle_spontaneously(NOON_IST, day=1, total_days=4) == []
    landed = [
        sim.settle_spontaneously(NOON_IST, day=d, total_days=4) for d in range(4)
    ]
    assert any(landed), "nothing ever settled across the whole window"


def test_chased_recovery_is_attributed_separately(sim):
    req, dec = allowed(ActionType.SEND_SMS, "i_small")
    sim.observe(req, dec, NOON_IST)
    sim.observe(req, dec, NOON_IST)
    assert sim.total_paise("chased") > 0
    assert sim.total_paise("spontaneous") == 0
    assert sim.total_paise() == sim.total_paise("chased")


def test_gross_is_the_sum_of_its_parts(sim, always_settles):
    sim.settle_spontaneously(NOON_IST)
    req, dec = allowed(ActionType.SEND_SMS, "i_small")
    sim.observe(req, dec, NOON_IST)
    sim.observe(req, dec, NOON_IST)
    assert sim.total_paise() == sim.total_paise("chased") + sim.total_paise("spontaneous")
    assert sim.count() == sim.count("chased") + sim.count("spontaneous")


# -- scope ------------------------------------------------------------------ #

def test_scope_keeps_the_simulator_off_invoices_the_run_never_touched(
    store, truth, always_settles
):
    # The exact bug: without a scope the whole book settles around the batch and
    # the recovery figure counts money nobody chased.
    scoped = OutcomeSimulator(store=store, truth=truth, scope={"i_small"})
    scoped.settle_spontaneously(NOON_IST)
    assert "i_big" not in scoped.recovered
    assert scoped.total_paise() == 0

    unscoped = OutcomeSimulator(store=store, truth=truth)
    unscoped.settle_spontaneously(NOON_IST)
    assert "i_big" in unscoped.recovered


def test_scope_also_blocks_observed_outcomes(store, truth):
    scoped = OutcomeSimulator(store=store, truth=truth, scope={"i_big"})
    req, dec = allowed(ActionType.SEND_SMS, "i_small")
    assert scoped.observe(req, dec, NOON_IST) is None


# -- reproducibility -------------------------------------------------------- #

def test_two_runs_of_the_same_batch_agree(dataset, truth):
    from engine.store import DataStore

    a = OutcomeSimulator(store=DataStore(dataset), truth=truth)
    b = OutcomeSimulator(store=DataStore(dataset), truth=truth)
    a.settle_spontaneously(NOON_IST)
    b.settle_spontaneously(NOON_IST)
    assert a.recovered == b.recovered


def test_retry_outcome_is_deterministic(store, truth):
    from engine.store import DataStore

    results = []
    for _ in range(3):
        s = OutcomeSimulator(store=DataStore(store_dataset(store)), truth=truth)
        req, dec = allowed(ActionType.RETRY_CHARGE, "i_big")
        results.append(s.observe(req, dec, NOON_IST).result)
    assert len(set(results)) == 1


def store_dataset(store) -> dict:
    return {
        "batch_id": store.batch_id,
        "seed": store.seed,
        "customers": [c.model_dump(mode="json") for c in store.customers.values()],
        "invoices": [i.model_dump(mode="json") for i in store.invoices.values()],
        "payments": [p.model_dump(mode="json") for p in store.payments.values()],
    }
