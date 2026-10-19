"""Fault injection for mockpay.

A fault spec is a comma-separated list of tokens, e.g. "latency=300,timeout_after_commit=2000".
For each refund request the fault comes from, in priority order:
  1. the `X-Mock-Fault` request header,
  2. the scripted queue (POST /_admin/faults), consumed one entry per request,
  3. random draws from the configured probabilities (seeded, so runs are reproducible).

Tokens:
  latency=<ms>                  sleep before handling
  error_before_commit[=<code>]  return 5xx, nothing persisted (default 503)
  error_after_commit[=<code>]   persist the refund, then return 5xx (default 502)
  timeout_after_commit[=<ms>]   persist the refund, then hang so the client times out
  duplicate_webhook[=<n>]       deliver every webhook for this refund n times (default 2)
  out_of_order_webhooks         hold this refund's webhooks, deliver them newest-first
  refund_fails                  the refund is accepted but later settles as failed
  none                          explicitly no fault (useful in scripted queues)
"""

import random
from collections import deque
from dataclasses import dataclass

from mockpay.config import MockpaySettings


@dataclass(frozen=True)
class Fault:
    latency_ms: int = 0
    error_before_commit: int | None = None
    error_after_commit: int | None = None
    timeout_after_commit_ms: int | None = None
    duplicate_webhook: int = 1
    out_of_order_webhooks: bool = False
    refund_fails: bool = False

    @property
    def webhook_mode(self) -> str:
        if self.out_of_order_webhooks:
            return "reverse"
        if self.duplicate_webhook > 1:
            return f"duplicate:{self.duplicate_webhook}"
        return ""


class FaultSpecError(ValueError):
    pass


def parse(spec: str, default_hang_ms: int = 5000) -> Fault:
    fields: dict[str, object] = {}
    for raw in spec.split(","):
        token = raw.strip()
        if not token or token == "none":
            continue
        name, _, value = token.partition("=")
        try:
            if name == "latency":
                fields["latency_ms"] = int(value)
            elif name == "error_before_commit":
                fields["error_before_commit"] = int(value or 503)
            elif name == "error_after_commit":
                fields["error_after_commit"] = int(value or 502)
            elif name == "timeout_after_commit":
                fields["timeout_after_commit_ms"] = int(value or default_hang_ms)
            elif name == "duplicate_webhook":
                fields["duplicate_webhook"] = int(value or 2)
            elif name == "out_of_order_webhooks":
                fields["out_of_order_webhooks"] = True
            elif name == "refund_fails":
                fields["refund_fails"] = True
            else:
                raise FaultSpecError(f"unknown fault token: {token!r}")
        except ValueError as exc:
            if isinstance(exc, FaultSpecError):
                raise
            raise FaultSpecError(f"bad value in fault token: {token!r}") from exc
    return Fault(**fields)  # type: ignore[arg-type]


class FaultInjector:
    def __init__(self, settings: MockpaySettings) -> None:
        self.settings = settings
        self.rng = random.Random(settings.fault_seed)
        self.scripted: deque[Fault] = deque()

    def script(self, specs: list[str]) -> None:
        self.scripted.extend(parse(s, self.settings.timeout_hang_ms) for s in specs)

    def clear(self) -> None:
        self.scripted.clear()

    def _hit(self, p: float) -> bool:
        return p > 0 and self.rng.random() < p

    def _random(self) -> Fault:
        s = self.settings
        return Fault(
            latency_ms=s.latency_ms if self._hit(s.p_latency) else 0,
            error_before_commit=503 if self._hit(s.p_error_before_commit) else None,
            error_after_commit=502 if self._hit(s.p_error_after_commit) else None,
            timeout_after_commit_ms=(
                s.timeout_hang_ms if self._hit(s.p_timeout_after_commit) else None
            ),
            duplicate_webhook=(
                s.duplicate_webhook_copies if self._hit(s.p_duplicate_webhook) else 1
            ),
            out_of_order_webhooks=self._hit(s.p_out_of_order_webhooks),
            refund_fails=self._hit(s.p_refund_fails),
        )

    def next(self, header: str | None) -> Fault:
        if header is not None:
            return parse(header, self.settings.timeout_hang_ms)
        if self.scripted:
            return self.scripted.popleft()
        return self._random()
