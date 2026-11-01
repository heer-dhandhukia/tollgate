"""The executor against a real ledger and an in-process mockpay that fails on purpose."""

import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from tests.guarded.conftest import World
from tollgate.db.models import AuditLog, Escalation, Order, Payment, Refund, RefundIntent
from tollgate.guarded.backend import tool_result
from tollgate.guarded.executor import ApprovalError, intent_id_for
from tollgate.provider_client import ProviderTimeout

pytestmark = pytest.mark.db


async def intent(w: World, iid: str) -> RefundIntent:
    async with w.maker() as s:
        row = await s.get(RefundIntent, iid)
        assert row is not None
        return row


async def ledger_refunds(w: World, payment_id: int) -> list[tuple[int, str]]:
    async with w.maker() as s:
        rows = await s.execute(
            select(Refund.amount_paise, Refund.status)
            .where(Refund.payment_id == payment_id, ~Refund.idempotency_key.like("seed:%"))
            .order_by(Refund.id)
        )
        return [(a, st) for a, st in rows]


async def audit_actions(w: World, iid: str) -> list[tuple[str, str]]:
    """The executor's and humans' entries, in order. Webhook entries arrive asynchronously, so
    their position isn't fixed; test_webhooks.py covers them."""
    async with w.maker() as s:
        rows = await s.execute(
            select(AuditLog.actor, AuditLog.action)
            .where(AuditLog.intent_id == iid, AuditLog.actor != "webhook")
            .order_by(AuditLog.id)
        )
        return [(a, b) for a, b in rows]


# ---------------------------------------------------------------- the happy path


async def test_refund_within_policy_moves_money_once_and_is_audited(world: World) -> None:
    o = await world.order(189_900)
    out = await world.executor.request_refund(o.session, o.order_ref, 30_000, "dented")
    assert out.status == "succeeded"
    assert await world.provider_refunds(o.provider_payment_id) == [30_000]
    assert await ledger_refunds(world, o.payment_id) == [(30_000, "pending")]
    assert await audit_actions(world, out.intent_id) == [
        ("agent", "requested"),
        ("executor", "reserved"),
        ("executor", "succeeded"),
    ]
    assert tool_result(out)["status"] == "refund_initiated"


async def test_the_same_request_twice_is_not_paid_twice(world: World) -> None:
    o = await world.order(189_900)
    first = await world.executor.request_refund(o.session, o.order_ref, 30_000, "dented")
    again = await world.executor.request_refund(o.session, o.order_ref, 30_000, "dented again")
    assert (first.status, again.status, again.replayed) == ("succeeded", "succeeded", True)
    assert tool_result(again)["status"] == "already_done"
    assert await world.provider_refunds(o.provider_payment_id) == [30_000]


# ---------------------------------------------------------------- the provider misbehaves


@pytest.mark.parametrize(
    ("faults", "attempts"),
    [
        (["error_after_commit"], 2),  # refund created, 502 returned: the retry replays it
        (["timeout_after_commit=1500"], 2),  # refund created, client times out at 0.5 s
        (["error_before_commit"], 2),  # 503 before anything was created: the retry creates it
        (["error_before_commit", "error_after_commit"], 3),
    ],
)
async def test_lost_responses_are_retried_with_the_same_key(
    world: World, faults: list[str], attempts: int
) -> None:
    o = await world.order(189_900)
    world.mockpay.faults.script(faults)
    out = await world.executor.request_refund(o.session, o.order_ref, 40_000, "dented")
    assert out.status == "succeeded"
    assert (await intent(world, out.intent_id)).attempts == attempts
    assert await world.provider_refunds(o.provider_payment_id) == [40_000]
    assert await ledger_refunds(world, o.payment_id) == [(40_000, "pending")]


async def test_retries_exhausted_but_refund_exists_is_found_by_receipt(world: World) -> None:
    o = await world.order(189_900)
    # 1st call creates the refund and loses the reply; the retries fail before reaching it.
    world.mockpay.faults.script(
        ["error_after_commit", "error_before_commit", "error_before_commit"]
    )
    out = await world.executor.request_refund(o.session, o.order_ref, 40_000, "dented")
    assert out.status == "succeeded"
    assert ("reconciler", "succeeded") in await audit_actions(world, out.intent_id)
    assert await world.provider_refunds(o.provider_payment_id) == [40_000]


async def test_retries_exhausted_and_nothing_at_provider_releases_the_reservation(
    world: World,
) -> None:
    o = await world.order(189_900)
    world.mockpay.faults.script(["error_before_commit"] * 3)
    out = await world.executor.request_refund(o.session, o.order_ref, 40_000, "dented")
    assert out.status == "failed"
    assert "no money moved" in tool_result(out)["error"]
    assert await world.provider_refunds(o.provider_payment_id) == []
    assert await ledger_refunds(world, o.payment_id) == [(40_000, "failed")]
    async with world.maker() as s:
        assert (await s.get(Payment, o.payment_id)).refunded_paise == 0  # type: ignore[union-attr]


