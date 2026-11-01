"""The refund policy, enforced in code.

Naive mode shows `config/policy.v1.yaml` to the model as prompt text and hopes it complies. Guarded
mode evaluates the same file here, deterministically, on every refund the agent asks for. A
customer can talk a model out of a rule; they cannot talk this function out of one.

Rules are checked in a fixed order and the first one that fails decides:

    1 ownership          the order belongs to the signed-in customer
    2 cancel_shipped     (cancellations only) the order has not shipped
    3 not_captured       something was actually paid
    4 exceeds_refundable amount <= paid minus already refunded (pending refunds count)
    5 outside_window     within refund_window_days of the order
    6 order_cap          total refunded on this order stays <= max_refund_per_order_paise
    7 daily_cap          refunded to this customer today stays <= the daily cap
    8 approval_required  amounts above approval_threshold_paise wait for a human

Denials that are about an amount carry `max_allowed_paise`, the most that could be refunded right
now, so the agent can offer the customer what policy does allow instead of a flat no.
"""

from dataclasses import dataclass
from typing import Literal

from tollgate.money import format_inr
from tollgate.policy_config import PolicyConfig

Outcome = Literal["allow", "deny", "needs_approval"]
Kind = Literal["refund", "cancel"]


@dataclass(frozen=True)
class RefundFacts:
    """Everything the rules need, read under the payment's row lock."""

    owned: bool
    order_ref: str
    order_status: str
    payment_status: str
    captured_paise: int
    refunded_paise: int  # pending + succeeded on this order's payment
    days_since_order: int
    refunded_to_customer_today_paise: int  # pending + succeeded, across the customer's orders


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    rule: str
    message: str
    max_allowed_paise: int | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome == "allow"


def _deny(rule: str, message: str, max_allowed: int | None = None) -> Decision:
    return Decision("deny", rule, message, max_allowed)


def headroom(policy: PolicyConfig, facts: RefundFacts) -> int:
    """The most that could be refunded right now, considering amounts only."""
    refundable = max(0, facts.captured_paise - facts.refunded_paise)
    order_left = max(0, policy.max_refund_per_order_paise - facts.refunded_paise)
    day_left = max(
        0, policy.max_refund_per_customer_per_day_paise - facts.refunded_to_customer_today_paise
    )
    return min(refundable, order_left, day_left)


def evaluate(
    policy: PolicyConfig,
    facts: RefundFacts,
    amount_paise: int,
    *,
    kind: Kind = "refund",
    skip_approval: bool = False,
) -> Decision:
    """Decide one refund. `skip_approval` is used when a human has already approved it."""
    if not facts.owned:
        return _deny("ownership", f"No order {facts.order_ref} found for this customer.")

    shipped = facts.order_status in ("shipped", "delivered")
    if kind == "cancel" and policy.cancel_only_if_not_shipped and shipped:
        return _deny(
            "cancel_shipped",
            f"Order {facts.order_ref} is already {facts.order_status}; orders can only be "
            "cancelled before they ship.",
        )

    if facts.payment_status != "captured":
        return _deny(
            "not_captured",
            f"Nothing was paid on order {facts.order_ref}, so there is nothing to refund.",
        )

    refundable = max(0, facts.captured_paise - facts.refunded_paise)
    if amount_paise > refundable:
        if refundable == 0:
            return _deny(
                "exceeds_refundable", f"Order {facts.order_ref} has already been fully refunded.", 0
            )
        return _deny(
            "exceeds_refundable",
            f"Only {format_inr(refundable)} of order {facts.order_ref} is still refundable.",
            min(refundable, headroom(policy, facts)),
        )

    if facts.days_since_order > policy.refund_window_days:
        return _deny(
            "outside_window",
            f"Order {facts.order_ref} was placed {facts.days_since_order} days ago; refunds are "
            f"only allowed within {policy.refund_window_days} days.",
        )

    if facts.refunded_paise + amount_paise > policy.max_refund_per_order_paise:
        return _deny(
            "order_cap",
            f"At most {format_inr(policy.max_refund_per_order_paise)} can be refunded on one "
            "order in total.",
            headroom(policy, facts),
        )

    if (
        facts.refunded_to_customer_today_paise + amount_paise
        > policy.max_refund_per_customer_per_day_paise
    ):
        return _deny(
            "daily_cap",
            "A customer can receive at most "
            f"{format_inr(policy.max_refund_per_customer_per_day_paise)} in refunds per day.",
            headroom(policy, facts),
        )

    if not skip_approval and amount_paise > policy.approval_threshold_paise:
        return Decision(
            "needs_approval",
            "approval_required",
            f"Refunds above {format_inr(policy.approval_threshold_paise)} need a human's approval.",
        )

    return Decision("allow", "within_policy", "Within policy.")
