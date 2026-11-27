"""Shared synthetic data and runtime helpers for the Forward Shadow tests.

Observation dates follow the verified KRX calendar; earlier warm-up dates are
plain business days.  Not a test module.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forward_shadow.market import FixtureProvider, PykrxProvider
from forward_shadow.runner import KST, initialize_runtime, process_date
from forward_shadow.store import RuntimePaths, read_json, read_jsonl
from forward_shadow.trading_calendar import TradingCalendar
from forward_shadow.warmup import WarmupInput
from strategy_freeze import load_frozen_strategy

UNIVERSE = [str(t) for t in load_frozen_strategy()["universe"]]
LISTING = {t: "20100104" for t in UNIVERSE}
OBSERVATION_START = dt.date(2026, 3, 3)
WARMUP = 150
CAPITAL = 100_000_000.0


def trading_dates(count: int) -> pd.DatetimeIndex:
    """``WARMUP`` dates before the observation start, then calendar trading days."""
    calendar = TradingCalendar()
    in_2026 = calendar.trading_days(dt.date(2026, 1, 1), OBSERVATION_START)[:-1]
    earlier = pd.bdate_range(end="2025-12-31", periods=WARMUP - len(in_2026))
    observed = [OBSERVATION_START]
    while len(observed) < count - WARMUP:
        observed.append(calendar.next_trading_day(observed[-1]))
    return earlier.append(pd.DatetimeIndex(pd.to_datetime(in_2026 + observed)))


def make_synthetic(
    n_days: int = 230,
    volume: float = 10_000_000.0,
    *,
    shape=None,
    index_shape=None,
):
    """16 ETF frames, a KOSPI frame and the dates.

    ``shape(idx, j)`` returns a close price; ``index_shape(j)`` an index level.
    """
    dates = trading_dates(n_days)
    etf: dict[str, pd.DataFrame] = {}
    for idx, ticker in enumerate(UNIVERSE):
        rows = []
        for j, day in enumerate(dates):
            close = (
                shape(idx, j)
                if shape
                else 10_000.0 + idx * 250.0 + j * 12.0 + 300.0 * math.sin(j / 9.0 + idx)
            )
            day_volume = volume(idx, j) if callable(volume) else volume
            rows.append(
                {
                    "date": day,
                    "ticker": ticker,
                    "open": close * 0.999,
                    "high": close * 1.012,
                    "low": close * 0.988,
                    "close": close,
                    "volume": float(day_volume),
                    "trading_value": 1e10,
                    "nav": close * 1.001,
                }
            )
        etf[ticker] = pd.DataFrame(rows)
    index = pd.DataFrame(
        {
            "date": dates,
            "close": [
                index_shape(j) if index_shape else 2_000.0 + j * 3.0 for j in range(n_days)
            ],
        }
    )
    return etf, index, dates


def warmup_input(etf, index, listing=None) -> WarmupInput:
    return WarmupInput(
        etf_frames={t: f.copy() for t, f in etf.items()},
        index_frame=index.copy(),
        listing_dates=dict(listing or LISTING),
        source="synthetic",
    )


class FakeLive(PykrxProvider):
    """A live-provider double that serves frames instead of calling pykrx."""

    def __init__(self, work_dir, etf, index) -> None:
        super().__init__(work_dir)
        self.etf, self.index = etf, index
        self.calls = 0

    def _etf_frame(self, ticker, start, end):
        self.calls += 1
        frame = self.etf[ticker]
        return frame[(frame.date >= pd.Timestamp(start)) & (frame.date <= pd.Timestamp(end))]

    def _index_frame(self, start, end):
        frame = self.index
        return frame[(frame.date >= pd.Timestamp(start)) & (frame.date <= pd.Timestamp(end))]

    def fetch_listing_dates(self, tickers):
        return dict(LISTING)


def kst(day, hhmm: str) -> dt.datetime:
    date = dt.date.fromisoformat(str(day)[:10])
    return dt.datetime.combine(date, dt.time.fromisoformat(hhmm), tzinfo=KST)


class ShadowCase(unittest.TestCase):
    """A fresh TEST runtime over the default synthetic market."""

    volume: float = 10_000_000.0

    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        tmp = tempfile.TemporaryDirectory(prefix="kqe-shadow-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.etf, self.index, self.dates = make_synthetic(volume=self.volume)
        self.root = self.new_runtime()
        self.paths = RuntimePaths(self.root)
        self.provider = FixtureProvider(self.etf, self.index, name="synthetic")

    def new_runtime(self, name: str = "runtime", **kwargs) -> Path:
        root = self.tmp / name
        kwargs.setdefault("warmup", warmup_input(self.etf, self.index))
        initialize_runtime(
            root,
            initial_capital=CAPITAL,
            observation_start_date=str(OBSERVATION_START),
            **kwargs,
        )
        return root

    def day(self, offset: int) -> str:
        return str(self.dates[WARMUP + offset].date())

    def run_day(self, offset: int, provider=None, **kwargs) -> dict:
        return process_date(self.day(offset), provider or self.provider, self.root, **kwargs)

    def run_days(self, count: int, start: int = 0, **kwargs) -> list[dict]:
        return [self.run_day(offset, **kwargs) for offset in range(start, start + count)]

    def state(self) -> dict:
        return read_json(self.paths.state)

    def events(self, kind: str | None = None) -> list[dict]:
        rows = read_jsonl(self.paths.ledger)
        return [r for r in rows if kind is None or r["type"] == kind]

    def broken_provider(self, offset: int, mutate) -> FixtureProvider:
        """Copy of the market where ``mutate(frames, row_mask_of_that_day)`` edits data."""
        frames = {t: f.copy() for t, f in self.etf.items()}
        first = UNIVERSE[0]
        mutate(frames, frames[first].date == pd.Timestamp(self.day(offset)))
        return FixtureProvider(frames, self.index, name="broken")
