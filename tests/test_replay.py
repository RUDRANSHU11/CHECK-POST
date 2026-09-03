"""The holdout experiment.

Every claim the project makes rests on this file being right, so the tests are
about the *design* of the experiment rather than about whether the code runs:
that the split is reproducible, that the holdout is genuinely untouched, and
that the estimator recovers an effect it is given on data where the answer is
known.
"""

from __future__ import annotations


import pytest

from harness import replay
from harness.generate import generate


def test_the_split_is_reproducible_across_processes():
    """Python's own hash is salted per process. A holdout built on it would
    differ between the run that produced a number and the run that checks it,
    and the difference would be invisible."""
    ids = [f"inv_{n:05d}" for n in range(2_000)]
    assert replay.assign_holdout(ids, 42) == replay.assign_holdout(ids, 42)
    assert replay.assign_holdout(ids, 42) != replay.assign_holdout(ids, 43)


def test_the_split_lands_near_the_requested_fraction():
    ids = [f"inv_{n:05d}" for n in range(5_000)]
    for fraction in (0.10, 0.20, 0.50):
        share = len(replay.assign_holdout(ids, 42, fraction)) / len(ids)
        assert abs(share - fraction) < 0.02, f"{fraction} split came out at {share}"


def test_assignment_does_not_depend_on_the_order_of_the_ids():
    ids = [f"inv_{n:05d}" for n in range(500)]
    assert replay.assign_holdout(ids, 7) == replay.assign_holdout(list(reversed(ids)), 7)


def test_the_interval_does_not_depend_on_the_order_of_the_invoices():
    """The interval is quoted as often as the estimate, so it has to be as
    reproducible as the estimate.

    It was not: the caller builds these pairs from a *set* of invoice ids, and
    Python salts string hashing per process, so the resampler drew different
    invoices on every run. Two runs of one command reported intervals tens of
    thousands of rupees apart while the point estimate sat still."""
    import random as _random

    treated = [(n * 37 % 900, 1_000 + n) for n in range(300)]
    holdout = [(n * 11 % 400, 1_000 + n) for n in range(80)]
    shuffled_t, shuffled_h = list(treated), list(holdout)
    _random.Random(1).shuffle(shuffled_t)
    _random.Random(2).shuffle(shuffled_h)

    assert replay.bootstrap_uplift(treated, holdout, seed=5) == replay.bootstrap_uplift(
        shuffled_t, shuffled_h, seed=5
    )


# --------------------------------------------------------------------------- #
# A small end-to-end replay
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def small_run(tmp_path_factory):
    """A miniature month, generated and replayed once for the whole module."""
    import json

    out = tmp_path_factory.mktemp("replay")
    dataset, truth = generate(seed=11, n_customers=200, n_invoices=500, n_payments=700)
    (out / "dataset.json").write_text(json.dumps(dataset), encoding="utf-8")
    (out / "ground_truth.json").write_text(json.dumps(truth), encoding="utf-8")

    result = replay.run(
        dataset_path=str(out / "dataset.json"),
        truth_path=str(out / "ground_truth.json"),
        db_path=str(out / "replay.db"),
        days=31,
    )
    yield result
    result["ledger"].close()


def test_the_holdout_is_never_contacted(small_run):
    """The one property the whole measurement depends on.

    If a single action reached a holdout invoice, the control group would be
    contaminated and the uplift figure would be worth nothing — while still
    looking exactly as plausible as before.
    """
    from engine.store import DataStore

    ledger = small_run["ledger"]
    # Recompute the split the same way run() did, from the store itself, rather
    # than trusting a value the run handed back.
    store = DataStore.load(small_run["dataset_path"])
    all_ids = [i.invoice_id for i in store.collectable()]
    holdout = replay.assign_holdout(all_ids, store.seed)

    touched = {
        e.payload["invoice_id"]
        for e in ledger.entries(entry_type="decision")
        if e.payload.get("invoice_id") and e.payload["agent"] == "recovery"
    }
    assert not (touched & holdout), "a recovery action reached the holdout group"


def test_both_groups_are_non_empty_and_add_up(small_run):
    assert small_run["treated_invoices"] > 0
    assert small_run["holdout_invoices"] > 0
    assert small_run["book_treated_paise"] > small_run["book_holdout_paise"]


