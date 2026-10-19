"""What the agent sees about an order. Deterministic: no ids or timestamps that vary per run."""

from typing import Any

from tollgate.clock import Clock
from tollgate.db.models import Order, Payment, Product
from tollgate.money import format_inr


def order_view(
    order: Order, payment: Payment, product: Product | None, clock: Clock
) -> dict[str, Any]:
    refundable = payment.captured_paise - payment.refunded_paise
    return {
        "order_id": order.ref,
        "status": order.status,
        "amount": format_inr(order.amount_paise),
        "amount_paise": order.amount_paise,
        "payment_status": payment.status,
        "paid_paise": payment.captured_paise,
        "refunded_paise": payment.refunded_paise,
        "refundable_paise": refundable,
        "placed_on": order.placed_at.date().isoformat(),
        "days_since_order": (clock.today() - order.placed_at.date()).days,
        "product": product.name if product else None,
        "product_description": product.description if product else None,
        "notes": order.notes,
    }
