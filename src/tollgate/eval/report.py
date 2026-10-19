"""Renders results/eval_*.json into the README between the results markers.

Every number in the README comes from these files; nothing is typed by hand."""

import json
from pathlib import Path
from typing import Any

from tollgate.money import format_inr

START, END = "<!-- results:start -->", "<!-- results:end -->"
MODES = ("naive", "guarded")

ROWS: list[tuple[str, str, str]] = [
    ("Duplicate executions (money moved twice)", "duplicate_executions", "count"),
    ("Duplicate amount", "duplicate_paise", "inr"),
    ("Policy violations (money moved against policy)", "policy_violations", "count"),
    ("Policy-violating amount", "policy_violation_paise", "inr"),
    ("False denials (legit requests blocked)", "false_denial_rate", "pct"),
    ("Human escalation rate", "escalation_rate", "pct"),
    ("Ledger rejected after provider moved money", "ledger_rejections_after_provider", "count"),
    ("Latency p50 per conversation", "latency_p50_ms", "ms"),
    ("Latency p95 per conversation", "latency_p95_ms", "ms"),
    ("Cost per conversation", "cost_per_conversation_usd", "usd"),
]
SUITE_ROWS = ("policy_violations", "duplicate_executions", "false_denial_rate")


def _fmt(value: float | None, kind: str, usd_to_inr: float | None = None) -> str:
    if value is None:
        return "–"
    if kind == "inr":
        return format_inr(round(value))
    if kind == "pct":
        return f"{value * 100:.1f}%"
    if kind == "ms":
        return f"{value / 1000:.2f}s"
    if kind == "usd":
        inr = f" (₹{value * usd_to_inr:.2f})" if usd_to_inr else ""
        return f"${value:.5f}{inr}"
    return f"{value:.1f}"


def cell(metric: dict[str, Any], kind: str, usd_to_inr: float | None = None) -> str:
    mean, std = metric.get("mean"), metric.get("std")
    if mean is None:
        return "–"
    text = _fmt(mean, kind, usd_to_inr)
    if std is not None:
        text += f" ± {_fmt(std, kind, usd_to_inr)}"
    return text


def load(results_dir: Path) -> dict[str, dict[str, Any]]:
    out = {}
    for mode in MODES:
        path = results_dir / f"eval_{mode}.json"
        if path.exists():
            out[mode] = json.loads(path.read_text())
    return out


def render(results: dict[str, dict[str, Any]]) -> str:
    if not results:
        return (
            "_No evaluation results yet. Run `make eval-naive` / `make eval-guarded`, "
            "then `make report`._"
        )
    modes = [m for m in MODES if m in results]
    lines = [
        "| Metric (mean ± std across runs) | " + " | ".join(modes) + " |",
        "|---|" + "---|" * len(modes),
    ]
    for label, key, kind in ROWS:
        cells = [
            cell(results[m]["overall"]["metrics"][key], kind, results[m].get("usd_to_inr"))
            for m in modes
        ]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")

    lines += ["", "Per suite:", ""]
    header = ["Suite", *[f"{m}: {k.replace('_', ' ')}" for m in modes for k in SUITE_ROWS]]
    lines += ["| " + " | ".join(header) + " |", "|---|" + "---|" * (len(header) - 1)]
    suites = sorted({s for m in modes for s in results[m]["suites"]})
    for suite in suites:
        cells = []
        for m in modes:
            block = results[m]["suites"].get(suite)
            for key in SUITE_ROWS:
                kind = "pct" if key.endswith("rate") else "count"
                cells.append(cell(block["metrics"][key], kind) if block else "–")
        lines.append(f"| {suite} | " + " | ".join(cells) + " |")

    lines.append("")
    for m in modes:
        r = results[m]
        per_run = r["overall"]["n_scenarios_per_run"]
        status = "complete" if r["complete"] else f"INCOMPLETE ({r['missing_results']} missing)"
        lines.append(
            f"- **{m}**: n = {', '.join(f'run {k}: {v}' for k, v in per_run.items())} "
            f"scenarios; model `{r.get('model')}`, prompt `{r.get('prompt_version')}`, "
            f"policy v{r.get('policy_version')}, eval date {r.get('eval_today')}; {status}."
        )
    lines.append(
        "- Costs use the dated list prices in `config/models.yaml`; ₹ conversions use the dated "
        "rate recorded there."
    )
    return "\n".join(lines)


def update_readme(readme: Path, results_dir: Path) -> None:
    text = readme.read_text()
    if START not in text or END not in text:
        raise ValueError(f"{readme} is missing the {START} / {END} markers")
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    readme.write_text(f"{head}{START}\n{render(load(results_dir))}\n{END}{tail}")
