"""HTTP surface. Thin by design, so these tests check wiring and status codes
rather than re-testing rules that test_policy.py already covers."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from engine.api import app


@pytest.fixture
def client(dataset, tmp_path, monkeypatch):
    ds = tmp_path / "dataset.json"
    ds.write_text(json.dumps(dataset), encoding="utf-8")
    monkeypatch.setenv("CHECKPOST_DATASET", str(ds))
    monkeypatch.setenv("CHECKPOST_DB", str(tmp_path / "api.db"))
    with TestClient(app) as c:
        yield c


def action(n: int, **over) -> dict:
    body = {
        "request_id": f"rq_{n}",
        "agent": "recovery",
        # Email, not SMS, and deliberately: the HTTP layer reads the real clock,
        # so a test that sends an SMS passes in the afternoon and fails after
        # 21:00 IST when the quiet-hours rule kicks in. Email is exempt from
        # quiet hours, which makes these tests time-independent. Rule behaviour
        # itself is tested against a frozen clock in test_policy.py.
        "action": "send_email",
        "customer_id": "c_ok",
    }
    body.update(over)
    return body


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_submit_returns_a_verdict_with_reasons(client):
    r = client.post("/v1/actions", json=action(1, invoice_id="i_big"))
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] in ("allow", "deny", "needs_human")
    assert body["results"], "a decision must always carry its reasoning"
    assert body["policy_version"]


def test_opted_out_customer_is_refused_over_http(client):
    r = client.post("/v1/actions", json=action(1, customer_id="c_stop"))
    body = r.json()
    assert body["verdict"] == "deny"
    assert any(x["rule_id"] == "opt_out" for x in body["results"])


def test_unknown_action_type_is_rejected_at_the_edge(client):
    r = client.post("/v1/actions", json=action(1, action="wire_transfer"))
    assert r.status_code == 422


def test_unknown_field_is_rejected(client):
    # extra="forbid" on the schema: a typo'd field must not silently vanish.
    r = client.post("/v1/actions", json={**action(1), "amount": 999})
    assert r.status_code == 422


def test_approval_endpoint_unblocks_a_needs_human_request(client):
    body = action(
        1, action="issue_refund", payment_id="p_expired", amount_paise=600_000
    )
    assert client.post("/v1/actions", json=body).json()["verdict"] == "needs_human"

    r = client.post("/v1/actions/rq_1/approve", json={"approver": "ops@merchant.in"})
    assert r.status_code == 200

    assert client.post("/v1/actions", json=body).json()["verdict"] == "allow"


def test_outcome_endpoint(client):
    client.post("/v1/actions", json=action(1))
    r = client.post(
        "/v1/outcomes",
        json={
            "outcome_id": "out_1",
            "request_id": "rq_1",
            "result": "recovered",
            "amount_recovered_paise": 4000,
        },
    )
    assert r.status_code == 200


def test_stats_reflects_submitted_actions(client):
    client.post("/v1/actions", json=action(1))
    client.post("/v1/actions", json=action(2, customer_id="c_stop"))
    s = client.get("/v1/stats").json()
    assert s["actions_requested"] == 2
    assert s["denied"] == 1
    assert s["denials_by_rule"]["opt_out"] == 1


def test_rules_endpoint_exposes_the_rulebook(client):
    r = client.get("/v1/rules").json()
    assert len(r["rules"]) == 16
    assert r["thresholds"]["max_contacts_per_24h"] == 2


def test_ledger_feed_and_verify(client):
    client.post("/v1/actions", json=action(1))
    feed = client.get("/v1/ledger?limit=10").json()
    assert feed["count"] == 2  # request + decision
    v = client.get("/v1/ledger/verify").json()
    assert v["ok"] is True
    assert v["entries_checked"] == 2
    assert len(v["head"]) == 64


# -- the dashboard ---------------------------------------------------------- #

def test_dashboard_is_served_by_the_same_process(client):
    """One command on demo day, not two. The page ships from the API server, so
    there is no build step, no second port and no CORS to misconfigure."""
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>Checkpost</title>" in r.text


def test_the_dashboard_needs_no_network_to_render(client):
    """A dashboard that fetches a CDN is a dashboard that fails in a room with
    bad wifi. Everything it needs is inline."""
    body = client.get("/").text
    assert "http://" not in body.replace("http://127.0.0.1", "")
    assert "https://" not in body
    assert "<script src=" not in body and "<link rel=\"stylesheet\"" not in body


def test_scorecard_says_what_to_run_when_there_is_none(client, tmp_path, monkeypatch):
    """A 404 that names the command is worth more than a stack trace."""
    monkeypatch.setattr("engine.api.SCORECARD_PATH", tmp_path / "nope.json")
    r = client.get("/v1/scorecard")
    assert r.status_code == 404
    assert "harness.replay" in r.json()["detail"]


def test_scorecard_is_served_from_the_file_the_harness_wrote(client, tmp_path, monkeypatch):
    """Read, never computed: a full replay takes about twenty seconds, which is
    not a thing to do inside a web request."""
    card = tmp_path / "scorecard.json"
    card.write_text(
        json.dumps({"batch_id": "test", "uplift_measured_paise": 1234}), encoding="utf-8"
    )
    monkeypatch.setattr("engine.api.SCORECARD_PATH", card)
    r = client.get("/v1/scorecard")
    assert r.status_code == 200
    assert r.json()["uplift_measured_paise"] == 1234
