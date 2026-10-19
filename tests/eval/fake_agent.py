"""A rule-based stand-in for the LLM, so the harness can be tested end to end without a model.

It behaves like a compliant-but-gullible agent: looks the order up, then refunds the ₹ amount
the customer named (or the refundable amount), or cancels when asked."""

import json
import re
from typing import Any

from tests.fakes import text, tool_call

REF = re.compile(r"ORD-[A-Z0-9-]+")
RUPEES = re.compile(r"₹([\d,]+)(?:\.(\d{2}))?")


class RuleAgent:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *, messages: list[dict[str, Any]], **_: Any) -> Any:
        self.calls += 1
        last = messages[-1]
        user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        if last["role"] == "user":
            match = REF.search(user)
            if not match:
                return text("Which order is this about?")
            return tool_call("lookup_order", "call_lookup", order_id=match.group(0))
        if last["role"] == "tool":
            prev = messages[-2]["tool_calls"][0]["function"]["name"]
            result = json.loads(last["content"])
            if prev != "lookup_order" or "error" in result:
                return text("All done." if "error" not in result else "Sorry, that failed.")
            if "cancel" in user.lower():
                return tool_call("cancel_order", "call_cancel", order_id=result["order_id"])
            m = RUPEES.search(user)
            amount = (
                int(m.group(1).replace(",", "")) * 100 + int(m.group(2) or 0)
                if m
                else result["refundable_paise"]
            )
            return tool_call(
                "issue_refund",
                "call_refund",
                order_id=result["order_id"],
                amount_paise=amount,
                reason="customer request",
            )
        return text("Hello")
