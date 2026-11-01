"""guarded mode: refund intents, append-only audit log, received webhook events

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-01

Guarded mode never lets the agent move money directly. Every refund the agent asks for becomes a
`refund_intents` row, and only the executor turns an intent into a provider call.

Rules enforced here, in the database (see DESIGN.md, "Guarded mode"):

1. Intent status only moves forward:
       proposed -> denied | awaiting_approval | executing
       awaiting_approval -> rejected | executing
       executing -> succeeded | failed | unknown
       unknown -> succeeded | failed
   Who asked, for which order, and how much are immutable once written.
2. At most one intent per order is in flight (executing or unknown). A second concurrent request
   on the same order fails the unique index instead of racing the first one to the provider.
3. The audit log is append-only, like the ledger.
4. A webhook event id is stored once; redelivery of the same event is a no-op.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INTENT_STATUSES = (
    "proposed",
    "denied",
    "awaiting_approval",
    "rejected",
    "executing",
    "succeeded",
    "failed",
    "unknown",
)

TRIGGERS = r"""
CREATE FUNCTION tg_refund_intents_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'proposed' THEN
      RAISE EXCEPTION 'tollgate: refund intents must be created proposed'
        USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.conversation_id <> OLD.conversation_id OR NEW.customer_id <> OLD.customer_id
     OR NEW.order_id <> OLD.order_id OR NEW.kind <> OLD.kind
     OR NEW.amount_paise <> OLD.amount_paise THEN
    RAISE EXCEPTION 'tollgate: refund intent % who, what and how much are immutable', OLD.id
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.status <> OLD.status AND NOT ((OLD.status, NEW.status) IN (
      ('proposed', 'denied'), ('proposed', 'awaiting_approval'), ('proposed', 'executing'),
      ('awaiting_approval', 'rejected'), ('awaiting_approval', 'executing'),
      ('executing', 'succeeded'), ('executing', 'failed'), ('executing', 'unknown'),
      ('unknown', 'succeeded'), ('unknown', 'failed'))) THEN
    RAISE EXCEPTION 'tollgate: illegal intent transition % -> % (intent %)',
      OLD.status, NEW.status, OLD.id USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.refund_id IS NOT NULL AND NEW.refund_id IS DISTINCT FROM OLD.refund_id THEN
    RAISE EXCEPTION 'tollgate: refund intent % refund is immutable once set', OLD.id
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
-- @@

CREATE TRIGGER refund_intents_guard BEFORE INSERT OR UPDATE ON refund_intents
  FOR EACH ROW EXECUTE FUNCTION tg_refund_intents_guard();
-- @@

CREATE TRIGGER refund_intents_no_delete BEFORE DELETE ON refund_intents
  FOR EACH ROW EXECUTE FUNCTION tg_append_only();
-- @@

CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION tg_append_only();
-- @@

CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION tg_append_only();
"""


def upgrade() -> None:
    op.create_table(
        "refund_intents",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("conversation_id", sa.String(128), nullable=False),
        sa.Column("customer_id", sa.BigInteger, sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("order_id", sa.BigInteger, sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("amount_paise", sa.BigInteger, nullable=False),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.String(24), nullable=False, server_default="proposed"),
        sa.Column("policy_rule", sa.String(64)),
        sa.Column("decision_note", sa.Text, nullable=False, server_default=""),
        sa.Column("decided_by", sa.String(64)),
        sa.Column("refund_id", sa.BigInteger, sa.ForeignKey("refunds.id"), unique=True),
        sa.Column("provider_refund_id", sa.String(64)),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("kind IN ('refund', 'cancel')", name="kind_valid"),
        sa.CheckConstraint("amount_paise > 0", name="amount_positive"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in INTENT_STATUSES) + ")",
            name="status_valid",
        ),
        sa.CheckConstraint(
            "status NOT IN ('executing', 'succeeded', 'unknown') OR refund_id IS NOT NULL",
            name="reserved_before_execution",
        ),
    )
    op.create_index("ix_refund_intents_conversation_id", "refund_intents", ["conversation_id"])
    op.create_index("ix_refund_intents_status", "refund_intents", ["status"])
    op.create_index(
        "uq_refund_intents_one_in_flight_per_order",
        "refund_intents",
        ["order_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('executing', 'unknown')"),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("at", sa.DateTime, nullable=False, server_default=sa.text("now()")),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("intent_id", sa.String(40), index=True),
        sa.Column("conversation_id", sa.String(128), index=True),
        sa.Column(
            "details",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    op.create_table(
        "webhook_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("sequence", sa.BigInteger, nullable=False),
        sa.Column("provider_refund_id", sa.String(64), index=True),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("received_at", sa.DateTime, nullable=False, server_default=sa.text("now()")),
    )

    for statement in TRIGGERS.split("-- @@"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    op.drop_table("webhook_events")
    op.drop_table("audit_log")
    op.drop_table("refund_intents")
    op.execute("DROP FUNCTION IF EXISTS tg_refund_intents_guard()")
