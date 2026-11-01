"""A guarded-mode world for tests: the migrated ledger, an in-process mockpay, and mockpay's
webhooks delivered to Tollgate's own API app (so webhook tests run end to end)."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mockpay.app import Provider, create_app
from mockpay.config import MockpaySettings
from tests.conftest import recreate_database
from tollgate.agent.session import AuthenticatedSession
from tollgate.api.app import create_app as create_tollgate_app
from tollgate.clock import FixedClock
from tollgate.config import REPO_ROOT
from tollgate.db.models import Customer
from tollgate.guarded.executor import Executor
from tollgate.ledger import service
from tollgate.policy_config import load_policy
from tollgate.provider_client import ProviderClient

pytestmark = pytest.mark.db

SECRET = "guarded-test-secret"
CLOCK = FixedClock(date(2026, 10, 1))
POLICY = load_policy(REPO_ROOT / "config" / "policy.v1.yaml")


@dataclass
class Seeded:
    session: AuthenticatedSession
    order_ref: str
    order_id: int
    payment_id: int
    provider_payment_id: str | None


@dataclass
class World:
    maker: async_sessionmaker[AsyncSession]
    mockpay: Provider
    http: httpx.AsyncClient
    provider: ProviderClient
    executor: Executor
    customers: dict[str, Customer] = field(default_factory=dict)

    def new_executor(self, **options: object) -> Executor:
        return Executor(self.maker, self.provider, POLICY, CLOCK, retry_delays_s=(0.0,), **options)  # type: ignore[arg-type]

    async def provider_refunds(self, provider_payment_id: str) -> list[int]:
        rows = await self.mockpay.store.list_refunds(payment_id=provider_payment_id)
        return [r.amount_paise for r in rows if not (r.receipt or "").startswith("seed:")]

    async def order(
        self,
        amount_paise: int,
        *,
        status: str = "delivered",
        days_ago: int = 3,
        customer: str | None = None,
        prior_refunds_today: int = 0,
    ) -> Seeded:
        """One order for a customer (new unless named), mirrored at mockpay when captured."""
        ref = f"ORD-G{uuid.uuid4().hex[:10].upper()}"
        key = customer or ref
        async with self.maker() as s, s.begin():
            if key not in self.customers:
                self.customers[key] = await service.create_customer(
                    s, f"cust_{uuid.uuid4().hex[:12]}", "Test Customer", "t@example.com"
                )
            cust = self.customers[key]
            placed = CLOCK.days_ago(days_ago)
            order, payment = await service.create_order(
                s, cust, ref, amount_paise, placed_at=placed
            )
            provider_payment_id = None
            if status == "cancelled":
                await service.cancel_order(s, order.id)
            elif status != "placed":
                provider_payment_id = f"pay_{ref}"
                payment.provider_payment_id = provider_payment_id
                await service.capture_payment(s, payment.id, at=placed)
                if status in ("shipped", "delivered"):
                    await service.ship_order(s, order.id, at=placed + timedelta(days=1))
                if status == "delivered":
                    await service.deliver_order(s, order.id)
                if prior_refunds_today:
                    refund = await service.reserve_refund(
                        s, payment.id, prior_refunds_today, f"seed:{ref}", at=CLOCK.now()
                    )
                    await service.settle_refund(s, refund.id)
            order_id, payment_id = order.id, payment.id
        if provider_payment_id:
            await self.provider.create_payment(amount_paise, provider_payment_id)
            if prior_refunds_today:
                await self.provider.create_refund(
                    provider_payment_id, prior_refunds_today, receipt=f"seed:{ref}"
                )
        session = AuthenticatedSession(
            customer_id=cust.id,
            customer_ref=cust.ref,
            customer_name="Test Customer",
            conversation_id=f"conv_{uuid.uuid4().hex[:8]}",
        )
        return Seeded(session, ref, order_id, payment_id, provider_payment_id)


@pytest.fixture(scope="session")
def guarded_mockpay_db_url() -> str:
    return recreate_database("mockpay_guarded_test")


@pytest.fixture
async def world(
    maker: async_sessionmaker[AsyncSession], guarded_mockpay_db_url: str
) -> AsyncIterator[World]:
    api = create_tollgate_app(maker, SECRET)
    async with api.router.lifespan_context(api):
        settings = MockpaySettings(
            database_url=guarded_mockpay_db_url,
            webhook_url="http://tollgate.test/webhooks/mockpay",
            webhook_secret=SECRET,
            settle_delay_ms=0,
            settle_poll_ms=0,
            timeout_hang_ms=1500,
        )
        app = create_app(settings, httpx.ASGITransport(app=api))
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://mockpay.test") as c:
                provider = ProviderClient(c, timeout_s=0.5)
                executor = Executor(maker, provider, POLICY, CLOCK, retry_delays_s=(0.0,))
                yield World(maker, app.state.provider, c, provider, executor)
