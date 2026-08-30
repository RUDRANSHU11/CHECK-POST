"""The generated month has to be reproducible and has to contain the traps the
demo depends on."""

from __future__ import annotations

from engine.schema import InvoiceStatus
from engine.store import DataStore
from harness.generate import generate


def test_generation_is_deterministic():
    # The scorecard is the deliverable. If the same seed produced different
    # numbers, none of it would be checkable by anyone else.
    a, ta = generate(seed=7, n_customers=100, n_invoices=200, n_payments=250)
    b, tb = generate(seed=7, n_customers=100, n_invoices=200, n_payments=250)
    assert a["invoices"] == b["invoices"]
    assert a["payments"] == b["payments"]
    assert ta["invoices"] == tb["invoices"]


def test_different_seeds_differ():
    a, _ = generate(seed=1, n_customers=50, n_invoices=100, n_payments=100)
    b, _ = generate(seed=2, n_customers=50, n_invoices=100, n_payments=100)
    assert a["invoices"] != b["invoices"]


def test_ground_truth_is_not_in_the_agent_facing_dataset():
    dataset, truth = generate(seed=3, n_customers=50, n_invoices=100, n_payments=100)
    blob = str(dataset)
    assert "would_pay_anyway" not in blob
    assert "pays_if_contacted" not in blob
    assert "pay_propensity" not in blob
    # ...and is present where the scorer looks for it.
    assert all("would_pay_anyway" in v for v in truth["invoices"].values())


def test_dataset_loads_into_the_store():
    dataset, _ = generate(seed=4, n_customers=80, n_invoices=150, n_payments=200)
    store = DataStore(dataset)
    assert len(store.customers) == 80
    assert len(store.invoices) == 150
    assert len(store.payments) == 200
    assert store.collectable()


def test_month_contains_the_cases_the_demo_needs():
    dataset, truth = generate(seed=42, n_customers=400, n_invoices=800, n_payments=1_000)
    store = DataStore(dataset)

    assert any(c.opted_out for c in store.customers.values()), "need STOP customers"
    assert any(
        i.status is InvoiceStatus.DISPUTED for i in store.invoices.values()
    ), "need disputed invoices"
    assert truth["poisoned_invoice_ids"], "need at least one poisoned memo"
    assert any(
        p.failure_reason and not p.is_retryable for p in store.payments.values()
    ), "need hard declines"
    # The uplift trap: some customers pay with no contact at all.
    assert any(v["would_pay_anyway"] for v in truth["invoices"].values())
    assert any(v["pays_if_contacted"] for v in truth["invoices"].values())


def test_small_invoices_exist_for_the_economics_demo():
    # The headline refusal is an 80-rupee SMS chasing a 40-rupee invoice. That
    # case has to actually occur in the data.
    dataset, _ = generate(seed=42, n_customers=400, n_invoices=800, n_payments=900)
    store = DataStore(dataset)
    cheap = [i for i in store.collectable() if i.amount_paise < 10_000]
    assert cheap, "no invoices cheap enough to make chasing uneconomic"


def test_poisoned_memos_are_caught_by_the_rulebook():
    from engine.policy import PolicyContext, decide
    from engine.schema import ActionRequest, ActionType, AgentName, Verdict, utcnow

    dataset, truth = generate(seed=42, n_customers=400, n_invoices=800, n_payments=900)
    store = DataStore(dataset)

    for iid in truth["poisoned_invoice_ids"]:
        inv = store.invoice(iid)
        req = ActionRequest(
            request_id="rq",
            agent=AgentName.RECOVERY,
            action=ActionType.ISSUE_REFUND,
            customer_id=inv.customer_id,
            invoice_id=iid,
            amount_paise=1_000,
        )
        verdict, results = decide(
            req, PolicyContext(now=utcnow(), customer=store.customer(inv.customer_id), invoice=inv)
        )
        assert verdict is Verdict.DENY, f"{iid} slipped past the injection guard"
        assert any(r.rule_id == "prompt_injection" for r in results)
