"""Property-based tests: hypothesis drives random sequences of ledger operations (including
over-refunds, illegal transitions and direct tampering) against real Postgres.

After every step it checks that
  * the database accepted exactly the operations a simple reference model says are legal, and
  * every ledger invariant still holds, globally and for this run's payments.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    invariant,
    multiple,
    rule,
    run_state_machine_as_test,
)
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.ledger.factory import uid
from tollgate.ledger import service
from tollgate.ledger.service import LedgerError

pytestmark = pytest.mark.db


@dataclass
class PaymentModel:
    order_id: int
    amount: int
    order_status: str = "placed"
    status: str = "authorized"
    captured: int = 0


@dataclass
class RefundModel:
    payment_id: int
    amount: int
    status: str = "pending"


class Rejected:
    def __init__(self, reason: str) -> None:
        self.reason = reason


# Each query returns rows only when an invariant is broken.
GLOBAL_VIOLATIONS = {
    "refunded > captured": "SELECT id FROM payments WHERE refunded_paise > captured_paise",
    "counter != sum of live refunds": """
        SELECT p.id FROM payments p
        LEFT JOIN refunds r ON r.payment_id = p.id AND r.status <> 'failed'
        GROUP BY p.id HAVING p.refunded_paise <> COALESCE(SUM(r.amount_paise), 0)""",
    "unbalanced journal": """
        SELECT j.id FROM journals j LEFT JOIN ledger_entries e ON e.journal_id = j.id
        GROUP BY j.id HAVING COUNT(e.id) < 2 OR COALESCE(SUM(e.amount_paise), 0) <> 0""",
    "refund on uncaptured payment": """
        SELECT r.id FROM refunds r JOIN payments p ON p.id = r.payment_id
        WHERE p.status <> 'captured'""",
    "captured payment on order cancelled before capture": """
        SELECT p.id FROM payments p JOIN orders o ON o.id = p.order_id
        WHERE o.status = 'cancelled' AND p.status = 'captured'
          AND p.captured_at > o.cancelled_at""",
}

# Account balances must equal what the operational tables imply.
ACCOUNT_IDENTITIES = """
SELECT
  (SELECT COALESCE(SUM(amount_paise), 0) FROM ledger_entries) AS ledger_total,
  (SELECT COALESCE(SUM(amount_paise), 0) FROM ledger_entries WHERE account = 'provider_clearing')
    - ((SELECT COALESCE(SUM(captured_paise), 0) FROM payments)
       - (SELECT COALESCE(SUM(amount_paise), 0) FROM refunds WHERE status = 'succeeded'))
    AS clearing_gap,
  (SELECT COALESCE(SUM(amount_paise), 0) FROM ledger_entries WHERE account = 'revenue')
    + (SELECT COALESCE(SUM(captured_paise), 0) FROM payments) AS revenue_gap,
  (SELECT COALESCE(SUM(amount_paise), 0) FROM ledger_entries WHERE account = 'refunds_payable')
    + (SELECT COALESCE(SUM(amount_paise), 0) FROM refunds WHERE status = 'pending')
    AS payable_gap,
  (SELECT COALESCE(SUM(amount_paise), 0) FROM ledger_entries WHERE account = 'refunds_contra')
    - (SELECT COALESCE(SUM(refunded_paise), 0) FROM payments) AS contra_gap
