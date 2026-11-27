"""Market data acquisition, strict validation and hash-pinned snapshots.

Missing or invalid data is never converted into ``risk_on=True`` or into an
empty-candidate "risk off" outcome.  It produces a validation failure and the
runner holds both signal and execution.

Integrity rules follow the P0-A price/index checks (sorted, unique dates, no
interpolation), except that the shadow rejects a violation instead of silently
repairing it.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import io
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

from forward_shadow.store import IntegrityError, atomic_write_json, read_json

BAR_KEYS = ("open", "high", "low", "close", "volume", "trading_value", "nav")
PRICE_KEYS = ("open", "high", "low", "close", "nav")
NON_NEGATIVE_KEYS = ("volume", "trading_value")
# KRX limits daily ETF moves to 30%; the margin covers ex-distribution base price
# adjustments.  A larger jump indicates a split or bad data, and no corporate
# action ledger exists to explain it, so it blocks.
MAX_DAILY_CHANGE = 0.35


@dataclass
class MarketDay:
    date: str
    bars: dict[str, dict[str, float | None]] = field(default_factory=dict)
    index_close: float | None = None
    index_signal: dict[str, Any] | None = None
    validation: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    retrieved_at: str = ""
    sha256: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.validation.get("ok"))


class DataProvider(Protocol):
    def fetch_day(self, date: str, tickers: list[str]) -> tuple[
        dict[str, dict[str, float | None]], dict[str, float], list[str]
    ]:
        """Return (bars_by_ticker, index_row, errors)."""

    def index_dates(self, start: str, end: str) -> set[str]:
        """ISO dates with a KOSPI session in ``[start, end]``; raises on failure."""


def _optional_float(value: Any) -> float | None:
    return None if value is None or pd.isna(value) else float(value)


def _extract_day(
    etf_frames: dict[str, pd.DataFrame],
    index_frame: pd.DataFrame | None,
    date: str,
    tickers: list[str],
    errors: list[str],
) -> tuple[dict[str, dict[str, float | None]], dict[str, float]]:
    """Pick exactly one row per instrument for ``date``; duplicates are errors."""
    target = pd.Timestamp(date)
    bars: dict[str, dict[str, float | None]] = {}
    for ticker in tickers:
        frame = etf_frames.get(str(ticker))
        if frame is None or "date" not in frame.columns:
            errors.append(f"{ticker}: no data frame")
            continue
        rows = frame[pd.to_datetime(frame["date"]) == target]
        if rows.empty:
            errors.append(f"{ticker}: no row for {date}")
            continue
        if len(rows) > 1:
            errors.append(f"{ticker}: duplicate rows for {date}")
            continue
        row = rows.iloc[0]
        bars[str(ticker)] = {key: _optional_float(row.get(key)) for key in BAR_KEYS}
    if index_frame is None or index_frame.empty or "date" not in index_frame.columns:
        errors.append("KOSPI: no index frame")
        return bars, {}
    rows = index_frame[pd.to_datetime(index_frame["date"]) == target]
    if rows.empty:
        errors.append(f"KOSPI: no row for {date}")
        return bars, {}
    if len(rows) > 1:
        errors.append(f"KOSPI: duplicate rows for {date}")
        return bars, {}
    row = rows.iloc[0]
    index_row = {
        key: _optional_float(row.get(key))
        for key in ("close", "open", "high", "low")
        if key in index_frame.columns
    }
    return bars, index_row


class FixtureProvider:
    """Deterministic in-memory provider.  Only TEST runtimes accept it."""

    def __init__(
        self,
        etf_frames: dict[str, pd.DataFrame],
        index_frame: pd.DataFrame,
        name: str = "fixture",
    ) -> None:
        self.etf_frames = {str(k): frame.copy() for k, frame in etf_frames.items()}
        self.index_frame = index_frame.copy()
        self.name = name

    def fetch_day(self, date: str, tickers: list[str]) -> tuple[
        dict[str, dict[str, float | None]], dict[str, float], list[str]
    ]:
        errors: list[str] = []
        bars, index_row = _extract_day(self.etf_frames, self.index_frame, date, tickers, errors)
        return bars, index_row, errors

    def index_dates(self, start: str, end: str) -> set[str]:
        dates = pd.to_datetime(self.index_frame["date"])
        selected = dates[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))]
        return {str(d.date()) for d in selected}


@contextlib.contextmanager
def _quiet() -> Any:
    """pykrx prints the KRX login ID on authentication; never let it reach logs."""
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        yield


def scrub_secrets(text: str) -> str:
    for key in ("KRX_ID", "KRX_PW"):
        value = os.environ.get(key)
        if value:
            text = text.replace(value, f"<{key}>")
    return text


class PykrxProvider:
    """Live pykrx provider.  Every call fetches fresh data; there is no read cache.

    A price cache would let an unconfirmed intraday bar or a hand-edited file
    stand in for the confirmed close, so the only persisted market data are the
    hash-pinned snapshots and warm-up bundle of the runtime.
    """

    name = "pykrx"

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = Path(work_dir)

    def _etf_frame(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        with _quiet():
            from pykrx_utils import fetch_etf_ohlcv_with_nav

            return fetch_etf_ohlcv_with_nav(start, end, ticker)

    def _index_frame(self, start: str, end: str) -> pd.DataFrame:
        with _quiet():
            from pykrx import stock

            from etf_shared import KOSPI_INDEX_CODE

            raw = stock.get_index_ohlcv_by_date(start, end, KOSPI_INDEX_CODE)
        if raw is None or raw.empty or "종가" not in raw.columns:
            raise RuntimeError("KOSPI index response empty or without a close column")
        frame = raw.reset_index().rename(
            columns={"날짜": "date", "시가": "open", "고가": "high", "저가": "low", "종가": "close"}
        )
        frame["date"] = pd.to_datetime(frame["date"])
        return frame

    def _fetch(
        self, tickers: list[str], start: str, end: str, errors: list[str]
    ) -> tuple[dict[str, pd.DataFrame], pd.DataFrame | None]:
        frames: dict[str, pd.DataFrame] = {}
        for ticker in tickers:
            try:
                frames[str(ticker)] = self._etf_frame(str(ticker), start, end)
            except Exception as exc:  # noqa: BLE001 - converted to validation error
                errors.append(scrub_secrets(f"{ticker}: fetch failed: {exc}"))
        try:
            index: pd.DataFrame | None = self._index_frame(start, end)
        except Exception as exc:  # noqa: BLE001 - converted to validation error
            errors.append(scrub_secrets(f"KOSPI index fetch failed: {exc}"))
            index = None
        return frames, index

    def fetch_day(self, date: str, tickers: list[str]) -> tuple[
        dict[str, dict[str, float | None]], dict[str, float], list[str]
    ]:
        errors: list[str] = []
        start = (pd.Timestamp(date) - pd.Timedelta(days=10)).strftime("%Y%m%d")
        end = pd.Timestamp(date).strftime("%Y%m%d")
        frames, index = self._fetch(tickers, start, end, errors)
        bars, index_row = _extract_day(frames, index, date, tickers, errors)
        return bars, index_row, errors

    def index_dates(self, start: str, end: str) -> set[str]:
        frame = self._index_frame(start.replace("-", ""), end.replace("-", ""))
        return {str(d.date()) for d in pd.to_datetime(frame["date"])}

    def fetch_history(
        self, tickers: list[str], start: str, end: str
    ) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
        """Warm-up history; any fetch failure aborts initialization."""
        errors: list[str] = []
        frames, index = self._fetch(
            tickers, start.replace("-", ""), end.replace("-", ""), errors
        )
        if errors or index is None:
            raise RuntimeError("warm-up fetch failed: " + "; ".join(errors))
        return frames, index

    def fetch_listing_dates(self, tickers: list[str]) -> dict[str, str]:
        cache = self.work_dir / "classification.parquet"
        cache.unlink(missing_ok=True)  # always fetch; never trust a file placed earlier
        with _quiet():
            from pykrx_utils import get_listing_dates

            return get_listing_dates(ticker_subset=set(tickers), cache_path=str(cache))


def compute_index_signal(
    index_frame: pd.DataFrame,
    date: str,
    ma_days: int = 120,
    slope_days: int = 20,
) -> dict[str, Any] | None:
    """Strict MA120/slope20 signal. Returns None when history is insufficient."""
    if index_frame is None or index_frame.empty or "date" not in index_frame.columns:
        return None
    frame = index_frame.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame[frame["date"] <= pd.Timestamp(date)].sort_values("date")
    if "close" not in frame.columns:
        return None
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["close"])
    if len(frame) < ma_days + slope_days:
        return None
    ma = frame["close"].rolling(ma_days).mean()
    slope = ma - ma.shift(slope_days)
    close = float(frame["close"].iloc[-1])
    ma_value = ma.iloc[-1]
    slope_value = slope.iloc[-1]
    if pd.isna(ma_value) or pd.isna(slope_value):
        return None
    return {
        "date": str(pd.Timestamp(date).date()),
        "close": close,
        "market_ma": float(ma_value),
        "market_ma_slope": float(slope_value),
        "risk_on": bool(close >= float(ma_value) and float(slope_value) >= 0),
    }


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def bar_problems(bar: dict[str, Any]) -> list[str]:
    """Mandatory fields, finiteness and OHLC range checks for one ETF bar."""
    problems = [
        f"{key} missing/non-positive"
        for key in PRICE_KEYS
        if not (_finite(bar.get(key)) and bar[key] > 0)
    ]
    problems += [
        f"{key} missing/negative"
        for key in NON_NEGATIVE_KEYS
        if not (_finite(bar.get(key)) and bar[key] >= 0)
    ]
    if not problems:
        low, high = bar["low"], bar["high"]
        if not (low <= bar["open"] <= high and low <= bar["close"] <= high):
            problems.append("open/close outside [low, high]")
    return problems


def validate_market_day(
    *,
    tickers: list[str],
    bars: dict[str, dict[str, float | None]],
    index_row: dict[str, float],
    index_signal: dict[str, Any] | None,
    fetch_errors: list[str],
    previous_closes: dict[str, float] | None = None,
    previous_index_close: float | None = None,
) -> dict[str, Any]:
    errors = list(fetch_errors)
    previous_closes = previous_closes or {}
    missing = sorted({t for t in tickers if t not in bars})
    invalid: list[str] = []
    for ticker, bar in sorted(bars.items()):
        problems = bar_problems(bar)
        previous = previous_closes.get(ticker)
        if not problems and previous:
            change = bar["close"] / previous - 1.0
            if abs(change) > MAX_DAILY_CHANGE:
                problems.append(f"close moved {change:+.1%} from the previous close")
        if problems:
            invalid.append(ticker)
            errors.append(f"{ticker}: " + ", ".join(problems))
    if missing:
        errors.append(f"missing ETF rows: {', '.join(missing)}")
    index_close = index_row.get("close")
    if not (_finite(index_close) and index_close > 0):
        errors.append("KOSPI index close missing/invalid")
        index_close = None
    elif previous_index_close and abs(index_close / previous_index_close - 1) > MAX_DAILY_CHANGE:
        errors.append("KOSPI index close moved implausibly from the previous close")
    if index_signal is None:
        errors.append("KOSPI index history insufficient for MA120/slope20")
    return {
        "ok": not errors,
        "errors": errors,
        "missing_tickers": missing,
        "invalid_tickers": invalid,
        "index_close": index_close,
    }


def content_sha256(payload: Any) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _snapshot_body(day: MarketDay) -> dict[str, Any]:
    return {
        "date": day.date,
        "source": day.source,
        "retrieved_at": day.retrieved_at,
        "bars": day.bars,
        "index_close": day.index_close,
        "index_signal": day.index_signal,
        "validation": day.validation,
    }


def snapshot_path(paths: Any, date: str) -> Path:
    return paths.snapshots / f"{date}.json"


def save_snapshot(paths: Any, day: MarketDay) -> str:
    """Pin a validated snapshot.  An existing file is never overwritten."""
    target = snapshot_path(paths, day.date)
    if target.exists():
        raise IntegrityError(f"snapshot already pinned: {target}")
    body = _snapshot_body(day)
    day.sha256 = content_sha256(body)
    atomic_write_json(target, {**body, "sha256": day.sha256})
    return day.sha256


def load_snapshot(paths: Any, date: str, expected_sha: str | None = None) -> MarketDay:
    """Load a pinned snapshot, verifying its content against its recorded hash."""
    target = snapshot_path(paths, date)
    try:
        payload = read_json(target)
        recorded = payload.pop("sha256")
        actual = content_sha256(payload)
    except (OSError, ValueError, KeyError) as exc:
        raise IntegrityError(f"snapshot unreadable: {target}: {exc}") from exc
    if actual != recorded or (expected_sha is not None and actual != expected_sha):
        raise IntegrityError(
            f"snapshot hash mismatch for {date}: "
            f"content={actual}, expected={expected_sha or recorded}"
        )
    if payload.get("date") != date:
        raise IntegrityError(f"snapshot date mismatch: {target}")
    return MarketDay(sha256=actual, **payload)


def now_utc_iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat()
