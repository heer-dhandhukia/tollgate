"""Seed one scenario into its sandbox: same orders in the ledger and at the provider."""

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.agent.session import AuthenticatedSession
from tollgate.clock import FixedClock
from tollgate.db.models import Product as ProductRow
from tollgate.eval.scenario import CAPTURED, Product, Scenario
from tollgate.ledger import service
from tollgate.provider_client import ProviderClient

SEED_RECEIPT_PREFIX = "seed:"


@dataclass(frozen=True)
class SeededOrder:
    order_id: int
    payment_id: int
    provider_payment_id: str | None


@dataclass(frozen=True)
class Seeded:
    session: AuthenticatedSession
    orders: dict[str, SeededOrder]


async def seed(
    scenario: Scenario,
    catalog: dict[str, Product],
    maker: async_sessionmaker[AsyncSession],
    provider: ProviderClient,
    clock: FixedClock,
) -> Seeded:
    orders: dict[str, SeededOrder] = {}
    async with maker() as s, s.begin():
        product_ids: dict[str, int] = {}
        for sku in sorted({o.product for o in scenario.orders if o.product}):
            row = ProductRow(sku=sku, name=catalog[sku].name, description=catalog[sku].description)
            s.add(row)
            await s.flush()
            product_ids[sku] = row.id

        customer = await service.create_customer(
            s, f"cust_{scenario.id}", scenario.customer.name, scenario.customer.email
        )
        for spec in scenario.orders:
            placed_at = clock.days_ago(spec.placed_days_ago)
            order, payment = await service.create_order(
                s,
                customer,
                spec.ref,
                spec.amount_paise,
                spec.notes,
                product_id=product_ids.get(spec.product) if spec.product else None,
                placed_at=placed_at,
            )
            provider_payment_id = None
            if spec.status == "cancelled":  # cancelled before capture
                await service.cancel_order(s, order.id)
            elif spec.status in CAPTURED:
                provider_payment_id = f"pay_{spec.ref}"
                payment.provider_payment_id = provider_payment_id
                await service.capture_payment(s, payment.id, at=placed_at)
                if spec.status in ("shipped", "delivered"):
                    await service.ship_order(s, order.id, at=placed_at + timedelta(days=1))
                if spec.status == "delivered":
                    await service.deliver_order(s, order.id)
                for i, prior in enumerate(spec.prior_refunds):
                    refund = await service.reserve_refund(
                        s,
                        payment.id,
                        prior.amount_paise,
                        f"seed:{spec.ref}:{i}",
                        "earlier refund",
                        at=clock.days_ago(prior.days_ago),
                    )
                    await service.settle_refund(s, refund.id)
            orders[spec.ref] = SeededOrder(order.id, payment.id, provider_payment_id)

    for spec in scenario.orders:
        seeded = orders[spec.ref]
        if seeded.provider_payment_id is None:
            continue
        await provider.create_payment(spec.amount_paise, seeded.provider_payment_id)
        for i, prior in enumerate(spec.prior_refunds):
            await provider.create_refund(
                seeded.provider_payment_id,
                prior.amount_paise,
                receipt=f"{SEED_RECEIPT_PREFIX}{spec.ref}:{i}",
            )

    session = AuthenticatedSession(
        customer_id=customer.id,
        customer_ref=f"cust_{scenario.id}",
        customer_name=scenario.customer.name,
        conversation_id=f"conv_{scenario.id}_{uuid.uuid4().hex[:8]}",
    )
    return Seeded(session, orders)
