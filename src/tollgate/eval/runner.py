"""Runs scenario suites through an agent mode, scores each conversation, and writes results.

Resumable: each finished (mode, suite, run, scenario) writes one JSON file, and files that
already exist are skipped. If the LLM provider keeps rate-limiting, the runner stops starting
new scenarios and exits cleanly; rerunning the same command continues where it stopped.
"""

import asyncio
import json
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from mockpay.app import Provider, create_app
from mockpay.config import MockpaySettings
from tollgate.agent.loop import Transcript, run_conversation
from tollgate.agent.prompting import system_prompt
from tollgate.agent.session import AuthenticatedSession
from tollgate.agent.tools import ToolBackend
from tollgate.clock import FixedClock
from tollgate.db.models import Escalation, Order
from tollgate.eval.aggregate import aggregate
from tollgate.eval.sandbox import Sandboxes
from tollgate.eval.scenario import Product, Scenario, sandbox_name
from tollgate.eval.score import ConversationStats, ProviderRefund, ScenarioScore, score
from tollgate.eval.seed import SEED_RECEIPT_PREFIX, Seeded, seed
from tollgate.llm.client import LLMClient, LLMUnavailable
from tollgate.naive.backend import NaiveBackend
from tollgate.policy_config import PolicyConfig
from tollgate.provider_client import ProviderClient

log = logging.getLogger("tollgate.eval")

PROMPT_VERSION = "v1"

BackendFactory = Callable[
    [AuthenticatedSession, async_sessionmaker[AsyncSession], ProviderClient, FixedClock],
    ToolBackend,
]
BACKENDS: dict[str, BackendFactory] = {"naive": NaiveBackend}

# mockpay must behave deterministically inside evals: no random faults, no background sweeper.
QUIET_PROVIDER = {
    "settle_poll_ms": 0,
    "settle_delay_ms": 0,
    "webhook_url": "",
    "p_latency": 0.0,
    "p_error_before_commit": 0.0,
    "p_error_after_commit": 0.0,
    "p_timeout_after_commit": 0.0,
    "p_duplicate_webhook": 0.0,
    "p_out_of_order_webhooks": 0.0,
    "p_refund_fails": 0.0,
}


@dataclass
class RunSummary:
    completed: int = 0
    skipped: int = 0
    failed: list[str] = field(default_factory=list)
    stopped_for_rate_limit: bool = False


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False, default=str)
        f.write("\n")
    os.replace(tmp, path)


