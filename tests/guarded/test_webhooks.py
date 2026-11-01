"""Webhooks end to end: mockpay delivers signed events to Tollgate's API, sometimes twice and
sometimes out of order; the ledger must end up right either way."""

import json

import httpx
import pytest
from sqlalchemy import func, select

from tests.guarded.conftest import SECRET, World
from tollgate.api.app import create_app
from tollgate.db.models import Journal, Payment, Refund, WebhookEvent
from tollgate.guarded.webhooks import SIGNATURE_HEADER, signature_for

pytestmark = pytest.mark.db


async def refund_row(w: World, intent_id: str) -> Refund:
    async with w.maker() as s:
        row = await s.scalar(select(Refund).where(Refund.idempotency_key == intent_id))
        assert row is not None
        return row


async def outcomes(w: World, provider_refund_id: str) -> list[str]:
    async with w.maker() as s:
        rows = await s.scalars(
            select(WebhookEvent.outcome)
            .where(WebhookEvent.provider_refund_id == provider_refund_id)
            .order_by(WebhookEvent.sequence)
        )
        return list(rows)


async def test_processed_webhook_settles_the_ledger(world: World) -> None:
    o = await world.order(189_900)
    out = await world.executor.request_refund(o.session, o.order_ref, 30_000, "dented")
    await world.mockpay.settle_due()
    await world.mockpay.dispatcher.drain()
    refund = await refund_row(world, out.intent_id)
    assert refund.status == "succeeded"
    assert await outcomes(world, refund.provider_refund_id) == ["noted", "settled"]


async def test_duplicate_deliveries_are_applied_once(world: World) -> None:
    o = await world.order(189_900)
    world.mockpay.faults.script(["duplicate_webhook=3"])
    out = await world.executor.request_refund(o.session, o.order_ref, 30_000, "dented")
    await world.mockpay.settle_due()
    await world.mockpay.dispatcher.drain()
    refund = await refund_row(world, out.intent_id)
    assert refund.status == "succeeded"
    delivered = [d for d in world.mockpay.dispatcher.deliveries if d.status_code == 200]
    assert len(delivered) >= 6  # 2 events x 3 copies
    async with world.maker() as s:
        settles = await s.scalar(
            select(func.count())
            .select_from(Journal)
            .where(Journal.kind == "refund_settle", Journal.ref == f"refund:{refund.id}")
        )
    assert settles == 1


async def test_out_of_order_events_never_move_a_refund_backwards(world: World) -> None:
    o = await world.order(189_900)
    world.mockpay.faults.script(["out_of_order_webhooks"])
    out = await world.executor.request_refund(o.session, o.order_ref, 30_000, "dented")
    await world.mockpay.settle_due()  # releases processed, then created
    await world.mockpay.dispatcher.drain()
    refund = await refund_row(world, out.intent_id)
    assert refund.status == "succeeded"
    seen = await outcomes(world, refund.provider_refund_id)
    assert sorted(seen) == ["ignored_stale", "settled"]


async def test_failed_refund_webhook_releases_the_reservation(world: World) -> None:
    o = await world.order(189_900)
    world.mockpay.faults.script(["refund_fails"])
    out = await world.executor.request_refund(o.session, o.order_ref, 30_000, "dented")
    assert out.status == "succeeded"  # the provider accepted it...
    await world.mockpay.settle_due()  # ...and later failed it
    await world.mockpay.dispatcher.drain()
    refund = await refund_row(world, out.intent_id)
    assert refund.status == "failed"
    async with world.maker() as s:
        assert (await s.get(Payment, o.payment_id)).refunded_paise == 0  # type: ignore[union-attr]


async def test_unsigned_or_forged_webhooks_are_rejected(world: World) -> None:
    api = create_app(world.maker, SECRET)
    body = json.dumps({"id": "evt_x", "type": "refund.processed", "sequence": 1}).encode()
    transport = httpx.ASGITransport(app=api)
    async with (
        api.router.lifespan_context(api),
        httpx.AsyncClient(transport=transport, base_url="http://t") as c,
    ):
        assert (await c.post("/webhooks/mockpay", content=body)).status_code == 401
        forged = {SIGNATURE_HEADER: signature_for("wrong-secret", body)}
        r = await c.post("/webhooks/mockpay", content=body, headers=forged)
        assert r.status_code == 401
        good = {SIGNATURE_HEADER: signature_for(SECRET, body)}
        r = await c.post("/webhooks/mockpay", content=body, headers=good)
        assert (r.status_code, r.json()["outcome"]) == (400, "malformed")
