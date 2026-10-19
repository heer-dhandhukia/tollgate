"""ledger invariants enforced in Postgres

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-19

Invariants (see DESIGN.md, "Ledger invariants live in the database"):

1. Total refunded <= captured, per payment. `payments.refunded_paise` is a reservation counter
   maintained only by triggers on `refunds` (pending + succeeded count; failed releases). The
   counter UPDATE is checked by `refunded_le_captured`, so an over-refund aborts the transaction.
   Because that UPDATE takes the payment's row lock, concurrent refunds on one payment serialise.
2. The ledger always balances. A deferred constraint trigger checks at COMMIT that every touched
   journal has >= 2 entries summing to 0. Journals and entries are append-only.
3. No refund on an order cancelled before capture. Refunds require a captured payment; a payment
   cannot be captured once its order is cancelled; status transitions are one-way.

All violations raise SQLSTATE 23514 (check_violation) so drivers surface them as IntegrityError.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = r"""
-- ---------------------------------------------------------------- orders
CREATE FUNCTION tg_orders_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.customer_id <> OLD.customer_id OR NEW.amount_paise <> OLD.amount_paise THEN
    RAISE EXCEPTION 'tollgate: order % customer and amount are immutable', OLD.id
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.status <> OLD.status AND NOT ((OLD.status, NEW.status) IN (
      ('placed', 'paid'), ('placed', 'cancelled'),
      ('paid', 'shipped'), ('paid', 'cancelled'),
      ('shipped', 'delivered'))) THEN
    RAISE EXCEPTION 'tollgate: illegal order transition % -> % (order %)',
      OLD.status, NEW.status, OLD.id USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
-- @@

CREATE TRIGGER orders_guard BEFORE UPDATE ON orders
  FOR EACH ROW EXECUTE FUNCTION tg_orders_guard();
-- @@

-- ---------------------------------------------------------------- payments
CREATE FUNCTION tg_payments_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_order_status text;
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.refunded_paise <> 0 THEN
      RAISE EXCEPTION 'tollgate: a new payment cannot carry refunds'
        USING ERRCODE = 'check_violation';
    END IF;
  ELSE
    -- refunded_paise is owned by the refunds triggers (which run at trigger depth >= 2).
    IF NEW.refunded_paise <> OLD.refunded_paise AND pg_trigger_depth() < 2 THEN
      RAISE EXCEPTION 'tollgate: payments.refunded_paise is maintained by refunds only'
        USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.order_id <> OLD.order_id OR NEW.amount_paise <> OLD.amount_paise THEN
      RAISE EXCEPTION 'tollgate: payment % order and amount are immutable', OLD.id
        USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.captured_paise <> OLD.captured_paise AND OLD.status <> 'authorized' THEN
      RAISE EXCEPTION 'tollgate: payment % capture amount is fixed once captured', OLD.id
        USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status <> OLD.status AND NOT ((OLD.status, NEW.status) IN (
        ('authorized', 'captured'), ('authorized', 'voided'))) THEN
      RAISE EXCEPTION 'tollgate: illegal payment transition % -> % (payment %)',
        OLD.status, NEW.status, OLD.id USING ERRCODE = 'check_violation';
    END IF;
  END IF;

  IF NEW.status = 'captured' AND (TG_OP = 'INSERT' OR OLD.status <> 'captured') THEN
    SELECT status INTO v_order_status FROM orders WHERE id = NEW.order_id FOR SHARE;
    IF v_order_status = 'cancelled' THEN
      RAISE EXCEPTION 'tollgate: cannot capture payment for cancelled order %', NEW.order_id
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;
-- @@

CREATE TRIGGER payments_guard BEFORE INSERT OR UPDATE ON payments
  FOR EACH ROW EXECUTE FUNCTION tg_payments_guard();
-- @@

-- ---------------------------------------------------------------- refunds
CREATE FUNCTION tg_refunds_before() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_payment_status text;
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'tollgate: refunds cannot be deleted' USING ERRCODE = 'check_violation';
  END IF;

  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'pending' THEN
      RAISE EXCEPTION 'tollgate: refunds must be created pending'
        USING ERRCODE = 'check_violation';
    END IF;
    SELECT status INTO v_payment_status FROM payments WHERE id = NEW.payment_id;
    IF v_payment_status IS DISTINCT FROM 'captured' THEN
      RAISE EXCEPTION 'tollgate: refund requires a captured payment (payment % is %)',
        NEW.payment_id, v_payment_status USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;

  -- UPDATE
  IF NEW.payment_id <> OLD.payment_id OR NEW.amount_paise <> OLD.amount_paise
     OR NEW.idempotency_key <> OLD.idempotency_key THEN
    RAISE EXCEPTION 'tollgate: refund % payment, amount and idempotency key are immutable',
      OLD.id USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.provider_refund_id IS NOT NULL
     AND NEW.provider_refund_id IS DISTINCT FROM OLD.provider_refund_id THEN
    RAISE EXCEPTION 'tollgate: refund % provider id is immutable once set', OLD.id
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.status <> OLD.status AND NOT ((OLD.status, NEW.status) IN (
      ('pending', 'succeeded'), ('pending', 'failed'))) THEN
    RAISE EXCEPTION 'tollgate: illegal refund transition % -> % (refund %)',
      OLD.status, NEW.status, OLD.id USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
