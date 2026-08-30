"""Run the recovery agent over a batch and print the first numbers.

Day 3's deliverable: numbers on screen, however ugly. This is not yet the real
scorecard — there is no holdout group, so the recovery figure it prints is the
*flattering* one, and it says so. Day 4's replay harness adds the control group
that turns this into a defensible uplift claim.

The shape of a run:

    for each simulated day
        1. invoices that were going to pay anyway quietly settle
        2. the agent picks one action per still-open invoice
        3. the gateway allows or refuses it
        4. the harness resolves allowed actions into outcomes
        5. outcomes go back into the ledger
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agents.recovery import RecoveryAgent, RulePlanner, build_planner
from engine.console import setup_console
from engine.gateway import Gateway
from engine.ledger import Ledger
from engine.schema import Verdict, fmt
from engine.store import DataStore
from harness.outcomes import OutcomeSimulator

#: Noon IST on a weekday — outside quiet hours, so the batch is not dominated by
#: a rule that has nothing to do with what we are measuring here.
START = datetime(2026, 8, 20, 6, 30, tzinfo=timezone.utc)


def run(
    dataset_path: str = "data/dataset.json",
    truth_path: str = "data/ground_truth.json",
    db_path: str = "data/batch.db",
    limit: int = 400,
    days: int = 5,
    use_llm: bool = False,
) -> dict:
    if Path(db_path).exists():
        Path(db_path).unlink()

    store = DataStore.load(dataset_path)
    targets = store.collectable()[:limit]
    target_ids = {i.invoice_id for i in targets}
    opening_book = sum(i.amount_paise for i in targets)

    # Scope the simulator to this batch. Without it the whole book settles around
    # us and the recovery figure counts invoices the run never touched.
    sim = OutcomeSimulator.load(store, truth_path, scope=target_ids)
    gateway = Gateway(store, Ledger(db_path))
    planner = build_planner() if use_llm else RulePlanner()
    agent = RecoveryAgent(store=store, gateway=gateway, planner=planner)

    worked = 0
    for day in range(days):
        now = START + timedelta(days=day)

        # 1. money that arrives without us doing anything, spread across the
        #    window so some of it lands *after* we have already spent on chasing
        sim.settle_spontaneously(now, day=day, total_days=days)

        # 2-5. the agent works whatever is still open
        still_open = [store.invoice(i) for i in target_ids]
        still_open = [i for i in still_open if i and i.invoice_id not in sim.recovered]
        for request, decision in agent.run(still_open, now):
            worked += 1
            outcome = sim.observe(request, decision, now)
            if outcome is not None:
                gateway.report_outcome(outcome)

    return {
        "planner": planner.name,
        "invoices_targeted": len(targets),
        "opening_book_paise": opening_book,
        "requests": worked,
        "stats": gateway.stats(),
        "denials": gateway.denial_breakdown(),
        "recovered_paise": sim.total_paise(),
        "spontaneous_paise": sim.total_paise("spontaneous"),
        "spontaneous_count": sim.count("spontaneous"),
        "earned_paise": sim.total_paise("chased"),
        "earned_count": sim.count("chased"),
        "settled_count": sim.count(),
        "verify": gateway.ledger.verify(),
        "ledger": gateway.ledger,
    }


def render(r: dict) -> str:
    s = r["stats"]
    spend = s["spend_paise"]
    lines = [
        "",
        f"CHECKPOST — first numbers            batch {s['batch_id']}",
        f"planner: {r['planner']}    policy v{s['policy_version']}",
        "=" * 66,
        "",
        f"Invoices worked                        {r['invoices_targeted']:>10,}",
        f"Opening book                           {fmt(r['opening_book_paise']):>16}",
        "",
        f"Actions requested                      {s['actions_requested']:>10,}",
        f"  allowed                              {s['allowed']:>10,}",
        f"  denied                               {s['denied']:>10,}",
        f"  escalated to a human                 {s['needs_human']:>10,}",
        "",
        f"Invoices settled                       {r['settled_count']:>10,}",
        f"Recovered, gross                       {fmt(r['recovered_paise']):>16}",
        f"  would have paid anyway               {fmt(r['spontaneous_paise']):>16}"
        f"   <- {r['spontaneous_count']:,} invoices, not earned",
        f"  attributable to chasing              {fmt(r['earned_paise']):>16}"
        f"   <- {r['earned_count']:,} invoices",
        f"Cost of interventions                  {fmt(spend):>16}",
        "-" * 66,
        f"Net, on the attributable figure        {fmt(r['earned_paise'] - spend):>16}",
        "",
    ]

    if r["denials"]:
        lines.append("Refusals by rule")
        for rule, count in r["denials"].items():
            lines.append(f"  {rule:<26} {count:>8,}")
        lines.append("")

    v = r["verify"]
    lines += [
        f"Ledger: {s['ledger_entries']:,} entries — {'intact' if v.ok else 'BROKEN'}",
        f"head:   {s['ledger_head']}",
        "",
        "NOTE: 'attributable' is the simulator's own bookkeeping, not a measured",
        "uplift, and it is biased upward. A retry or an escalation that lands on",
        "an invoice which would have paid anyway is credited here to chasing,",
        "because with no control group there is no way to tell the two apart.",
        "Day 4's 80/20 treated-vs-holdout comparison is what separates them.",
        "Do not quote this number in the pitch until then.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    setup_console()
    ap = argparse.ArgumentParser(description="Run the recovery agent over a batch.")
    ap.add_argument("--limit", type=int, default=400, help="invoices to work")
    ap.add_argument("--days", type=int, default=5, help="simulated days")
    ap.add_argument("--db", default="data/batch.db")
    ap.add_argument(
        "--llm",
        action="store_true",
        help="use the Gemini planner (needs GEMINI_API_KEY; falls back to rules)",
    )
    args = ap.parse_args()

    result = run(limit=args.limit, days=args.days, db_path=args.db, use_llm=args.llm)
    print(render(result))
    result["ledger"].close()


if __name__ == "__main__":
    main()