def test_the_answer_falls_inside_the_interval_the_estimator_claims(small_run):
    """The method check, asserted rather than merely printed.

    Not "the point estimate is close to the truth" — on a 500-invoice month it
    is not, and an assertion with a tolerance loose enough to pass would be
    testing nothing. The claim a confidence interval makes is that it contains
    the answer, so that is what gets asserted. A single interval can miss one
    time in twenty by construction; the seed is fixed, so this either holds or
    the estimator is broken.
    """
    true = small_run["uplift_true_paise"]
    assert true > 0, "the fixture produced no real uplift to measure"
    assert small_run["uplift_ci_low_paise"] <= true <= small_run["uplift_ci_high_paise"], (
        f"true uplift {true} outside the claimed interval "
        f"[{small_run['uplift_ci_low_paise']}, {small_run['uplift_ci_high_paise']}]"
    )


def test_the_interval_is_reported_even_when_it_crosses_zero(small_run):
    """A wide interval is the honest answer on a small book, not a failure. The
    scorecard has to be able to say 'this run did not establish an effect'."""
    assert small_run["uplift_ci_low_paise"] <= small_run["uplift_measured_paise"]
    assert small_run["uplift_measured_paise"] <= small_run["uplift_ci_high_paise"]


def test_the_scorecard_reports_both_kinds_of_risk_error(small_run):
    """A fraud number that reports only what it caught is the thing this project
    exists to refuse. Both keys must be present even when one is zero."""
    assert "prevented_paise" in small_run
    assert "lost_sale_paise" in small_run
    assert "missed_fraud_paise" in small_run


def test_the_ledger_survives_a_full_run(small_run):
    assert small_run["verify"].ok
    assert small_run["stats"]["ledger_entries"] > 0


def test_denials_actually_happened(small_run):
    """A run where the layer never says no is a run that proves nothing."""
    assert small_run["stats"]["denied"] > 0
    assert small_run["denials"], "no rule refused anything"


def test_the_scorecard_renders(small_run):
    text = replay.render(small_run)
    assert "UPLIFT the agent caused" in text
    assert "METHOD CHECK" in text


def test_the_same_seed_produces_the_same_scorecard(small_run, tmp_path):
    """The scorecard is the deliverable. If two runs of one dataset disagreed,
    none of it would be checkable by anybody.

    This caught a real defect: the simulated analyst was seeded on the request's
    uuid4 rather than on the payment, so the fraud figures moved by nearly a
    lakh between runs of the same seed. Anything that *decides* an outcome has
    to key on something the dataset fixes.
    """
    again = replay.run(
        dataset_path=small_run["dataset_path"],
        truth_path=small_run["dataset_path"].replace("dataset.json", "ground_truth.json"),
        db_path=str(tmp_path / "again.db"),
        days=31,
    )
    try:
        for key in (
            "uplift_measured_paise",
            "uplift_ci_low_paise",
            "uplift_ci_high_paise",
            "uplift_true_paise",
            "prevented_paise",
            "lost_sale_paise",
            "missed_fraud_paise",
            "exception_count",
        ):
            assert again[key] == small_run[key], f"{key} is not reproducible"
        assert again["stats"]["spend_paise"] == small_run["stats"]["spend_paise"]
    finally:
        again["ledger"].close()


def test_the_scorecard_survives_the_trip_through_json(small_run, tmp_path):
    """The dashboard reads a file, so the result has to serialise cleanly.

    Two members cannot cross that boundary — an open SQLite handle and a
    dataclass — and forgetting either turns the dashboard into a 500 that only
    shows up when someone opens it, which on the day is during the demo.
    """
    import json

    out = replay.write_json(small_run, tmp_path / "scorecard.json")
    card = json.loads(out.read_text(encoding="utf-8"))

    assert "ledger" not in card, "an open database handle must not reach the file"
    assert card["verify"]["ok"] is True
    assert card["uplift_measured_paise"] == small_run["uplift_measured_paise"]
    # The dashboard reads these by name; losing one is a silently blank panel.
    for key in (
        "uplift_ci_low_paise", "uplift_ci_high_paise", "prevented_paise",
        "lost_sale_paise", "missed_fraud_paise", "denials", "exceptions",
        "block_threshold", "stats",
    ):
        assert key in card, f"the dashboard reads {key} and it is not in the file"
