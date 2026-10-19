"""The naive tool backend: how a first implementation typically wires an agent to money.

What makes it naive (and nothing else differs from guarded mode):
- issue_refund / cancel_order call the payment provider directly, with no Idempotency-Key;
- the refund policy exists only in the system prompt: no amount, window or limit checks;
- refund/cancel do not re-check that the order belongs to the signed-in customer;
- the external call happens first and the ledger is updated afterwards, best effort.

Errors and timeouts are returned to the model as tool results, so whether to retry is the
model's decision. That is how duplicate refunds happen.
"""

from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.agent.session import AuthenticatedSession
from tollgate.agent.tools import CancelOrderArgs, EscalateArgs, IssueRefundArgs, LookupOrderArgs
from tollgate.agent.views import order_view
from tollgate.clock import Clock
from tollgate.db.models import Escalation, Order, Payment, Product
from tollgate.ledger import service
from tollgate.ledger.service import LedgerError
from tollgate.money import format_inr
from tollgate.provider_client import ProviderClient, ProviderError, ProviderTimeout


class NaiveBackend:
    def __init__(
        self,
        session: AuthenticatedSession,
        maker: async_sessionmaker[AsyncSession],
        provider: ProviderClient,
        clock: Clock,
    ) -> None:
        self.session = session
        self.maker = maker
        self.provider = provider
        self.clock = clock
        self.events: list[dict[str, Any]] = []

    async def _order(
        self, ref: str, owned_only: bool
    ) -> tuple[Order, Payment, Product | None] | None:
        stmt = (
            select(Order, Payment, Product)
            .join(Payment, Payment.order_id == Order.id)
            .outerjoin(Product, Product.id == Order.product_id)
            .where(Order.ref == ref)
        )
        if owned_only:
            stmt = stmt.where(Order.customer_id == self.session.customer_id)
        async with self.maker() as s:
            row = (await s.execute(stmt)).first()
        return None if row is None else (row[0], row[1], row[2])

    async def lookup_order(self, args: LookupOrderArgs) -> dict[str, Any]:
        found = await self._order(args.order_id, owned_only=True)
        if found is None:
            return {"error": f"No order {args.order_id} found for this customer."}
        return order_view(*found, self.clock)

    async def _provider_refund(
        self, payment: Payment, order: Order, amount: int, reason: str
    ) -> dict[str, Any] | None:
        """Refund at the provider, then mirror it in the ledger. Returns an error dict or None."""
        provider_payment = payment.provider_payment_id or f"pay_{order.ref}"
        try:
            refund = await self.provider.create_refund(
                provider_payment, amount, notes={"reason": reason[:200]}
            )
        except ProviderTimeout:
            self.events.append({"type": "provider_timeout", "order": order.ref, "amount": amount})
            return {"error": "Payment provider timed out."}
        except ProviderError as exc:
            self.events.append({"type": "provider_error", "order": order.ref, "status": exc.status})
            return {"error": f"Payment provider error ({exc.status}): {exc.description}"}

        try:
            async with self.maker() as s, s.begin():
                await service.reserve_refund(
                    s, payment.id, amount, f"naive:{refund['id']}", reason, at=self.clock.now()
                )
        except (IntegrityError, LedgerError, ValidationError) as exc:
            # Money has already left at the provider; our books now disagree with it.
            self.events.append(
                {
                    "type": "ledger_rejected_after_provider",
                    "order": order.ref,
                    "amount": amount,
                    "error": type(exc).__name__,
                }
            )
        return None

    async def issue_refund(self, args: IssueRefundArgs) -> dict[str, Any]:
        found = await self._order(args.order_id, owned_only=False)
        if found is None:
            return {"error": f"No order {args.order_id} found."}
        order, payment, _ = found
        error = await self._provider_refund(payment, order, args.amount_paise, args.reason)
        if error is not None:
            return error
        return {
            "status": "refund_initiated",
            "order_id": order.ref,
            "amount": format_inr(args.amount_paise),
            "amount_paise": args.amount_paise,
        }

    async def cancel_order(self, args: CancelOrderArgs) -> dict[str, Any]:
        found = await self._order(args.order_id, owned_only=False)
        if found is None:
            return {"error": f"No order {args.order_id} found."}
        order, payment, _ = found
        refunded = 0
        remaining = payment.captured_paise - payment.refunded_paise
        if payment.status == "captured" and remaining > 0:
            error = await self._provider_refund(payment, order, remaining, "order cancelled")
            if error is not None:
                return error
            refunded = remaining
        try:
            async with self.maker() as s, s.begin():
                await service.cancel_order(s, order.id)
        except (IntegrityError, LedgerError) as exc:
            self.events.append(
                {"type": "cancel_rejected", "order": order.ref, "error": type(exc).__name__}
            )
            return {"error": f"Order {order.ref} could not be cancelled (status: {order.status})."}
        return {"status": "cancelled", "order_id": order.ref, "refunded": format_inr(refunded)}

    async def escalate_to_human(self, args: EscalateArgs) -> dict[str, Any]:
        async with self.maker() as s, s.begin():
            s.add(
                Escalation(
                    customer_id=self.session.customer_id,
                    conversation_id=self.session.conversation_id,
                    order_ref=args.order_id,
                    summary=args.summary,
                    created_at=self.clock.now(),
                )
            )
            await s.flush()
            n = await s.scalar(
                select(func.count())
                .select_from(Escalation)
                .where(Escalation.conversation_id == self.session.conversation_id)
            )
        self.events.append({"type": "escalated", "order": args.order_id})
        return {"status": "escalated", "ticket": f"ESC-{n}"}
