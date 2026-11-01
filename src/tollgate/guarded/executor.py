"""The executor: in guarded mode, the only code that can move money.

The agent's refund and cancel tools never call the payment provider. They hand the executor a
request; the executor turns it into a *refund intent*, runs the policy, and only then talks to
the provider. The order of operations is the safety argument:

1. **Fingerprint the request.** The intent id is a hash of (conversation, kind, order, amount).
   If the model repeats a request it already made, it gets the first answer back, not a second
   refund.
2. **Decide under a lock.** The customer row is locked (`SELECT … FOR UPDATE`), so two requests
   for the same customer are decided one after the other and the daily cap can't be raced.
3. **Reserve before sending.** In the same transaction that marks the intent `executing`, the
   ledger reserves the refund as `pending`. Postgres refuses a reservation above what was
   captured. The provider is only ever called for money the ledger has already agreed to.
4. **Send with a key.** Every provider call carries `Idempotency-Key` = intent id and
   `receipt` = intent id. Timeouts, 5xx and 409 are retried with the *same* key, so a retry
   after a lost response replays the original refund instead of creating a second one.
5. **Reconcile when unsure.** If retries run out, look the refund up by receipt. Found:
   succeeded. Not found: failed, and the reservation is released. Lookup impossible: the intent
   becomes `unknown`, is never retried blindly, and goes to a human.

Every decision is written to the append-only audit log.
"""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.agent.session import AuthenticatedSession
from tollgate.clock import Clock
from tollgate.db.models import (
    AuditLog,
    Customer,
    Escalation,
    Order,
    Payment,
    Refund,
    RefundIntent,
)
from tollgate.guarded.policy import Decision, Kind, RefundFacts, evaluate
from tollgate.ledger import service
from tollgate.policy_config import PolicyConfig
from tollgate.provider_client import ProviderClient, ProviderError, ProviderTimeout

RETRYABLE_STATUSES = {409, 429}


class ApprovalError(Exception):
    """The intent can't be approved or rejected in its current state."""


@dataclass(frozen=True)
class Outcome:
    """What happened to one request. `status` is the intent's status after the call, or
    `cancelled` for a cancellation that moved no money."""

    status: str
    order_ref: str
    amount_paise: int
    kind: Kind = "refund"
    rule: str | None = None
    message: str = ""
    max_allowed_paise: int | None = None
    replayed: bool = False
    ticket: str | None = None
    intent_id: str | None = None


def intent_id_for(conversation_id: str, kind: str, order_ref: str, amount_paise: int) -> str:
    raw = f"{conversation_id}|{kind}|{order_ref}|{amount_paise}"
    return "int_" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def audit(
    s: AsyncSession,
    actor: str,
    action: str,
    *,
    intent_id: str | None = None,
    conversation_id: str | None = None,
    **details: Any,
) -> None:
    s.add(
        AuditLog(
            actor=actor,
            action=action,
            intent_id=intent_id,
            conversation_id=conversation_id,
            details=details,
        )
    )