"""


class LedgerMachine(RuleBasedStateMachine):
    db_url: ClassVar[str] = ""

    payments = Bundle("payments")
    refunds = Bundle("refunds")

    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.engine = create_async_engine(self.db_url, poolclass=NullPool)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)
        self.model_payments: dict[int, PaymentModel] = {}
        self.model_refunds: dict[int, RefundModel] = {}
        self.customer = self.run(self._tx(self._create_customer))

    # -------------------------------------------------------------- plumbing

    def run(self, coro: Awaitable[Any]) -> Any:
        return self.loop.run_until_complete(coro)

    async def _tx(self, fn: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
        try:
            async with self.maker() as s, s.begin():
                return await fn(s)
        except (IntegrityError, LedgerError) as exc:
            return Rejected(str(exc))

    def attempt(self, fn: Callable[[AsyncSession], Awaitable[Any]], legal: bool, what: str) -> Any:
        result = self.run(self._tx(fn))
        accepted = not isinstance(result, Rejected)
        reason = result.reason if isinstance(result, Rejected) else ""
        assert accepted == legal, f"{what}: legal={legal} but accepted={accepted} {reason}"
        return result

    @staticmethod
    async def _create_customer(s: AsyncSession) -> Any:
        return await service.create_customer(s, f"cust_{uid()}", "Prop", "p@example.com")

    def teardown(self) -> None:
        self.run(self.engine.dispose())
        self.loop.close()

    # -------------------------------------------------------------- rules

    @rule(target=payments, amount=st.integers(min_value=1, max_value=2_000_000))
    def place_order(self, amount: int) -> int:
        async def op(s: AsyncSession) -> tuple[int, int]:
            order, payment = await service.create_order(s, self.customer, f"ORD-{uid()}", amount)
            return order.id, payment.id

        order_id, payment_id = self.attempt(op, True, "place order")
        self.model_payments[payment_id] = PaymentModel(order_id=order_id, amount=amount)
        return payment_id

    @rule(
        pid=payments,
        amount=st.one_of(st.none(), st.integers(min_value=1, max_value=2_500_000)),
    )
    def capture(self, pid: int, amount: int | None) -> None:
        m = self.model_payments[pid]
        capture_amount = m.amount if amount is None else amount
        legal = (
            m.status == "authorized" and m.order_status == "placed" and capture_amount <= m.amount
        )
        self.attempt(lambda s: service.capture_payment(s, pid, amount), legal, "capture")
        if legal:
            m.status, m.captured, m.order_status = "captured", capture_amount, "paid"

    @rule(pid=payments)
    def ship(self, pid: int) -> None:
        m = self.model_payments[pid]
        legal = m.order_status == "paid"
        self.attempt(lambda s: service.ship_order(s, m.order_id), legal, "ship")
        if legal:
            m.order_status = "shipped"

    @rule(pid=payments)
    def cancel(self, pid: int) -> None:
        m = self.model_payments[pid]
        legal = m.order_status in ("placed", "paid")
        self.attempt(lambda s: service.cancel_order(s, m.order_id), legal, "cancel")
        if legal:
            m.order_status = "cancelled"
            if m.status == "authorized":
                m.status = "voided"

    def _reserved(self, pid: int) -> int:
        return sum(
            r.amount
            for r in self.model_refunds.values()
            if r.payment_id == pid and r.status != "failed"
        )

    @rule(target=refunds, pid=payments, amount=st.integers(min_value=1, max_value=1_500_000))
    def request_refund(self, pid: int, amount: int) -> Any:
        m = self.model_payments[pid]
        legal = m.status == "captured" and self._reserved(pid) + amount <= m.captured

        async def op(s: AsyncSession) -> int:
            refund = await service.reserve_refund(s, pid, amount, f"key_{uid()}")
            return refund.id

        result = self.attempt(op, legal, f"refund {amount} (reserved {self._reserved(pid)})")
        if not legal:
            return multiple()
        self.model_refunds[result] = RefundModel(payment_id=pid, amount=amount)
        return result

    @rule(rid=refunds)
    def settle(self, rid: int) -> None:
        r = self.model_refunds[rid]
        legal = r.status in ("pending", "succeeded")
        self.attempt(lambda s: service.settle_refund(s, rid), legal, "settle")
        if legal:
            r.status = "succeeded"

    @rule(rid=refunds)
    def fail(self, rid: int) -> None:
        r = self.model_refunds[rid]
        legal = r.status in ("pending", "failed")
        self.attempt(lambda s: service.fail_refund(s, rid), legal, "fail")
        if legal:
            r.status = "failed"

    @rule(pid=payments, value=st.integers(min_value=0, max_value=3_000_000))
    def tamper_with_refund_counter(self, pid: int, value: int) -> None:
        async def op(s: AsyncSession) -> None:
            await s.execute(
                text("UPDATE payments SET refunded_paise = :v WHERE id = :p"),
                {"v": value, "p": pid},
            )

        # Only a no-op write can pass (the trigger fires only when the value changes).
        legal = value == self._reserved(pid)
        self.attempt(op, legal, "tamper refunded_paise")

    @rule(
        legs=st.lists(
            st.integers(min_value=-1_000_000, max_value=1_000_000).filter(bool), max_size=3
        )
    )
    def post_raw_unbalanced_journal(self, legs: list[int]) -> None:
        if len(legs) >= 2 and sum(legs) == 0:
            legs = [*legs, 1]  # keep this rule about unbalanced/short journals only

        async def op(s: AsyncSession) -> None:
            jid = (
                await s.execute(
                    text("INSERT INTO journals (kind, ref) VALUES ('raw', 'prop') RETURNING id")
                )
            ).scalar_one()
            for amount in legs:
                await s.execute(
                    text(
                        "INSERT INTO ledger_entries (journal_id, account, amount_paise)"
                        " VALUES (:j, 'revenue', :a)"
                    ),
                    {"j": jid, "a": amount},
                )

        self.attempt(op, False, f"raw journal {legs}")

    # -------------------------------------------------------------- invariants

    @invariant()
    def database_matches_model(self) -> None:
        if not self.model_payments:
            return

        async def fetch(s: AsyncSession) -> list[Any]:
            rows = await s.execute(
                text(
                    "SELECT p.id, p.status, p.captured_paise, p.refunded_paise, o.status"
                    " FROM payments p JOIN orders o ON o.id = p.order_id WHERE p.id = ANY(:ids)"
                ),
                {"ids": list(self.model_payments)},
            )
            return list(rows)

        for pid, status, captured, refunded, order_status in self.run(self._tx(fetch)):
            m = self.model_payments[pid]
            assert (status, captured, order_status) == (m.status, m.captured, m.order_status)
            assert refunded == self._reserved(pid)
            assert refunded <= captured

    @invariant()
    def global_invariants_hold(self) -> None:
        async def check(s: AsyncSession) -> tuple[dict[str, list[Any]], Any]:
            broken = {
                name: list((await s.execute(text(sql))).scalars())
                for name, sql in GLOBAL_VIOLATIONS.items()
            }
            return broken, (await s.execute(text(ACCOUNT_IDENTITIES))).one()

        broken, gaps = self.run(self._tx(check))
        assert not any(broken.values()), broken
        assert tuple(gaps) == (0, 0, 0, 0, 0), gaps._asdict()


def test_ledger_state_machine(tollgate_db_url: str) -> None:
    LedgerMachine.db_url = tollgate_db_url
    run_state_machine_as_test(LedgerMachine, settings=settings(stateful_step_count=30))
