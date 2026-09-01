"""The reconciler.

Takes the acquirer's settlement batches, checks each one against the merchant's
own payment records, and asks the gateway to mark the ones that agree. The ones
that do not agree it hands to a human, named — never rounded away.

The failure mode this is built against
--------------------------------------
Reconciliation goes wrong quietly. A reconciler that marks everything matched
produces a clean report, balanced books, and a hole in the merchant's cash
position that surfaces a quarter later. So this agent does its own arithmetic
and says what it found, and the gateway does the *same* arithmetic independently
before allowing any match to be recorded (``r16_settlement_discrepancy``). Two
sums that must agree before the books move.

That redundancy is not paranoia about this file. It is what makes it safe to
swap this agent for an LLM later: whatever a model decides a settlement means,
the layer underneath re-derives the total from the payment records and refuses
anything that does not reconcile to the rupee.

The four disagreements
----------------------
``unknown_payment``   the bank claims a payment the merchant has no record of
``amount_mismatch``   the net credited is not gross minus the stated fee
``duplicate_payment`` a payment claimed by two batches
``unsettled``         a captured payment that appears in no batch at all — the
                      merchant is owed money and nobody has noticed

The last one is the one an agent working batch-by-batch would never find, which
is why it is swept separately at the end of a run rather than per settlement.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from engine.gateway import Gateway
from engine.schema import (
    ActionRequest,
    ActionType,
    AgentName,
    Decision,
    Settlement,
    fmt,
)
from engine.store import DataStore

#: Rounding slack on the fee arithmetic, matching the rulebook's own tolerance.
#: A rupee, not a percentage — a tolerance that scales with the batch is a
#: tolerance that hides a large error inside a large batch.
TOLERANCE_PAISE = 100

#: A captured payment older than this with no settlement against it is money the
#: merchant is owed. Before this it is simply in flight: acquirers pay T+1, and
#: flagging a payment made yesterday would bury the real cases in noise.
UNSETTLED_AFTER = timedelta(days=3)


@dataclass(frozen=True)
class Exception_:  # pylint: disable=invalid-name
    """One thing the reconciler could not resolve.

    Named ``Exception_`` because it is emphatically not a Python exception: it
    is a line on a report a human works through on Monday morning.
    """

    kind: str
    reference: str
    amount_paise: int
    detail: str

    def __str__(self) -> str:
        return f"{self.kind:<18} {self.reference:<12} {fmt(self.amount_paise):>16}  {self.detail}"


@dataclass
class ReconcileAgent:
    store: DataStore
    gateway: Gateway
    #: Everything that did not reconcile, in the order it was found.
    exceptions: list[Exception_] = field(default_factory=list)
    #: Settlement ids already worked.
    seen: set[str] = field(default_factory=set)
    #: payment_id -> the settlement that first claimed it, for duplicate hunting.
    claimed_by: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # One settlement
    # ------------------------------------------------------------------ #

    def examine(self, settlement: Settlement) -> tuple[int, list[str], list[str]]:
        """Our side of the comparison: gross, unknown ids, duplicate ids.

        Computed from the merchant's payment records only. The bank's stated
        total is not an input here — it is the thing being checked.
        """
        gross = 0
        unknown: list[str] = []
        duplicates: list[str] = []
        for pid in settlement.payment_ids:
            payment = self.store.payment(pid)
            if payment is None:
                unknown.append(pid)
                continue
            first = self.claimed_by.get(pid)
            if first is not None and first != settlement.settlement_id:
                duplicates.append(pid)
            else:
                self.claimed_by[pid] = settlement.settlement_id
            gross += payment.amount_paise
        return gross, unknown, duplicates

    def work(
        self, settlement: Settlement, now: datetime
    ) -> tuple[ActionRequest, Decision] | None:
        if settlement.settlement_id in self.seen:
            return None
        self.seen.add(settlement.settlement_id)

        gross, unknown, duplicates = self.examine(settlement)
        expected_net = gross - settlement.fee_paise
        gap = settlement.amount_paise - expected_net

        problem: str | None = None
        detail = ""
        if unknown:
            problem = "unknown_payment"
            detail = (
                f"{settlement.utr} claims {len(unknown)} payment(s) we have no record "
                f"of: {', '.join(unknown[:3])}"
            )
        elif duplicates:
            problem = "duplicate_payment"
            detail = (
                f"{settlement.utr} claims {len(duplicates)} payment(s) already settled "
                f"in an earlier batch: {', '.join(duplicates[:3])}"
            )
        elif abs(gap) > TOLERANCE_PAISE:
            problem = "amount_mismatch"
            direction = "short" if gap < 0 else "over"
            detail = (
                f"{settlement.utr} credited {fmt(settlement.amount_paise)}; our records "
                f"make it {fmt(expected_net)} — the bank is {fmt(abs(gap))} {direction}"
            )

        if problem is None:
            action = ActionType.MATCH_SETTLEMENT
            rationale = (
                f"{settlement.utr}: {len(settlement.payment_ids)} payments totalling "
                f"{fmt(gross)} gross, less {fmt(settlement.fee_paise)} fee, is the "
                f"{fmt(settlement.amount_paise)} credited"
            )
        else:
            self.exceptions.append(
                Exception_(problem, settlement.settlement_id, abs(gap) or gross, detail)
            )
            action = ActionType.ESCALATE_TO_HUMAN
            rationale = f"cannot reconcile {settlement.settlement_id}: {detail}"

        request = ActionRequest(
            request_id="rq_" + uuid.uuid4().hex[:12],
            agent=AgentName.RECONCILE,
            action=action,
            # Settlements belong to the merchant, not to one customer. The field
            # is required, so it carries the batch rather than a fictional buyer.
            customer_id=f"merchant:{self.store.batch_id}",
            settlement_id=settlement.settlement_id,
            rationale=rationale,
            idempotency_key=f"{settlement.settlement_id}:{action.value}",
        )
        return request, self.gateway.submit(request, now=now)

    # ------------------------------------------------------------------ #
    # The sweep no batch-by-batch pass can do
    # ------------------------------------------------------------------ #

    def sweep_unsettled(self, now: datetime) -> list[Exception_]:
        """Captured payments that appear in no batch at all.

        Nothing about any single settlement reveals these — they are defined by
        absence, so they can only be found by walking the merchant's own book
        and asking what never came back.
        """
        found = []
        for payment in self.store.captured_payments():
            if now - payment.created_at < UNSETTLED_AFTER:
                continue  # still legitimately in flight
            if self.store.settlements_for_payment(payment.payment_id):
                continue
            found.append(
                Exception_(
                    "unsettled",
                    payment.payment_id,
                    payment.amount_paise,
                    f"captured {payment.created_at:%d %b} and never settled by the bank",
                )
            )
        self.exceptions.extend(found)
        return found

    # ------------------------------------------------------------------ #

    def run(
        self, settlements: list[Settlement], now: datetime
    ) -> list[tuple[ActionRequest, Decision]]:
        out = []
        for settlement in settlements:
            attempt = self.work(settlement, now)
            if attempt is not None:
                out.append(attempt)
        return out

    def exception_summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.exceptions:
            counts[e.kind] = counts.get(e.kind, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def exception_value_paise(self) -> int:
        """Money sitting in the exception list. The number a CFO asks for."""
        return sum(e.amount_paise for e in self.exceptions)
