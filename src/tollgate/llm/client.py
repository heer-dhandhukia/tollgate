"""LLM client: cache → concurrency cap → litellm, with exponential backoff.

Free-tier safety: a semaphore caps in-flight calls, rate-limit and transient errors are retried
with exponential backoff and jitter (honouring Retry-After), and every response is cached so a
crashed or rate-limited run resumes without paying for the same call twice.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from tollgate.llm.cache import CachedResponse, ResponseCache, cache_key
from tollgate.llm.pricing import PriceTable

CompletionFn = Callable[..., Awaitable[Any]]
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class LLMUnavailable(Exception):
    """Retries exhausted. `rate_limited` tells the runner to stop and resume later."""

    def __init__(self, message: str, rate_limited: bool) -> None:
        super().__init__(message)
        self.rate_limited = rate_limited


@dataclass(frozen=True)
class LLMResult:
    message: dict[str, Any]
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    cost_usd: float | None
    cache_hit: bool


def _status_of(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _retry_after_s(exc: BaseException) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        value = headers.get("retry-after")
        return float(value) if value is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def _is_retryable(exc: BaseException) -> bool:
    status = _status_of(exc)
    if status is not None:
        return status in RETRYABLE_STATUS
    name = type(exc).__name__
    return any(
        marker in name
        for marker in ("RateLimit", "Timeout", "Connection", "Unavailable", "Overloaded")
    )


def normalise_message(message: Any) -> dict[str, Any]:
    """Reduce a provider message to the stable fields we feed back into the conversation."""
    raw = message.model_dump() if hasattr(message, "model_dump") else dict(message)
    out: dict[str, Any] = {"role": "assistant", "content": raw.get("content")}
    calls = raw.get("tool_calls") or []
    if calls:
        out["tool_calls"] = [
            {
                "id": c["id"],
                "type": "function",
                "function": {
                    "name": c["function"]["name"],
                    "arguments": c["function"]["arguments"] or "{}",
                },
            }
            for c in calls
        ]
    return out


class LLMClient:
    def __init__(
        self,
        model: str,
        cache: ResponseCache | None,
        prices: PriceTable,
        *,
        concurrency: int = 2,
        max_retries: int = 6,
        backoff_base_s: float = 1.0,
        backoff_cap_s: float = 60.0,
        params: dict[str, Any] | None = None,
        completion_fn: CompletionFn | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.model = model
        self.cache = cache
        self.prices = prices
        self.semaphore = asyncio.Semaphore(concurrency)
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self.params = params or {}
        self._completion_fn = completion_fn
        self._sleep = sleep
        self._rng = rng or random.Random(0)

    async def _call(self, **kwargs: Any) -> Any:
        if self._completion_fn is not None:
            return await self._completion_fn(**kwargs)
        import litellm  # imported lazily: heavy, and unnecessary when replaying from cache

        return await litellm.acompletion(**kwargs)

    def _backoff(self, attempt: int, exc: BaseException) -> float:
        retry_after = _retry_after_s(exc)
        if retry_after is not None:
            return min(retry_after, self.backoff_cap_s)
        ceiling = min(self.backoff_cap_s, self.backoff_base_s * 2**attempt)
        return ceiling * (0.5 + 0.5 * self._rng.random())

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        prompt_version: str,
        run_index: int,
    ) -> LLMResult:
        key = cache_key(self.model, messages, tools, self.params, prompt_version, run_index)
        if self.cache is not None and (hit := self.cache.get(key)) is not None:
            return LLMResult(
                hit.message,
                hit.model,
                hit.prompt_tokens,
                hit.completion_tokens,
                hit.latency_ms,
                hit.cost_usd,
                cache_hit=True,
            )

        last: BaseException | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with self.semaphore:
                    started = time.perf_counter()
                    response = await self._call(
                        model=self.model, messages=messages, tools=tools, **self.params
                    )
                    latency_ms = (time.perf_counter() - started) * 1000
                break
            except Exception as exc:
                if not _is_retryable(exc) or attempt == self.max_retries:
                    raise LLMUnavailable(
                        f"{self.model}: {type(exc).__name__}: {exc}",
                        rate_limited=_status_of(exc) == 429 or "RateLimit" in type(exc).__name__,
                    ) from exc
                last = exc
                await self._sleep(self._backoff(attempt, exc))
        else:  # pragma: no cover - loop always breaks or raises
            raise LLMUnavailable(str(last), rate_limited=False)

        message = normalise_message(response.choices[0].message)
        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        cost = self.prices.cost_usd(self.model, prompt_tokens, completion_tokens)
        entry = CachedResponse(
            key, self.model, message, prompt_tokens, completion_tokens, round(latency_ms, 1), cost
        )
        if self.cache is not None:
            self.cache.put(entry)
        return LLMResult(
            message, self.model, prompt_tokens, completion_tokens, entry.latency_ms, cost, False
        )
