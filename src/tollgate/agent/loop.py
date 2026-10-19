"""The tool-calling loop. Mode-agnostic: naive and guarded differ only in the ToolBackend."""

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from tollgate.agent.tools import ToolBackend, dispatch, tool_schemas
from tollgate.llm.client import LLMClient, LLMResult


@dataclass
class ToolEvent:
    turn: int
    name: str
    arguments: str
    result: dict[str, Any]
    duration_ms: float


@dataclass
class Transcript:
    messages: list[dict[str, Any]]
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_events: list[ToolEvent] = field(default_factory=list)
    stopped_reason: str = "completed"

    @property
    def llm_latency_ms(self) -> float:
        return sum(c["latency_ms"] for c in self.llm_calls)

    @property
    def tool_latency_ms(self) -> float:
        return sum(e.duration_ms for e in self.tool_events)

    @property
    def cost_usd(self) -> float | None:
        costs = [c["cost_usd"] for c in self.llm_calls]
        return None if any(c is None for c in costs) else sum(costs)

    def to_json(self) -> dict[str, Any]:
        return {
            "messages": self.messages,
            "llm_calls": self.llm_calls,
            "tool_events": [asdict(e) for e in self.tool_events],
            "stopped_reason": self.stopped_reason,
        }


def _record(result: LLMResult) -> dict[str, Any]:
    return {
        "model": result.model,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "latency_ms": result.latency_ms,
        "cost_usd": result.cost_usd,
        "cache_hit": result.cache_hit,
    }


async def run_conversation(
    llm: LLMClient,
    backend: ToolBackend,
    system_prompt: str,
    customer_turns: list[str],
    *,
    prompt_version: str,
    run_index: int,
    max_steps: int = 8,
) -> Transcript:
    tools = tool_schemas()
    transcript = Transcript(messages=[{"role": "system", "content": system_prompt}])
    messages = transcript.messages
    for turn, customer_text in enumerate(customer_turns):
        messages.append({"role": "user", "content": customer_text})
        for _ in range(max_steps):
            result = await llm.complete(
                messages, tools, prompt_version=prompt_version, run_index=run_index
            )
            transcript.llm_calls.append(_record(result))
            messages.append(result.message)
            calls = result.message.get("tool_calls") or []
            if not calls:
                break
            for call in calls:
                name = call["function"]["name"]
                arguments = call["function"]["arguments"]
                started = time.perf_counter()
                try:
                    output = await dispatch(backend, name, arguments)
                except Exception as exc:  # a tool crash is reported to the model, like an API error
                    output = {"error": f"Internal error in {name}: {type(exc).__name__}"}
                duration = (time.perf_counter() - started) * 1000
                transcript.tool_events.append(ToolEvent(turn, name, arguments, output, duration))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(output, ensure_ascii=False, sort_keys=True),
                    }
                )
        else:
            transcript.stopped_reason = "max_steps"
            return transcript
    return transcript
