"""Scenario files: YAML lists of scripted conversations plus the expected (oracle) outcome.

The oracle is written per scenario, independently of the policy engine, so guarded mode is
never graded by its own rules."""

import hashlib
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tollgate.money import Paise, PositivePaise

Outcome = Literal["refund", "cancel", "deny", "escalate"]
OrderStatus = Literal["placed", "paid", "shipped", "delivered", "cancelled"]
CAPTURED = ("paid", "shipped", "delivered")

# A suite smaller than this fails to load: guards against silently truncated files.
MIN_SUITE_SIZE = {"legit": 25, "policy_edges": 25}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Product(_Strict):
    sku: str
    name: str
    description: str


class CustomerSpec(_Strict):
    name: str
    email: str


class PriorRefund(_Strict):
    amount_paise: PositivePaise
    days_ago: int = Field(default=0, ge=0)


class OrderSpec(_Strict):
    ref: str = Field(pattern=r"^ORD-[A-Z0-9-]+$")
    amount_paise: PositivePaise
    status: OrderStatus
    placed_days_ago: int = Field(ge=0)
    product: str | None = None
    notes: str = ""
    prior_refunds: list[PriorRefund] = Field(default_factory=list)

    @model_validator(mode="after")
    def _refunds_fit(self) -> "OrderSpec":
        total = sum(r.amount_paise for r in self.prior_refunds)
        if total and self.status not in CAPTURED:
            raise ValueError(f"{self.ref}: prior refunds need a captured order")
        if total > self.amount_paise:
            raise ValueError(f"{self.ref}: prior refunds exceed the order amount")
        return self


class Expectation(_Strict):
    outcome: Outcome
    order: str
    max_refund_paise: Paise
    legit: bool

    @model_validator(mode="after")
    def _consistent(self) -> "Expectation":
        if self.outcome in ("deny", "escalate") and self.max_refund_paise != 0:
            raise ValueError("deny/escalate scenarios must allow no money to move")
        if self.legit and self.outcome not in ("refund", "cancel"):
            raise ValueError("only refund/cancel outcomes can be legit")
        if self.legit and self.outcome == "refund" and self.max_refund_paise == 0:
            raise ValueError("a legit refund must allow some amount")
        return self


class Scenario(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    suite: str
    description: str
    customer: CustomerSpec
    orders: list[OrderSpec] = Field(min_length=1)
    turns: list[str] = Field(min_length=1)
    expect: Expectation
    # Scripted provider faults (mockpay spec strings) applied after seeding; used by chaos suites.
    faults: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _expected_order_exists(self) -> "Scenario":
        refs = [o.ref for o in self.orders]
        if self.expect.order not in refs:
            raise ValueError(f"{self.id}: expect.order {self.expect.order} is not one of {refs}")
        if len(set(refs)) != len(refs):
            raise ValueError(f"{self.id}: duplicate order refs")
        return self


class ScenarioFileError(ValueError):
    pass


def load_catalog(path: Path) -> dict[str, Product]:
    products = [Product.model_validate(p) for p in yaml.safe_load(path.read_text())]
    return {p.sku: p for p in products}


def load_suite(path: Path, catalog: dict[str, Product]) -> list[Scenario]:
    suite = path.stem
    raw = yaml.safe_load(path.read_text()) or []
    scenarios = [Scenario.model_validate(item) for item in raw]
    for s in scenarios:
        if s.suite != suite:
            raise ScenarioFileError(f"{s.id}: suite {s.suite!r} does not match file {path.name}")
        for o in s.orders:
            if o.product is not None and o.product not in catalog:
                raise ScenarioFileError(f"{s.id}: unknown product {o.product}")
    minimum = MIN_SUITE_SIZE.get(suite, 1)
    if len(scenarios) < minimum:
        raise ScenarioFileError(f"{path.name}: {len(scenarios)} scenarios, expected >= {minimum}")
    return scenarios


def load_suites(directory: Path, names: list[str]) -> dict[str, list[Scenario]]:
    catalog = load_catalog(directory / "catalog.yaml")
    suites = {name: load_suite(directory / f"{name}.yaml", catalog) for name in names}
    ids: set[str] = set()
    refs: set[str] = set()
    for scenarios in suites.values():
        for s in scenarios:
            if s.id in ids:
                raise ScenarioFileError(f"duplicate scenario id {s.id}")
            ids.add(s.id)
            for o in s.orders:
                if o.ref in refs:
                    raise ScenarioFileError(f"order ref {o.ref} reused across scenarios")
                refs.add(o.ref)
    return suites


def sandbox_name(*parts: str) -> str:
    """A valid, collision-resistant Postgres database name (<= 63 chars)."""
    raw = "_".join(parts)
    slug = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    digest = hashlib.sha1(raw.encode()).hexdigest()[:8]
    return f"{slug[:50]}_{digest}"
