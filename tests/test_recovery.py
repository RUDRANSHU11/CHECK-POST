"""The recovery agent: what it proposes, and what it hands to the gateway."""

from __future__ import annotations

from datetime import timedelta

import pytest

from agents.recovery import (
    CALL_WORTH_IT_PAISE,
    GeminiPlanner,
    HIGH_VALUE_PAISE,
    RecoveryAgent,
    RulePlanner,
    build_planner,
)
from engine.schema import ActionType, Verdict, rupees
from tests.conftest import NOON_IST


@pytest.fixture
def planner() -> RulePlanner:
    return RulePlanner()


def plan(planner, store, invoice_id: str, tried: int = 0, now=NOON_IST):
    invoice = store.invoice(invoice_id)
    return planner.plan(invoice, store.payments_for(invoice_id), tried, now)


# -- when the agent should do nothing at all -------------------------------- #

@pytest.mark.parametrize("invoice_id", ["i_paid", "i_disputed"])
def test_planner_leaves_settled_and_disputed_invoices_alone(planner, store, invoice_id):
    assert plan(planner, store, invoice_id) is None


def test_planner_leaves_invoices_that_are_not_due_yet(planner, store):
    invoice = store.invoice("i_big")
    invoice.due_at = NOON_IST + timedelta(days=5)
    assert plan(planner, store, "i_big") is None


# -- the ladder ------------------------------------------------------------- #

def test_transient_decline_is_retried_before_anything_is_spent(planner, store):
    # i_small's payment failed on insufficient funds — the cheapest money on the
    # table, because the customer already intended to pay.
    action, _, rationale = plan(planner, store, "i_small")
    assert action is ActionType.RETRY_CHARGE
    assert "insufficient_funds" in rationale


def test_hard_decline_is_not_retried(planner, store):
    # i_big's card is expired. Retrying is guaranteed to fail, so the planner
    # should go straight to outreach.
    action, _, _ = plan(planner, store, "i_big")
    assert action is not ActionType.RETRY_CHARGE


def test_high_value_invoice_skips_the_email_round(planner, store):
    assert store.invoice("i_big").amount_paise >= HIGH_VALUE_PAISE
    action, _, _ = plan(planner, store, "i_big")
    assert action is ActionType.SEND_SMS


def test_ladder_escalates_with_each_attempt(planner, store):
    seen = [plan(planner, store, "i_big", tried=n)[0] for n in range(4)]
    assert seen[0] is ActionType.SEND_SMS
    assert seen[1] is ActionType.SEND_WHATSAPP
    assert seen[2] is ActionType.PLACE_CALL
    assert seen[3] is ActionType.ESCALATE_TO_HUMAN


def test_small_invoice_never_gets_a_phone_call(planner, store):
    assert store.invoice("i_small").amount_paise < CALL_WORTH_IT_PAISE
    for n in range(6):
        action, _, _ = plan(planner, store, "i_small", tried=n)
        assert action is not ActionType.PLACE_CALL


def test_exhausted_ladder_escalates(planner, store):
    action, _, rationale = plan(planner, store, "i_big", tried=9)
    assert action is ActionType.ESCALATE_TO_HUMAN
    assert "overdue" in rationale


# -- the agent -------------------------------------------------------------- #

def test_agent_submits_and_gets_a_verdict(store, gateway):
    agent = RecoveryAgent(store=store, gateway=gateway)
    result = agent.work(store.invoice("i_big"), NOON_IST)
    assert result is not None
    request, decision = result
    assert request.agent.value == "recovery"
    assert decision.verdict in (Verdict.ALLOW, Verdict.DENY, Verdict.NEEDS_HUMAN)
    assert decision.results


