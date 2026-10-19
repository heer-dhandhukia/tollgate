"""Webhook delivery: HMAC-signed, retried, and optionally duplicated or reordered (faults)."""

import asyncio
import contextlib
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

import httpx

from mockpay.config import MockpaySettings
from mockpay.store import OutgoingEvent

SIGNATURE_HEADER = "X-Mockpay-Signature"
EVENT_ID_HEADER = "X-Mockpay-Event-Id"


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(secret: str, body: bytes, signature: str) -> bool:
    return hmac.compare_digest(sign(secret, body), signature)


@dataclass(frozen=True)
class Delivery:
    event_id: str
    type: str
    sequence: int
    attempt: int
    status_code: int | None
    error: str | None = None


class Dispatcher:
    """Delivers events in the order they are queued, one at a time."""

    def __init__(
        self, settings: MockpaySettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(transport=transport, timeout=5.0)
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.held: dict[str, list[dict[str, Any]]] = {}
        self.deliveries: list[Delivery] = []
        self._worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        await self.client.aclose()

    def publish(self, event: OutgoingEvent) -> None:
        mode = event.webhook_mode
        if mode == "reverse":
            held = self.held.setdefault(event.refund_id, [])
            held.append(event.envelope)
            if event.terminal:
                for envelope in reversed(self.held.pop(event.refund_id)):
                    self.queue.put_nowait(envelope)
            return
        copies = int(mode.split(":", 1)[1]) if mode.startswith("duplicate:") else 1
        for _ in range(copies):
            self.queue.put_nowait(event.envelope)

    def flush_held(self) -> None:
        for refund_id in list(self.held):
            for envelope in reversed(self.held.pop(refund_id)):
                self.queue.put_nowait(envelope)

    async def drain(self) -> None:
        await self.queue.join()

    async def _run(self) -> None:
        while True:
            envelope = await self.queue.get()
            try:
                await self._deliver(envelope)
            finally:
                self.queue.task_done()

    async def _deliver(self, envelope: dict[str, Any]) -> None:
        body = json.dumps(envelope, separators=(",", ":")).encode()
        headers = {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: sign(self.settings.webhook_secret, body),
            EVENT_ID_HEADER: envelope["id"],
        }
        url = self.settings.webhook_url
        for attempt in range(1, self.settings.webhook_max_attempts + 1):
            if not url:
                self._record(envelope, attempt, None, "no webhook_url configured")
                return
            try:
                response = await self.client.post(url, content=body, headers=headers)
            except httpx.HTTPError as exc:
                self._record(envelope, attempt, None, repr(exc))
            else:
                self._record(envelope, attempt, response.status_code)
                if response.is_success:
                    return
            await asyncio.sleep(0.05 * 2**attempt)

    def _record(
        self, envelope: dict[str, Any], attempt: int, status: int | None, error: str | None = None
    ) -> None:
        self.deliveries.append(
            Delivery(envelope["id"], envelope["type"], envelope["sequence"], attempt, status, error)
        )
