"""The guarded tool backend: same tools, same arguments, same validation as naive mode, but
refund and cancel go through the executor instead of calling the provider.

Tool results are deterministic (no intent ids, provider ids or timestamps), so evaluation
replays from the LLM cache stay exact."""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.agent.session import AuthenticatedSession
from tollgate.agent.tools import CancelOrderArgs, EscalateArgs, IssueRefundArgs, LookupOrderArgs
from tollgate.agent.views import order_view
from tollgate.clock import Clock
from tollgate.db.models import Escalation, Order, Payment, Product
from tollgate.guarded.executor import Executor, Outcome
from tollgate.money import format_inr
from tollgate.policy_config import PolicyConfig
from tollgate.provider_client import ProviderClient


def tool_result(outcome: Outcome) -> dict[str, Any]:
    """What the model sees for one executor outcome."""
    ref, amount = outcome.order_ref, outcome.amount_paise
    if outcome.replayed and outcome.status == "succeeded":
        return {
            "status": "already_done",
            "order_id": ref,
            "amount": format_inr(amount),
            "message": "This exact refund was already issued in this conversation. "
            "It was not repeated.",
        }
    if outcome.replayed and outcome.status in ("executing", "unknown"):
        return {
            "status": "already_in_progress",
            "order_id": ref,
            "message": "This exact refund is already being processed. It was not repeated.",
        }
    if outcome.status == "succeeded" and outcome.kind == "cancel":
        return {"status": "cancelled", "order_id": ref, "refunded": format_inr(amount)}
    if outcome.status == "succeeded":
        return {
            "status": "refund_initiated",
            "order_id": ref,
            "amount": format_inr(amount),
            "amount_paise": amount,
        }
    if outcome.status == "cancelled":
        return {"status": "cancelled", "order_id": ref, "refunded": format_inr(0)}
    if outcome.status == "awaiting_approval":
        ticket = f" (ticket {outcome.ticket})" if outcome.ticket else ""
        return {
            "status": "pending_approval",
            "order_id": ref,
            "amount": format_inr(amount),
            "message": f"{outcome.message} The request was sent to a supervisor{ticket}. "
            "Nothing has been refunded yet; tell the customer a person will review it.",
        }
    if outcome.status in ("denied", "rejected"):
        result: dict[str, Any] = {
            "error": f"Not allowed by policy: {outcome.message}",
            "policy_rule": outcome.rule,
        }
        if outcome.max_allowed_paise:
            result["max_refund_now"] = format_inr(outcome.max_allowed_paise)
            result["max_refund_now_paise"] = outcome.max_allowed_paise
        return result
    if outcome.status == "unknown":
        return {
            "status": "pending_confirmation",
            "order_id": ref,
            "message": "The refund was sent but the payment provider has not confirmed it. "
            "Do not retry; a person has been notified and will confirm it.",
        }
    # failed
    return {
        "error": "The payment provider could not complete this refund, and no money moved. "
        "Escalate to a human.",
    }


class GuardedBackend:
    def __init__(
        self,
        session: AuthenticatedSession,
        maker: async_sessionmaker[AsyncSession],
        provider: ProviderClient,
        clock: Clock,
        policy: PolicyConfig,
        **executor_options: Any,
    ) -> None:
        self.session = session
        self.maker = maker
        self.clock = clock
        self.events: list[dict[str, Any]] = []
        self.executor = Executor(
            maker, provider, policy, clock, events=self.events, **executor_options
        )

    async def lookup_order(self, args: LookupOrderArgs) -> dict[str, Any]:
        stmt = (
            select(Order, Payment, Product)
            .join(Payment, Payment.order_id == Order.id)
            .outerjoin(Product, Product.id == Order.product_id)
            .where(Order.ref == args.order_id, Order.customer_id == self.session.customer_id)
        )
        async with self.maker() as s:
            row = (await s.execute(stmt)).first()
        if row is None:
            return {"error": f"No order {args.order_id} found for this customer."}
        return order_view(row[0], row[1], row[2], self.clock)

    async def issue_refund(self, args: IssueRefundArgs) -> dict[str, Any]:
        outcome = await self.executor.request_refund(
            self.session, args.order_id, args.amount_paise, args.reason
        )
        return tool_result(outcome)

    async def cancel_order(self, args: CancelOrderArgs) -> dict[str, Any]:
        outcome = await self.executor.request_cancel(self.session, args.order_id)
        return tool_result(outcome)

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
