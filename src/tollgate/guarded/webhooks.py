"""Receives the provider's refund webhooks and applies them to the ledger.

Webhooks are delivered at least once, may be duplicated, and may arrive out of order (mockpay
can do all three on purpose). The receiver therefore:

- verifies the HMAC-SHA256 signature over the raw body before reading anything else;
- stores each event id once (`webhook_events` primary key): a redelivery is a no-op;
- never moves a refund backwards: `refund.created` after `refund.processed` is ignored as stale,
  and settling an already-settled refund does nothing (the ledger operations are idempotent).

The provider's own SDK would normally verify signatures; this is a small reimplementation so
Tollgate doesn't import the provider's code.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.db.models import Refund, RefundIntent, WebhookEvent
from tollgate.guarded.executor import audit
from tollgate.ledger import service
from tollgate.ledger.service import LedgerError

SIGNATURE_HEADER = "X-Mockpay-Signature"


def signature_for(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class WebhookResult:
    status_code: int
    outcome: str


class WebhookReceiver:
    def __init__(self, maker: async_sessionmaker[AsyncSession], secret: str) -> None:
        self.maker = maker
        self.secret = secret

    async def handle(self, body: bytes, signature: str | None) -> WebhookResult:
        if not signature or not hmac.compare_digest(signature_for(self.secret, body), signature):
            return WebhookResult(401, "bad_signature")
        try:
            envelope: dict[str, Any] = json.loads(body)
            event_id = str(envelope["id"])
            event_type = str(envelope["type"])
            sequence = int(envelope["sequence"])
            refund_data: dict[str, Any] = envelope["data"]["refund"]
        except (ValueError, KeyError, TypeError):
            return WebhookResult(400, "malformed")

        async with self.maker() as s, s.begin():
            inserted = await s.scalar(
                insert(WebhookEvent)
                .values(
                    event_id=event_id,
                    type=event_type,
                    sequence=sequence,
                    provider_refund_id=refund_data.get("id"),
                    outcome="received",
                )
                .on_conflict_do_nothing(index_elements=["event_id"])
                .returning(WebhookEvent.event_id)
            )
            if inserted is None:
                return WebhookResult(200, "duplicate")
            outcome = await self._apply(s, event_type, refund_data)
            event = await s.get(WebhookEvent, event_id)
            assert event is not None
            event.outcome = outcome
            audit(
                s,
                "webhook",
                outcome,
                intent_id=refund_data.get("receipt"),
                event_id=event_id,
                event_type=event_type,
            )
        return WebhookResult(200, outcome)

    async def _find_refund(self, s: AsyncSession, refund_data: dict[str, Any]) -> Refund | None:
        provider_id = refund_data.get("id")
        if provider_id:
            refund = await s.scalar(
                select(Refund).where(Refund.provider_refund_id == provider_id).with_for_update()
            )
            if refund is not None:
                return refund
        receipt = refund_data.get("receipt")
        if receipt:
            refund_id = await s.scalar(
                select(RefundIntent.refund_id).where(RefundIntent.id == receipt)
            )
            if refund_id is not None:
                return await s.get(Refund, refund_id, with_for_update=True)
        return None

    async def _apply(self, s: AsyncSession, event_type: str, refund_data: dict[str, Any]) -> str:
        refund = await self._find_refund(s, refund_data)
        if refund is None:
            return "unmatched"
        if refund.provider_refund_id is None and refund_data.get("id"):
            refund.provider_refund_id = refund_data["id"]
            await s.flush()
        try:
            if event_type == "refund.processed":
                if refund.status == "succeeded":
                    return "already_applied"
                await service.settle_refund(s, refund.id)
                return "settled"
            if event_type == "refund.failed":
                if refund.status == "failed":
                    return "already_applied"
                await service.fail_refund(s, refund.id)
                return "failed"
            if event_type == "refund.created":
                return "noted" if refund.status == "pending" else "ignored_stale"
        except LedgerError:
            # e.g. "processed" for a refund we already marked failed: never move backwards;
            # the audit log keeps the conflict for a human.
            return "conflict"
        return "ignored_type"
