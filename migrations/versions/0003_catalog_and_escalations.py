"""product catalog, orders.product_id, escalations

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("sku", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
    )
    op.add_column("orders", sa.Column("product_id", sa.BigInteger, sa.ForeignKey("products.id")))
    op.create_table(
        "escalations",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("customer_id", sa.BigInteger, sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("conversation_id", sa.String(128), nullable=False),
        sa.Column("order_ref", sa.String(64)),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_escalations_customer_id", "escalations", ["customer_id"])
    op.create_index("ix_escalations_conversation_id", "escalations", ["conversation_id"])


def downgrade() -> None:
    op.drop_table("escalations")
    op.drop_column("orders", "product_id")
    op.drop_table("products")
