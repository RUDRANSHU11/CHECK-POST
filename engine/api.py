"""HTTP surface over the gateway.

Kept separate from gateway.py on purpose. The replay harness pushes thousands of
requests through Gateway.submit() in-process — putting HTTP in that path would
add nothing but latency and a second failure mode. So the class holds the logic
and this module is a thin translation layer: parse, call, serialise.

Run it with:  python -m uvicorn engine.api:app --reload

Serves the dashboard at ``/`` as well as the JSON API. One process, no build
step: the page is a single static file in ``web/`` that fetches these same
endpoints. Point the server at a run you want to look at::

    CHECKPOST_DB=data/replay.db python -m uvicorn engine.api:app
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from engine import policy
from engine.gateway import Gateway
from engine.ledger import Ledger
from engine.schema import ActionRequest, Decision, Outcome
from engine.store import DataStore

#: uvicorn configures this logger, so the startup line lands in the same stream
#: as its own, rather than in a bare print that scrolls past unformatted.
log = logging.getLogger("uvicorn.error")

# Not a constant — a singleton the lifespan hook publishes once at startup.
_gateway: Gateway | None = None  # pylint: disable=invalid-name


#: The dashboard, and the file the harness writes for it to read.
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
SCORECARD_PATH = Path(os.getenv("CHECKPOST_SCORECARD", "out/scorecard.json"))


def pick_db() -> tuple[str, str]:
    """Which ledger to serve, and a line saying why.

    An explicit CHECKPOST_DB always wins. Failing that, the newest database in
    data/ that actually holds entries — because the old default was
    data/checkpost.db, the one file that is reliably empty. Starting the server
    the obvious way therefore served a blank dashboard, reported nothing wrong,
    and the cure was an environment variable documented 400 lines into the
    README. A tool that is silently useless until you read the manual is worse
    than one that says what it picked.
    """
    explicit = os.getenv("CHECKPOST_DB")
    if explicit:
        return explicit, f"ledger: {explicit} (from CHECKPOST_DB)"

    runs = []
    for path in Path("data").glob("*.db"):
        try:
            led = Ledger(path)
            count = len(led)
            led.close()
        except sqlite3.DatabaseError:
            continue  # not one of ours; a stray .db is not a reason to fail boot
        if count:
            runs.append((path.stat().st_mtime, count, path))
    if not runs:
        return "data/checkpost.db", (
            "ledger: none found in data/ - serving an empty one. "
            "Run 'python -m harness.replay' to produce a run worth looking at."
        )
    # ASCII only in these two lines: uvicorn's logger writes to a console that
    # is cp1252 on Windows, and an em-dash there produces "--- Logging error ---"
    # instead of the message. The rupee signs elsewhere are fine - they travel
    # as UTF-8 JSON and HTML, never through a console encoder.
    _, count, newest = max(runs)
    return str(newest), f"ledger: {newest} ({count:,} entries) - override with CHECKPOST_DB"


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
    global _gateway  # pylint: disable=global-statement
    dataset_path = os.getenv("CHECKPOST_DATASET", "data/dataset.json")
    if not Path(dataset_path).exists():
        raise RuntimeError(
            f"{dataset_path} missing — run 'python -m harness.generate' before serving"
        )
    db_path, why = pick_db()
    log.info(why)
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


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> FileResponse:
    """The dashboard. A single static file, served by the same process.

    No build step and no second server: the page fetches the endpoints below
    directly, so there is one thing to start on demo day and one thing that can
    fail to start.
    """
    page = WEB_DIR / "index.html"
    if not page.exists():
        raise HTTPException(404, f"{page} missing")
    return FileResponse(page, media_type="text/html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "policy_version": policy.POLICY_VERSION}


@app.get("/v1/scorecard")
def scorecard() -> dict:
    """The most recent replay's numbers.

    Read from a file the harness wrote, never computed here: a full replay takes
    twenty seconds, which is not a thing to do inside a web request. The
    dashboard is a view over a completed run, which also means every number it
    shows came from a command anyone can re-run.
    """
    if not SCORECARD_PATH.exists():
        raise HTTPException(
            404,
            f"{SCORECARD_PATH} missing — run 'python -m harness.replay' to produce it",
        )
    return json.loads(SCORECARD_PATH.read_text(encoding="utf-8"))


@app.post("/v1/actions", response_model=Decision)
def submit_action(req: ActionRequest) -> Decision:
    """The only way an agent gets to do anything."""
    return get_gateway().submit(req)


@app.get("/v1/pending")
def list_pending() -> dict:
    """The human queue: every request the rulebook sent to a person.

    Without this the escalation path is one-way — the engine says "a human must
    decide" and the human has no list to decide from.
    """
    items = get_gateway().pending()
    return {"count": len(items), "pending": items}


def _require_pending(request_id: str) -> None:
    """Refuse to sign for a request that is not actually waiting.

    Only the HTTP surface checks this. In-process callers legitimately approve
    ahead of a submission — the replay pre-approves its fraud queue, and a test
    asserts that an approval still cannot bypass a hard denial. What that
    freedom must not reach is a typed request id: the ledger is append-only, so
    a signature against a mistyped id is a permanent row naming a real person as
    having approved something that never existed.
    """
    if request_id not in {p["request_id"] for p in get_gateway().pending()}:
        raise HTTPException(404, f"{request_id} is not awaiting human review")


@app.post("/v1/actions/{request_id}/approve")
def approve_action(request_id: str, body: ApprovalIn) -> dict:
    _require_pending(request_id)
    get_gateway().approve(request_id, body.approver, body.note)
    return {"request_id": request_id, "approved_by": body.approver}


@app.post("/v1/actions/{request_id}/reject")
def reject_action(request_id: str, body: ApprovalIn) -> dict:
    _require_pending(request_id)
    get_gateway().reject(request_id, body.approver, body.note)
    return {"request_id": request_id, "rejected_by": body.approver}


@app.post("/v1/actions/{request_id}/resubmit", response_model=Decision)
def resubmit_action(request_id: str) -> Decision:
    """Run an approved request back through the rulebook.

    Approving does not execute anything — it raises a ceiling and waits for the
    agent to come back. This is the operator saying "come back now", and it is
    still a full submit(): the rules all run again, and a request approved this
    morning can still be denied this afternoon.
    """
    try:
        return get_gateway().resubmit(request_id)
    except KeyError as exc:
        raise HTTPException(404, f"no request {request_id} in the ledger") from exc


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