async def test_when_even_the_lookup_fails_the_intent_is_unknown_until_reconciled(
    world: World,
) -> None:
    o = await world.order(189_900)
    world.mockpay.faults.script(
        ["error_after_commit", "error_before_commit", "error_before_commit"]
    )
    real_lookup = world.provider.list_refunds

    async def lookup_down(**_: object) -> list[dict[str, object]]:
        raise ProviderTimeout("lookup timed out")

    world.provider.list_refunds = lookup_down  # type: ignore[method-assign]
    out = await world.executor.request_refund(o.session, o.order_ref, 40_000, "dented")
    assert out.status == "unknown" and out.ticket == "ESC-1"
    assert tool_result(out)["status"] == "pending_confirmation"
    # The agent asking again must not trigger another attempt.
    again = await world.executor.request_refund(o.session, o.order_ref, 40_000, "dented")
    assert again.replayed and tool_result(again)["status"] == "already_in_progress"

    world.provider.list_refunds = real_lookup  # type: ignore[method-assign]
    changes = await world.executor.reconcile()
    assert {"intent": out.intent_id, "result": "succeeded"} in changes
    assert (await intent(world, out.intent_id)).status == "succeeded"
    assert await world.provider_refunds(o.provider_payment_id) == [40_000]


async def test_definitive_provider_refusal_fails_without_retrying(world: World) -> None:
    o = await world.order(189_900)
    # Someone refunded at the provider behind our back; the provider now refuses ours.
    await world.provider.create_refund(o.provider_payment_id, 189_900, receipt="manual")
    out = await world.executor.request_refund(o.session, o.order_ref, 40_000, "dented")
    assert out.status == "failed" and "provider refused (400)" in out.message
    assert (await intent(world, out.intent_id)).attempts == 1


# ---------------------------------------------------------------- policy in code


async def test_policy_denials_move_nothing(world: World) -> None:
    old = await world.order(189_900, days_ago=31)
    out = await world.executor.request_refund(old.session, old.order_ref, 189_900, "late")
    assert (out.status, out.rule) == ("denied", "outside_window")

    o = await world.order(300_000)
    out = await world.executor.request_refund(o.session, o.order_ref, 400_000, "more please")
    assert out.rule == "exceeds_refundable" and out.max_allowed_paise == 300_000
    assert tool_result(out)["max_refund_now_paise"] == 300_000

    other = await world.order(189_900)
    out = await world.executor.request_refund(o.session, other.order_ref, 1_000, "not mine")
    assert out.rule == "ownership"

    never_paid = await world.order(189_900, status="cancelled")
    out = await world.executor.request_refund(never_paid.session, never_paid.order_ref, 1_000, "x")
    assert out.rule == "not_captured"

    for seeded in (old, o, other):
        assert await world.provider_refunds(seeded.provider_payment_id) == []


async def test_daily_cap_spans_the_customers_orders(world: World) -> None:
    a = await world.order(1_400_000, customer="asha", prior_refunds_today=1_200_000)
    b = await world.order(459_900, customer="asha")
    out = await world.executor.request_refund(b.session, b.order_ref, 459_900, "broken")
    assert (out.rule, out.max_allowed_paise) == ("daily_cap", 300_000)
    out = await world.executor.request_refund(b.session, b.order_ref, 300_000, "broken")
    assert out.status == "succeeded"
    assert await world.provider_refunds(b.provider_payment_id) == [300_000]
    assert a.session.customer_id == b.session.customer_id


# ---------------------------------------------------------------- the human approval queue


async def test_large_refund_waits_for_a_named_human(world: World) -> None:
    o = await world.order(900_000)
    out = await world.executor.request_refund(o.session, o.order_ref, 600_000, "TV cracked")
    assert (out.status, out.ticket) == ("awaiting_approval", "ESC-1")
    assert tool_result(out)["status"] == "pending_approval"
    assert await world.provider_refunds(o.provider_payment_id) == []
    assert out.intent_id in [i.id for i in await world.executor.pending_approvals()]

    approved = await world.executor.approve(out.intent_id, "priya.ops")
    assert approved.status == "succeeded"
    assert await world.provider_refunds(o.provider_payment_id) == [600_000]
    actions = await audit_actions(world, out.intent_id)
    assert ("human:priya.ops", "approved") in actions
    assert (await intent(world, out.intent_id)).decided_by == "priya.ops"
    with pytest.raises(ApprovalError):
        await world.executor.approve(out.intent_id, "priya.ops")


async def test_rejected_refund_moves_nothing(world: World) -> None:
    o = await world.order(900_000)
    out = await world.executor.request_refund(o.session, o.order_ref, 600_000, "TV cracked")
    rejected = await world.executor.reject(out.intent_id, "ravi.ops", "photos don't match")
    assert rejected.status == "rejected"
    assert await world.provider_refunds(o.provider_payment_id) == []
    with pytest.raises(ApprovalError):
        await world.executor.approve(out.intent_id, "ravi.ops")


