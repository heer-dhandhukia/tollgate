"""mockpay persistence: payments, refunds, idempotency keys and the event log."""

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mockpay.config import MockpaySettings
from mockpay.faults import Fault
from mockpay.models import MpEvent, MpIdempotencyKey, MpPayment, MpRefund


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(7)}"


def request_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def error_body(code: str, description: str) -> dict[str, Any]:
    return {"error": {"code": code, "description": description}}


def payment_json(p: MpPayment) -> dict[str, Any]:
    return {
        "id": p.id,
        "entity": "payment",
        "amount_paise": p.amount_paise,
        "captured_paise": p.captured_paise,
        "refunded_paise": p.refunded_paise,
    }


def refund_json(r: MpRefund) -> dict[str, Any]:
    return {
        "id": r.id,
        "entity": "refund",
        "payment_id": r.payment_id,
        "amount_paise": r.amount_paise,
        "status": r.status,
        "receipt": r.receipt,
        "notes": r.notes,
        "created_at": r.created_at.isoformat(),
        "processed_at": r.processed_at.isoformat() if r.processed_at else None,
    }


@dataclass(frozen=True)
class OutgoingEvent:
    envelope: dict[str, Any]
    refund_id: str
    webhook_mode: str
    terminal: bool


class Store:
    def __init__(self, maker: async_sessionmaker[AsyncSession], settings: MockpaySettings) -> None:
        self.maker = maker
        self.settings = settings

    # ------------------------------------------------------------------ payments

    async def create_payment(
        self, amount_paise: int, captured_paise: int | None = None, payment_id: str | None = None
    ) -> MpPayment:
        async with self.maker() as s, s.begin():
            payment = MpPayment(
                id=payment_id or new_id("pay"),
                amount_paise=amount_paise,
                captured_paise=amount_paise if captured_paise is None else captured_paise,
                refunded_paise=0,
                created_at=now(),
            )
            s.add(payment)
        return payment

    async def get_payment(self, payment_id: str) -> MpPayment | None:
        async with self.maker() as s:
            return await s.get(MpPayment, payment_id)

    # ------------------------------------------------------------------ idempotency

    async def claim_key(self, key: str, req_hash: str) -> MpIdempotencyKey | None:
        """Claim `key` for this request. Returns None if claimed, else the existing record.

        The claim commits on its own, so a concurrent request with the same key sees it as
        in flight (-> 409) instead of creating a second refund."""
        async with self.maker() as s, s.begin():
            claimed = await s.scalar(
                pg_insert(MpIdempotencyKey)
                .values(key=key, request_hash=req_hash, state="in_flight", created_at=now())
                .on_conflict_do_nothing(index_elements=["key"])
                .returning(MpIdempotencyKey.key)
            )
            if claimed is not None:
                return None
            return await s.get(MpIdempotencyKey, key)

    async def release_key(self, key: str) -> None:
        async with self.maker() as s, s.begin():
            await s.execute(
                delete(MpIdempotencyKey).where(
                    MpIdempotencyKey.key == key, MpIdempotencyKey.state == "in_flight"
                )
            )

    # ------------------------------------------------------------------ refunds

    async def create_refund(
        self,
        payment_id: str,
        amount_paise: int,
        receipt: str | None,
        notes: dict[str, str],
        idempotency_key: str | None,
        fault: Fault,
    ) -> tuple[int, dict[str, Any], list[OutgoingEvent]]:
        """Create a pending refund and, in the same transaction, record the response under the
        idempotency key. Returns (http_status, body, events_to_publish)."""
        events: list[OutgoingEvent] = []
        async with self.maker() as s, s.begin():
            payment = await s.get(MpPayment, payment_id, with_for_update=True)
            if payment is None:
                status, body = 404, error_body("NOT_FOUND", f"payment {payment_id} not found")
            elif payment.refunded_paise + amount_paise > payment.captured_paise:
                status, body = (
                    400,
                    error_body(
                        "BAD_REQUEST_ERROR",
                        "refund amount exceeds the remaining captured amount "
                        f"({payment.captured_paise - payment.refunded_paise} paise)",
                    ),
                )
            else:
                payment.refunded_paise += amount_paise
                refund = MpRefund(
                    id=new_id("rfnd"),
                    payment_id=payment_id,
                    amount_paise=amount_paise,
                    status="pending",
                    receipt=receipt,
                    notes=notes,
                    webhook_mode=fault.webhook_mode,
                    will_fail=fault.refund_fails,
                    created_at=now(),
                )
                s.add(refund)
                await s.flush()
                events.append(await self._event(s, "refund.created", refund, terminal=False))
                status, body = 200, refund_json(refund)
            if idempotency_key is not None:
                await s.execute(
                    update(MpIdempotencyKey)
                    .where(MpIdempotencyKey.key == idempotency_key)
                    .values(state="done", response_status=status, response_body=body)
                )
        return status, body, events

    async def get_refund(self, refund_id: str) -> MpRefund | None:
        async with self.maker() as s:
            return await s.get(MpRefund, refund_id)

    async def list_refunds(
        self, payment_id: str | None = None, receipt: str | None = None
    ) -> list[MpRefund]:
        stmt = select(MpRefund).order_by(MpRefund.created_at, MpRefund.id)
        if payment_id is not None:
            stmt = stmt.where(MpRefund.payment_id == payment_id)
        if receipt is not None:
            stmt = stmt.where(MpRefund.receipt == receipt)
        async with self.maker() as s:
            return list(await s.scalars(stmt))

    async def settle_due(self, at: datetime | None = None) -> list[OutgoingEvent]:
        """Move pending refunds older than the settle delay to processed (or failed)."""
        at = at or now()
        cutoff = at - timedelta(milliseconds=self.settings.settle_delay_ms)
        events: list[OutgoingEvent] = []
        async with self.maker() as s, s.begin():
            due = await s.scalars(
                select(MpRefund)
                .where(MpRefund.status == "pending", MpRefund.created_at <= cutoff)
                .order_by(MpRefund.created_at)
                .with_for_update(skip_locked=True)
            )
            for refund in list(due):
                if refund.will_fail:
                    refund.status = "failed"
                    await s.execute(
                        update(MpPayment)
                        .where(MpPayment.id == refund.payment_id)
                        .values(refunded_paise=MpPayment.refunded_paise - refund.amount_paise)
                    )
                else:
                    refund.status = "processed"
                refund.processed_at = at
                await s.flush()
                events.append(
                    await self._event(s, f"refund.{refund.status}", refund, terminal=True)
                )
        return events

    async def _event(
        self, s: AsyncSession, type_: str, refund: MpRefund, terminal: bool
    ) -> OutgoingEvent:
        event = MpEvent(
            id=new_id("evt"),
            type=type_,
            refund_id=refund.id,
            payload={"refund": refund_json(refund)},
            created_at=now(),
        )
        s.add(event)
        await s.flush()
        envelope = {
            "id": event.id,
            "sequence": event.sequence,
            "type": event.type,
            "created_at": event.created_at.isoformat(),
            "data": event.payload,
        }
        return OutgoingEvent(envelope, refund.id, refund.webhook_mode, terminal)