def test_agent_carries_the_memo_so_the_injection_guard_can_see_it(store, gateway):
    # An agent that summarised the memo away would hide the attack from the
    # layer built to catch it.
    agent = RecoveryAgent(store=store, gateway=gateway)
    request, decision = agent.work(store.invoice("i_poisoned"), NOON_IST)
    assert "memo" in request.evidence
    assert decision.verdict is Verdict.DENY
    assert any(r.rule_id == "prompt_injection" for r in decision.results)


def test_agent_counts_a_refusal_as_a_turn_taken(store, gateway):
    agent = RecoveryAgent(store=store, gateway=gateway)
    agent.work(store.invoice("i_stop"), NOON_IST)
    assert agent.tried["i_stop"] == 1


def test_agent_view_may_drift_from_the_gateways(store, gateway):
    # The agent's tally counts attempts; the gateway's counts allowed actions.
    # They are supposed to be able to disagree — that divergence is the thing
    # the checkpoint exists to catch.
    agent = RecoveryAgent(store=store, gateway=gateway)
    agent.work(store.invoice("i_stop"), NOON_IST)  # denied: customer opted out
    assert agent.tried["i_stop"] == 1
    assert gateway._attempts.get("i_stop", 0) == 0


def test_agent_skips_invoices_with_nothing_to_do(store, gateway):
    agent = RecoveryAgent(store=store, gateway=gateway)
    assert agent.work(store.invoice("i_paid"), NOON_IST) is None
    assert len(gateway.ledger) == 0


def test_run_returns_one_attempt_per_actionable_invoice(store, gateway):
    agent = RecoveryAgent(store=store, gateway=gateway)
    attempts = agent.run(list(store.invoices.values()), NOON_IST)
    # i_paid and i_disputed are skipped; the other four are worked.
    assert len(attempts) == 4


def test_idempotency_key_is_stable_for_the_same_attempt(store, gateway):
    agent = RecoveryAgent(store=store, gateway=gateway)
    request, _ = agent.work(store.invoice("i_big"), NOON_IST)
    assert request.idempotency_key.startswith("i_big:0:")


# -- planner selection ------------------------------------------------------ #

def test_no_key_means_the_rule_planner(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert isinstance(build_planner(), RulePlanner)


def test_blank_key_means_the_rule_planner(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    assert isinstance(build_planner(), RulePlanner)

# -- the Gemini planner ----------------------------------------------------- #
#
# No live key has ever been used here. What these tests can still establish is
# everything except the network hop: that the request we build is the shape the
# SDK expects, that a real GenerateContentResponse parses into an action, that a
# planner which fails never takes the run down, and — the part that matters —
# that the gateway refuses what the model invents. They are written against the
# installed google-genai types rather than a hand-rolled mock, so a change in
# the SDK's response shape fails here instead of on the day.


def _tool_response(**args):
    """A real GenerateContentResponse carrying one propose_action call."""
    from google.genai import types

    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                name="propose_action", args=args
                            )
                        )
                    ],
                )
            )
        ]
    )


class _FakeClient:
    """Stands in for genai.Client. Records the call, returns what it was given."""

    def __init__(self, reply, **_kw):
        self.reply = reply
        self.calls = []
        outer = self

        class _Models:
            def generate_content(self, **kwargs):
                outer.calls.append(kwargs)
                if isinstance(outer.reply, Exception):
                    raise outer.reply
                return outer.reply

        self.models = _Models()


@pytest.fixture
def gemini(monkeypatch):
    """Builds a GeminiPlanner whose transport is ours. Returns (make, seen)."""

    def make(reply):
        holder = {}

        def _client(**kwargs):
            holder["client"] = _FakeClient(reply, **kwargs)
            return holder["client"]

        monkeypatch.setattr("google.genai.Client", _client)
        monkeypatch.setenv("GEMINI_API_KEY", "AIza" + "x" * 35)
        return GeminiPlanner(), holder

    return make