-- @@

CREATE TRIGGER refunds_before BEFORE INSERT OR UPDATE OR DELETE ON refunds
  FOR EACH ROW EXECUTE FUNCTION tg_refunds_before();
-- @@

CREATE FUNCTION tg_refunds_reserve() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    -- Reserve. Takes the payment row lock; the CHECK rejects refunded > captured.
    UPDATE payments SET refunded_paise = refunded_paise + NEW.amount_paise
      WHERE id = NEW.payment_id;
  ELSIF OLD.status = 'pending' AND NEW.status = 'failed' THEN
    -- Release the reservation.
    UPDATE payments SET refunded_paise = refunded_paise - NEW.amount_paise
      WHERE id = NEW.payment_id;
  END IF;
  RETURN NULL;
END $$;
-- @@

CREATE TRIGGER refunds_reserve AFTER INSERT OR UPDATE OF status ON refunds
  FOR EACH ROW EXECUTE FUNCTION tg_refunds_reserve();
-- @@

-- ---------------------------------------------------------------- ledger
CREATE FUNCTION tg_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'tollgate: % is append-only (% rejected)', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'check_violation';
END $$;
-- @@

CREATE TRIGGER journals_append_only BEFORE UPDATE OR DELETE ON journals
  FOR EACH ROW EXECUTE FUNCTION tg_append_only();
-- @@
CREATE TRIGGER journals_no_truncate BEFORE TRUNCATE ON journals
  FOR EACH STATEMENT EXECUTE FUNCTION tg_append_only();
-- @@
CREATE TRIGGER ledger_entries_append_only BEFORE UPDATE OR DELETE ON ledger_entries
  FOR EACH ROW EXECUTE FUNCTION tg_append_only();
-- @@
CREATE TRIGGER ledger_entries_no_truncate BEFORE TRUNCATE ON ledger_entries
  FOR EACH STATEMENT EXECUTE FUNCTION tg_append_only();
-- @@

CREATE FUNCTION tg_journal_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_journal bigint;
  v_sum numeric;
  v_count bigint;
BEGIN
  IF TG_TABLE_NAME = 'journals' THEN
    v_journal := NEW.id;
  ELSE
    v_journal := NEW.journal_id;
  END IF;
  SELECT COALESCE(SUM(amount_paise), 0), COUNT(*) INTO v_sum, v_count
    FROM ledger_entries WHERE journal_id = v_journal;
  IF v_count < 2 THEN
    RAISE EXCEPTION 'tollgate: journal % has % entries; double entry needs >= 2',
      v_journal, v_count USING ERRCODE = 'check_violation';
  END IF;
  IF v_sum <> 0 THEN
    RAISE EXCEPTION 'tollgate: journal % does not balance (sum = % paise)', v_journal, v_sum
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END $$;
-- @@

-- Deferred: entries are inserted one by one, the journal must balance by COMMIT.
CREATE CONSTRAINT TRIGGER journals_balanced AFTER INSERT ON journals
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION tg_journal_balanced();
-- @@
CREATE CONSTRAINT TRIGGER ledger_entries_balanced AFTER INSERT ON ledger_entries
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION tg_journal_balanced();
-- @@
"""

DOWNGRADE = r"""
DROP TRIGGER IF EXISTS ledger_entries_balanced ON ledger_entries;
DROP TRIGGER IF EXISTS journals_balanced ON journals;
DROP FUNCTION IF EXISTS tg_journal_balanced();
DROP TRIGGER IF EXISTS ledger_entries_no_truncate ON ledger_entries;
DROP TRIGGER IF EXISTS ledger_entries_append_only ON ledger_entries;
DROP TRIGGER IF EXISTS journals_no_truncate ON journals;
DROP TRIGGER IF EXISTS journals_append_only ON journals;
DROP FUNCTION IF EXISTS tg_append_only();
DROP TRIGGER IF EXISTS refunds_reserve ON refunds;
DROP FUNCTION IF EXISTS tg_refunds_reserve();
DROP TRIGGER IF EXISTS refunds_before ON refunds;
DROP FUNCTION IF EXISTS tg_refunds_before();
DROP TRIGGER IF EXISTS payments_guard ON payments;
DROP FUNCTION IF EXISTS tg_payments_guard();
DROP TRIGGER IF EXISTS orders_guard ON orders;
DROP FUNCTION IF EXISTS tg_orders_guard();
"""


def _statements(script: str) -> list[str]:
    # asyncpg prepares each statement, so multi-statement strings must be split.
    return [stmt.strip() for stmt in script.split("-- @@") if stmt.strip()]


def upgrade() -> None:
    for stmt in _statements(UPGRADE):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE.strip().splitlines():
        op.execute(stmt)
