"""ledger schema: customers, orders, payments, refunds, journals, ledger entries

Revision ID: 0001
Revises:
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("ref", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=NOW),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("ref", sa.String(64), nullable=False, unique=True),
        sa.Column("customer_id", sa.BigInteger, sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="placed"),
        sa.Column("amount_paise", sa.BigInteger, nullable=False),
        sa.Column("notes", sa.Text, nullable=False, server_default=""),
        sa.Column("placed_at", sa.DateTime, nullable=False, server_default=NOW),
        sa.Column("shipped_at", sa.DateTime),
        sa.Column("cancelled_at", sa.DateTime),
        sa.CheckConstraint(
            "status IN ('placed', 'paid', 'shipped', 'delivered', 'cancelled')",
            name="status_valid",
        ),
        sa.CheckConstraint("amount_paise > 0", name="amount_positive"),
        sa.CheckConstraint(
            "status <> 'cancelled' OR cancelled_at IS NOT NULL", name="cancelled_at"
        ),
        sa.CheckConstraint(
            "status NOT IN ('shipped', 'delivered') OR shipped_at IS NOT NULL", name="shipped_at"
        ),
    )
    op.create_index("ix_orders_customer_id", "orders", ["customer_id"])
    op.create_table(
        "payments",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "order_id", sa.BigInteger, sa.ForeignKey("orders.id"), nullable=False, unique=True
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="authorized"),
        sa.Column("amount_paise", sa.BigInteger, nullable=False),
        sa.Column("captured_paise", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("refunded_paise", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("provider_payment_id", sa.String(64), unique=True),
        sa.Column("captured_at", sa.DateTime),
        sa.CheckConstraint("status IN ('authorized', 'captured', 'voided')", name="status_valid"),
        sa.CheckConstraint("amount_paise > 0", name="amount_positive"),
        sa.CheckConstraint(
            "0 <= captured_paise AND captured_paise <= amount_paise", name="captured_bounds"
        ),
        sa.CheckConstraint(
            "0 <= refunded_paise AND refunded_paise <= captured_paise",
            name="refunded_le_captured",
        ),
        sa.CheckConstraint(
            "(status = 'captured') = (captured_paise > 0)", name="captured_iff_status"
        ),
    )
    op.create_table(
        "refunds",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("payment_id", sa.BigInteger, sa.ForeignKey("payments.id"), nullable=False),
        sa.Column("amount_paise", sa.BigInteger, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("provider_refund_id", sa.String(64), unique=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=NOW),
        sa.Column("settled_at", sa.DateTime),
        sa.CheckConstraint("status IN ('pending', 'succeeded', 'failed')", name="status_valid"),
        sa.CheckConstraint("amount_paise > 0", name="amount_positive"),
    )
    op.create_index("ix_refunds_payment_id", "refunds", ["payment_id"])
    op.create_table(
        "journals",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("ref", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=NOW),
    )
    op.create_index("ix_journals_ref", "journals", ["ref"])
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("journal_id", sa.BigInteger, sa.ForeignKey("journals.id"), nullable=False),
        sa.Column("account", sa.String(32), nullable=False),
        sa.Column("amount_paise", sa.BigInteger, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "account IN ('provider_clearing', 'revenue', 'refunds_contra', 'refunds_payable')",
            name="account_valid",
        ),
        sa.CheckConstraint("amount_paise <> 0", name="amount_nonzero"),
    )
    op.create_index("ix_ledger_entries_journal_id", "ledger_entries", ["journal_id"])


def downgrade() -> None:
    for table in ("ledger_entries", "journals", "refunds", "payments", "orders", "customers"):
        op.drop_table(table)
