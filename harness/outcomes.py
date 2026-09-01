"""What actually happened after an allowed action.

This module reads ``data/ground_truth.json``. It is the only module that may,
and it lives in ``harness/`` rather than ``engine/`` or ``agents/`` for exactly
that reason — the import graph is the enforcement.

The simulation is deliberately simple and deliberately honest:

* an invoice whose truth says ``would_pay_anyway`` pays on its own schedule,
  whether or not anybody chased it. **These recoveries get credited to a chase
  that did not cause them**, which is the illusion the day-4 holdout exists to
  strip out. The simulator has to produce the illusion or there would be nothing
  to correct.
* an invoice whose truth says ``pays_if_contacted`` pays only once it has been
  contacted ``contacts_needed`` times. This is the only genuinely earned money.
* everything else never pays, no matter how much is spent on it.

Randomness is seeded per invoice id, so a rerun of the same batch produces the
same outcomes and the scorecard is reproducible.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from engine.economics import REVIEW_CATCH_RATE
from engine.schema import (
    ActionRequest,
    ActionType,
    CONTACT_ACTIONS,
    Decision,
    InvoiceStatus,
    Outcome,
    OutcomeResult,
    Verdict,
)
from engine.store import DataStore

#: Chance a human working the account closes it, given the customer was ever
#: going to pay. Escalation is expensive but it is not magic.
ESCALATION_CLOSE_RATE = 0.65

#: Chance a retry lands, given the customer had the money. Below 1.0 because a
#: transient decline can recur.
RETRY_LAND_RATE = 0.75

#: Chance a would-pay-anyway invoice happens to settle during the window we are
#: watching, absent any contact at all.
SPONTANEOUS_RATE = 0.55


@dataclass
class OutcomeSimulator:
    store: DataStore
    truth: dict
    #: The invoices this run is allowed to touch. Everything outside it is left
    #: alone — an earlier version settled the whole book spontaneously while only
    #: subtracting the batch's share, which silently inflated the recovery figure
    #: with money from invoices nobody in the run had even looked at.
    scope: set[str] | None = None
    #: Contacts actually delivered per invoice.
    delivered: dict[str, int] = field(default_factory=dict)
    #: invoice_id -> (paise, "spontaneous" | "chased"). Attribution is recorded at
    #: the moment of settlement rather than reconstructed by subtraction later,
    #: because subtraction is what got it wrong the first time.
    recovered: dict[str, tuple[int, str]] = field(default_factory=dict)

    @classmethod
    def load(
        cls,
        store: DataStore,
        path: str | Path = "data/ground_truth.json",
        scope: set[str] | None = None,
    ) -> "OutcomeSimulator":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"{p} not found — run: python -m harness.generate")
        return cls(
            store=store, truth=json.loads(p.read_text(encoding="utf-8")), scope=scope
        )

    # -- accounting ------------------------------------------------------ #

    @property
    def settled(self) -> set[str]:
        return set(self.recovered)

    def total_paise(self, cause: str | None = None) -> int:
        return sum(
            amount for amount, why in self.recovered.values() if cause is None or why == cause
        )

    def count(self, cause: str | None = None) -> int:
        return sum(1 for _, why in self.recovered.values() if cause is None or why == cause)

    def _in_scope(self, invoice_id: str) -> bool:
        return self.scope is None or invoice_id in self.scope

    def _rng(self, invoice_id: str, salt: str) -> random.Random:
        return random.Random(f"{self.truth['seed']}:{invoice_id}:{salt}")

    def _facts(self, invoice_id: str) -> dict | None:
        return self.truth["invoices"].get(invoice_id)

    # ------------------------------------------------------------------ #

    def observe(
        self, req: ActionRequest, decision: Decision, now: datetime
    ) -> Outcome | None:
        """Resolve one allowed action into what happened.

        Returns None for refused actions — a denial has no outcome, which is the
        whole reason denials are cheap.
        """
        if decision.verdict is not Verdict.ALLOW:
            return None
        invoice_id = req.invoice_id
        if not invoice_id or invoice_id in self.recovered or not self._in_scope(invoice_id):
            return None
        facts = self._facts(invoice_id)
        invoice = self.store.invoice(invoice_id)
        if facts is None or invoice is None:
            return None

        will_pay_eventually = facts["would_pay_anyway"] or facts["pays_if_contacted"]

        if req.action in CONTACT_ACTIONS:
            n = self.delivered.get(invoice_id, 0) + 1
            self.delivered[invoice_id] = n
            if facts["pays_if_contacted"] and n >= facts["contacts_needed"]:
                return self._settle(req, invoice, now, OutcomeResult.RECOVERED)
            return self._nothing(req, now, OutcomeResult.NO_RESPONSE)

        if req.action is ActionType.RETRY_CHARGE:
            if will_pay_eventually and self._rng(invoice_id, "retry").random() < RETRY_LAND_RATE:
                return self._settle(req, invoice, now, OutcomeResult.RECOVERED)
            return self._nothing(req, now, OutcomeResult.FAILED)

        if req.action is ActionType.ESCALATE_TO_HUMAN:
            if will_pay_eventually and self._rng(invoice_id, "esc").random() < ESCALATION_CLOSE_RATE:
                return self._settle(req, invoice, now, OutcomeResult.RECOVERED)
            return self._nothing(req, now, OutcomeResult.NO_RESPONSE)

        if req.action is ActionType.OFFER_DISCOUNT:
            if will_pay_eventually:
                return self._settle(
                    req, invoice, now, OutcomeResult.PARTIAL, minus=req.amount_paise
                )
            return self._nothing(req, now, OutcomeResult.NO_RESPONSE)

        return self._nothing(req, now, OutcomeResult.NO_RESPONSE)

    # ------------------------------------------------------------------ #

    def settle_spontaneously(
        self, now: datetime, day: int = 0, total_days: int = 1
    ) -> list[tuple[str, int]]:
        """Invoices that pay with no intervention at all.

        Run at the start of each simulated day. These are the recoveries a naive
        scorecard proudly claims credit for.

        Two separate draws, and the second one matters more than it looks:

        1. *Does* this invoice settle on its own during the window?
        2. *Which day* does it land on?

        Without the second draw every spontaneous payment arrives on day one,
        before the agent has chased anybody — so the simulation would never
        produce the case the whole project is about: money spent chasing someone
        who was going to pay regardless. Spreading them across the window means
        some get chased first, and that wasted spend shows up in the cost line
        where it belongs.
        """
        paid = []
        for invoice in self.store.collectable():
            iid = invoice.invoice_id
            if not self._in_scope(iid) or iid in self.recovered:
                continue
            facts = self._facts(iid)
            if not facts or not facts["would_pay_anyway"]:
                continue
            if self._rng(iid, "spontaneous").random() >= SPONTANEOUS_RATE:
                continue  # this one never settles on its own
            if self._rng(iid, "spontaneous_day").randrange(max(1, total_days)) != day:
                continue  # settles, but not today
            invoice.status = InvoiceStatus.PAID
            self.recovered[iid] = (invoice.amount_paise, "spontaneous")
            paid.append((iid, invoice.amount_paise))
        return paid

    # ------------------------------------------------------------------ #

    def _settle(self, req, invoice, now, result, minus: int = 0) -> Outcome:
        amount = invoice.amount_paise - minus
        # Mark it paid in the store, so the rulebook starts refusing further
        # chasing on it — r10_settled_invoice. Without this the simulation would
        # keep dunning people who have already paid, which is the exact bug the
        # rule exists to prevent.
        invoice.status = InvoiceStatus.PAID
        self.recovered[invoice.invoice_id] = (amount, "chased")
        return Outcome(
            outcome_id="out_" + req.request_id.split("_", 1)[-1],
            request_id=req.request_id,
            result=result,
            amount_recovered_paise=amount,
            occurred_at=now,
        )

    @staticmethod
    def _nothing(req, now, result) -> Outcome:
        return Outcome(
            outcome_id="out_" + req.request_id.split("_", 1)[-1],
            request_id=req.request_id,
            result=result,
            amount_recovered_paise=0,
            occurred_at=now,
        )


# --------------------------------------------------------------------------- #
# What a block or a review was actually worth
# --------------------------------------------------------------------------- #


@dataclass
class RiskOutcomeSimulator:
    """Resolves risk actions against the truth about who was really a fraudster.

    The asymmetry this exists to record: a block that was right prevents a loss,
    and a block that was wrong destroys a sale. Both are money, both belong on
    the scorecard, and a fraud system that reports only the first is the reason
    nobody trusts fraud numbers.

    A *flag* is deliberately cheap in both directions. An analyst who waves a
    genuine customer through costs the merchant fifteen rupees and nothing else,
    which is exactly why the middle of the score range routes here instead of to
    a block.
    """

    truth: dict
    prevented_paise: int = 0
    lost_sale_paise: int = 0
    blocked_fraud: int = 0
    blocked_genuine: int = 0
    reviewed_fraud: int = 0
    reviewed_clean: int = 0
    #: Payments the risk agent looked at, so "what got through" can be counted
    #: over the population that was actually screened rather than the whole book.
    judged: set[str] = field(default_factory=set)
    #: Fraud that was screened and stopped, by payment id.
    stopped: set[str] = field(default_factory=set)

    def _is_fraud(self, payment_id: str) -> bool:
        return bool(self.truth.get("payments", {}).get(payment_id, {}).get("is_fraud"))

    def _rng(self, payment_id: str) -> random.Random:
        return random.Random(f"{self.truth['seed']}:review:{payment_id}")

    def observe(
        self, req: ActionRequest, decision: Decision, now: datetime
    ) -> Outcome | None:
        if decision.verdict is not Verdict.ALLOW or not req.payment_id:
            return None

        pid = req.payment_id
        self.judged.add(pid)
        fraud = self._is_fraud(pid)

        if req.action is ActionType.BLOCK_ORDER:
            if fraud:
                self.prevented_paise += req.amount_paise
                self.blocked_fraud += 1
                self.stopped.add(pid)
                result = OutcomeResult.PREVENTED_FRAUD
            else:
                self.lost_sale_paise += req.amount_paise
                self.blocked_genuine += 1
                result = OutcomeResult.LOST_SALE
            return Outcome(
                outcome_id="out_" + req.request_id.split("_", 1)[-1],
                request_id=req.request_id,
                result=result,
                amount_recovered_paise=req.amount_paise if fraud else 0,
                occurred_at=now,
            )

        if req.action is ActionType.FLAG_FOR_REVIEW:
            if fraud and self._rng(pid).random() < REVIEW_CATCH_RATE:
                self.prevented_paise += req.amount_paise
                self.reviewed_fraud += 1
                self.stopped.add(pid)
                result = OutcomeResult.REVIEWED_FRAUD
                recovered = req.amount_paise
            else:
                self.reviewed_clean += 1
                result = OutcomeResult.REVIEWED_CLEAN
                recovered = 0
            return Outcome(
                outcome_id="out_" + req.request_id.split("_", 1)[-1],
                request_id=req.request_id,
                result=result,
                amount_recovered_paise=recovered,
                occurred_at=now,
            )

        return None

    def missed(self, screened: list) -> tuple[int, int]:
        """Fraud that went through: (count, paise).

        Counted over the payments the agent actually screened. Charging it for
        fraud on payments nobody showed it would be measuring the harness, not
        the agent.
        """
        count = 0
        total = 0
        for payment in screened:
            pid = payment.payment_id
            if self._is_fraud(pid) and pid not in self.stopped:
                count += 1
                total += payment.amount_paise
        return count, total


#: How often the person working the fraud queue reaches the right answer. Not
#: 1.0: a human reviewer is better than the model on the cases the model was
#: unsure about, and still wrong sometimes. Setting it to 1.0 would make
#: escalation a free correctness oracle and every ceiling in the rulebook would
#: look costless, which is the opposite of the point.
ANALYST_ACCURACY = 0.85


@dataclass
class HumanReviewer:
    """The person who works the escalation queue overnight.

    Only the *fraud* queue. A block that needs a signature is time-critical —
    the payment is settling now — so it is reviewed within a day or the decision
    makes itself. Escalated recovery work is not like that: it goes on a list,
    and the scorecard reports it as pending rather than pretending somebody
    cleared it. Simulating a human who promptly resolves everything would quietly
    delete the largest real cost of an escalation, which is that it waits.

    Reads ground truth, which is why it lives in ``harness/``.
    """

    truth: dict
    accuracy: float = ANALYST_ACCURACY
    approved: int = 0
    rejected: int = 0

    def _rng(self, payment_id: str) -> random.Random:
        """Seeded on the payment, never on the request.

        ``request_id`` is a uuid4 minted fresh on every run, so an analyst keyed
        on it decides differently each time and the scorecard stops being
        reproducible — the fraud figures moved by nearly a lakh between two runs
        of the same seed before this was found. Anything that *decides* an
        outcome has to key on something the dataset fixes.
        """
        return random.Random(f"{self.truth['seed']}:analyst:{payment_id}")

    def approves_block(self, req: ActionRequest) -> bool:
        """Would this analyst sign off on blocking that payment?

        They are right ``accuracy`` of the time, in both directions: they clear
        genuine fraud, and they occasionally wave through a real customer's
        payment into a block. The second kind is where a false positive that no
        model produced comes from.
        """
        fraud = bool(
            self.truth.get("payments", {}).get(req.payment_id, {}).get("is_fraud")
        )
        correct = self._rng(req.payment_id or "").random() < self.accuracy
        decision = fraud if correct else not fraud
        if decision:
            self.approved += 1
        else:
            self.rejected += 1
        return decision