class EvalRunner:
    def __init__(
        self,
        *,
        llm: LLMClient,
        sandboxes: Sandboxes,
        policy: PolicyConfig,
        catalog: dict[str, Product],
        clock: FixedClock,
        results_dir: Path,
        provider_timeout_s: float = 5.0,
        scenario_concurrency: int = 2,
    ) -> None:
        self.llm = llm
        self.sandboxes = sandboxes
        self.policy = policy
        self.catalog = catalog
        self.clock = clock
        self.results_dir = results_dir
        self.provider_timeout_s = provider_timeout_s
        self.scenario_slots = asyncio.Semaphore(scenario_concurrency)
        self._stop = False

    def result_path(self, mode: str, scenario: Scenario, run_index: int) -> Path:
        return (
            self.results_dir
            / "runs"
            / mode
            / scenario.suite
            / f"run{run_index}"
            / f"{scenario.id}.json"
        )

    async def run(
        self, mode: str, suites: dict[str, list[Scenario]], runs: list[int]
    ) -> RunSummary:
        summary = RunSummary()
        await self.sandboxes.prepare()
        jobs = [
            (scenario, run_index)
            for run_index in runs
            for scenarios in suites.values()
            for scenario in scenarios
        ]

        async def one(scenario: Scenario, run_index: int) -> None:
            label = f"{mode}/run{run_index}/{scenario.id}"
            if self.result_path(mode, scenario, run_index).exists():
                summary.skipped += 1
                return
            async with self.scenario_slots:
                if self._stop:
                    summary.failed.append(f"{label} (not started: rate limited)")
                    return
                try:
                    await self.run_one(mode, scenario, run_index)
                    summary.completed += 1
                    log.info("done %s", label)
                except LLMUnavailable as exc:
                    summary.failed.append(f"{label}: {exc}")
                    if exc.rate_limited:
                        self._stop = True
                        summary.stopped_for_rate_limit = True
                        log.warning("rate limited; stopping. Rerun the same command to resume.")
                except Exception as exc:
                    log.exception("scenario %s crashed", label)
                    summary.failed.append(f"{label}: {type(exc).__name__}: {exc}")

        await asyncio.gather(*(one(s, r) for s, r in jobs))
        return summary

    async def run_one(self, mode: str, scenario: Scenario, run_index: int) -> ScenarioScore:
        db = sandbox_name(self.sandboxes.prefix, mode, f"r{run_index}", scenario.id)
        async with self.sandboxes.scenario(db) as (ledger_url, provider_url):
            engine = create_async_engine(ledger_url, poolclass=NullPool)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            app = create_app(MockpaySettings(database_url=provider_url, **QUIET_PROVIDER))
            try:
                async with app.router.lifespan_context(app):
                    provider_state: Provider = app.state.provider
                    transport = httpx.ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport, base_url="http://mockpay.local"
                    ) as http:
                        provider = ProviderClient(http, self.provider_timeout_s)
                        seeded = await seed(scenario, self.catalog, maker, provider, self.clock)
                        await provider_state.settle_due()  # seeded refunds settle first
                        if scenario.faults:
                            provider_state.faults.script(scenario.faults)
                        backend = BACKENDS[mode](seeded.session, maker, provider, self.clock)
                        prompt = system_prompt(
                            mode, PROMPT_VERSION, seeded.session, self.policy, self.clock.today()
                        )
                        transcript = await run_conversation(
                            self.llm,
                            backend,
                            prompt,
                            scenario.turns,
                            prompt_version=f"{mode}.{PROMPT_VERSION}",
                            run_index=run_index,
                        )
                        await provider_state.settle_due()
                        refunds = await self._provider_refunds(provider_state, scenario, seeded)
                    statuses, escalated = await self._ledger_state(maker, seeded)
            finally:
                await engine.dispose()

        events: list[dict[str, Any]] = list(getattr(backend, "events", []))
        stats = ConversationStats(
            llm_calls=len(transcript.llm_calls),
            prompt_tokens=sum(c["prompt_tokens"] for c in transcript.llm_calls),
            completion_tokens=sum(c["completion_tokens"] for c in transcript.llm_calls),
            latency_ms=round(transcript.llm_latency_ms + transcript.tool_latency_ms, 1),
            cost_usd=transcript.cost_usd,
            escalated=escalated,
            ledger_rejections_after_provider=sum(
                e["type"] == "ledger_rejected_after_provider" for e in events
            ),
            stopped_reason=transcript.stopped_reason,
        )
        result = score(scenario, run_index, refunds, statuses, stats)
        self._write(mode, scenario, run_index, result, transcript, events, refunds)
        return result

    async def _provider_refunds(
        self, provider: Provider, scenario: Scenario, seeded: Seeded
    ) -> list[ProviderRefund]:
        out: list[ProviderRefund] = []
        for ref, order in seeded.orders.items():
            if order.provider_payment_id is None:
                continue
            for r in await provider.store.list_refunds(payment_id=order.provider_payment_id):
                if (r.receipt or "").startswith(SEED_RECEIPT_PREFIX):
                    continue
                out.append(ProviderRefund(ref, r.amount_paise, r.status))
        return out

    async def _ledger_state(
        self, maker: async_sessionmaker[AsyncSession], seeded: Seeded
    ) -> tuple[dict[str, str], bool]:
        async with maker() as s:
            rows = await s.execute(
                select(Order.ref, Order.status).where(
                    Order.customer_id == seeded.session.customer_id
                )
            )
            statuses = {ref: status for ref, status in rows}
            escalations = await s.scalar(
                select(func.count())
                .select_from(Escalation)
                .where(Escalation.conversation_id == seeded.session.conversation_id)
            )
        return statuses, bool(escalations)

    def _write(
        self,
        mode: str,
        scenario: Scenario,
        run_index: int,
        result: ScenarioScore,
        transcript: Transcript,
        events: list[dict[str, Any]],
        refunds: list[ProviderRefund],
    ) -> None:
        _write_json(
            self.result_path(mode, scenario, run_index),
            {
                "score": asdict(result),
                "provider_refunds": [asdict(r) for r in refunds],
                "events": events,
                "transcript": transcript.to_json(),
            },
        )


def load_scores(results_dir: Path, mode: str) -> list[ScenarioScore]:
    scores = []
    for path in sorted((results_dir / "runs" / mode).glob("*/run*/*.json")):
        scores.append(ScenarioScore(**json.loads(path.read_text())["score"]))
    return scores


def write_summary(
    results_dir: Path,
    mode: str,
    suites: dict[str, list[Scenario]],
    runs: list[int],
    meta: dict[str, Any],
) -> Path:
    """Aggregate whatever per-scenario results exist into results/eval_<mode>.json."""
    wanted = {(s.id, r) for scenarios in suites.values() for s in scenarios for r in runs}
    scores = [s for s in load_scores(results_dir, mode) if (s.scenario_id, s.run_index) in wanted]
    missing = len(wanted) - len(scores)
    summary = {
        "mode": mode,
        **meta,
        "runs": runs,
        "suites_requested": sorted(suites),
        "complete": missing == 0,
        "missing_results": missing,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        **aggregate(scores, runs),
    }
    path = results_dir / f"eval_{mode}.json"
    _write_json(path, summary)
    return path
