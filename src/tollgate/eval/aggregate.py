"""Aggregate per-scenario scores into per-run metrics, then mean / spread across runs."""

import math
import statistics
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from tollgate.eval.score import ScenarioScore

Metric = Callable[[Sequence[ScenarioScore]], float | None]


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile, q in [0, 100]."""
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * q / 100
    lo, hi = math.floor(rank), math.ceil(rank)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def _rate(flags: Iterable[bool | None]) -> float | None:
    known = [f for f in flags if f is not None]
    return sum(known) / len(known) if known else None


def _mean_cost(scores: Sequence[ScenarioScore]) -> float | None:
    costs = [s.cost_usd for s in scores]
    if not costs or any(c is None for c in costs):
        return None
    return sum(c for c in costs if c is not None) / len(costs)


METRICS: dict[str, Metric] = {
    "scenarios": lambda s: float(len(s)),
    "duplicate_executions": lambda s: float(sum(x.duplicate_count for x in s)),
    "duplicate_paise": lambda s: float(sum(x.duplicate_paise for x in s)),
    "policy_violations": lambda s: float(sum(x.violation for x in s)),
    "policy_violation_paise": lambda s: float(sum(x.violation_paise for x in s)),
    "false_denial_rate": lambda s: _rate(x.false_denial for x in s),
    "escalation_rate": lambda s: _rate(x.escalated for x in s),
    "ledger_rejections_after_provider": lambda s: float(
        sum(x.ledger_rejections_after_provider for x in s)
    ),
    "latency_p50_ms": lambda s: percentile([x.latency_ms for x in s], 50),
    "latency_p95_ms": lambda s: percentile([x.latency_ms for x in s], 95),
    "cost_per_conversation_usd": _mean_cost,
    "max_steps_stops": lambda s: float(sum(x.stopped_reason == "max_steps" for x in s)),
}


def summarise(per_run: list[float | None]) -> dict[str, Any]:
    values = [v for v in per_run if v is not None]
    return {
        "mean": statistics.fmean(values) if values else None,
        "std": statistics.stdev(values) if len(values) > 1 else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "per_run": per_run,
        "n_runs": len(values),
    }


def aggregate(scores: list[ScenarioScore], runs: list[int]) -> dict[str, Any]:
    """{"suites": {suite: {...}}, "overall": {...}}; each metric summarised across runs."""

    def block(subset: list[ScenarioScore]) -> dict[str, Any]:
        by_run = {r: [s for s in subset if s.run_index == r] for r in runs}
        return {
            "n_scenarios_per_run": {str(r): len(v) for r, v in by_run.items()},
            "metrics": {
                name: summarise([fn(by_run[r]) if by_run[r] else None for r in runs])
                for name, fn in METRICS.items()
            },
        }

    suites = sorted({s.suite for s in scores})
    return {
        "suites": {suite: block([s for s in scores if s.suite == suite]) for suite in suites},
        "overall": block(scores),
    }
