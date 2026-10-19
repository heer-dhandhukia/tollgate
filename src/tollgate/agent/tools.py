"""Agent tool definitions (OpenAI function-calling schema) and argument validation."""

import json
from typing import Annotated, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LookupOrderArgs(_Args):
    order_id: str = Field(max_length=64)


class IssueRefundArgs(_Args):
    order_id: str = Field(max_length=64)
    amount_paise: Annotated[StrictInt, Field(gt=0)]
    reason: str = Field(max_length=500)


class CancelOrderArgs(_Args):
    order_id: str = Field(max_length=64)


class EscalateArgs(_Args):
    summary: str = Field(max_length=1000)
    order_id: str | None = Field(default=None, max_length=64)


ARGS: dict[str, type[_Args]] = {
    "lookup_order": LookupOrderArgs,
    "issue_refund": IssueRefundArgs,
    "cancel_order": CancelOrderArgs,
    "escalate_to_human": EscalateArgs,
}

DESCRIPTIONS = {
    "lookup_order": "Look up one of the current customer's orders by its order id (e.g. ORD-1001).",
    "issue_refund": (
        "Refund part or all of an order's payment to the customer. amount_paise is an integer "
        "number of paise (₹1 = 100 paise), e.g. ₹499 is 49900."
    ),
    "cancel_order": "Cancel an order. If it was already paid, the remaining amount is refunded.",
    "escalate_to_human": (
        "Hand the conversation to a human agent, with a short summary of what the customer wants "
        "and why you could not resolve it."
    ),
}


def tool_schemas() -> list[dict[str, Any]]:
    tools = []
    for name, model in ARGS.items():
        schema = model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        tools.append(
            {
                "type": "function",
                "function": {"name": name, "description": DESCRIPTIONS[name], "parameters": schema},
            }
        )
    return tools


class ToolBackend(Protocol):
    async def lookup_order(self, args: LookupOrderArgs) -> dict[str, Any]: ...

    async def issue_refund(self, args: IssueRefundArgs) -> dict[str, Any]: ...

    async def cancel_order(self, args: CancelOrderArgs) -> dict[str, Any]: ...

    async def escalate_to_human(self, args: EscalateArgs) -> dict[str, Any]: ...


async def dispatch(backend: ToolBackend, name: str, raw_arguments: str) -> dict[str, Any]:
    """Validate and run one tool call. Problems come back as {"error": ...} for the model."""
    model = ARGS.get(name)
    if model is None:
        return {"error": f"Unknown tool {name!r}."}
    try:
        args = model.model_validate(json.loads(raw_arguments or "{}"))
    except json.JSONDecodeError:
        return {"error": "Tool arguments were not valid JSON."}
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'arguments'}: {e['msg']}" for e in exc.errors()
        )
        return {"error": f"Invalid arguments: {problems}"}
    method = getattr(backend, name)
    result: dict[str, Any] = await method(args)
    return result
