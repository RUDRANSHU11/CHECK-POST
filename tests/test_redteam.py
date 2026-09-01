"""The red team, run as part of the suite.

``python -m harness.redteam`` is the demo-day gate. This is the same cases in
CI, so an attack cannot start getting through between now and the pitch without
somebody being told.

Each case is reported individually rather than as one pass/fail, so a failure
names the attack that got through instead of saying "18 cases, one of them".
"""

from __future__ import annotations

import pytest

from harness import redteam


@pytest.mark.parametrize("result", redteam.run(), ids=lambda r: r.case.name)
def test_the_attack_is_stopped_by_the_rule_that_should_stop_it(result):
    assert result.verdict_ok, (
        f"{result.case.story}\n"
        f"expected {result.case.expect.value}, got {result.verdict.value}"
    )
    assert result.rule_ok, (
        f"{result.case.story}\n"
        f"expected {result.case.expect_rule} to object; "
        f"what fired was {', '.join(result.fired) or 'nothing'} — "
        f"the right answer from the wrong rule is a coincidence, and "
        f"coincidences stop working when the data moves"
    )


def test_the_red_team_covers_every_agent():
    """A red team that only attacks the recovery agent proves the recovery agent
    is safe."""
    from engine.schema import AgentName

    agents = {c.build().agent for c in redteam.CASES}
    assert agents == set(AgentName), f"no attack against {set(AgentName) - agents}"


def test_every_case_names_a_rule_that_exists():
    """A case expecting a rule that was renamed would pass forever by never
    matching, if the assertion were the other way round."""
    from engine import policy

    known = {rid.split("_", 1)[1] for rid in policy.rule_ids()} | {"economics"}
    for case in redteam.CASES:
        assert case.expect_rule in known, f"{case.name} names an unknown rule"
