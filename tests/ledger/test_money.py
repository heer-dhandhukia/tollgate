import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from tollgate.money import format_inr, paise, positive_paise, rupees_to_paise


@pytest.mark.parametrize("bad", [1.0, 0.5, -1, True, False, "100", None, 10**2 + 0.0])
def test_paise_rejects_non_int_or_negative(bad: object) -> None:
    with pytest.raises(ValidationError):
        paise(bad)


def test_positive_paise_rejects_zero() -> None:
    with pytest.raises(ValidationError):
        positive_paise(0)


@given(st.integers(min_value=0, max_value=10**15))
def test_paise_accepts_any_non_negative_int(n: int) -> None:
    assert paise(n) == n


def test_rupees_to_paise_rejects_float() -> None:
    with pytest.raises(TypeError):
        rupees_to_paise(10.5)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (0, "₹0.00"),
        (5, "₹0.05"),
        (100_000, "₹1,000.00"),
        (5_000_000, "₹50,000.00"),
        (1_234_567_89, "₹12,34,567.89"),
        (-250, "-₹2.50"),
    ],
)
def test_format_inr_indian_grouping(amount: int, expected: str) -> None:
    assert format_inr(amount) == expected


@given(st.integers(min_value=-(10**15), max_value=10**15))
def test_format_inr_round_trips(n: int) -> None:
    text = format_inr(n).replace("₹", "").replace(",", "")
    rupees, p = text.lstrip("-").split(".")
    assert (-1 if text.startswith("-") else 1) * (int(rupees) * 100 + int(p)) == n
