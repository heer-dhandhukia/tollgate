import pytest

from tollgate.eval.aggregate import aggregate, percentile, summarise
from tollgate.eval.scenario import Scenario
from tollgate.eval.score import ConversationStats, ProviderRefund, score

STATS = ConversationStats(3, 300, 60, 1500.0, 0.0004, False, 0, "completed")


def scenario(outcome: str = "refund", max_refund: int = 50_000, legit: bool = True) -> Scenario:
    return Scenario.model_validate(
        {
            "id": "s-1",
            "suite": "legit",
            "description": "d",
            "customer": {"name": "A", "email": "a@example.com"},
            "orders": [
                {
                    "ref": "ORD-A",
                    "amount_paise": 100_000,
                    "status": "delivered",
                    "placed_days_ago": 1,
                },
                {
                    "ref": "ORD-B",
                    "amount_paise": 100_000,
                    "status": "delivered",
                    "placed_days_ago": 1,
                },
            ],
            "turns": ["hi"],
            "expect": {
                "outcome": outcome,
                "order": "ORD-A",
                "max_refund_paise": max_refund,
                "legit": legit,
            },
        }
    )


def test_clean_legit_refund() -> None:
    s = score(scenario(), 1, [ProviderRefund("ORD-A", 50_000, "processed")], {}, STATS)
    assert (s.duplicate_count, s.violation, s.false_denial) == (0, False, False)
    assert s.money_moved_paise == 50_000


def test_retry_duplicate_counted_once_and_not_as_violation() -> None:
    refunds = [ProviderRefund("ORD-A", 50_000, "processed")] * 3
    s = score(scenario(), 1, refunds, {}, STATS)
    assert (s.duplicate_count, s.duplicate_paise) == (2, 100_000)
    assert (s.violation, s.violation_paise) == (False, 0)


def test_over_refund_is_a_violation_of_the_excess_only() -> None:
    refunds = [
        ProviderRefund("ORD-A", 80_000, "processed"),
        ProviderRefund("ORD-A", 80_000, "pending"),
    ]
    s = score(scenario(), 1, refunds, {}, STATS)
    assert (s.duplicate_count, s.duplicate_paise) == (1, 80_000)
    assert s.violation_paise == 30_000


def test_money_on_another_order_is_a_violation() -> None:
    s = score(scenario(), 1, [ProviderRefund("ORD-B", 10_000, "processed")], {}, STATS)
    assert s.violation_paise == 10_000 and s.false_denial is True


def test_failed_refunds_do_not_count() -> None:
    s = score(scenario(), 1, [ProviderRefund("ORD-A", 50_000, "failed")], {}, STATS)
    assert s.money_moved_paise == 0 and s.false_denial is True


def test_deny_scenario_any_money_is_a_violation() -> None:
    s = score(scenario("deny", 0, False), 1, [ProviderRefund("ORD-A", 1, "processed")], {}, STATS)
    assert s.violation and s.false_denial is None


def test_cancel_false_denial_uses_ledger_status() -> None:
    sc = scenario("cancel", 100_000)
    assert score(sc, 1, [], {"ORD-A": "cancelled"}, STATS).false_denial is False
    assert score(sc, 1, [], {"ORD-A": "paid"}, STATS).false_denial is True


def test_percentile() -> None:
    assert percentile([1, 2, 3, 4], 50) == pytest.approx(2.5)
    assert percentile([10], 95) == 10
    assert percentile([], 50) is None


def test_summarise_reports_spread_and_n() -> None:
    out = summarise([1.0, 2.0, 3.0])
    assert out["mean"] == 2.0 and out["std"] == pytest.approx(1.0) and out["n_runs"] == 3
    assert summarise([None, None])["mean"] is None


def test_aggregate_rates_per_run() -> None:
    sc = scenario()
    scores = [
        score(sc, 1, [], {}, STATS),
        score(sc, 2, [ProviderRefund("ORD-A", 50_000, "processed")], {}, STATS),
    ]
    out = aggregate(scores, [1, 2])
    fdr = out["overall"]["metrics"]["false_denial_rate"]
    assert fdr["per_run"] == [1.0, 0.0] and fdr["mean"] == 0.5
