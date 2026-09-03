"""The replay harness — treated against holdout, and the scorecard that follows.

This is the file the whole project is an argument for.

Day 3's ``harness.batch`` could already print a recovery figure. The problem
with that figure is not that it was computed wrongly; it is that no recovery
figure computed that way can be believed by anybody. Money arrived after the
agent sent a message. Some of it would have arrived anyway. Nothing in the
merchant's own records distinguishes the two, so any system that chases invoices
and then counts the money can claim credit for the whole lot, and every such
system does.

The only fix is to not chase some of them.

    treated  80% of the collectable book — the agents work it
    holdout  20% — deliberately left alone, no contact of any kind

Both groups contain the same mix of people who would have paid regardless. Only
the treated group gets chased. So the money that arrives in the holdout is the
counterfactual: what the treated group would have produced with no agent at all.
Scale it up to the treated group's size and subtract, and what is left is the
uplift the agent actually caused.

    uplift = recovered(treated) - recovered(holdout) x book(treated)/book(holdout)

Scaled by *book value* rather than invoice count, because the split is random
over invoices and the two groups will not hold equal amounts of money. Dividing
by count would let one large invoice landing in the holdout swing the estimate.

What this number costs
----------------------
The holdout is not free. It is 20% of the book nobody is allowed to chase, and
some of those invoices would have been recovered. That is the price of knowing
whether the other 80% was worth chasing, and it is the reason honest measurement
is rare: it shows up as forgone revenue in the same quarter as the claim.

Checking the estimator itself
-----------------------------
The scorecard reports the uplift twice. Once as measured — from money in, per
group, using only what an outside auditor could see. Once from
``ground_truth.json``, which knows exactly which invoices were going to pay
anyway. The second number is not available to any real merchant; it is here so
that the *method* can be checked on data where the answer is known. If the two
disagree badly, the estimator is broken, and that is worth knowing before it is
pointed at a real book.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agents.recovery import RecoveryAgent, RulePlanner, build_planner
from agents.reconcile import ReconcileAgent
from agents.risk import RiskAgent
from engine.console import setup_console
from engine.gateway import Gateway
from engine.ledger import Ledger
from engine.schema import Verdict, fmt
from engine.store import DataStore
from harness.outcomes import HumanReviewer, OutcomeSimulator, RiskOutcomeSimulator

#: Share of the book held back. Big enough that the holdout's own recovery is
#: not dominated by noise, small enough that the forgone revenue is bearable.
HOLDOUT_FRACTION = 0.20

#: The month the synthetic data covers. Replaying the whole of it is the point —
#: a five-day slice cannot show an invoice being chased, escalated and closed.
START = datetime(2026, 8, 1, 6, 30, tzinfo=timezone.utc)
DAYS = 31


def assign_holdout(
    invoice_ids: list[str], seed: int, fraction: float = HOLDOUT_FRACTION
) -> set[str]:
    """Split by a hash of the invoice id, not by a shuffle.

    Deterministic across runs and across machines — Python's own ``hash`` is
    salted per process, so a holdout built on it would silently differ between
    the run that produced the numbers and the run that checks them. Anyone can
    recompute this assignment from the ids and the seed, which is what makes the
    split auditable rather than merely random.
    """
    cutoff = int(fraction * (1 << 32))
    holdout = set()
    for iid in invoice_ids:
        digest = hashlib.blake2b(f"{seed}:{iid}".encode(), digest_size=4).digest()
        if int.from_bytes(digest, "big") < cutoff:
            holdout.add(iid)
    return holdout


#: Bootstrap resamples behind the confidence interval. A thousand is plenty for
#: a 95% interval and keeps a full run under a second of extra work.
BOOTSTRAP_SAMPLES = 1_000


def bootstrap_uplift(
    treated: list[tuple[int, int]],
    holdout: list[tuple[int, int]],
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = 0,
) -> tuple[int, int]:
    """A 95% interval on the uplift, by resampling invoices within each group.

    The point estimate is one draw of one month. Reported alone it invites
    exactly the overclaiming this project was built to refuse: a number with no
    interval reads as a measurement when it is a sample. On a small book the
    interval comes out wide enough to cross zero, and that is the honest answer
    — the run did not establish an effect — rather than a smaller number that
    happens to be positive.

    Each pair is ``(recovered_paise, book_paise)`` for one invoice. Resampling
    at the invoice level is what makes the interval reflect the real source of
    the noise: which invoices landed in which group.
    """
    if not treated or not holdout:
        return 0, 0

    # Sorted, because the caller builds these from a set of invoice ids and
    # Python salts string hashing per process: same seed, same data, different
    # set iteration order, so rng.randrange picked different invoices on every
    # run and the interval moved by tens of thousands of rupees between two
    # runs of one command. The point estimate never moved, which is what made it
    # invisible. assign_holdout refuses process-salted hashing for the same
    # reason; the interval is quoted at least as often as the estimate.
    treated = sorted(treated)
    holdout = sorted(holdout)

    rng = random.Random(seed)
    book_treated = sum(book for _, book in treated)
    estimates = []
    for _ in range(samples):
        t = [treated[rng.randrange(len(treated))] for _ in range(len(treated))]
        h = [holdout[rng.randrange(len(holdout))] for _ in range(len(holdout))]
        t_book = sum(book for _, book in t)
        h_book = sum(book for _, book in h)
        if not t_book or not h_book:
            continue
        rate_t = sum(rec for rec, _ in t) / t_book
        rate_h = sum(rec for rec, _ in h) / h_book
        estimates.append((rate_t - rate_h) * book_treated)

    if not estimates:
        return 0, 0
    estimates.sort()
    lo = estimates[int(0.025 * len(estimates))]
    hi = estimates[int(0.975 * len(estimates)) - 1]
    return int(lo), int(hi)


def run(
    dataset_path: str = "data/dataset.json",
    truth_path: str = "data/ground_truth.json",
    db_path: str = "data/replay.db",
    limit: int | None = None,
    days: int = DAYS,
    start: datetime = START,
    holdout_fraction: float = HOLDOUT_FRACTION,
    block_threshold: float | None = None,
    use_llm: bool = False,
) -> dict:
    if Path(db_path).exists():
        Path(db_path).unlink()

    store = DataStore.load(dataset_path)
    truth_json = Path(truth_path)
    if not truth_json.exists():
        raise FileNotFoundError(f"{truth_path} not found — run: python -m harness.generate")

    collectable = store.collectable()
    if limit:
        collectable = collectable[:limit]
    all_ids = [i.invoice_id for i in collectable]

    holdout_ids = assign_holdout(all_ids, store.seed, holdout_fraction)
    # A list *and* a set, deliberately. The set is for membership tests; the
    # list is the order the recovery agent works the book in. Iterating the set
    # instead — which this did — handed the agent a different order every run,
    # because Python salts string hashing per process. With a daily budget cap
    # and a contact-frequency window, order decides which invoices get an
    # action at all: two runs of one command came out 2,445 and 2,447 requests.
    treated_order = [i for i in all_ids if i not in holdout_ids]
    treated_ids = set(treated_order)

    book_treated = sum(
        store.invoice(i).amount_paise for i in treated_ids  # type: ignore[union-attr]
    )
    book_holdout = sum(
        store.invoice(i).amount_paise for i in holdout_ids  # type: ignore[union-attr]
    )

    # One simulator over both groups. The holdout is inside its scope so that
    # invoices there still settle on their own — that is the entire point of it.
    # What the holdout never receives is an action, and the only thing that
    # guarantees is that no agent is ever handed one of its invoices.
    sim = OutcomeSimulator.load(store, truth_path, scope=set(all_ids))
    risk_sim = RiskOutcomeSimulator(truth=sim.truth)
    reviewer = HumanReviewer(truth=sim.truth)

    gateway = Gateway(store, Ledger(db_path))
    planner = build_planner() if use_llm else RulePlanner()
    recovery = RecoveryAgent(store=store, gateway=gateway, planner=planner)
    risk = RiskAgent(store=store, gateway=gateway)
    if block_threshold is not None:
        risk.block_threshold = block_threshold
    reconcile = ReconcileAgent(store=store, gateway=gateway)

    screened = []
    requests = 0
    #: Blocks the rulebook sent to a person, waiting for tomorrow's queue.
    pending_blocks: list = []
    #: Escalated recovery work nobody has cleared. Reported, not resolved.
    pending_recovery: list = []

    for day in range(days):
        now = start + timedelta(days=day)

        # 0. Yesterday's fraud queue, worked overnight. An approval raises the
        #    ceiling for that one request; the rulebook still runs again, so a
        #    signature obtained yesterday cannot authorise something that became
        #    disallowed since. That is why the request is resubmitted rather
        #    than simply executed.
        queue, pending_blocks = pending_blocks, []
        for request in queue:
            if not reviewer.approves_block(request):
                continue
            gateway.approve(request.request_id, approver="analyst", note="fraud queue")
            decision = gateway.submit(request, now=now)
            requests += 1
            outcome = risk_sim.observe(request, decision, now)
            if outcome is not None:
                gateway.report_outcome(outcome)

        # 1. Money that arrives with nobody doing anything. Both groups.
        sim.settle_spontaneously(now, day=day, total_days=days)

        # 2. Recovery works the treated group only. This line is the experiment.
        open_treated = [
            inv
            for inv in (store.invoice(i) for i in treated_order)
            if inv is not None and inv.invoice_id not in sim.recovered
        ]
        for request, decision in recovery.run(open_treated, now):
            requests += 1
            if decision.verdict is Verdict.NEEDS_HUMAN:
                pending_recovery.append(request)
            outcome = sim.observe(request, decision, now)
            if outcome is not None:
                gateway.report_outcome(outcome)

        # 3. Risk screens the day's captured payments. Not part of the holdout
        #    experiment — fraud has its own counterfactual, which is ground
        #    truth about who was really a fraudster, and no customer needs to be
        #    left unprotected to establish it.
        todays_payments = [
            p for p in store.captured_payments() if p.created_at.date() == now.date()
        ]
        screened.extend(todays_payments)
        for request, decision in risk.run(todays_payments, now):
            requests += 1
            if decision.verdict is Verdict.NEEDS_HUMAN:
                pending_blocks.append(request)
            outcome = risk_sim.observe(request, decision, now)
            if outcome is not None:
                gateway.report_outcome(outcome)

        # 4. Reconcile checks the day's settlement batches.
        for request, decision in reconcile.run(store.settlements_on(now), now):
            requests += 1

    # The sweep that no batch-by-batch pass can do: captured payments that never
    # came back from the bank at all.
    reconcile.sweep_unsettled(start + timedelta(days=days))

    # ------------------------------------------------------------------ #
    # The measurement
    # ------------------------------------------------------------------ #

    def group_total(ids: set[str]) -> int:
        return sum(amount for iid, (amount, _) in sim.recovered.items() if iid in ids)

    recovered_treated = group_total(treated_ids)
    recovered_holdout = group_total(holdout_ids)

    scale = (book_treated / book_holdout) if book_holdout else 0.0
    holdout_scaled = int(recovered_holdout * scale)
    uplift_measured = recovered_treated - holdout_scaled

    def pairs(ids: set[str]) -> list[tuple[int, int]]:
        out = []
        for iid in ids:
            invoice = store.invoice(iid)
            if invoice is None:
                continue
            recovered, _ = sim.recovered.get(iid, (0, ""))
            out.append((recovered, invoice.amount_paise))
        return out

    ci_low, ci_high = bootstrap_uplift(
        pairs(treated_ids), pairs(holdout_ids), seed=store.seed
    )

    # The same number computed from ground truth, which no real merchant has.
    # Only here so the estimator above can be checked on data where the answer
    # is known. Never quote this one as the result.
    uplift_true = sum(
        amount
        for iid, (amount, why) in sim.recovered.items()
        if iid in treated_ids and why == "chased"
    )

    missed_count, missed_paise = risk_sim.missed(screened)
    stats = gateway.stats()

    return {
        "batch_id": stats["batch_id"],
        "dataset_path": dataset_path,
        "planner": planner.name,
        "block_threshold": risk.block_threshold,
        "days": days,
        "requests": requests,
        # -- the experiment --
        "treated_invoices": len(treated_ids),
        "holdout_invoices": len(holdout_ids),
        "book_treated_paise": book_treated,
        "book_holdout_paise": book_holdout,
        "recovered_treated_paise": recovered_treated,
        "recovered_holdout_paise": recovered_holdout,
        "holdout_scaled_paise": holdout_scaled,
        "scale": scale,
        "uplift_measured_paise": uplift_measured,
        "uplift_ci_low_paise": ci_low,
        "uplift_ci_high_paise": ci_high,
        "uplift_true_paise": uplift_true,
        # -- risk --
        "payments_screened": len(screened),
        "prevented_paise": risk_sim.prevented_paise,
        "lost_sale_paise": risk_sim.lost_sale_paise,
        "blocked_fraud": risk_sim.blocked_fraud,
        "blocked_genuine": risk_sim.blocked_genuine,
        "reviewed_fraud": risk_sim.reviewed_fraud,
        "reviewed_clean": risk_sim.reviewed_clean,
        "missed_fraud_count": missed_count,
        "missed_fraud_paise": missed_paise,
        "analyst_approved": reviewer.approved,
        "analyst_rejected": reviewer.rejected,
        "pending_recovery": len(pending_recovery),
        "pending_blocks": len(pending_blocks),
        # -- reconcile --
        "settlements_checked": len(reconcile.seen),
        "exceptions": reconcile.exception_summary(),
        "exception_count": len(reconcile.exceptions),
        "exception_paise": reconcile.exception_value_paise(),
        # -- the layer --
        "stats": stats,
        "denials": gateway.denial_breakdown(),
        "verify": gateway.ledger.verify(),
        "ledger": gateway.ledger,
    }


# --------------------------------------------------------------------------- #
# Handing the scorecard to something other than a terminal
# --------------------------------------------------------------------------- #

#: Keys that cannot cross a JSON boundary: an open SQLite handle and a dataclass.
#: Everything else in the result is already a number, a string or a dict.
_NOT_SERIALISABLE = ("ledger", "verify")


def to_json(result: dict) -> dict:
    """The scorecard as data, for the dashboard.

    A run takes twenty seconds over the full month, which is far too long to do
    inside a web request — so the harness writes the answer once and the server
    reads the file. The dashboard is a view over a completed run, not a trigger
    for one, and that is the honest relationship: the numbers on screen came
    from a command someone can re-run and check.
    """
    payload = {k: v for k, v in result.items() if k not in _NOT_SERIALISABLE}
    verify = result.get("verify")
    payload["verify"] = {
        "ok": bool(verify.ok),
        "entries_checked": verify.entries_checked,
        "detail": verify.detail or str(verify),
    }
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    return payload


def write_json(result: dict, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(to_json(result), indent=2), encoding="utf-8")
    return out


# --------------------------------------------------------------------------- #
# The scorecard
# --------------------------------------------------------------------------- #

WIDTH = 74


def render(r: dict) -> str:
    s = r["stats"]
    spend = s["spend_paise"]
    net = r["uplift_measured_paise"] + r["prevented_paise"] - spend - r["lost_sale_paise"]

    def line(label: str, value: str, note: str = "") -> str:
        return f"{label:<40}{value:>18}   {note}".rstrip()

    rule = "-" * WIDTH
    out = [
        "",
        f"CHECKPOST SCORECARD                     batch {r['batch_id']}",
        f"{r['days']} simulated days   planner: {r['planner']}   "
        f"policy v{s['policy_version']}",
        "=" * WIDTH,
        "",
        "RECOVERY — measured against a holdout",
        "",
        line(
            f"  Treated group ({r['treated_invoices']:,} invoices)",
            fmt(r["book_treated_paise"]),
            "book",
        ),
        line(
            f"  Holdout group ({r['holdout_invoices']:,} invoices)",
            fmt(r["book_holdout_paise"]),
            "book, never contacted",
        ),
        "",
        line("  Recovered, treated", fmt(r["recovered_treated_paise"])),
        line(
            "  Recovered, holdout scaled",
            fmt(r["holdout_scaled_paise"]),
            f"x{r['scale']:.2f} from {fmt(r['recovered_holdout_paise'])}",
        ),
        rule,
        line("  UPLIFT the agent caused", fmt(r["uplift_measured_paise"])),
        line(
            "    95% confidence interval",
            f"{fmt(r['uplift_ci_low_paise'])} to {fmt(r['uplift_ci_high_paise'])}",
            "" if r["uplift_ci_low_paise"] > 0 else "<- crosses zero: no effect shown",
        ),
        "",
        f"RISK — every block priced both ways   (block at {r['block_threshold']:.2f})",
        "",
        line(
            f"  Payments screened ({r['payments_screened']:,})",
            f"{r['blocked_fraud'] + r['blocked_genuine']:,} blocked",
            f"{r['reviewed_fraud'] + r['reviewed_clean']:,} reviewed",
        ),
        line(
            "  Fraud prevented",
            fmt(r["prevented_paise"]),
            f"{r['blocked_fraud']} blocked, {r['reviewed_fraud']} caught in review",
        ),
        line(
            "  Lost sales, blocked genuine",
            fmt(-r["lost_sale_paise"]),
            f"{r['blocked_genuine']} customers turned away",
        ),
        line(
            "  Fraud that got through",
            fmt(r["missed_fraud_paise"]),
            f"{r['missed_fraud_count']} payments, not prevented",
        ),
        "",
        "RECONCILIATION",
        "",
        line("  Settlements checked", f"{r['settlements_checked']:,}"),
        line(
            "  Unresolved exceptions",
            f"{r['exception_count']:,}",
            fmt(r["exception_paise"]) + " at stake",
        ),
    ]
    for kind, count in r["exceptions"].items():
        out.append(f"      {kind:<22} {count:>6,}")

    out += [
        "",
        "THE BOTTOM LINE",
        "",
        line("  Uplift from recovery", fmt(r["uplift_measured_paise"])),
        line("  Fraud prevented", fmt(r["prevented_paise"])),
        line("  Cost of interventions", fmt(-spend)),
        line("  Cost of false positives", fmt(-r["lost_sale_paise"])),
        rule,
        line("  NET VALUE CREATED", fmt(net)),
        "",
        "THE LAYER",
        "",
        line("  Actions requested", f"{s['actions_requested']:,}"),
        line("  Allowed", f"{s['allowed']:,}"),
        line("  Denied", f"{s['denied']:,}"),
        line("  Escalated to a human", f"{s['needs_human']:,}"),
        line(
            "    fraud queue, worked",
            f"{r['analyst_approved'] + r['analyst_rejected']:,}",
            f"{r['analyst_approved']} approved, {r['analyst_rejected']} rejected",
        ),
        line(
            "    recovery queue, still waiting",
            f"{r['pending_recovery']:,}",
            "nobody has cleared these",
        ),
        "",
    ]

    if r["denials"]:
        out.append("  Refusals by rule")
        for rule_id, count in r["denials"].items():
            out.append(f"      {rule_id:<26} {count:>6,}")
        out.append("")

    v = r["verify"]
    out += [
        f"  Ledger: {s['ledger_entries']:,} entries — "
        f"{'intact' if v.ok else 'BROKEN: ' + v.detail}",
        f"  head:   {s['ledger_head']}",
        "",
        "=" * WIDTH,
        "METHOD CHECK — not available to a real merchant",
        "",
        line("  Uplift, measured from the holdout", fmt(r["uplift_measured_paise"])),
        line("  Uplift, from ground truth", fmt(r["uplift_true_paise"])),
        line(
            "  Estimator error",
            fmt(r["uplift_measured_paise"] - r["uplift_true_paise"]),
            _error_note(r),
        ),
        "",
        "The first number is the claim. The second is the answer, which exists",
        "only because the month is synthetic. They are printed together so the",
        "method can be checked where the truth is known — a real book offers no",
        "such luxury, which is exactly why the holdout has to be paid for.",
        "",
    ]
    return "\n".join(out)


def _error_note(r: dict) -> str:
    truth = r["uplift_true_paise"]
    if not truth:
        return ""
    pct = abs(r["uplift_measured_paise"] - truth) / truth
    return f"{pct:.0%} off"


def main() -> None:
    setup_console()
    ap = argparse.ArgumentParser(
        description="Replay the month with a treated/holdout split."
    )
    ap.add_argument("--limit", type=int, default=None, help="cap invoices worked")
    ap.add_argument("--days", type=int, default=DAYS)
    ap.add_argument("--holdout", type=float, default=HOLDOUT_FRACTION)
    ap.add_argument(
        "--block-threshold",
        type=float,
        default=None,
        help="risk score at which an order is blocked outright (default 0.70)",
    )
    ap.add_argument("--db", default="data/replay.db")
    ap.add_argument(
        "--out",
        default="out/scorecard.json",
        help="where to write the scorecard as JSON for the dashboard",
    )
    ap.add_argument(
        "--llm",
        action="store_true",
        help="use the Gemini planner for recovery (needs GEMINI_API_KEY)",
    )
    args = ap.parse_args()

    result = run(
        db_path=args.db,
        limit=args.limit,
        days=args.days,
        holdout_fraction=args.holdout,
        block_threshold=args.block_threshold,
        use_llm=args.llm,
    )
    print(render(result))
    written = write_json(result, args.out)
    print(f"scorecard written to {written}")
    result["ledger"].close()


if __name__ == "__main__":
    main()
