"""Naive backend against an in-process mockpay, and the eval runner end to end (fake LLM)."""

import json
from datetime import date
from pathlib import Path

import pytest

from tests.conftest import ADMIN_URL
from tests.eval.fake_agent import RuleAgent
from tests.fakes import Scripted, text, tool_call
from tollgate.clock import FixedClock
from tollgate.config import REPO_ROOT
from tollgate.eval.runner import EvalRunner, load_scores, write_summary
from tollgate.eval.sandbox import Sandboxes
from tollgate.eval.scenario import Scenario, load_catalog, load_suites
from tollgate.llm.cache import ResponseCache
from tollgate.llm.client import LLMClient
from tollgate.llm.pricing import load_prices
from tollgate.policy_config import load_policy

pytestmark = pytest.mark.db

SCENARIOS = REPO_ROOT / "scenarios"
CLOCK = FixedClock(date(2026, 10, 1))


def runner(tmp_path: Path, completion_fn: object, cache: bool = True) -> EvalRunner:
    llm = LLMClient(
        "groq/llama-3.3-70b-versatile",
        ResponseCache(tmp_path / "cache") if cache else None,
        load_prices(REPO_ROOT / "config" / "models.yaml"),
        completion_fn=completion_fn,  # type: ignore[arg-type]
    )
    return EvalRunner(
        llm=llm,
        sandboxes=Sandboxes(ADMIN_URL, prefix="tg_test_eval"),
        policy=load_policy(REPO_ROOT / "config" / "policy.v1.yaml"),
        catalog=load_catalog(SCENARIOS / "catalog.yaml"),
        clock=CLOCK,
        results_dir=tmp_path / "results",
        provider_timeout_s=0.5,
    )


def pick(*ids: str) -> dict[str, list[Scenario]]:
    suites = load_suites(SCENARIOS, ["legit", "policy_edges"])
    out: dict[str, list[Scenario]] = {}
    for name, scenarios in suites.items():
        chosen = [s for s in scenarios if s.id in ids]
        if chosen:
            out[name] = chosen
    return out


async def test_end_to_end_scores_naive_behaviour(tmp_path: Path) -> None:
    suites = pick("legit-full-001", "legit-cancel-021", "edge-window-001", "edge-shipped-026")
    r = runner(tmp_path, RuleAgent())
    summary = await r.run("naive", suites, [1])
    assert summary.failed == [] and summary.completed == 4

    scores = {s.scenario_id: s for s in load_scores(tmp_path / "results", "naive")}
    assert scores["legit-full-001"].false_denial is False
    assert scores["legit-full-001"].violation is False
    assert scores["legit-cancel-021"].false_denial is False
    # The gullible agent refunds an out-of-window order: naive mode lets it through.
    assert scores["edge-window-001"].violation is True
    # Cancelling a shipped order: the provider refund goes out before the ledger refuses.
    shipped = scores["edge-shipped-026"]
    assert shipped.violation is True and shipped.money_moved_paise > 0

    detail = json.loads(
        (tmp_path / "results/runs/naive/policy_edges/run1/edge-shipped-026.json").read_text()
    )
    assert any(e["type"] == "cancel_rejected" for e in detail["events"])

    path = write_summary(
        tmp_path / "results", "naive", suites, [1], {"model": "fake", "policy_version": 1}
    )
    data = json.loads(path.read_text())
    assert data["complete"] is True
    assert data["overall"]["metrics"]["policy_violations"]["per_run"] == [2.0]


async def test_rerun_skips_finished_and_replays_from_cache(tmp_path: Path) -> None:
    suites = pick("legit-partial-011")
    agent = RuleAgent()
    first = await runner(tmp_path, agent).run("naive", suites, [1])
    assert first.completed == 1
    calls = agent.calls
    again = await runner(tmp_path, agent).run("naive", suites, [1])
    assert (again.completed, again.skipped) == (0, 1)

    # Delete the result: the scenario re-runs, and every LLM call is served from the cache.
    for p in (tmp_path / "results").rglob("legit-partial-011.json"):
        p.unlink()
    third = await runner(tmp_path, agent).run("naive", suites, [1])
    assert third.completed == 1 and agent.calls == calls
    before = json.loads(next((tmp_path / "results").rglob("legit-partial-011.json")).read_text())
    assert all(c["cache_hit"] for c in before["transcript"]["llm_calls"])


async def test_naive_retry_after_lost_response_refunds_twice(tmp_path: Path) -> None:
    """error_after_commit: the refund happened but the agent saw an error and retried."""
    base = pick("legit-full-002")["legit"][0]
    scenario = base.model_copy(update={"faults": ["error_after_commit"]})
    ref = scenario.expect.order
    amount = scenario.expect.max_refund_paise
    refund_args = {"order_id": ref, "amount_paise": amount, "reason": "faulty"}
    agent = Scripted(
        tool_call("lookup_order", "c1", order_id=ref),
        tool_call("issue_refund", "c2", **refund_args),
        tool_call("issue_refund", "c3", **refund_args),  # the model retries after the 502
        text("Your refund is on its way."),
    )
    r = runner(tmp_path, agent, cache=False)
    await r.sandboxes.prepare()
    result = await r.run_one("naive", scenario, 1)
    assert result.duplicate_count == 1
    assert result.duplicate_paise == amount


async def test_rate_limit_stops_the_run_cleanly(tmp_path: Path) -> None:
    from tests.fakes import HTTPStatusError

    suites = pick("legit-full-003", "legit-full-004")

    async def always_429(**_: object) -> object:
        raise HTTPStatusError(429)

    r = runner(tmp_path, always_429, cache=False)
    r.llm.max_retries = 1

    async def no_sleep(_: float) -> None:
        return None

    r.llm._sleep = no_sleep
    summary = await r.run("naive", suites, [1])
    assert summary.stopped_for_rate_limit and summary.completed == 0
    assert not list((tmp_path / "results").rglob("*.json"))
