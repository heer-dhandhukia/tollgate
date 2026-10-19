"""Test doubles for LLM responses (litellm-shaped)."""

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeFunction:
    name: str
    arguments: str


@dataclass
class FakeToolCall:
    id: str
    function: FakeFunction
    type: str = "function"


@dataclass
class FakeMessage:
    content: str | None
    tool_calls: list[FakeToolCall] | None = None
    role: str = "assistant"

    def model_dump(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": [
                {
                    "id": c.id,
                    "type": c.type,
                    "function": {"name": c.function.name, "arguments": c.function.arguments},
                }
                for c in self.tool_calls or []
            ]
            or None,
        }


@dataclass
class FakeChoice:
    message: FakeMessage


@dataclass
class FakeUsage:
    prompt_tokens: int = 100
    completion_tokens: int = 20


@dataclass
class FakeResponse:
    choices: list[FakeChoice]
    usage: FakeUsage = field(default_factory=FakeUsage)


def text(content: str) -> FakeResponse:
    return FakeResponse([FakeChoice(FakeMessage(content))])


def tool_call(name: str, call_id: str = "call_1", **arguments: Any) -> FakeResponse:
    call = FakeToolCall(call_id, FakeFunction(name, json.dumps(arguments)))
    return FakeResponse([FakeChoice(FakeMessage(None, [call]))])


class HTTPStatusError(Exception):
    def __init__(self, status_code: int, retry_after: str | None = None) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code
        self.response = type(
            "R", (), {"status_code": status_code, "headers": {"retry-after": retry_after}}
        )()


class Scripted:
    """completion_fn returning queued responses (or raising queued exceptions) in order."""

    def __init__(self, *items: Any) -> None:
        self.items = list(items)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item
