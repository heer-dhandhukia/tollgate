import asyncio
from pathlib import Path

import pytest

from tests.fakes import HTTPStatusError, Scripted, text, tool_call
from tollgate.llm.cache import CachedResponse, ResponseCache, cache_key
from tollgate.llm.client import LLMClient, LLMUnavailable
from tollgate.llm.pricing import Price, PriceTable

PRICES = PriceTable({"m": Price(1.0, 2.0, "2025-01-01")}, 84.0, "2025-01-01")
MSGS = [{"role": "user", "content": "hi"}]


def _client(tmp_path: Path, fn: Scripted, **kw: object) -> tuple[LLMClient, list[float]]:
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    client = LLMClient(
        "m", ResponseCache(tmp_path), PRICES, completion_fn=fn, sleep=fake_sleep, **kw
    )  # type: ignore[arg-type]
    return client, sleeps


def test_key_ignores_dict_order_but_not_content() -> None:
    a = cache_key("m", [{"role": "user", "content": "x"}], None, {"t": 1, "u": 2}, "v1", 1)
    b = cache_key("m", [{"content": "x", "role": "user"}], None, {"u": 2, "t": 1}, "v1", 1)
    assert a == b
    assert a != cache_key("m", [{"role": "user", "content": "x "}], None, {}, "v1", 1)


@pytest.mark.parametrize(
    "change",
    [{"model": "m2"}, {"prompt_version": "v2"}, {"run_index": 2}, {"tools": [{"x": 1}]}],
)
def test_key_changes_with_every_input(change: dict[str, object]) -> None:
    base = {"model": "m", "messages": MSGS, "tools": None, "params": {}, "prompt_version": "v1"}
    base["run_index"] = 1
    assert cache_key(**base) != cache_key(**{**base, **change})  # type: ignore[arg-type]


def test_cache_round_trip_and_stable_bytes(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    entry = CachedResponse("ab" * 32, "m", {"role": "assistant", "content": "₹"}, 1, 2, 3.0, 0.1)
    cache.put(entry)
    first = next(tmp_path.rglob("*.json.gz")).read_bytes()
    cache.put(entry)
    assert next(tmp_path.rglob("*.json.gz")).read_bytes() == first
    assert cache.get(entry.key) == entry


def test_corrupted_entry_is_a_miss(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    key = "cd" * 32
    path = tmp_path / key[:2] / f"{key}.json.gz"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not gzip")
    assert cache.get(key) is None


async def test_second_call_is_served_from_cache_with_recorded_metrics(tmp_path: Path) -> None:
    fn = Scripted(text("hello"))
    client, _ = _client(tmp_path, fn)
    first = await client.complete(MSGS, None, prompt_version="v1", run_index=1)
    again = await client.complete(MSGS, None, prompt_version="v1", run_index=1)
    assert len(fn.calls) == 1
    assert not first.cache_hit and again.cache_hit
    assert again.message == first.message == {"role": "assistant", "content": "hello"}
    assert again.latency_ms == first.latency_ms
    assert again.cost_usd == pytest.approx((100 * 1.0 + 20 * 2.0) / 1e6)


async def test_run_index_is_an_independent_sample(tmp_path: Path) -> None:
    fn = Scripted(text("a"), text("b"))
    client, _ = _client(tmp_path, fn)
    r1 = await client.complete(MSGS, None, prompt_version="v1", run_index=1)
    r2 = await client.complete(MSGS, None, prompt_version="v1", run_index=2)
    assert (r1.message["content"], r2.message["content"]) == ("a", "b")


async def test_retries_rate_limit_then_succeeds(tmp_path: Path) -> None:
    fn = Scripted(HTTPStatusError(429), HTTPStatusError(503), text("ok"))
    client, sleeps = _client(tmp_path, fn, backoff_base_s=1.0)
    result = await client.complete(MSGS, None, prompt_version="v1", run_index=1)
    assert result.message["content"] == "ok"
    assert len(sleeps) == 2
    assert 0.5 <= sleeps[0] <= 1.0 and 1.0 <= sleeps[1] <= 2.0


async def test_retry_after_header_is_honoured_and_capped(tmp_path: Path) -> None:
    fn = Scripted(HTTPStatusError(429, retry_after="7"), HTTPStatusError(429, "999"), text("ok"))
    client, sleeps = _client(tmp_path, fn, backoff_cap_s=30.0)
    await client.complete(MSGS, None, prompt_version="v1", run_index=1)
    assert sleeps == [7.0, 30.0]


async def test_gives_up_and_flags_rate_limit(tmp_path: Path) -> None:
    fn = Scripted(*[HTTPStatusError(429) for _ in range(4)])
    client, sleeps = _client(tmp_path, fn, max_retries=3)
    with pytest.raises(LLMUnavailable) as info:
        await client.complete(MSGS, None, prompt_version="v1", run_index=1)
    assert info.value.rate_limited
    assert len(sleeps) == 3


async def test_non_retryable_error_is_not_retried(tmp_path: Path) -> None:
    fn = Scripted(HTTPStatusError(400))
    client, sleeps = _client(tmp_path, fn)
    with pytest.raises(LLMUnavailable) as info:
        await client.complete(MSGS, None, prompt_version="v1", run_index=1)
    assert not info.value.rate_limited and sleeps == []


async def test_tool_calls_are_normalised(tmp_path: Path) -> None:
    fn = Scripted(tool_call("lookup_order", order_id="ORD-1"))
    client, _ = _client(tmp_path, fn)
    result = await client.complete(MSGS, [{"type": "function"}], prompt_version="v1", run_index=1)
    assert result.message == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup_order", "arguments": '{"order_id": "ORD-1"}'},
            }
        ],
    }


async def test_concurrency_is_capped(tmp_path: Path) -> None:
    in_flight = peak = 0

    async def slow(**_: object) -> object:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return text("x")

    client = LLMClient("m", None, PRICES, completion_fn=slow, concurrency=2)
    await asyncio.gather(
        *(
            client.complete(
                [{"role": "user", "content": str(i)}], None, prompt_version="v", run_index=1
            )
            for i in range(6)
        )
    )
    assert peak == 2
