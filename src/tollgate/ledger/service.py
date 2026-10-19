"""Ledger operations. Each one posts a balanced journal in the caller's transaction.

The caller owns the transaction (`async with session.begin(): ...`). The database is the final
authority on every invariant; these functions only raise `LedgerError` early for states the
caller could have checked, so error messages stay readable.

Postings (debit > 0, credit < 0):
    capture        provider_clearing +A   revenue          -A
    refund reserve refunds_contra    +A   refunds_payable  -A   (refund created, pending)
    refund settle  refunds_payable   +A   provider_clearing -A  (provider confirmed)
    refund release refunds_payable   +A   refunds_contra    -A  (provider said failed)
"""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tollgate.db.models import (
    PROVIDER_CLEARING,
    REFUNDS_CONTRA,
    REFUNDS_PAYABLE,
    REVENUE,
    Customer,
    Journal,
    LedgerEntry,
    Order,
    Payment,
    Refund,
)
from tollgate.money import positive_paise


class LedgerError(Exception):
    """A requested ledger operation is not allowed in the current state."""


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def post_journal(
    session: AsyncSession, kind: str, ref: str, legs: list[tuple[str, int]]
) -> Journal:
    if len(legs) < 2 or sum(amount for _, amount in legs) != 0:
        raise LedgerError(f"journal {kind}/{ref} must have >= 2 legs summing to 0: {legs}")
    journal = Journal(kind=kind, ref=ref)
    session.add(journal)
    await session.flush()
    session.add_all(
        LedgerEntry(journal_id=journal.id, account=account, amount_paise=amount)
        for account, amount in legs
    )
    await session.flush()
    return journal


async def create_customer(session: AsyncSession, ref: str, name: str, email: str) -> Customer:
    customer = Customer(ref=ref, name=name, email=email)
    session.add(customer)
    await session.flush()
    return customer


async def create_order(
    session: AsyncSession,
    customer: Customer,
    ref: str,
    amount_paise: int,
    notes: str = "",
    *,
    product_id: int | None = None,
    placed_at: datetime | None = None,
) -> tuple[Order, Payment]:
    """Place an order with an authorized (not yet captured) payment."""
    amount = positive_paise(amount_paise)
    order = Order(
        ref=ref, customer_id=customer.id, amount_paise=amount, notes=notes, product_id=product_id
    )
    if placed_at is not None:
        order.placed_at = placed_at
    session.add(order)
    await session.flush()
    payment = Payment(order_id=order.id, amount_paise=amount)
    session.add(payment)
    await session.flush()
    return order, payment


async def _locked_payment(session: AsyncSession, payment_id: int) -> Payment:
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )
    if payment is None:
        raise LedgerError(f"payment {payment_id} not found")
    return payment


async def _locked_order(session: AsyncSession, order_id: int) -> Order:
    order = await session.scalar(select(Order).where(Order.id == order_id).with_for_update())
    if order is None:
        raise LedgerError(f"order {order_id} not found")
    return order


async def capture_payment(
    session: AsyncSession,
    payment_id: int,
    amount_paise: int | None = None,
    at: datetime | None = None,
) -> Payment:
    payment = await _locked_payment(session, payment_id)
    order = await _locked_order(session, payment.order_id)
    amount = positive_paise(payment.amount_paise if amount_paise is None else amount_paise)
    if payment.status != "authorized":
        raise LedgerError(f"payment {payment.id} is {payment.status}, not authorized")
    if order.status != "placed":
        raise LedgerError(f"order {order.id} is {order.status}, cannot capture")
    payment.status = "captured"
    payment.captured_paise = amount
    payment.captured_at = at or _now()
    order.status = "paid"
    await session.flush()
    await post_journal(
        session,
        "capture",
        f"payment:{payment.id}",
        [(PROVIDER_CLEARING, amount), (REVENUE, -amount)],
    )
    return payment


