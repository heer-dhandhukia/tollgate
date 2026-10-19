"""Money is always integer paise (1 INR = 100 paise). Never floats.

`Paise` is a strict Pydantic type: floats, bools, numeric strings and negatives are rejected at
every boundary (API bodies, tool arguments, config), so a float can never reach the ledger.
"""

from typing import Annotated

from pydantic import Field, StrictInt, TypeAdapter

Paise = Annotated[StrictInt, Field(ge=0)]
PositivePaise = Annotated[StrictInt, Field(gt=0)]

_paise = TypeAdapter(Paise)
_positive = TypeAdapter(PositivePaise)


def paise(value: object) -> int:
    """Validate a non-negative paise amount. Raises pydantic.ValidationError otherwise."""
    return _paise.validate_python(value)


def positive_paise(value: object) -> int:
    """Validate a strictly positive paise amount."""
    return _positive.validate_python(value)


def rupees_to_paise(rupees: int) -> int:
    """Whole rupees to paise. Only accepts int; fractional rupees must be given as paise."""
    if isinstance(rupees, bool) or not isinstance(rupees, int):
        raise TypeError("rupees_to_paise accepts int only; pass paise for fractional amounts")
    return rupees * 100


def format_inr(amount_paise: int) -> str:
    """Render paise as ₹ with Indian digit grouping, e.g. 5000000 -> '₹50,000.00'."""
    if isinstance(amount_paise, bool) or not isinstance(amount_paise, int):
        raise TypeError("format_inr accepts int paise only")
    sign = "-" if amount_paise < 0 else ""
    rupees, p = divmod(abs(amount_paise), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join([*groups, tail])
    return f"{sign}₹{digits}.{p:02d}"
