"""KRX trading calendar with an explicit verified coverage window.

Outside the coverage window every query raises ``CalendarRangeError``: an
unknown year must block execution, never fall back to "weekday == open".

The 2026 list is a read-only copy of ``_KRX_HOLIDAYS`` in
``live_trading/etf_daily_runner.py`` (duplicated deliberately so the shadow
never imports broker code).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

COVERAGE_START = dt.date(2026, 1, 1)
COVERAGE_END = dt.date(2027, 12, 31)

CALENDAR_STATUS = {
    "2026": (
        "copy of the live runner list; matches cached KRX KOSPI dates "
        "2026-01-02..2026-09-28 exactly"
    ),
    "2027": (
        "PROVISIONAL: KASA 2027 월력요항 (2026-06-29) public/substitute holidays plus the "
        "KRX year-end closing rule; no KRX closure notice has been verified"
    ),
}


def _dates(year: int, *month_days: tuple[int, int]) -> set[dt.date]:
    return {dt.date(year, month, day) for month, day in month_days}


HOLIDAYS: frozenset[dt.date] = frozenset(
    _dates(
        2026,
        (1, 1), (2, 16), (2, 17), (2, 18), (3, 1), (3, 2), (5, 1), (5, 5), (5, 24),
        (5, 25), (6, 3), (6, 6), (7, 17), (8, 15), (8, 17), (9, 24), (9, 25),
        (9, 26), (10, 3), (10, 5), (10, 9), (12, 25), (12, 31),
    )
    | _dates(
        2027,
        (1, 1), (2, 8), (2, 9), (3, 1), (5, 3), (5, 5), (5, 13), (7, 19), (8, 16),
        (9, 14), (9, 15), (9, 16), (10, 4), (10, 11), (12, 27), (12, 31),
    )
)


class CalendarRangeError(RuntimeError):
    """A date outside the verified calendar coverage was requested."""


class TradingCalendar:
    """Frozen holiday list plus ledger-recorded ad-hoc closures."""

    def __init__(self, extra_holidays: Iterable[str] = ()) -> None:
        self.extra = frozenset(dt.date.fromisoformat(str(day)) for day in extra_holidays)

    @staticmethod
    def check(day: dt.date) -> None:
        if not COVERAGE_START <= day <= COVERAGE_END:
            raise CalendarRangeError(
                f"{day} is outside the verified KRX calendar coverage "
                f"{COVERAGE_START}..{COVERAGE_END}"
            )

    def is_trading_day(self, day: dt.date) -> bool:
        self.check(day)
        return day.weekday() < 5 and day not in HOLIDAYS and day not in self.extra

    def _step(self, day: dt.date, delta: int) -> dt.date:
        current = day + dt.timedelta(days=delta)
        while not self.is_trading_day(current):
            current += dt.timedelta(days=delta)
        return current

    def next_trading_day(self, day: dt.date) -> dt.date:
        return self._step(day, 1)

    def previous_trading_day(self, day: dt.date) -> dt.date:
        return self._step(day, -1)

    def trading_days(self, start: dt.date, end: dt.date) -> list[dt.date]:
        """Trading days in the closed interval ``[start, end]``."""
        self.check(start)
        self.check(end)
        span = (end - start).days
        days = (start + dt.timedelta(days=offset) for offset in range(span + 1))
        return [day for day in days if self.is_trading_day(day)]

    def ordinal(self, anchor: dt.date, day: dt.date) -> int:
        """Signed trading-day distance from ``anchor`` to ``day``."""
        if day >= anchor:
            return len([d for d in self.trading_days(anchor, day) if d != anchor])
        return -len([d for d in self.trading_days(day, anchor) if d != day])

    def is_decision_day(self, day: dt.date, anchor: dt.date, offset: int, interval: int) -> bool:
        """Phase is a pure function of the calendar, never of processing success."""
        return self.is_trading_day(day) and (self.ordinal(anchor, day) - offset) % interval == 0

    def next_decision_day(
        self, after: dt.date, anchor: dt.date, offset: int, interval: int
    ) -> dt.date | None:
        """First decision day strictly after ``after``; None past calendar coverage."""
        current = after
        try:
            while True:
                current = self.next_trading_day(current)
                if self.is_decision_day(current, anchor, offset, interval):
                    return current
        except CalendarRangeError:
            return None
