"""Builders for committed, valid ledger state. Only the service writes committed rows in tests,
so every test leaves the database in a state that satisfies all invariants."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.ledger import service


def uid() -> str:
    return uuid.uuid4().hex[:12]


async def new_order(
    maker: async_sessionmaker[AsyncSession], amount: int = 100_000
) -> tuple[int, int]:
    """An order with an authorized payment. Returns (order_id, payment_id)."""
    async with maker() as s, s.begin():
        customer = await service.create_customer(s, f"cust_{uid()}", "Test", "t@example.com")
        order, payment = await service.create_order(s, customer, f"ORD-{uid()}", amount)
        return order.id, payment.id


async def captured_order(
    maker: async_sessionmaker[AsyncSession], amount: int = 100_000
) -> tuple[int, int]:
    order_id, payment_id = await new_order(maker, amount)
    async with maker() as s, s.begin():
        await service.capture_payment(s, payment_id)
    return order_id, payment_id


async def refund(maker: async_sessionmaker[AsyncSession], payment_id: int, amount: int) -> int:
    async with maker() as s, s.begin():
        r = await service.reserve_refund(s, payment_id, amount, f"key_{uid()}")
        return r.id