async def ship_order(session: AsyncSession, order_id: int, at: datetime | None = None) -> Order:
    order = await _locked_order(session, order_id)
    if order.status != "paid":
        raise LedgerError(f"order {order.id} is {order.status}, cannot ship")
    order.status = "shipped"
    order.shipped_at = at or _now()
    await session.flush()
    return order


async def deliver_order(session: AsyncSession, order_id: int) -> Order:
    order = await _locked_order(session, order_id)
    if order.status != "shipped":
        raise LedgerError(f"order {order.id} is {order.status}, cannot deliver")
    order.status = "delivered"
    await session.flush()
    return order


async def cancel_order(session: AsyncSession, order_id: int) -> Order:
    """Cancel an order that has not shipped.

    Before capture this voids the authorization (no money moved, nothing to post). After capture
    the order is cancelled but money is returned only through a refund, which goes through the
    same guarded path as any other refund.
    """
    order = await _locked_order(session, order_id)
    if order.status not in ("placed", "paid"):
        raise LedgerError(f"order {order.id} is {order.status}, cannot cancel")
    payment = await session.scalar(
        select(Payment).where(Payment.order_id == order.id).with_for_update()
    )
    if payment is not None and payment.status == "authorized":
        payment.status = "voided"
    order.status = "cancelled"
    order.cancelled_at = _now()
    await session.flush()
    return order


async def reserve_refund(
    session: AsyncSession,
    payment_id: int,
    amount_paise: int,
    idempotency_key: str,
    reason: str = "",
) -> Refund:
    """Create a pending refund. The DB trigger reserves it against the captured amount."""
    amount = positive_paise(amount_paise)
    refund = Refund(
        payment_id=payment_id, amount_paise=amount, idempotency_key=idempotency_key, reason=reason
    )
    session.add(refund)
    await session.flush()
    await post_journal(
        session,
        "refund_reserve",
        f"refund:{refund.id}",
        [(REFUNDS_CONTRA, amount), (REFUNDS_PAYABLE, -amount)],
    )
    return refund


async def _locked_refund(session: AsyncSession, refund_id: int) -> Refund:
    refund = await session.scalar(select(Refund).where(Refund.id == refund_id).with_for_update())
    if refund is None:
        raise LedgerError(f"refund {refund_id} not found")
    return refund


async def settle_refund(
    session: AsyncSession, refund_id: int, provider_refund_id: str | None = None
) -> Refund:
    """Mark a refund succeeded. Idempotent: settling a settled refund is a no-op."""
    refund = await _locked_refund(session, refund_id)
    if refund.status == "succeeded":
        return refund
    if refund.status != "pending":
        raise LedgerError(f"refund {refund.id} is {refund.status}, cannot settle")
    refund.status = "succeeded"
    refund.settled_at = _now()
    if provider_refund_id is not None:
        refund.provider_refund_id = provider_refund_id
    await session.flush()
    amount = refund.amount_paise
    await post_journal(
        session,
        "refund_settle",
        f"refund:{refund.id}",
        [(REFUNDS_PAYABLE, amount), (PROVIDER_CLEARING, -amount)],
    )
    return refund


async def fail_refund(session: AsyncSession, refund_id: int) -> Refund:
    """Mark a refund failed and release its reservation. Idempotent for already-failed refunds."""
    refund = await _locked_refund(session, refund_id)
    if refund.status == "failed":
        return refund
    if refund.status != "pending":
        raise LedgerError(f"refund {refund.id} is {refund.status}, cannot fail")
    refund.status = "failed"
    await session.flush()
    amount = refund.amount_paise
    await post_journal(
        session,
        "refund_release",
        f"refund:{refund.id}",
        [(REFUNDS_PAYABLE, amount), (REFUNDS_CONTRA, -amount)],
    )
    return refund


async def account_balances(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(
        select(LedgerEntry.account, func.sum(LedgerEntry.amount_paise)).group_by(
            LedgerEntry.account
        )
    )
    return {account: int(total) for account, total in rows}
