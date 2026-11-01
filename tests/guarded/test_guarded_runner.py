"""Naive vs guarded through the real eval runner, with scripted and rule-based fake models."""

import json
from pathlib import Path

import pytest

from tests.eval.fake_agent import RuleAgent
from tests.eval.test_naive_and_runner import runner
from tests.fakes import Scripted, text, tool_call
from tollgate.config import REPO_ROOT
from tollgate.eval.runner import load_scores
from tollgate.eval.scenario import Scenario, load_suites

pytestmark = pytest.mark.db

SCENARIOS = REPO_ROOT / "scenarios"


def pick(*ids: str) -> dict[str, list[Scenario]]:
    suites = load_suites(SCENARIOS, ["legit", "policy_edges", "chaos"])
    out: dict[str, list[Scenario]] = {}
    for name, scenarios in suites.items():
        chosen = [s for s in scenarios if s.id in ids]
        if chosen:
            out[name] = chosen
    return out


def retrying_agent(scenario: Scenario) -> Scripted:
    """A model that retries the refund once after seeing an error, like a helpful assistant."""
    ref, amount = scenario.expect.order, scenario.expect.max_refund_paise
    args = {"order_id": ref, "amount_paise": amount, "reason": "damaged"}
    return Scripted(
        tool_call("lookup_order", "c1", order_id=ref),
        tool_call("issue_refund", "c2", **args),
        tool_call("issue_refund", "c3", **args),
        text("Your refund is on its way."),
    )


@pytest.mark.parametrize(
    "scenario_id", ["chaos-lost-reply-001", "chaos-timeout-005", "chaos-mixed-011"]
)
async def test_same_model_behaviour_naive_pays_twice_guarded_once(
    tmp_path: Path, scenario_id: str
) -> None:
    scenario = pick(scenario_id)["chaos"][0]
    results = {}
    for mode in ("naive", "guarded"):
        r = runner(tmp_path / mode, retrying_agent(scenario), cache=False)
        r.provider_timeout_s = 0.5  # the timeout fault hangs for 6 s
        await r.sandboxes.prepare()
        results[mode] = await r.run_one(mode, scenario, 1)

    if scenario_id == "chaos-mixed-011":
        # Naive's one retry meets the 502-after-commit: one refund lands, but the model was
        # told it failed twice. With a second retry it would have paid twice.
        naive = results["naive"]
        assert naive.duplicate_count == 0
        assert naive.money_moved_paise == scenario.expect.max_refund_paise
    else:
        assert results["naive"].duplicate_count == 1
    guarded = results["guarded"]
    assert guarded.duplicate_count == 0
    assert guarded.money_moved_paise == scenario.expect.max_refund_paise
    assert guarded.false_denial is False and guarded.violation is False

    detail = json.loads(next((tmp_path / "guarded").rglob(f"{scenario_id}.json")).read_text())
    second_call = detail["transcript"]["tool_events"][2]
    assert second_call["result"]["status"] == "already_done"
    assert any(e["type"] == "provider_retry" for e in detail["events"])


async def test_rule_based_agent_end_to_end_in_guarded_mode(tmp_path: Path) -> None:
    ids = (
        "legit-full-001",
        "legit-cancel-021",
        "edge-window-001",
        "edge-shipped-026",
        "edge-approval-009",
        "edge-remaining-023",
    )
    suites = pick(*ids)
    summary = await runner(tmp_path, RuleAgent()).run("guarded", suites, [1])
    assert summary.failed == [] and summary.completed == len(ids)

    scores = {s.scenario_id: s for s in load_scores(tmp_path / "results", "guarded")}
    # Genuine requests still go through.
    assert scores["legit-full-001"].false_denial is False
    assert scores["legit-cancel-021"].false_denial is False
    # The same gullible agent that breaks policy in naive mode is stopped in code.
    for sid in ("edge-window-001", "edge-shipped-026", "edge-approval-009", "edge-remaining-023"):
        assert scores[sid].violation is False, sid
    assert scores["edge-shipped-026"].money_moved_paise == 0
    assert scores["edge-approval-009"].escalated is True
    assert scores["edge-approval-009"].money_moved_paise == 0
