"""Ledger schema. Every amount is BIGINT paise.

Cross-row invariants (refunded <= captured, balanced journals, status transitions, append-only
ledger) are enforced by Postgres triggers in migration 0002, not only here. These models carry the
single-row CHECK constraints so `metadata` matches the migrated schema.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from tollgate.db.base import Base

ORDER_STATUSES = ("placed", "paid", "shipped", "delivered", "cancelled")
PAYMENT_STATUSES = ("authorized", "captured", "voided")
REFUND_STATUSES = ("pending", "succeeded", "failed")

# Double-entry accounts. Signed amounts: debit > 0, credit < 0; every journal sums to 0.
PROVIDER_CLEARING = "provider_clearing"  # asset: money held for us at the payment provider
REVENUE = "revenue"  # income from captured payments
REFUNDS_CONTRA = "refunds_contra"  # contra-revenue: refunds granted
REFUNDS_PAYABLE = "refunds_payable"  # liability: refunds promised but not yet settled
ACCOUNTS = (PROVIDER_CLEARING, REVENUE, REFUNDS_CONTRA, REFUNDS_PAYABLE)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(Text)
    # Free text shown to the agent via lookup_order: an injection surface (suite A5).
    description: Mapped[str] = mapped_column(Text, server_default="")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(_in("status", ORDER_STATUSES), name="status_valid"),
        CheckConstraint("amount_paise > 0", name="amount_positive"),
        CheckConstraint("status <> 'cancelled' OR cancelled_at IS NOT NULL", name="cancelled_at"),
        CheckConstraint(
            "status NOT IN ('shipped', 'delivered') OR shipped_at IS NOT NULL", name="shipped_at"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(64), unique=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    status: Mapped[str] = mapped_column(String(16), server_default="placed")
    amount_paise: Mapped[int]
    notes: Mapped[str] = mapped_column(Text, server_default="")
    placed_at: Mapped[datetime] = mapped_column(server_default=func.now())
    shipped_at: Mapped[datetime | None]
    cancelled_at: Mapped[datetime | None]


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(_in("status", PAYMENT_STATUSES), name="status_valid"),
        CheckConstraint("amount_paise > 0", name="amount_positive"),
        CheckConstraint(
            "0 <= captured_paise AND captured_paise <= amount_paise", name="captured_bounds"
        ),
        # Invariant 1 (row part): total refunded (incl. pending reservations) <= captured.
        CheckConstraint(
            "0 <= refunded_paise AND refunded_paise <= captured_paise", name="refunded_le_captured"
        ),
        CheckConstraint("(status = 'captured') = (captured_paise > 0)", name="captured_iff_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)
    status: Mapped[str] = mapped_column(String(16), server_default="authorized")
    amount_paise: Mapped[int]
    captured_paise: Mapped[int] = mapped_column(server_default="0")
    # Maintained only by the refunds trigger; direct updates are rejected.
    refunded_paise: Mapped[int] = mapped_column(server_default="0")
    provider_payment_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    captured_at: Mapped[datetime | None]


class Refund(Base):
    __tablename__ = "refunds"
    __table_args__ = (
        CheckConstraint(_in("status", REFUND_STATUSES), name="status_valid"),
        CheckConstraint("amount_paise > 0", name="amount_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), index=True)
    amount_paise: Mapped[int]
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    reason: Mapped[str] = mapped_column(Text, server_default="")
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    provider_refund_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    settled_at: Mapped[datetime | None]


class Journal(Base):
    __tablename__ = "journals"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    ref: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint(_in("account", ACCOUNTS), name="account_valid"),
        CheckConstraint("amount_paise <> 0", name="amount_nonzero"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    journal_id: Mapped[int] = mapped_column(ForeignKey("journals.id"), index=True)
    account: Mapped[str] = mapped_column(String(32))
    amount_paise: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(String(128), index=True)
    order_ref: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
