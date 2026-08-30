"""HTTP surface over the gateway.

Kept separate from gateway.py on purpose. The replay harness pushes thousands of
requests through Gateway.submit() in-process — putting HTTP in that path would
add nothing but latency and a second failure mode. So the class holds the logic
and this module is a thin translation layer: parse, call, serialise.

Run it with:  python -m uvicorn engine.api:app --reload
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from engine import policy
from engine.gateway import Gateway
from engine.ledger import Ledger
from engine.schema import ActionRequest, Decision, Outcome
from engine.store import DataStore

_gateway: Gateway | None = None


def get_gateway() -> Gateway:
    if _gateway is None:
        raise HTTPException(503, "gateway not initialised")
    return _gateway


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Read paths at startup, not at import. Import-time config cannot be pointed
    # at a temp directory by a test without reimporting the module, and a server
    # whose data location is fixed the moment Python parses the file is awkward
    # to run twice on one machine.
    global _gateway
    dataset_path = os.getenv("CHECKPOST_DATASET", "data/dataset.json")
    db_path = os.getenv("CHECKPOST_DB", "data/checkpost.db")
    if not Path(dataset_path).exists():
        raise RuntimeError(
            f"{dataset_path} missing — run 'python -m harness.generate' before serving"
        )
    _gateway = Gateway(DataStore.load(dataset_path), Ledger(db_path))
    yield
    if _gateway is not None:
        _gateway.ledger.close()
        _gateway = None


app = FastAPI(
    title="Checkpost",
    version=policy.POLICY_VERSION,
    summary="A safety checkpoint between an AI agent and a company's money.",
    lifespan=lifespan,
)


class ApprovalIn(BaseModel):
    approver: str
    note: str = ""


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "policy_version": policy.POLICY_VERSION}


@app.post("/v1/actions", response_model=Decision)
def submit_action(req: ActionRequest) -> Decision:
    """The only way an agent gets to do anything."""
    return get_gateway().submit(req)


@app.post("/v1/actions/{request_id}/approve")
def approve_action(request_id: str, body: ApprovalIn) -> dict:
    get_gateway().approve(request_id, body.approver, body.note)
    return {"request_id": request_id, "approved_by": body.approver}


@app.post("/v1/outcomes")
def report_outcome(outcome: Outcome) -> dict:
    get_gateway().report_outcome(outcome)
    return {"recorded": outcome.outcome_id}


@app.get("/v1/stats")
def stats() -> dict:
    gw = get_gateway()
    return {**gw.stats(), "denials_by_rule": gw.denial_breakdown()}


@app.get("/v1/rules")
def rules() -> dict:
    return {
        "policy_version": policy.POLICY_VERSION,
        "rules": policy.rule_ids(),
        "thresholds": {
            "max_contacts_per_24h": policy.MAX_CONTACTS_PER_24H,
            "max_recovery_attempts": policy.MAX_RECOVERY_ATTEMPTS,
            "refund_human_threshold_paise": policy.REFUND_HUMAN_THRESHOLD_PAISE,
            "write_off_human_threshold_paise": policy.WRITE_OFF_HUMAN_THRESHOLD_PAISE,
            "quiet_hours_ist": f"{policy.QUIET_START_HOUR:02d}:00-{policy.QUIET_END_HOUR:02d}:00",
        },
    }


@app.get("/v1/ledger")
def ledger_tail(limit: int = 50) -> dict:
    """The live decision feed the dashboard renders."""
    entries = get_gateway().ledger.entries()[-limit:]
    return {"count": len(entries), "entries": [e.model_dump(mode="json") for e in entries]}


@app.get("/v1/ledger/verify")
def ledger_verify() -> dict:
    result = get_gateway().ledger.verify()
    return {
        "ok": result.ok,
        "entries_checked": result.entries_checked,
        "broken_seq": result.broken_seq,
        "detail": result.detail or str(result),
        "head": get_gateway().ledger.head(),
    }
