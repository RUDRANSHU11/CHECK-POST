"""The gateway — the single door between an agent and the merchant's money.

Agents do not act. They ask. Everything they ask for lands here, gets judged by
the rulebook, gets written to the ledger, and comes back as a verdict with a
reason attached.

Two ordering decisions worth stating, because they are what make the log
evidence rather than decoration:

* **The request is written before it is judged.** If the process dies mid-rule,
  the ask is already on record. A log that only records completed decisions can
  be emptied by crashing at the right moment.
* **Counters are rebuilt from the ledger, not kept alongside it.** On startup
  the gateway replays its own log to recover how many times each customer was
  contacted and each invoice chased. The ledger is therefore the only source of
  truth: if the two ever disagreed, the in-memory copy would be the liar, so we
  do not let a second copy exist across restarts.

Only *allowed* actions move any counter. A denied contact did not happen, so it
does not consume the customer's daily quota, and an agent that gets refused is
free to come back with a better-founded request.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from engine import economics, policy, risk_model
from engine.ledger import Ledger
from engine.policy import PolicyContext
from engine.schema import (
    ACTION_COST_PAISE,
    CONTACT_ACTIONS,
    RISK_ACTIONS,
    ActionRequest,
    ActionType,
    Decision,
    Outcome,
    Verdict,
    utcnow,
)
from engine.store import DataStore

CONTACT_WINDOW = timedelta(hours=24)

#: Actions that count as "chasing this invoice" for the attempt limit. Escalating
#: or writing off are how a chase *ends*, so they must not themselves count as
#: another attempt — otherwise the stopping rule would block the only two moves
#: that stop the loop.
ATTEMPT_ACTIONS = CONTACT_ACTIONS | {ActionType.RETRY_CHARGE, ActionType.OFFER_DISCOUNT}


class Gateway:
    def __init__(self, store: DataStore, ledger: Ledger) -> None:
        self.store = store
        self.ledger = ledger

        self._contacts: dict[str, list[datetime]] = {}
        self._attempts: dict[str, int] = {}
        self._spent: dict[str, int] = {}
        self._refunded: dict[str, int] = {}
        self._used_keys: set[str] = set()
        self._approved_requests: set[str] = set()
        self._pending: dict[str, dict] = {}
        self._verdict_counts: dict[str, int] = {v.value: 0 for v in Verdict}
        self._spend_paise = 0

        self._rebuild_from_ledger()

    # ------------------------------------------------------------------ #
    # Recovery of state
    # ------------------------------------------------------------------ #

    def _rebuild_from_ledger(self) -> None:
        """Replay the log to restore every counter. Idempotent by construction."""
        for entry in self.ledger.entries():
            if entry.entry_type == "human_approval":
                self._approved_requests.add(entry.payload["request_id"])
                self._pending.pop(entry.payload["request_id"], None)
            elif entry.entry_type == "human_rejection":
                self._pending.pop(entry.payload["request_id"], None)
            elif entry.entry_type == "decision":
                p = entry.payload
                self._verdict_counts[p["verdict"]] = self._verdict_counts.get(p["verdict"], 0) + 1
                self._track_pending(p)
                if p["verdict"] != Verdict.ALLOW.value:
                    continue
                self._spend_paise += p.get("cost_paise", 0)
                self._apply_allowed(
                    action=ActionType(p["action"]),
                    customer_id=p["customer_id"],
                    invoice_id=p.get("invoice_id"),
                    payment_id=p.get("payment_id"),
                    amount_paise=p.get("amount_paise", 0),
                    cost_paise=p.get("cost_paise", 0),
                    idempotency_key=p.get("idempotency_key"),
                    when=datetime.fromisoformat(p["decided_at"]),
                )

    def _track_pending(self, payload: dict) -> None:
        """Keep the human queue in step with the decisions.

        A needs_human verdict puts the request in the queue; any other verdict
        on the same request_id takes it out. That second half is what handles an
        approval: the agent resubmits under the same id, the rules run again,
        and whatever they say this time resolves the queue entry.
        """
        if payload["verdict"] == Verdict.NEEDS_HUMAN.value:
            self._pending[payload["request_id"]] = payload
        else:
            self._pending.pop(payload["request_id"], None)

    def _apply_allowed(
        self,
        *,
        action: ActionType,
        customer_id: str,
        invoice_id: str | None,
        payment_id: str | None,
        amount_paise: int,
        cost_paise: int,
        idempotency_key: str | None,
        when: datetime,
    ) -> None:
        if action in CONTACT_ACTIONS:
            self._contacts.setdefault(customer_id, []).append(when)
        if invoice_id and action in ATTEMPT_ACTIONS:
            self._attempts[invoice_id] = self._attempts.get(invoice_id, 0) + 1
        if invoice_id and cost_paise:
            self._spent[invoice_id] = self._spent.get(invoice_id, 0) + cost_paise
        if action is ActionType.ISSUE_REFUND and payment_id:
            self._refunded[payment_id] = self._refunded.get(payment_id, 0) + amount_paise
        if idempotency_key:
            self._used_keys.add(idempotency_key)

    # ------------------------------------------------------------------ #
    # Context
    # ------------------------------------------------------------------ #

    def _contacts_in_window(self, customer_id: str, now: datetime) -> int:
        cutoff = now - CONTACT_WINDOW
        return sum(1 for t in self._contacts.get(customer_id, []) if t >= cutoff)

    def _risk_facts(self, req: ActionRequest, payment) -> tuple[float | None, list[str]]:
        """Score the payment ourselves.

        The agent's own score arrives on ``req.evidence`` and is not consulted
        here. That separation is the whole reason a risk agent can be allowed to
        exist: it proposes a block and supplies its reasoning, and the layer
        underneath re-derives the number from the merchant's records before
        anything is refused on the strength of it.
        """
        if req.action not in RISK_ACTIONS or payment is None:
            return None, []
        result = risk_model.score(
            payment,
            self.store.customer(payment.customer_id),
            self.store.payments_by_customer(payment.customer_id),
        )
        return result.score, result.names

    def _settlement_facts(self, req: ActionRequest) -> tuple[object, int, list[str]]:
        """Re-derive the batch total from the merchant's own payment records.

        The bank's ``amount_paise`` is its claim; this is ours. Reconciliation is
        the comparison, so the comparison must not be computed from one side.
        """
        settlement = self.store.settlement(req.settlement_id)
        if settlement is None:
            return None, 0, []
        gross = 0
        unknown: list[str] = []
        for pid in settlement.payment_ids:
            payment = self.store.payment(pid)
            if payment is None:
                unknown.append(pid)
            else:
                gross += payment.amount_paise
        return settlement, gross, unknown

    def build_context(self, req: ActionRequest, now: datetime) -> PolicyContext:
        payment = self.store.payment(req.payment_id)
        risk_score, risk_signals = self._risk_facts(req, payment)
        settlement, gross, unknown = self._settlement_facts(req)
        return PolicyContext(
            now=now,
            customer=self.store.customer(req.customer_id),
            invoice=self.store.invoice(req.invoice_id),
            payment=payment,
            contacts_last_24h=self._contacts_in_window(req.customer_id, now),
            attempts_on_invoice=self._attempts.get(req.invoice_id or "", 0),
            spent_on_invoice=self._spent.get(req.invoice_id or "", 0),
            refunded_paise=self._refunded.get(req.payment_id or "", 0),
            used_idempotency_keys=self._used_keys,
            human_approved=req.request_id in self._approved_requests,
            risk_score=risk_score,
            risk_signals=risk_signals,
            settlement=settlement,
            settlement_gross_paise=gross,
            settlement_unknown_ids=unknown,
        )

    # ------------------------------------------------------------------ #
    # The one public verb
    # ------------------------------------------------------------------ #

    def submit(self, req: ActionRequest, now: datetime | None = None) -> Decision:
        now = now or utcnow()

        # Record the ask first — see module docstring.
        self.ledger.append("request", req.model_dump(mode="json"))

        ctx = self.build_context(req, now)
        verdict, results = policy.decide(req, ctx)

        # The money check runs second and can only tighten the answer. It is
        # skipped once the rulebook has already refused: there is no point
        # pricing an action that is not permitted, and computing an expected
        # return for a message we will never send would put a misleading number
        # in the audit trail.
        assessment = economics.assess(req, ctx) if verdict is Verdict.ALLOW else None
        if assessment is not None and assessment.applicable:
            results = results + [assessment.to_rule_result()]
            if assessment.verdict is not Verdict.ALLOW:
                verdict = assessment.verdict

        decision = Decision(
            decision_id="dec_" + uuid.uuid4().hex[:12],
            request_id=req.request_id,
            verdict=verdict,
            results=results,
            cost_paise=self.cost_of(req) if verdict is Verdict.ALLOW else 0,
            expected_recovery_paise=(
                assessment.expected_recovery_paise if assessment else 0
            ),
            risk_score=ctx.risk_score,
            decided_at=now,
            policy_version=policy.POLICY_VERSION,
        )

        # The decision entry carries enough of the request to rebuild every
        # counter from the log alone, without having to join back to the
        # request entry. Slight duplication, bought deliberately: verification
        # should never depend on two entries agreeing.
        payload = decision.model_dump(mode="json")
        payload.update(
            action=req.action.value,
            agent=req.agent.value,
            customer_id=req.customer_id,
            invoice_id=req.invoice_id,
            payment_id=req.payment_id,
            settlement_id=req.settlement_id,
            amount_paise=req.amount_paise,
            idempotency_key=req.idempotency_key,
            rationale=req.rationale,
        )
        self.ledger.append("decision", payload)
        self._track_pending(payload)

        self._verdict_counts[verdict.value] = self._verdict_counts.get(verdict.value, 0) + 1
        if verdict is Verdict.ALLOW:
            self._spend_paise += decision.cost_paise
            self._apply_allowed(
                action=req.action,
                customer_id=req.customer_id,
                invoice_id=req.invoice_id,
                payment_id=req.payment_id,
                amount_paise=req.amount_paise,
                cost_paise=decision.cost_paise,
                idempotency_key=req.idempotency_key,
                when=now,
            )

        return decision

    @staticmethod
    def cost_of(req: ActionRequest) -> int:
        """What running this action costs the merchant, in paise."""
        base = ACTION_COST_PAISE[req.action]
        if req.action is ActionType.OFFER_DISCOUNT:
            return req.amount_paise
        return base

    # ------------------------------------------------------------------ #
    # Human sign-off and outcomes
    # ------------------------------------------------------------------ #

    def approve(self, request_id: str, approver: str, note: str = "") -> None:
        """Record a human approving a needs_human request.

        Note what this does *not* do: it does not re-run the request. The agent
        must resubmit, and the rules run again with human_approved set. A
        human's signature raises a ceiling; it never bypasses the rulebook, so
        an approval granted yesterday cannot authorise contacting someone who
        opted out this morning.
        """
        self._approved_requests.add(request_id)
        self._pending.pop(request_id, None)
        self.ledger.append(
            "human_approval",
            {"request_id": request_id, "approver": approver, "note": note,
             "recorded_at": utcnow().isoformat()},
        )

    def reject(self, request_id: str, approver: str, note: str = "") -> None:
        """Record a human refusing a needs_human request.

        The mirror of approve(), and deliberately weaker: it takes the request
        out of the queue and signs a row saying who refused it, but it does not
        blacklist anything. A resubmission is judged exactly as it was the first
        time. Without this the queue only ever grows — every escalation a human
        looks at and declines would sit there for good, and a queue that cannot
        be emptied stops being read.
        """
        self._pending.pop(request_id, None)
        self.ledger.append(
            "human_rejection",
            {"request_id": request_id, "approver": approver, "note": note,
             "recorded_at": utcnow().isoformat()},
        )

    def resubmit(self, request_id: str, now: datetime | None = None) -> Decision:
        """Judge a request again, from the ask exactly as it was first made.

        Read back out of the ledger rather than rebuilt from the decision entry,
        which does not carry ``evidence`` — and evidence is one of the places a
        poisoned memo lives. A resubmission assembled from the decision would
        sail past the prompt_injection rule that the original failed, which is
        not a convenience feature, it is the console laundering an attack.

        Nothing here bypasses anything: it calls submit() like any agent would,
        so every rule runs again on the request as it stands now.
        """
        # ponytail: linear scan of the log, once per human click on a queue of
        # tens. Index request_id in SQLite if the queue is ever worked in bulk.
        for entry in reversed(self.ledger.entries("request")):
            if entry.payload["request_id"] == request_id:
                return self.submit(ActionRequest(**entry.payload), now=now)
        raise KeyError(request_id)

    def report_outcome(self, outcome: Outcome) -> None:
        """What actually happened after an allowed action. The scorecard is
        computed from these entries, not from what the agents intended."""
        self.ledger.append("outcome", outcome.model_dump(mode="json"))

    # ------------------------------------------------------------------ #
    # Reporting
    # ------------------------------------------------------------------ #

    def pending(self) -> list[dict]:
        """Every request sitting at needs_human, oldest first.

        The decision payload as it was logged, which already carries the action,
        the amount and the rule results — a human deciding needs the reason the
        rulebook gave, not just the request.
        """
        return list(self._pending.values())

    def stats(self) -> dict:
        total = sum(self._verdict_counts.values())
        return {
            "batch_id": self.store.batch_id,
            "actions_requested": total,
            "allowed": self._verdict_counts.get(Verdict.ALLOW.value, 0),
            "denied": self._verdict_counts.get(Verdict.DENY.value, 0),
            "needs_human": self._verdict_counts.get(Verdict.NEEDS_HUMAN.value, 0),
            "spend_paise": self._spend_paise,
            "ledger_entries": len(self.ledger),
            "ledger_head": self.ledger.head(),
            "policy_version": policy.POLICY_VERSION,
        }

    def denial_breakdown(self) -> dict[str, int]:
        """Which rules are doing the work. Straight from the ledger, so the
        dashboard and an auditor read the same numbers."""
        counts: dict[str, int] = {}
        for entry in self.ledger.entries(entry_type="decision"):
            if entry.payload["verdict"] == Verdict.ALLOW.value:
                continue
            for r in entry.payload.get("results", []):
                if r["verdict"] != Verdict.ALLOW.value:
                    counts[r["rule_id"]] = counts.get(r["rule_id"], 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
