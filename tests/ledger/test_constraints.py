"""Every invariant must hold even when application code is bypassed: these tests write raw SQL
straight at Postgres and expect the database itself to refuse."""

import asyncio

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.ledger.factory import captured_order, new_order, refund, uid
from tollgate.db.models import Journal, Payment
from tollgate.ledger import service

pytestmark = pytest.mark.db

Maker = async_sessionmaker[AsyncSession]


async def assert_rejected(maker: Maker, sql: str, params: dict[str, object], match: str) -> None:
    with pytest.raises(IntegrityError, match=match):
        async with maker() as s, s.begin():
            await s.execute(text(sql), params)


async def insert_refund_sql(maker: Maker, payment_id: int, amount: int, match: str) -> None:
    await assert_rejected(
        maker,
        "INSERT INTO refunds (payment_id, amount_paise, idempotency_key) VALUES (:p, :a, :k)",
        {"p": payment_id, "a": amount, "k": f"raw_{uid()}"},
        match,
    )


# ------------------------------------------------------------ refunded <= captured


async def test_over_refund_rejected(maker: Maker) -> None:
    _, pid = await captured_order(maker, 100_000)
    await insert_refund_sql(maker, pid, 100_001, "refunded_le_captured")


async def test_cumulative_over_refund_rejected(maker: Maker) -> None:
    _, pid = await captured_order(maker, 100_000)
    await refund(maker, pid, 60_000)
    await insert_refund_sql(maker, pid, 40_001, "refunded_le_captured")
    await refund(maker, pid, 40_000)  # exactly the remainder is fine


async def test_failed_refund_releases_its_reservation(maker: Maker) -> None:
    _, pid = await captured_order(maker, 100_000)
    rid = await refund(maker, pid, 100_000)
    async with maker() as s, s.begin():
        await service.fail_refund(s, rid)
    await refund(maker, pid, 100_000)


async def test_refunded_counter_cannot_be_written_directly(maker: Maker) -> None:
    _, pid = await captured_order(maker)
    await refund(maker, pid, 10_000)
    await assert_rejected(
        maker,
        "UPDATE payments SET refunded_paise = 0 WHERE id = :p",
        {"p": pid},
        "maintained by refunds only",
    )


async def test_new_payment_cannot_carry_refunds(maker: Maker) -> None:
    order_id, _ = await new_order(maker)
    customer_id = await _customer_of(maker, order_id)
    async with maker() as s, s.begin():
        bare_order_id = (
            await s.execute(
                text(
                    "INSERT INTO orders (ref, customer_id, amount_paise) VALUES (:r, :c, 500)"
                    " RETURNING id"
                ),
                {"r": f"ORD-{uid()}", "c": customer_id},
            )
        ).scalar_one()
    await assert_rejected(
        maker,
        "INSERT INTO payments (order_id, amount_paise, status, captured_paise, refunded_paise)"
        " VALUES (:o, 500, 'captured', 500, 1)",
        {"o": bare_order_id},
        "cannot carry refunds",
    )


async def _customer_of(maker: Maker, order_id: int) -> int:
    async with maker() as s:
        return (
            await s.execute(text("SELECT customer_id FROM orders WHERE id = :o"), {"o": order_id})
        ).scalar_one()


async def test_concurrent_refunds_serialise_on_payment_row_lock(maker: Maker) -> None:
    """Two refunds of 60% each, started at the same time on one payment: exactly one wins.

    The first transaction's reservation UPDATE holds the payment row lock; the second blocks on
    it, then re-evaluates against the committed counter and hits the CHECK."""
    _, pid = await captured_order(maker, 100_000)

    async def second() -> int:
        return await refund(maker, pid, 60_000)

    async with maker() as first:
        await first.begin()
        await service.reserve_refund(first, pid, 60_000, f"key_{uid()}")
        task = asyncio.create_task(second())
        await asyncio.sleep(0.3)
        assert not task.done(), "second refund should be blocked on the payment row lock"
        await first.commit()
    with pytest.raises(IntegrityError, match="refunded_le_captured"):
        await task


async def test_concurrent_refunds_that_fit_both_succeed(maker: Maker) -> None:
    _, pid = await captured_order(maker, 100_000)
    results = await asyncio.gather(*(refund(maker, pid, 25_000) for _ in range(4)))
    assert len(set(results)) == 4
    async with maker() as s:
        payment = await s.get(Payment, pid)
        assert payment is not None and payment.refunded_paise == 100_000


# ------------------------------------------------------------ no refund unless captured


async def test_refund_on_order_cancelled_before_capture_rejected(maker: Maker) -> None:
    order_id, pid = await new_order(maker)
    async with maker() as s, s.begin():
        await service.cancel_order(s, order_id)
    await insert_refund_sql(maker, pid, 1, "requires a captured payment")


async def test_refund_on_uncaptured_payment_rejected(maker: Maker) -> None:
    _, pid = await new_order(maker)
    await insert_refund_sql(maker, pid, 1, "requires a captured payment")


async def test_capture_after_cancel_rejected(maker: Maker) -> None:
    order_id, pid = await new_order(maker, 5_000)
    async with maker() as s, s.begin():
        await service.cancel_order(s, order_id)
    # Un-voiding the payment is refused...
    await assert_rejected(
        maker,
        "UPDATE payments SET status = 'captured' WHERE id = :p",
        {"p": pid},
        "illegal payment transition",
    )
    # ...and so is un-voiding and capturing in one statement (the amount guard fires first).
    await assert_rejected(
        maker,
        "UPDATE payments SET status = 'captured', captured_paise = 5000 WHERE id = :p",
        {"p": pid},
        "capture amount is fixed",
    )


