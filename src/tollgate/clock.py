"""A fixed clock for evals.

Prompts and tool outputs mention dates ("placed 6 days ago"). If they used the wall clock, every
day's prompts would differ, the LLM cache would miss, and replays would not be deterministic.
Evals therefore run "on" a fixed date (`Settings.eval_today`); production would use SystemClock.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol


class Clock(Protocol):
    def today(self) -> date: ...

    def now(self) -> datetime: ...


@dataclass(frozen=True)
class FixedClock:
    day: date

    def today(self) -> date:
        return self.day

    def now(self) -> datetime:
        """Noon on the fixed day (naive UTC, matching the DB's timestamp columns)."""
        return datetime.combine(self.day, time(12, 0))

    def days_ago(self, n: int) -> datetime:
        return self.now() - timedelta(days=n)


class SystemClock:
    def today(self) -> date:
        return datetime.now(UTC).date()

    def now(self) -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)