@dataclass
class Executor:
    maker: async_sessionmaker[AsyncSession]
    provider: ProviderClient
    policy: PolicyConfig
    clock: Clock
    max_attempts: int = 3
    retry_delays_s: tuple[float, ...] = (0.05, 0.2)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    events: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ facts and helpers

    async def _facts(
        self, s: AsyncSession, order: Order, payment: Payment, customer_id: int
    ) -> RefundFacts:
        start = datetime.combine(self.clock.today(), time.min)
        today_total = await s.scalar(
            select(func.coalesce(func.sum(Refund.amount_paise), 0))
            .join(Payment, Payment.id == Refund.payment_id)
            .join(Order, Order.id == Payment.order_id)
            .where(
                Order.customer_id == customer_id,
                Refund.status.in_(("pending", "succeeded")),
                Refund.created_at >= start,
                Refund.created_at < start + timedelta(days=1),
            )
        )
        return RefundFacts(
            owned=order.customer_id == customer_id,
            order_ref=order.ref,
            order_status=order.status,
            payment_status=payment.status,
            captured_paise=payment.captured_paise,
            refunded_paise=payment.refunded_paise,
            days_since_order=(self.clock.today() - order.placed_at.date()).days,
            refunded_to_customer_today_paise=int(today_total or 0),
        )

    async def _lock_order(
        self, s: AsyncSession, customer_id: int, order_ref: str
    ) -> tuple[Order, Payment] | None:
        """Lock the customer, then the order and its payment. Same order every time: no
        deadlocks between concurrent requests."""
        await s.execute(select(Customer.id).where(Customer.id == customer_id).with_for_update())
        order = await s.scalar(select(Order).where(Order.ref == order_ref).with_for_update())
        if order is None:
            return None
        payment = await s.scalar(
            select(Payment).where(Payment.order_id == order.id).with_for_update()
        )
        if payment is None:
            return None
        return order, payment

    async def _escalate(
        self, s: AsyncSession, customer_id: int, conversation_id: str, order_ref: str, summary: str
    ) -> str:
        s.add(
            Escalation(
                customer_id=customer_id,
                conversation_id=conversation_id,
                order_ref=order_ref,
                summary=summary,
                created_at=self.clock.now(),
            )
        )
        await s.flush()
        n = await s.scalar(
            select(func.count())
            .select_from(Escalation)
            .where(Escalation.conversation_id == conversation_id)
        )
        return f"ESC-{n}"

    def _event(self, type_: str, **data: Any) -> None:
        self.events.append({"type": type_, **data})

    @staticmethod
    def _replay(intent: RefundIntent, order_ref: str) -> Outcome:
        return Outcome(
            status=intent.status,
            order_ref=order_ref,
            amount_paise=intent.amount_paise,
            kind=intent.kind,  # type: ignore[arg-type]
            rule=intent.policy_rule,
            message=intent.decision_note,
            replayed=True,
            intent_id=intent.id,
        )

    # ------------------------------------------------------------------ requests from the agent

    async def request_refund(
        self, session: AuthenticatedSession, order_ref: str, amount_paise: int, reason: str
    ) -> Outcome:
        return await self._request(session, "refund", order_ref, amount_paise, reason)

    async def request_cancel(self, session: AuthenticatedSession, order_ref: str) -> Outcome:
        """Cancel an unshipped order. Money paid goes back through the same refund path."""
        async with self.maker() as s, s.begin():
            locked = await self._lock_order(s, session.customer_id, order_ref)
            if locked is None or locked[0].customer_id != session.customer_id:
                return await self._deny_unknown_order(s, session, order_ref, "cancel")
            order, payment = locked
            if order.status == "cancelled":
                return Outcome(
                    "denied",
                    order_ref,
                    0,
                    "cancel",
                    "already_cancelled",
                    f"Order {order_ref} is already cancelled.",
                )
            remaining = payment.captured_paise - payment.refunded_paise
            if payment.status != "captured" or remaining <= 0:
                facts = await self._facts(s, order, payment, session.customer_id)
                if order.status in ("shipped", "delivered"):
                    decision = evaluate(self.policy, facts, 1, kind="cancel")
                    audit(
                        s,
                        "policy",
                        "denied",
                        conversation_id=session.conversation_id,
                        order=order_ref,
                        rule=decision.rule,
                    )
                    self._event("policy_denied", order=order_ref, rule=decision.rule)
                    return Outcome(
                        "denied", order_ref, 0, "cancel", decision.rule, decision.message
                    )
                await service.cancel_order(s, order.id)
                audit(
                    s,
                    "executor",
                    "cancelled_without_refund",
                    conversation_id=session.conversation_id,
                    order=order_ref,
                )
                self._event("cancelled", order=order_ref, refunded_paise=0)
                return Outcome("cancelled", order_ref, 0, "cancel")
        return await self._request(session, "cancel", order_ref, remaining, "order cancelled")

    async def _deny_unknown_order(
        self, s: AsyncSession, session: AuthenticatedSession, order_ref: str, kind: Kind
    ) -> Outcome:
        message = f"No order {order_ref} found for this customer."
        audit(
            s,
            "policy",
            "denied",
            conversation_id=session.conversation_id,
            order=order_ref,
            rule="ownership",
        )
        self._event("policy_denied", order=order_ref, rule="ownership")
        return Outcome("denied", order_ref, 0, kind, "ownership", message)

    async def _request(
        self,
        session: AuthenticatedSession,
        kind: Kind,
        order_ref: str,
        amount_paise: int,
        reason: str,
    ) -> Outcome:
        iid = intent_id_for(session.conversation_id, kind, order_ref, amount_paise)
        try:
            outcome = await self._decide(session, kind, order_ref, amount_paise, reason, iid)
        except IntegrityError as exc:
            # The database refused the reservation (e.g. another refund on this order is still
            # in flight, or the ledger's refunded <= captured check). Nothing was sent.
            rule = "in_flight" if "one_in_flight" in str(exc.orig) else "ledger_refused"
            async with self.maker() as s, s.begin():
                audit(
                    s,
                    "executor",
                    "reservation_refused",
                    intent_id=iid,
                    conversation_id=session.conversation_id,
                    rule=rule,
                    error=type(exc.orig).__name__,
                )
            self._event("reservation_refused", order=order_ref, rule=rule)
            message = (
                "Another refund on this order is still being processed. Try again shortly."
                if rule == "in_flight"
                else "The ledger refused this refund."
            )
            return Outcome("denied", order_ref, amount_paise, kind, rule, message, intent_id=iid)
        if outcome.replayed or outcome.status != "executing":
            return outcome
        return await self._execute(iid)

    async def _decide(
        self,
        session: AuthenticatedSession,
        kind: Kind,
        order_ref: str,
        amount_paise: int,
        reason: str,
        iid: str,
    ) -> Outcome:
        conv = session.conversation_id
        async with self.maker() as s, s.begin():
            locked = await self._lock_order(s, session.customer_id, order_ref)
            if locked is None or locked[0].customer_id != session.customer_id:
                return await self._deny_unknown_order(s, session, order_ref, kind)
            order, payment = locked

            existing = await s.get(RefundIntent, iid)
            if existing is not None:
                audit(s, "executor", "duplicate_request", intent_id=iid, conversation_id=conv)
                self._event("duplicate_suppressed", order=order_ref, status=existing.status)
                return self._replay(existing, order_ref)

            facts = await self._facts(s, order, payment, session.customer_id)
            decision = evaluate(self.policy, facts, amount_paise, kind=kind)
            intent = RefundIntent(
                id=iid,
                conversation_id=conv,
                customer_id=session.customer_id,
                order_id=order.id,
                kind=kind,
                amount_paise=amount_paise,
                reason=reason[:500],
                status="proposed",
            )
            s.add(intent)
            await s.flush()
            audit(
                s,
                "agent",
                "requested",
                intent_id=iid,
                conversation_id=conv,
                kind=kind,
                order=order_ref,
                amount_paise=amount_paise,
            )
            return await self._apply_decision(s, intent, order, payment, decision, actor="policy")

    async def _apply_decision(
        self,
        s: AsyncSession,
        intent: RefundIntent,
        order: Order,
        payment: Payment,
        decision: Decision,
        *,
        actor: str,
    ) -> Outcome:
        intent.policy_rule = decision.rule
        intent.decision_note = decision.message
        intent.updated_at = _now()
        common = {"intent_id": intent.id, "conversation_id": intent.conversation_id}
        if decision.outcome == "deny":
            intent.status = "denied"
            audit(s, actor, "denied", rule=decision.rule, **common)
            self._event("policy_denied", order=order.ref, rule=decision.rule)
            return Outcome(
                "denied",
                order.ref,
                intent.amount_paise,
                intent.kind,  # type: ignore[arg-type]
                decision.rule,
                decision.message,
                decision.max_allowed_paise,
                intent_id=intent.id,
            )
        if decision.outcome == "needs_approval":
            intent.status = "awaiting_approval"
            ticket = await self._escalate(
                s,
                intent.customer_id,
                intent.conversation_id,
                order.ref,
                f"Approval needed: {intent.kind} of {intent.amount_paise} paise on {order.ref}",
            )
            audit(s, actor, "approval_required", rule=decision.rule, ticket=ticket, **common)
            self._event("approval_requested", order=order.ref, amount_paise=intent.amount_paise)
            return Outcome(
                "awaiting_approval",
                order.ref,
                intent.amount_paise,
                intent.kind,  # type: ignore[arg-type]
                decision.rule,
                decision.message,
                ticket=ticket,
                intent_id=intent.id,
            )

        if intent.kind == "cancel":
            await service.cancel_order(s, order.id)
        refund = await service.reserve_refund(
            s,
            payment.id,
            intent.amount_paise,
            intent.id,
            intent.reason,
            at=self.clock.now(),
        )
        intent.refund_id = refund.id
        intent.status = "executing"
        audit(s, "executor", "reserved", refund_id=refund.id, **common)
        return Outcome(
            "executing",
            order.ref,
            intent.amount_paise,
            intent.kind,  # type: ignore[arg-type]
            intent_id=intent.id,
        )

    # ------------------------------------------------------------------ talking to the provider

    async def _execute(self, iid: str) -> Outcome:
        async with self.maker() as s:
            intent = await s.get(RefundIntent, iid)
            assert intent is not None
            row = (
                await s.execute(
                    select(Order.ref, Payment.provider_payment_id)
                    .join(Payment, Payment.order_id == Order.id)
                    .where(Order.id == intent.order_id)
                )
            ).one()
        order_ref, provider_payment_id = row
        amount, reason = intent.amount_paise, intent.reason

        attempts = 0
        last_error = ""
        while attempts < self.max_attempts:
            attempts += 1
            try:
                response = await self.provider.create_refund(
                    provider_payment_id or f"pay_{order_ref}",
                    amount,
                    idempotency_key=iid,
                    receipt=iid,
                    notes={"reason": reason[:200]},
                )
            except ProviderTimeout:
                last_error = "timeout"
            except ProviderError as exc:
                if exc.status < 500 and exc.status not in RETRYABLE_STATUSES:
                    return await self._mark_failed(
                        iid, order_ref, attempts, f"provider refused ({exc.status})"
                    )
                last_error = f"http {exc.status}"
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__
            else:
                return await self._mark_succeeded(iid, order_ref, response, attempts)
            self._event("provider_retry", order=order_ref, attempt=attempts, error=last_error)
            if attempts < self.max_attempts:
                delays = self.retry_delays_s or (0.0,)
                await self.sleep(delays[min(attempts - 1, len(delays) - 1)])

        return await self._reconcile_one(iid, order_ref, attempts, last_error)

    async def _reconcile_one(
        self, iid: str, order_ref: str, attempts: int, last_error: str
    ) -> Outcome:
        try:
            found = await self.provider.list_refunds(receipt=iid)
        except (ProviderTimeout, ProviderError, httpx.HTTPError) as exc:
            return await self._mark_unknown(iid, order_ref, attempts, f"{last_error}; {exc}")
        if found:
            self._event("reconciled", order=order_ref, found=True)
            return await self._mark_succeeded(iid, order_ref, found[0], attempts, reconciled=True)
        self._event("reconciled", order=order_ref, found=False)
        return await self._mark_failed(iid, order_ref, attempts, f"not at provider ({last_error})")

    async def _mark_succeeded(
        self,
        iid: str,
        order_ref: str,
        provider_refund: dict[str, Any],
        attempts: int,
        *,
        reconciled: bool = False,
    ) -> Outcome:
        async with self.maker() as s, s.begin():
            intent = await s.get(RefundIntent, iid, with_for_update=True)
            assert intent is not None and intent.refund_id is not None
            if intent.status in ("executing", "unknown"):
                intent.status = "succeeded"
            intent.provider_refund_id = provider_refund["id"]
            intent.attempts = attempts
            intent.updated_at = _now()
            await self._apply_provider_status(s, intent.refund_id, provider_refund)
            audit(
                s,
                "reconciler" if reconciled else "executor",
                "succeeded",
                intent_id=iid,
                conversation_id=intent.conversation_id,
                provider_refund_id=provider_refund["id"],
                attempts=attempts,
            )
        self._event("refund_succeeded", order=order_ref, attempts=attempts, reconciled=reconciled)
        return Outcome(
            "succeeded",
            order_ref,
            intent.amount_paise,
            intent.kind,  # type: ignore[arg-type]
            intent_id=iid,
        )

    async def _apply_provider_status(
        self, s: AsyncSession, refund_id: int, provider_refund: dict[str, Any]
    ) -> None:
        refund = await s.get(Refund, refund_id, with_for_update=True)
        assert refund is not None
        if refund.provider_refund_id is None:
            refund.provider_refund_id = provider_refund["id"]
            await s.flush()
        if refund.status != "pending":
            return
        if provider_refund.get("status") == "processed":
            await service.settle_refund(s, refund_id)
        elif provider_refund.get("status") == "failed":
            await service.fail_refund(s, refund_id)

    async def _mark_failed(self, iid: str, order_ref: str, attempts: int, why: str) -> Outcome:
        async with self.maker() as s, s.begin():
            intent = await s.get(RefundIntent, iid, with_for_update=True)
            assert intent is not None and intent.refund_id is not None
            intent.status = "failed"
            intent.attempts = attempts
            intent.decision_note = why
            intent.updated_at = _now()
            await service.fail_refund(s, intent.refund_id)
            ticket = None
            if intent.kind == "cancel":
                ticket = await self._escalate(
                    s,
                    intent.customer_id,
                    intent.conversation_id,
                    order_ref,
                    f"Order {order_ref} cancelled but its refund failed: {why}",
                )
            audit(
                s,
                "executor",
                "failed",
                intent_id=iid,
                conversation_id=intent.conversation_id,
                why=why,
                attempts=attempts,
            )
        self._event("refund_failed", order=order_ref, why=why)
        return Outcome(
            "failed",
            order_ref,
            intent.amount_paise,
            intent.kind,  # type: ignore[arg-type]
            message=why,
            ticket=ticket,
            intent_id=iid,
        )

    async def _mark_unknown(self, iid: str, order_ref: str, attempts: int, why: str) -> Outcome:
        async with self.maker() as s, s.begin():
            intent = await s.get(RefundIntent, iid, with_for_update=True)
            assert intent is not None
            intent.status = "unknown"
            intent.attempts = attempts
            intent.decision_note = why
            intent.updated_at = _now()
            ticket = await self._escalate(
                s,
                intent.customer_id,
                intent.conversation_id,
                order_ref,
                f"Refund on {order_ref} may or may not have happened: {why}",
            )
            audit(
                s,
                "executor",
                "unknown",
                intent_id=iid,
                conversation_id=intent.conversation_id,
                why=why,
                ticket=ticket,
            )
        self._event("refund_unknown", order=order_ref, why=why)
        return Outcome(
            "unknown",
            order_ref,
            intent.amount_paise,
            intent.kind,  # type: ignore[arg-type]
            message=why,
            ticket=ticket,
            intent_id=iid,
        )

    # ------------------------------------------------------------------ humans and housekeeping

    async def pending_approvals(self) -> list[RefundIntent]:
        async with self.maker() as s:
            rows = await s.scalars(
                select(RefundIntent)
                .where(RefundIntent.status == "awaiting_approval")
                .order_by(RefundIntent.created_at)
            )
            return list(rows)

    async def approve(self, iid: str, approver: str) -> Outcome:
        """A named human approves an intent. The policy is checked again (minus the approval
        rule): limits may have been used up while the request waited."""
        async with self.maker() as s, s.begin():
            intent = await s.get(RefundIntent, iid, with_for_update=True)
            if intent is None or intent.status != "awaiting_approval":
                raise ApprovalError(f"{iid} is not awaiting approval")
            order_ref = await s.scalar(select(Order.ref).where(Order.id == intent.order_id))
            assert order_ref is not None
            locked = await self._lock_order(s, intent.customer_id, order_ref)
            assert locked is not None
            order, payment = locked
            facts = await self._facts(s, order, payment, intent.customer_id)
            decision = evaluate(
                self.policy,
                facts,
                intent.amount_paise,
                kind=intent.kind,  # type: ignore[arg-type]
                skip_approval=True,
            )
            intent.decided_by = approver[:64]
            if not decision.allowed:
                intent.status = "rejected"
                intent.policy_rule = decision.rule
                intent.decision_note = f"No longer within policy: {decision.message}"
                intent.updated_at = _now()
                audit(
                    s,
                    f"human:{approver}"[:64],
                    "approval_overridden_by_policy",
                    intent_id=iid,
                    conversation_id=intent.conversation_id,
                    rule=decision.rule,
                )
                return Outcome(
                    "rejected",
                    order_ref,
                    intent.amount_paise,
                    intent.kind,  # type: ignore[arg-type]
                    decision.rule,
                    intent.decision_note,
                    intent_id=iid,
                )
            audit(
                s,
                f"human:{approver}"[:64],
                "approved",
                intent_id=iid,
                conversation_id=intent.conversation_id,
            )
            await self._apply_decision(s, intent, order, payment, decision, actor="human")
        return await self._execute(iid)

    async def reject(self, iid: str, approver: str, note: str = "") -> Outcome:
        async with self.maker() as s, s.begin():
            intent = await s.get(RefundIntent, iid, with_for_update=True)
            if intent is None or intent.status != "awaiting_approval":
                raise ApprovalError(f"{iid} is not awaiting approval")
            order_ref = await s.scalar(select(Order.ref).where(Order.id == intent.order_id))
            intent.status = "rejected"
            intent.decided_by = approver[:64]
            intent.decision_note = note[:500] or "Rejected by a human."
            intent.updated_at = _now()
            audit(
                s,
                f"human:{approver}"[:64],
                "rejected",
                intent_id=iid,
                conversation_id=intent.conversation_id,
                note=note[:500],
            )
        return Outcome(
            "rejected",
            str(order_ref),
            intent.amount_paise,
            intent.kind,  # type: ignore[arg-type]
            message=intent.decision_note,
            intent_id=iid,
        )

    async def reconcile(self) -> list[dict[str, Any]]:
        """Bring the ledger in line with the provider:

        - `unknown` intents: look up by receipt; found -> succeeded, missing -> failed.
        - succeeded intents whose ledger refund is still pending: settle or fail it from the
          provider's current status (normally webhooks do this; this is the backstop).
        """
        changes: list[dict[str, Any]] = []
        async with self.maker() as s:
            rows = (
                await s.execute(
                    select(RefundIntent.id, RefundIntent.status, Order.ref)
                    .join(Order, Order.id == RefundIntent.order_id)
                    .outerjoin(Refund, Refund.id == RefundIntent.refund_id)
                    .where(
                        (RefundIntent.status == "unknown")
                        | ((RefundIntent.status == "succeeded") & (Refund.status == "pending"))
                    )
                    .order_by(RefundIntent.created_at)
                )
            ).all()
        for iid, status, order_ref in rows:
            try:
                found = await self.provider.list_refunds(receipt=iid)
            except (ProviderTimeout, ProviderError, httpx.HTTPError):
                continue
            if status == "unknown":
                if found:
                    await self._mark_succeeded(iid, order_ref, found[0], 0, reconciled=True)
                    changes.append({"intent": iid, "result": "succeeded"})
                else:
                    await self._mark_failed(iid, order_ref, 0, "not at provider (reconciled)")
                    changes.append({"intent": iid, "result": "failed"})
                continue
            if not found:
                continue
            async with self.maker() as s, s.begin():
                intent = await s.get(RefundIntent, iid)
                assert intent is not None and intent.refund_id is not None
                await self._apply_provider_status(s, intent.refund_id, found[0])
            changes.append({"intent": iid, "result": f"ledger {found[0].get('status')}"})
        if changes:
            self._event("reconciled_batch", changes=len(changes))
        return changes
