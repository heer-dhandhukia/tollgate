"""The policy engine on its own: pure functions, no database."""

from dataclasses import replace

import pytest

from tollgate.config import REPO_ROOT
from tollgate.guarded.policy import RefundFacts, evaluate, headroom
from tollgate.policy_config import load_policy

POLICY = load_policy(REPO_ROOT / "config" / "policy.v1.yaml")

OK = RefundFacts(
    owned=True,
    order_ref="ORD-1",
    order_status="delivered",
    payment_status="captured",
    captured_paise=300_000,
    refunded_paise=0,
    days_since_order=5,
    refunded_to_customer_today_paise=0,
)


def test_within_policy_is_allowed() -> None:
    d = evaluate(POLICY, OK, 30_000)
    assert d.allowed and d.rule == "within_policy"


@pytest.mark.parametrize(
    ("facts", "amount", "rule"),
    [
        (replace(OK, owned=False), 30_000, "ownership"),
        (replace(OK, payment_status="voided", captured_paise=0), 30_000, "not_captured"),
        (replace(OK, refunded_paise=300_000), 1, "exceeds_refundable"),
        (OK, 300_001, "exceeds_refundable"),
        (replace(OK, days_since_order=31), 30_000, "outside_window"),
        (replace(OK, captured_paise=1_500_000, refunded_paise=900_000), 200_000, "order_cap"),
        (replace(OK, refunded_to_customer_today_paise=1_400_000), 200_000, "daily_cap"),
    ],
)
def test_each_rule_denies(facts: RefundFacts, amount: int, rule: str) -> None:
    d = evaluate(POLICY, facts, amount)
    assert (d.outcome, d.rule) == ("deny", rule)


def test_window_boundary_is_inclusive() -> None:
    assert evaluate(POLICY, replace(OK, days_since_order=30), 30_000).allowed
    assert not evaluate(POLICY, replace(OK, days_since_order=31), 30_000).allowed


def test_approval_above_threshold_only() -> None:
    big = replace(OK, captured_paise=900_000)
    assert evaluate(POLICY, big, 500_000).allowed  # exactly ₹5,000 is fine
    d = evaluate(POLICY, big, 500_001)
    assert (d.outcome, d.rule) == ("needs_approval", "approval_required")
    assert evaluate(POLICY, big, 500_001, skip_approval=True).allowed


def test_ownership_is_checked_first_and_hides_other_customers_orders() -> None:
    d = evaluate(POLICY, replace(OK, owned=False, days_since_order=90), 999_999_999)
    assert d.rule == "ownership"
    assert "No order ORD-1 found for this customer" in d.message


def test_amount_denials_say_what_is_still_possible() -> None:
    remaining = replace(OK, refunded_paise=200_000)  # ₹3,000 paid, ₹2,000 already refunded
    d = evaluate(POLICY, remaining, 300_000)
    assert d.rule == "exceeds_refundable" and d.max_allowed_paise == 100_000

    today = replace(OK, captured_paise=459_900, refunded_to_customer_today_paise=1_200_000)
    d = evaluate(POLICY, today, 459_900)
    assert d.rule == "daily_cap" and d.max_allowed_paise == 300_000
    assert evaluate(POLICY, today, 300_000).allowed


def test_headroom_is_the_tightest_limit() -> None:
    assert headroom(POLICY, OK) == 300_000
    assert headroom(POLICY, replace(OK, refunded_to_customer_today_paise=1_450_000)) == 50_000
    big = replace(OK, captured_paise=2_000_000, refunded_paise=950_000)
    assert headroom(POLICY, big) == 50_000  # order cap ₹10,000 minus ₹9,500


def test_cancellation_rules() -> None:
    for status in ("shipped", "delivered"):
        d = evaluate(POLICY, replace(OK, order_status=status), 30_000, kind="cancel")
        assert d.rule == "cancel_shipped"
    paid = replace(OK, order_status="paid")
    assert evaluate(POLICY, paid, 300_000, kind="cancel").allowed
    # A cancellation refund is still a refund: the same limits and approval threshold apply.
    big = replace(paid, captured_paise=899_900)
    assert evaluate(POLICY, big, 899_900, kind="cancel").outcome == "needs_approval"
