"""Fault spec parsing and selection (no database)."""

import pytest

from mockpay.config import MockpaySettings
from mockpay.faults import Fault, FaultInjector, FaultSpecError, parse


def test_parse_combined_spec() -> None:
    fault = parse("latency=300, timeout_after_commit=2000, duplicate_webhook=3")
    assert fault == Fault(latency_ms=300, timeout_after_commit_ms=2000, duplicate_webhook=3)
    assert fault.webhook_mode == "duplicate:3"


def test_parse_defaults() -> None:
    fault = parse("error_before_commit,error_after_commit,timeout_after_commit", 1234)
    assert fault.error_before_commit == 503
    assert fault.error_after_commit == 502
    assert fault.timeout_after_commit_ms == 1234


def test_out_of_order_wins_webhook_mode() -> None:
    assert parse("duplicate_webhook=2,out_of_order_webhooks").webhook_mode == "reverse"


@pytest.mark.parametrize("spec", ["explode", "latency=abc", "duplicate_webhook=x"])
def test_bad_specs_rejected(spec: str) -> None:
    with pytest.raises(FaultSpecError):
        parse(spec)


def test_header_beats_script_beats_random() -> None:
    injector = FaultInjector(MockpaySettings(p_error_before_commit=1.0))
    injector.script(["error_after_commit", "none"])
    assert injector.next("latency=5") == Fault(latency_ms=5)
    assert injector.next(None) == Fault(error_after_commit=502)
    assert injector.next(None) == Fault()
    assert injector.next(None).error_before_commit == 503  # script exhausted -> random


def test_random_faults_are_reproducible_from_seed() -> None:
    settings = MockpaySettings(fault_seed=7, p_timeout_after_commit=0.3, p_duplicate_webhook=0.5)
    a, b = FaultInjector(settings), FaultInjector(settings)
    assert [a.next(None) for _ in range(50)] == [b.next(None) for _ in range(50)]
    assert any(f.timeout_after_commit_ms for f in [a.next(None) for _ in range(50)])
