"""Cost from the date-stamped price table in config/models.yaml."""

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Price:
    input_per_mtok_usd: float
    output_per_mtok_usd: float
    as_of: str


@dataclass(frozen=True)
class PriceTable:
    prices: dict[str, Price]
    usd_to_inr: float
    usd_to_inr_as_of: str

    def cost_usd(self, model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
        price = self.prices.get(model)
        if price is None:
            return None
        return (
            prompt_tokens * price.input_per_mtok_usd + completion_tokens * price.output_per_mtok_usd
        ) / 1_000_000


def load_prices(path: Path) -> PriceTable:
    raw = yaml.safe_load(path.read_text())
    prices = {
        name: Price(e["input_per_mtok_usd"], e["output_per_mtok_usd"], str(e["as_of"]))
        for name, e in raw["models"].items()
    }
    fx = raw["usd_to_inr"]
    return PriceTable(prices, float(fx["rate"]), str(fx["as_of"]))
