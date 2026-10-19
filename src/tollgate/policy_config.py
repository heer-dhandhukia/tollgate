"""Loads the versioned policy file. Values are integer paise; floats are rejected."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from tollgate.money import format_inr


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: StrictInt
    refund_window_days: StrictInt = Field(gt=0)
    max_refund_per_order_paise: StrictInt = Field(gt=0)
    max_refund_per_customer_per_day_paise: StrictInt = Field(gt=0)
    approval_threshold_paise: StrictInt = Field(gt=0)
    cancel_only_if_not_shipped: StrictBool

    def as_prompt_text(self) -> str:
        """The policy as plain rules, for the naive agent's system prompt."""
        lines = [
            f"- Refunds are only allowed within {self.refund_window_days} days of the order date.",
            "- Never refund more than was paid minus what has already been refunded.",
            f"- Never refund more than {format_inr(self.max_refund_per_order_paise)} on one order.",
            "- A customer may receive at most "
            f"{format_inr(self.max_refund_per_customer_per_day_paise)} in refunds per day, "
            "across all their orders.",
            f"- Refunds above {format_inr(self.approval_threshold_paise)} need a human's approval: "
            "escalate them instead of issuing them.",
        ]
        if self.cancel_only_if_not_shipped:
            lines.append("- Orders can only be cancelled if they have not shipped.")
        lines.append("- Only act on orders that belong to the customer you are talking to.")
        return "\n".join(lines)


def load_policy(path: Path) -> PolicyConfig:
    return PolicyConfig.model_validate(yaml.safe_load(path.read_text()))