async def test_capture_on_cancelled_order_with_authorized_payment_rejected(maker: Maker) -> None:
    order_id, pid = await new_order(maker, 5_000)
    with pytest.raises(IntegrityError, match="cannot capture payment for cancelled order"):
        async with maker() as s, s.begin():
            # Cancel the order without voiding (a buggy code path), then try to capture.
            await s.execute(
                text("UPDATE orders SET status = 'cancelled', cancelled_at = now() WHERE id = :o"),
                {"o": order_id},
            )
            await s.execute(
                text(
                    "UPDATE payments SET status = 'captured', captured_paise = 5000 WHERE id = :p"
                ),
                {"p": pid},
            )


# ------------------------------------------------------------ status transitions


@pytest.mark.parametrize(
    ("setup", "new_status"),
    [("shipped", "cancelled"), ("shipped", "paid"), ("cancelled", "paid"), ("paid", "placed")],
)
async def test_illegal_order_transitions_rejected(
    maker: Maker, setup: str, new_status: str
) -> None:
    order_id, _ = await captured_order(maker) if setup != "cancelled" else await new_order(maker)
    async with maker() as s, s.begin():
        if setup == "shipped":
            await service.ship_order(s, order_id)
        elif setup == "cancelled":
            await service.cancel_order(s, order_id)
    await assert_rejected(
        maker,
        "UPDATE orders SET status = :st WHERE id = :o",
        {"st": new_status, "o": order_id},
        "illegal order transition",
    )


async def test_settled_refund_cannot_fail(maker: Maker) -> None:
    _, pid = await captured_order(maker)
    rid = await refund(maker, pid, 1_000)
    async with maker() as s, s.begin():
        await service.settle_refund(s, rid, f"rfnd_{uid()}")
    await assert_rejected(
        maker,
        "UPDATE refunds SET status = 'failed' WHERE id = :r",
        {"r": rid},
        "illegal refund transition",
    )


async def test_refund_amount_is_immutable(maker: Maker) -> None:
    _, pid = await captured_order(maker)
    rid = await refund(maker, pid, 1_000)
    await assert_rejected(
        maker,
        "UPDATE refunds SET amount_paise = 1 WHERE id = :r",
        {"r": rid},
        "immutable",
    )


async def test_refund_cannot_be_deleted(maker: Maker) -> None:
    _, pid = await captured_order(maker)
    rid = await refund(maker, pid, 1_000)
    await assert_rejected(
        maker, "DELETE FROM refunds WHERE id = :r", {"r": rid}, "cannot be deleted"
    )


# ------------------------------------------------------------ ledger balances


async def test_unbalanced_journal_rejected_at_commit(maker: Maker) -> None:
    with pytest.raises(IntegrityError, match="does not balance"):
        async with maker() as s, s.begin():
            jid = (
                await s.execute(
                    text("INSERT INTO journals (kind, ref) VALUES ('manual', 'x') RETURNING id")
                )
            ).scalar_one()
            await s.execute(
                text(
                    "INSERT INTO ledger_entries (journal_id, account, amount_paise) VALUES"
                    " (:j, 'provider_clearing', 100), (:j, 'revenue', -50)"
                ),
                {"j": jid},
            )


async def test_single_leg_journal_rejected(maker: Maker) -> None:
    with pytest.raises(IntegrityError, match="double entry needs >= 2"):
        async with maker() as s, s.begin():
            jid = (
                await s.execute(
                    text("INSERT INTO journals (kind, ref) VALUES ('manual', 'x') RETURNING id")
                )
            ).scalar_one()
            await s.execute(
                text(
                    "INSERT INTO ledger_entries (journal_id, account, amount_paise)"
                    " VALUES (:j, 'revenue', 100)"
                ),
                {"j": jid},
            )


async def test_empty_journal_rejected(maker: Maker) -> None:
    await assert_rejected(
        maker, "INSERT INTO journals (kind, ref) VALUES ('manual', 'x')", {}, "needs >= 2"
    )


async def test_service_refuses_unbalanced_legs_before_db(maker: Maker) -> None:
    with pytest.raises(service.LedgerError):
        async with maker() as s, s.begin():
            await service.post_journal(s, "manual", "x", [("revenue", 1), ("refunds_contra", 1)])


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE ledger_entries SET amount_paise = amount_paise + 1"
        " WHERE id = (SELECT min(id) FROM ledger_entries)",
        "DELETE FROM ledger_entries WHERE id = (SELECT min(id) FROM ledger_entries)",
        "TRUNCATE ledger_entries",
        "UPDATE journals SET ref = 'tampered' WHERE id = (SELECT min(id) FROM journals)",
        "DELETE FROM journals WHERE id = (SELECT min(id) FROM journals)",
    ],
)
async def test_ledger_is_append_only(maker: Maker, sql: str) -> None:
    await captured_order(maker)  # guarantees at least one journal exists
    await assert_rejected(maker, sql, {}, "append-only|violates foreign key")


# ------------------------------------------------------------ service boundary


async def test_service_rejects_float_amounts(maker: Maker) -> None:
    _, pid = await captured_order(maker)
    with pytest.raises(ValidationError):
        async with maker() as s, s.begin():
            await service.reserve_refund(s, pid, 10.5, f"key_{uid()}")  # type: ignore[arg-type]


async def test_settle_is_idempotent(maker: Maker) -> None:
    _, pid = await captured_order(maker)
    rid = await refund(maker, pid, 1_000)
    for _ in range(3):
        async with maker() as s, s.begin():
            await service.settle_refund(s, rid, "rfnd_same")
    async with maker() as s:
        settles = await s.scalar(
            select(func.count())
            .select_from(Journal)
            .where(Journal.kind == "refund_settle", Journal.ref == f"refund:{rid}")
        )
    assert settles == 1
