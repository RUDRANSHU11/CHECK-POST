"""Checkpost event schema — the contract every other module depends on.

Two rules hold across the whole codebase:

1. Money is an integer count of paise. Never a float, never rupees. A demo whose
   headline claim is "honest numbers" cannot afford float drift across a sum of
   5,000 amounts, and 0.1 + 0.2 is exactly the bug a payments judge looks for.
2. Every timestamp is timezone-aware UTC. Quiet-hours rules need a real clock,
   and naive datetimes compare wrong without saying so.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Money
# --------------------------------------------------------------------------- #


def rupees(amount: float) -> int:
    """Rupees -> paise. Use at the edges only; internals stay in paise."""
    return int(round(amount * 100))


def fmt(paise: int) -> str:
    """Paise -> a display string like the scorecard's, with Indian grouping."""
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), 100)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return f"{sign}₹ {s}.{frac:02d}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class Channel(str, Enum):
    SMS = "sms"
    EMAIL = "email"
    WHATSAPP = "whatsapp"
    VOICE = "voice"


class ActionType(str, Enum):
    """Everything an agent may ask for. Nothing outside this list exists."""

    # recovery agent
    SEND_SMS = "send_sms"
    SEND_EMAIL = "send_email"
    SEND_WHATSAPP = "send_whatsapp"
    PLACE_CALL = "place_call"
    RETRY_CHARGE = "retry_charge"
    OFFER_DISCOUNT = "offer_discount"
    ISSUE_REFUND = "issue_refund"
    WRITE_OFF = "write_off"
    # risk agent
    BLOCK_ORDER = "block_order"
    FLAG_FOR_REVIEW = "flag_for_review"
    # reconcile agent
    MATCH_SETTLEMENT = "match_settlement"
    # any agent
    ESCALATE_TO_HUMAN = "escalate_to_human"


