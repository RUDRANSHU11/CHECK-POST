"""In-memory view of the merchant's data.

Deliberately not a database. The dataset is a generated month that fits in
memory several times over, and a replay of 5,000 events wants dictionary lookups
rather than round trips. The *ledger* is the durable artifact here; this is just
the world the agents observe.

Loading ground_truth.json from this class is impossible on purpose — there is no
code path that opens it. Only the replay harness reads that file.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from engine.schema import Customer, Invoice, InvoiceStatus, Payment, PaymentStatus, Settlement


class DataStore:
    def __init__(self, dataset: dict) -> None:
        self.batch_id: str = dataset["batch_id"]
        self.seed: int = dataset["seed"]
        self.customers: dict[str, Customer] = {
            c["customer_id"]: Customer.model_validate(c) for c in dataset["customers"]
        }
        self.invoices: dict[str, Invoice] = {
            i["invoice_id"]: Invoice.model_validate(i) for i in dataset["invoices"]
        }
        self.payments: dict[str, Payment] = {
            p["payment_id"]: Payment.model_validate(p) for p in dataset["payments"]
        }

        self._by_customer: dict[str, list[str]] = {}
        for inv in self.invoices.values():
            self._by_customer.setdefault(inv.customer_id, []).append(inv.invoice_id)

        self._payments_by_invoice: dict[str, list[str]] = {}
        for pay in self.payments.values():
            self._payments_by_invoice.setdefault(pay.invoice_id, []).append(pay.payment_id)

        self._payments_by_customer: dict[str, list[str]] = {}
        for pay in self.payments.values():
            self._payments_by_customer.setdefault(pay.customer_id, []).append(pay.payment_id)

        # .get, not [], so a fixture written before settlements existed still
        # loads. A KeyError here would break every policy test for a reason
        # having nothing to do with policy.
        self.settlements: dict[str, Settlement] = {
            s["settlement_id"]: Settlement.model_validate(s)
            for s in dataset.get("settlements", [])
        }

        #: payment_id -> the settlements claiming it. A list, not a single value:
        #: the same payment appearing in two batches is itself an exception the
        #: reconciler has to be able to report.
        self._settlements_by_payment: dict[str, list[str]] = {}
        for st in self.settlements.values():
            for pid in st.payment_ids:
                self._settlements_by_payment.setdefault(pid, []).append(st.settlement_id)

    @classmethod
    def load(cls, path: str | Path = "data/dataset.json") -> "DataStore":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"{p} not found — run: python -m harness.generate"
            )
        return cls(json.loads(p.read_text(encoding="utf-8")))

    # -- lookups ---------------------------------------------------------- #

    def customer(self, customer_id: str | None) -> Customer | None:
        return self.customers.get(customer_id) if customer_id else None

    def invoice(self, invoice_id: str | None) -> Invoice | None:
        return self.invoices.get(invoice_id) if invoice_id else None

    def payment(self, payment_id: str | None) -> Payment | None:
        return self.payments.get(payment_id) if payment_id else None

    def invoices_for(self, customer_id: str) -> list[Invoice]:
        return [self.invoices[i] for i in self._by_customer.get(customer_id, [])]

    def payments_for(self, invoice_id: str) -> list[Payment]:
        return [self.payments[p] for p in self._payments_by_invoice.get(invoice_id, [])]

    def settlement(self, settlement_id: str | None) -> Settlement | None:
        return self.settlements.get(settlement_id) if settlement_id else None

    def payments_by_customer(self, customer_id: str) -> list[Payment]:
        """Every attempt this customer has made, oldest first.

        The risk signals are all shaped like this — a burst of declines, a young
        account spending large — so they need history, not one payment.
        """
        pays = [self.payments[p] for p in self._payments_by_customer.get(customer_id, [])]
        return sorted(pays, key=lambda p: p.created_at)

    def settlements_for_payment(self, payment_id: str) -> list[str]:
        return self._settlements_by_payment.get(payment_id, [])

    def settlements_on(self, day: "datetime") -> list["Settlement"]:
        """Settlements credited on one calendar day, oldest first."""
        return sorted(
            (s for s in self.settlements.values() if s.settled_at.date() == day.date()),
            key=lambda s: s.settled_at,
        )

    def captured_payments(self) -> list[Payment]:
        """Payments that actually took money. The risk agent's universe: you
        block the transaction that is about to succeed, not one that already
        failed on its own."""
        return [p for p in self.payments.values() if p.status is PaymentStatus.CAPTURED]

    def collectable(self) -> list[Invoice]:
        """Invoices a recovery agent would legitimately look at."""
        return [
            inv
            for inv in self.invoices.values()
            if inv.status in (InvoiceStatus.OPEN, InvoiceStatus.OVERDUE)
        ]

    def __repr__(self) -> str:
        return (
            f"<DataStore {self.batch_id}: {len(self.customers)} customers, "
            f"{len(self.invoices)} invoices, {len(self.payments)} payments, "
            f"{len(self.settlements)} settlements>"
        )
