import json
from datetime import date
from pathlib import Path

import pytest

from tollgate.agent.prompting import system_prompt
from tollgate.agent.session import AuthenticatedSession
from tollgate.config import REPO_ROOT
from tollgate.eval.report import END, START, render, update_readme
from tollgate.eval.scenario import MIN_SUITE_SIZE, ScenarioFileError, load_suites, sandbox_name
from tollgate.policy_config import load_policy

SCENARIOS = REPO_ROOT / "scenarios"


def test_all_suites_load_with_minimum_sizes() -> None:
    suites = load_suites(SCENARIOS, ["legit", "policy_edges"])
    for name, scenarios in suites.items():
        assert len(scenarios) >= MIN_SUITE_SIZE[name]
    legit = suites["legit"]
    assert all(s.expect.legit for s in legit)
    assert {s.expect.outcome for s in legit} == {"refund", "cancel"}


def test_duplicate_ids_across_suites_rejected(tmp_path: Path) -> None:
    for name in ("catalog", "legit"):
        (tmp_path / f"{name}.yaml").write_text((SCENARIOS / f"{name}.yaml").read_text())
    (tmp_path / "copy.yaml").write_text(
        (SCENARIOS / "legit.yaml").read_text().replace("suite: legit", "suite: copy")
    )
    with pytest.raises(ScenarioFileError):
        load_suites(tmp_path, ["legit", "copy"])


def test_system_prompt_is_deterministic_and_contains_policy() -> None:
    policy = load_policy(REPO_ROOT / "config" / "policy.v1.yaml")
    session = AuthenticatedSession(1, "cust_x", "Asha Rao", "conv_random_1")
    other = AuthenticatedSession(99, "cust_x", "Asha Rao", "conv_random_2")
    a = system_prompt("naive", "v1", session, policy, date(2026, 10, 1))
    b = system_prompt("naive", "v1", other, policy, date(2026, 10, 1))
    assert a == b  # db ids and conversation ids never reach the prompt
    assert "2026-10-01" in a and "30 days" in a and "Asha Rao" in a


def test_sandbox_names_are_valid_and_distinct() -> None:
    a = sandbox_name("tg_eval", "naive", "r1", "legit-full-001" * 5)
    b = sandbox_name("tg_eval", "naive", "r2", "legit-full-001" * 5)
    assert a != b and len(a) <= 59 and a.replace("_", "").isalnum()


def _fake_result(mode: str) -> dict[str, object]:
    metric = {
        "mean": 2.0,
        "std": 0.5,
        "min": 1.5,
        "max": 2.5,
        "per_run": [1.5, 2.0, 2.5],
        "n_runs": 3,
    }
    metrics = {
        k: metric
        for k in (
            "duplicate_executions",
            "duplicate_paise",
            "policy_violations",
            "policy_violation_paise",
            "false_denial_rate",
            "escalation_rate",
            "ledger_rejections_after_provider",
            "latency_p50_ms",
            "latency_p95_ms",
            "cost_per_conversation_usd",
        )
    }
    block = {"n_scenarios_per_run": {"1": 66, "2": 66, "3": 66}, "metrics": metrics}
    return {
        "mode": mode,
        "model": "m",
        "prompt_version": f"{mode}.v1",
        "policy_version": 1,
        "eval_today": "2026-10-01",
        "usd_to_inr": 84.0,
        "complete": True,
        "missing_results": 0,
        "overall": block,
        "suites": {"legit": block},
    }


def test_report_renders_from_results_only(tmp_path: Path) -> None:
    assert "No evaluation results yet" in render({})
    (tmp_path / "eval_naive.json").write_text(json.dumps(_fake_result("naive")))
    readme = tmp_path / "README.md"
    readme.write_text(f"# x\n{START}\nold\n{END}\ntail\n")
    update_readme(readme, tmp_path)
    out = readme.read_text()
    assert "old" not in out and out.endswith("tail\n")
    assert "| Duplicate executions (money moved twice) | 2.0 ± 0.5 |" in out
    assert "n = run 1: 66, run 2: 66, run 3: 66" in out


def test_readme_has_markers() -> None:
    text = (REPO_ROOT / "README.md").read_text()
    assert START in text and END in text