class Verdict(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    NEEDS_HUMAN = "needs_human"


class InvoiceStatus(str, Enum):
    OPEN = "open"
    PAID = "paid"
    OVERDUE = "overdue"
    DISPUTED = "disputed"
    WRITTEN_OFF = "written_off"


class PaymentStatus(str, Enum):
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"


class PaymentMethod(str, Enum):
    CARD = "card"
    UPI = "upi"
    NETBANKING = "netbanking"
    WALLET = "wallet"


class FailureReason(str, Enum):
    INSUFFICIENT_FUNDS = "insufficient_funds"
    CARD_EXPIRED = "card_expired"
    DO_NOT_HONOUR = "do_not_honour"
    BANK_DOWN = "bank_down"
    NETWORK_TIMEOUT = "network_timeout"
    LIMIT_EXCEEDED = "limit_exceeded"
    INCORRECT_OTP = "incorrect_otp"


#: Failures worth retrying at all. A retry on an expired card is pure cost with a
#: guaranteed zero return — the economics gate leans on this.
RETRYABLE_FAILURES: frozenset[FailureReason] = frozenset(
    {
        FailureReason.INSUFFICIENT_FUNDS,
        FailureReason.BANK_DOWN,
        FailureReason.NETWORK_TIMEOUT,
        FailureReason.INCORRECT_OTP,
    }
)


class AgentName(str, Enum):
    RECOVERY = "recovery"
    RISK = "risk"
    RECONCILE = "reconcile"


class OutcomeResult(str, Enum):
    RECOVERED = "recovered"
    PARTIAL = "partial"
    NO_RESPONSE = "no_response"
    BOUNCED = "bounced"
    FAILED = "failed"
    REFUNDED = "refunded"


# --------------------------------------------------------------------------- #
# Price list
# --------------------------------------------------------------------------- #
# Kept next to ActionType on purpose: a new action type with no price should fail
# at import, not silently cost nothing at run time. The *logic* that spends this
# money lives in economics.py — this is only the tariff.

ACTION_COST_PAISE: dict[ActionType, int] = {
    ActionType.SEND_SMS: 25,  # 0.25
    ActionType.SEND_EMAIL: 5,  # 0.05
    ActionType.SEND_WHATSAPP: 75,  # 0.75
    ActionType.PLACE_CALL: 450,  # 4.50
    ActionType.RETRY_CHARGE: 200,  # 2.00 gateway fee on the attempt itself
    ActionType.OFFER_DISCOUNT: 0,  # priced from the discount amount
    ActionType.ISSUE_REFUND: 0,  # cost is the refunded money
    ActionType.WRITE_OFF: 0,  # cost is the written-off money
    ActionType.BLOCK_ORDER: 0,  # cost is the lost sale, when wrong
    ActionType.FLAG_FOR_REVIEW: 1500,  # 15.00 of analyst time
    ActionType.MATCH_SETTLEMENT: 0,
    ActionType.ESCALATE_TO_HUMAN: 5000,  # 50.00 of human time
}

assert set(ACTION_COST_PAISE) == set(ActionType), "every ActionType needs a price"

#: Actions that reach a human being. Opt-out, contact caps and quiet hours apply
#: to exactly these and nothing else.
CONTACT_ACTIONS: frozenset[ActionType] = frozenset(
    {
        ActionType.SEND_SMS,
        ActionType.SEND_EMAIL,
        ActionType.SEND_WHATSAPP,
        ActionType.PLACE_CALL,
    }
)

#: Actions that move money out of the merchant's account.
MONEY_OUT_ACTIONS: frozenset[ActionType] = frozenset(
    {
        ActionType.ISSUE_REFUND,
        ActionType.OFFER_DISCOUNT,
        ActionType.WRITE_OFF,
    }
)

ACTION_CHANNEL: dict[ActionType, Channel] = {
    ActionType.SEND_SMS: Channel.SMS,
    ActionType.SEND_EMAIL: Channel.EMAIL,
    ActionType.SEND_WHATSAPP: Channel.WHATSAPP,
    ActionType.PLACE_CALL: Channel.VOICE,
}


# --------------------------------------------------------------------------- #
# Domain objects
# --------------------------------------------------------------------------- #


class Base(BaseModel):
    # extra="forbid" so a typo'd field in an agent payload is a loud 422 rather
    # than a value that silently never reaches a rule.
    model_config = ConfigDict(extra="forbid")


class Customer(Base):
    customer_id: str
    name: str
    email: str
    phone: str
    created_at: datetime
    #: Set when the customer replied STOP or unsubscribed. Permanent, by policy.
    opted_out_at: datetime | None = None
    opt_out_channel: Channel | None = None

    @property
    def opted_out(self) -> bool:
        return self.opted_out_at is not None


class Invoice(Base):
    invoice_id: str
    customer_id: str
    amount_paise: int
    issued_at: datetime
    due_at: datetime
    status: InvoiceStatus = InvoiceStatus.OPEN
    #: Free text, usually lifted straight out of an uploaded PDF. This is the
    #: untrusted field — the red-team injection arrives here.
    memo: str = ""

    def is_overdue(self, now: datetime) -> bool:
        return (
            self.status in (InvoiceStatus.OPEN, InvoiceStatus.OVERDUE)
            and now > self.due_at
        )


class Payment(Base):
    payment_id: str
    invoice_id: str
    customer_id: str
    amount_paise: int
    method: PaymentMethod
    status: PaymentStatus
    created_at: datetime
    failure_reason: FailureReason | None = None

    @property
    def is_retryable(self) -> bool:
        return (
            self.status is PaymentStatus.FAILED
            and self.failure_reason in RETRYABLE_FAILURES
        )


class ActionRequest(Base):
    """What an agent asks Checkpost for permission to do.

    ``evidence`` is whatever the agent read to reach its conclusion. It is
    untrusted by construction: if a poisoned invoice memo reached the agent's
    context it shows up here, which is what lets the policy engine catch the
    injection rather than trusting the agent's own summary of its reasoning.
    """

    request_id: str
    agent: AgentName
    action: ActionType
    customer_id: str
    invoice_id: str | None = None
    payment_id: str | None = None
    #: Money the action moves (refund, discount). Zero for messages.
    amount_paise: int = 0
    #: The agent's plain-language justification, shown in the live decision feed.
    rationale: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    requested_at: datetime = Field(default_factory=utcnow)
    #: Same key twice means the same intent. This is what stops double refunds.
    idempotency_key: str | None = None


class RuleResult(Base):
    """One rule's opinion. The engine collects these and takes the worst."""

    rule_id: str
    verdict: Verdict
    reason: str


class Decision(Base):
    decision_id: str
    request_id: str
    verdict: Verdict
    #: Every rule that fired, in evaluation order. Kept in full even when the
    #: verdict is allow — "nothing objected" is itself auditable evidence.
    results: list[RuleResult] = Field(default_factory=list)
    cost_paise: int = 0
    expected_recovery_paise: int = 0
    decided_at: datetime = Field(default_factory=utcnow)
    policy_version: str = "0"

    @property
    def blocking_reasons(self) -> list[str]:
        return [r.reason for r in self.results if r.verdict is not Verdict.ALLOW]


class Outcome(Base):
    """What actually happened after an allowed action.

    Without this the ledger records intentions only, and the scorecard cannot be
    computed at all.
    """

    outcome_id: str
    request_id: str
    result: OutcomeResult
    amount_recovered_paise: int = 0
    occurred_at: datetime = Field(default_factory=utcnow)


class LedgerEntry(Base):
    seq: int
    entry_type: str
    payload: dict[str, Any]
    recorded_at: datetime
    prev_hash: str
    entry_hash: str