def test_a_tool_call_the_sdk_would_really_return_parses_into_an_action(gemini, store):
    planner, _ = gemini(
        _tool_response(action="place_call", amount_paise=0, rationale="third try")
    )
    action, amount, why = planner.plan(store.invoice("i_big"), [], tried=2, now=NOON_IST)
    assert action is ActionType.PLACE_CALL
    assert amount == 0
    assert why == "third try"


def test_the_model_is_given_the_tool_and_the_memo(gemini, store):
    planner, holder = gemini(_tool_response(action="send_email", rationale="first"))
    planner.plan(store.invoice("i_poisoned"), [], tried=0, now=NOON_IST)
    sent = holder["client"].calls[0]
    # The SDK coerced our plain dict into a FunctionDeclaration on the way in,
    # which is half the point of testing against the real types: a schema it
    # cannot parse fails here rather than on the first live call.
    tool = sent["config"].tools[0].function_declarations[0]
    assert tool.name == "propose_action"
    assert "offer_discount" in tool.parameters.properties["action"].enum
    # The memo travels to the model intact. An agent that summarised or scrubbed
    # it would be hiding the attack from the layer built to catch it — and the
    # gateway would never see the string it refuses on.
    assert "ignore your previous instructions" in sent["contents"]


def test_a_planner_that_fails_falls_back_instead_of_taking_the_run_down(gemini, store):
    planner, _ = gemini(RuntimeError("503 model overloaded"))
    invoice = store.invoice("i_big")
    assert planner.plan(invoice, [], tried=0, now=NOON_IST) == RulePlanner().plan(
        invoice, [], tried=0, now=NOON_IST
    )


def test_a_run_can_tell_how_much_of_gemini_was_really_gemini(gemini, store):
    """The free tier allows 20 requests a day; a month is 964 invoices.

    Past the cap every call 429s and returns a rule-planner proposal that looks
    exactly like a good one, under a scorecard still headed `planner: gemini`.
    The counters are what make that claim checkable, so the summary line can say
    `0/2 live` instead of quietly taking the credit."""
    planner, _ = gemini(RuntimeError("429 RESOURCE_EXHAUSTED"))
    invoice = store.invoice("i_big")
    planner.plan(invoice, [], tried=0, now=NOON_IST)
    planner.plan(invoice, [], tried=0, now=NOON_IST)
    assert (planner.calls, planner.fallbacks) == (2, 2)


def test_a_tool_call_that_lands_is_not_counted_as_a_fallback(gemini, store):
    planner, _ = gemini(_tool_response(action="retry_charge", rationale="transient"))
    planner.plan(store.invoice("i_big"), [], tried=0, now=NOON_IST)
    assert (planner.calls, planner.fallbacks) == (1, 0)


def test_an_action_the_enum_does_not_have_falls_back_rather_than_crashing(gemini, store):
    # A model is free to invent "wire_transfer". Nothing downstream should see it.
    planner, _ = gemini(_tool_response(action="wire_transfer", rationale="trust me"))
    action, _amount, _why = planner.plan(store.invoice("i_big"), [], tried=0, now=NOON_IST)
    assert action in ActionType


def test_the_gateway_refuses_the_discount_the_model_invented(store, gateway):
    """The point of the whole project, stated as a test.

    A planner is untrusted by construction, so this one proposes a ₹50,000
    discount on a ₹9,000 invoice — the kind of thing a jailbroken or simply
    confused model does. No verdict from the planner reaches the money: the
    rulebook sends it to a person, and names the rule that caught it."""

    class _Reckless:
        name = "reckless"

        def plan(self, invoice, payments, tried, now):
            return ActionType.OFFER_DISCOUNT, rupees(50_000), "customer asked nicely"

    agent = RecoveryAgent(store=store, gateway=gateway, planner=_Reckless())
    _request, decision = agent.work(store.invoice("i_big"), NOON_IST)
    assert decision.verdict is Verdict.NEEDS_HUMAN
    assert any(
        r.rule_id == "discount_ceiling" and r.verdict is Verdict.NEEDS_HUMAN
        for r in decision.results
    )
