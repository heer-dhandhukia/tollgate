from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from tollgate.clock import FixedClock
from tollgate.config import REPO_ROOT
from tollgate.policy_config import PolicyConfig, load_policy

POLICY = REPO_ROOT / "config" / "policy.v1.yaml"


def test_policy_v1_loads_and_mentions_every_rule() -> None:
    policy = load_policy(POLICY)
    text = policy.as_prompt_text()
    assert f"{policy.refund_window_days} days" in text
    assert "₹10,000.00" in text and "₹15,000.00" in text and "₹5,000.00" in text
    assert "not shipped" in text


def test_policy_rejects_float_money(tmp_path: Path) -> None:
    raw = yaml.safe_load(POLICY.read_text())
    raw["approval_threshold_paise"] = 5000.0
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate(raw)


def test_model_prices_are_dated() -> None:
    models = yaml.safe_load((REPO_ROOT / "config" / "models.yaml").read_text())
    assert models["usd_to_inr"]["as_of"]
    for name, entry in models["models"].items():
        assert entry["as_of"], name
        assert entry["input_per_mtok_usd"] >= 0 and entry["output_per_mtok_usd"] >= 0


def test_fixed_clock_is_stable() -> None:
    from datetime import date

    clock = FixedClock(date(2026, 10, 1))
    assert clock.now() == clock.now()
    assert (clock.now() - clock.days_ago(30)).days == 30