async def test_approval_rechecks_the_policy(world: World) -> None:
    o = await world.order(1_000_000)
    waiting = await world.executor.request_refund(o.session, o.order_ref, 600_000, "big")
    small = await world.executor.request_refund(o.session, o.order_ref, 450_000, "small")
    assert small.status == "succeeded"
    # Only ₹5,500 is still refundable, so the ₹6,000 request can no longer be honoured.
    late = await world.executor.approve(waiting.intent_id, "priya.ops")
    assert (late.status, late.rule) == ("rejected", "exceeds_refundable")
    assert await world.provider_refunds(o.provider_payment_id) == [450_000]


# ---------------------------------------------------------------- cancellations


async def test_cancelling_a_paid_order_refunds_through_the_executor(world: World) -> None:
    o = await world.order(249_900, status="paid")
    out = await world.executor.request_cancel(o.session, o.order_ref)
    assert (out.status, out.kind) == ("succeeded", "cancel")
    assert tool_result(out) == {
        "status": "cancelled",
        "order_id": o.order_ref,
        "refunded": "₹2,499.00",
    }
    assert await world.provider_refunds(o.provider_payment_id) == [249_900]
    async with world.maker() as s:
        assert (await s.get(Order, o.order_id)).status == "cancelled"  # type: ignore[union-attr]


async def test_cancelling_shipped_or_unpaid_orders(world: World) -> None:
    shipped = await world.order(189_900, status="shipped")
    out = await world.executor.request_cancel(shipped.session, shipped.order_ref)
    assert (out.status, out.rule) == ("denied", "cancel_shipped")
    assert await world.provider_refunds(shipped.provider_payment_id) == []

    placed = await world.order(1_249_900, status="placed")
    out = await world.executor.request_cancel(placed.session, placed.order_ref)
    assert out.status == "cancelled" and tool_result(out)["refunded"] == "₹0.00"
    async with world.maker() as s:
        assert (await s.get(Payment, placed.payment_id)).status == "voided"  # type: ignore[union-attr]


# ---------------------------------------------------------------- concurrency


async def test_concurrent_identical_requests_pay_once(world: World) -> None:
    o = await world.order(189_900)
    results = await asyncio.gather(
        *(world.executor.request_refund(o.session, o.order_ref, 30_000, "x") for _ in range(4))
    )
    assert sorted(r.replayed for r in results) == [False, True, True, True]
    assert await world.provider_refunds(o.provider_payment_id) == [30_000]


async def test_concurrent_different_requests_never_exceed_what_was_paid(world: World) -> None:
    o = await world.order(100_000)
    amounts = [60_000, 50_000, 40_000, 30_000]
    results = await asyncio.gather(
        *(world.executor.request_refund(o.session, o.order_ref, a, "x") for a in amounts)
    )
    paid = await world.provider_refunds(o.provider_payment_id)
    assert sum(paid) <= 100_000
    assert len([r for r in results if r.status == "succeeded"]) == len(paid)


# ---------------------------------------------------------------- rules in the database


async def test_intent_rules_are_enforced_by_postgres(world: World) -> None:
    o = await world.order(189_900)
    out = await world.executor.request_refund(o.session, o.order_ref, 30_000, "x")
    statements = [
        (
            "UPDATE refund_intents SET status = 'executing' WHERE id = :i",
            "illegal intent transition",
        ),
        ("UPDATE refund_intents SET amount_paise = 1 WHERE id = :i", "immutable"),
        ("DELETE FROM refund_intents WHERE id = :i", "append-only"),
        ("UPDATE audit_log SET action = 'x' WHERE intent_id = :i", "append-only"),
        ("DELETE FROM audit_log WHERE intent_id = :i", "append-only"),
    ]
    for sql, message in statements:
        with pytest.raises(IntegrityError, match=message):
            async with world.maker() as s, s.begin():
                await s.execute(text(sql), {"i": out.intent_id})


async def test_only_one_intent_per_order_can_be_in_flight(world: World) -> None:
    o = await world.order(189_900)
    with pytest.raises(IntegrityError, match="one_in_flight"):
        async with world.maker() as s, s.begin():
            for amount in (10_000, 20_000):
                iid = intent_id_for(o.session.conversation_id, "refund", o.order_ref, amount)
                row = RefundIntent(
                    id=iid,
                    conversation_id=o.session.conversation_id,
                    customer_id=o.session.customer_id,
                    order_id=o.order_id,
                    kind="refund",
                    amount_paise=amount,
                )
                s.add(row)
                await s.flush()
                refund = Refund(payment_id=o.payment_id, amount_paise=amount, idempotency_key=iid)
                s.add(refund)
                await s.flush()
                row.refund_id, row.status = refund.id, "executing"
                await s.flush()


async def test_escalations_are_real_rows(world: World) -> None:
    o = await world.order(900_000)
    out = await world.executor.request_refund(o.session, o.order_ref, 700_000, "x")
    async with world.maker() as s:
        count = await s.scalar(
            select(Escalation.id).where(Escalation.conversation_id == o.session.conversation_id)
        )
    assert out.ticket == "ESC-1" and count is not None
