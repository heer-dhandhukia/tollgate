import json
from typing import Any

from tests.fakes import Scripted, text, tool_call
from tollgate.agent.loop import run_conversation
from tollgate.agent.tools import (
    CancelOrderArgs,
    EscalateArgs,
    IssueRefundArgs,
    LookupOrderArgs,
    dispatch,
    tool_schemas,
)
from tollgate.llm.client import LLMClient
from tollgate.llm.pricing import PriceTable

PRICES = PriceTable({}, 84.0, "2025-01-01")


class RecordingBackend:
    def __init__(self, crash: bool = False) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.crash = crash

    async def lookup_order(self, args: LookupOrderArgs) -> dict[str, Any]:
        self.calls.append(("lookup_order", args))
        if self.crash:
            raise RuntimeError("db down")
        return {"order_id": args.order_id, "status": "delivered"}

    async def issue_refund(self, args: IssueRefundArgs) -> dict[str, Any]:
        self.calls.append(("issue_refund", args))
        return {"status": "refund_initiated"}

    async def cancel_order(self, args: CancelOrderArgs) -> dict[str, Any]:
        self.calls.append(("cancel_order", args))
        return {"status": "cancelled"}

    async def escalate_to_human(self, args: EscalateArgs) -> dict[str, Any]:
        self.calls.append(("escalate_to_human", args))
        return {"status": "escalated"}


def test_schemas_cover_all_tools_and_amount_is_integer() -> None:
    by_name = {t["function"]["name"]: t["function"] for t in tool_schemas()}
    assert set(by_name) == {"lookup_order", "issue_refund", "cancel_order", "escalate_to_human"}
    props = by_name["issue_refund"]["parameters"]["properties"]
    assert props["amount_paise"]["type"] == "integer"
    # Identity comes from the session, never from a tool argument the model could fill in.
    for fn in by_name.values():
        assert not any("customer" in name for name in fn["parameters"]["properties"])


async def test_dispatch_rejects_bad_arguments() -> None:
    backend = RecordingBackend()
    float_amount = json.dumps({"order_id": "ORD-1", "amount_paise": 499.0, "reason": "x"})
    assert "Invalid arguments" in (await dispatch(backend, "issue_refund", float_amount))["error"]
    assert "not valid JSON" in (await dispatch(backend, "lookup_order", "{"))["error"]
    assert "Unknown tool" in (await dispatch(backend, "transfer_funds", "{}"))["error"]
    extra = json.dumps({"order_id": "ORD-1", "customer_id": 7})
    assert "Invalid arguments" in (await dispatch(backend, "lookup_order", extra))["error"]
    assert backend.calls == []


async def test_loop_runs_tools_until_text_reply() -> None:
    fn = Scripted(
        tool_call("lookup_order", order_id="ORD-1"),
        tool_call("issue_refund", "call_2", order_id="ORD-1", amount_paise=49900, reason="torn"),
        text("Refunded ₹499."),
    )
    backend = RecordingBackend()
    llm = LLMClient("m", None, PRICES, completion_fn=fn)
    t = await run_conversation(
        llm, backend, "sys", ["refund please"], prompt_version="t", run_index=1
    )
    assert [c[0] for c in backend.calls] == ["lookup_order", "issue_refund"]
    assert t.stopped_reason == "completed"
    assert t.messages[-1]["content"] == "Refunded ₹499."
    assert [m["role"] for m in t.messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
    assert len(t.llm_calls) == 3


async def test_loop_stops_at_max_steps() -> None:
    fn = Scripted(*[tool_call("lookup_order", f"c{i}", order_id="ORD-1") for i in range(3)])
    llm = LLMClient("m", None, PRICES, completion_fn=fn)
    t = await run_conversation(
        llm,
        RecordingBackend(),
        "sys",
        ["hi", "never sent"],
        prompt_version="t",
        run_index=1,
        max_steps=3,
    )
    assert t.stopped_reason == "max_steps"
    assert sum(m["role"] == "user" for m in t.messages) == 1


async def test_tool_crash_is_reported_to_the_model() -> None:
    fn = Scripted(tool_call("lookup_order", order_id="ORD-1"), text("Sorry"))
    llm = LLMClient("m", None, PRICES, completion_fn=fn)
    t = await run_conversation(
        llm, RecordingBackend(crash=True), "sys", ["hi"], prompt_version="t", run_index=1
    )
    assert t.tool_events[0].result == {"error": "Internal error in lookup_order: RuntimeError"}


async def test_multi_turn_conversation_appends_each_customer_turn() -> None:
    fn = Scripted(text("Which order?"), text("Thanks"))
    llm = LLMClient("m", None, PRICES, completion_fn=fn)
    t = await run_conversation(
        llm, RecordingBackend(), "sys", ["refund", "ORD-1"], prompt_version="t", run_index=1
    )
    assert [m["content"] for m in t.messages if m["role"] == "user"] == ["refund", "ORD-1"]
