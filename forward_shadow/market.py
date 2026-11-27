"""Market data acquisition and strict validation for the shadow track.

Missing or invalid data is never converted into ``risk_on=True`` or into an
empty-candidate "risk off" outcome.  It produces a validation failure and the
runner holds both signal and execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

from forward_shadow.store import atomic_write_json


@dataclass
class MarketDay:
    date: str
    bars: dict[str, dict[str, float | None]] = field(default_factory=dict)
    index_close: float | None = None
    index_signal: dict[str, Any] | None = None
    validation: dict[str, Any] = field(default_factory=dict)
    stale_tickers: list[str] = field(default_factory=list)
    source: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.validation.get("ok"))


class DataProvider(Protocol):
    def fetch_day(self, date: str, tickers: list[str]) -> tuple[
        dict[str, dict[str, float | None]], dict[str, float], list[str]
    ]:
        """Return (bars_by_ticker, index_row, errors)."""


class FixtureProvider:
    """Deterministic provider backed by in-memory DataFrames (tests / replay)."""

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
        bars: dict[str, dict[str, float | None]] = {}
        errors: list[str] = []
        target = pd.Timestamp(date)
        for ticker in tickers:
            frame = self.etf_frames.get(str(ticker))
            if frame is None or "date" not in frame.columns:
                errors.append(f"{ticker}: no fixture frame")
                continue
            rows = frame[pd.to_datetime(frame["date"]) == target]
            if rows.empty:
                errors.append(f"{ticker}: no row for {date}")
                continue
            row = rows.iloc[-1]
            bars[str(ticker)] = {
                key: (None if pd.isna(row.get(key)) else float(row.get(key)))
                for key in ("open", "high", "low", "close", "volume", "trading_value", "nav")
                if key in frame.columns
            }
        idx = self.index_frame
        if idx is None or idx.empty or "date" not in idx.columns:
            errors.append("KOSPI: no fixture index frame")
            return bars, {}, errors
        rows = idx[pd.to_datetime(idx["date"]) == target]
        if rows.empty:
            errors.append(f"KOSPI: no row for {date}")
            return bars, {}, errors
        row = rows.iloc[-1]
        index_row = {
            key: (None if pd.isna(row.get(key)) else float(row.get(key)))
            for key in ("close", "open", "high", "low")
            if key in idx.columns
        }
        return bars, index_row, errors


class PykrxProvider:
    """Live pykrx provider with a private cache; no broker credentials used."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _etf_frame(self, ticker: str, date: str) -> pd.DataFrame:
        from pykrx_utils import fetch_etf_ohlcv_with_nav

        cache_file = self.cache_dir / f"{ticker}.parquet"
        if cache_file.exists():
            frame = pd.read_parquet(cache_file)
            if "date" in frame.columns:
                frame["date"] = pd.to_datetime(frame["date"])
            if (frame["date"] == pd.Timestamp(date)).any():
                return frame
        start = (pd.Timestamp(date) - pd.Timedelta(days=10)).strftime("%Y%m%d")
        end = pd.Timestamp(date).strftime("%Y%m%d")
        fresh = fetch_etf_ohlcv_with_nav(start, end, ticker)
        if cache_file.exists():
            try:
                old = pd.read_parquet(cache_file)
                old["date"] = pd.to_datetime(old["date"])
                frame = (
                    pd.concat([old, fresh], ignore_index=True)
                    .drop_duplicates(subset=["date"], keep="last")
                    .sort_values("date")
                    .reset_index(drop=True)
                )
            except Exception:  # noqa: BLE001 - cache merge is best-effort
                frame = fresh
        else:
            frame = fresh
        frame.to_parquet(cache_file, index=False)
        return frame

    def _index_frame(self, date: str) -> pd.DataFrame:
        from pykrx import stock

        cache_file = self.cache_dir / "index.parquet"
        if cache_file.exists():
            frame = pd.read_parquet(cache_file)
            if "date" in frame.columns:
                frame["date"] = pd.to_datetime(frame["date"])
            if (frame["date"] == pd.Timestamp(date)).any():
                return frame
        start = (pd.Timestamp(date) - pd.Timedelta(days=10)).strftime("%Y%m%d")
        end = pd.Timestamp(date).strftime("%Y%m%d")
        try:
            # pykrx expects the numeric KRX index code, not the display name.
            from etf_shared import KOSPI_INDEX_CODE

            raw = stock.get_index_ohlcv_by_date(start, end, KOSPI_INDEX_CODE)
        except Exception as exc:
            raise RuntimeError(f"KOSPI index fetch failed for {date}: {exc}") from exc
        if raw is None or raw.empty or "종가" not in raw.columns:
            raise RuntimeError(f"KOSPI index fetch empty/invalid for {date}")
        fresh = raw.reset_index().rename(
            columns={"날짜": "date", "시가": "open", "고가": "high", "저가": "low", "종가": "close"}
        )
        fresh["date"] = pd.to_datetime(fresh["date"])
        if cache_file.exists():
            try:
                old = pd.read_parquet(cache_file)
                old["date"] = pd.to_datetime(old["date"])
                frame = (
                    pd.concat([old, fresh], ignore_index=True)
                    .drop_duplicates(subset=["date"], keep="last")
                    .sort_values("date")
                    .reset_index(drop=True)
                )
            except Exception:  # noqa: BLE001 - cache merge is best-effort
                frame = fresh
        else:
            frame = fresh
        frame.to_parquet(cache_file, index=False)
        return frame

    def fetch_day(self, date: str, tickers: list[str]) -> tuple[
        dict[str, dict[str, float | None]], dict[str, float], list[str]
    ]:
        bars: dict[str, dict[str, float | None]] = {}
        errors: list[str] = []
        target = pd.Timestamp(date)
        for ticker in tickers:
            try:
                frame = self._etf_frame(ticker, date)
            except Exception as exc:  # noqa: BLE001 - converted to validation error
                errors.append(f"{ticker}: fetch failed: {exc}")
                continue
            rows = frame[pd.to_datetime(frame["date"]) == target]
            if rows.empty:
                errors.append(f"{ticker}: no row for {date}")
                continue
            row = rows.iloc[-1]
            bars[str(ticker)] = {
                key: (None if pd.isna(row.get(key)) else float(row.get(key)))
                for key in ("open", "high", "low", "close", "volume", "trading_value", "nav")
                if key in frame.columns
            }
        try:
            idx = self._index_frame(date)
        except Exception as exc:  # noqa: BLE001 - converted to validation error
            errors.append(str(exc))
            return bars, {}, errors
        rows = idx[pd.to_datetime(idx["date"]) == target]
        if rows.empty:
            errors.append(f"KOSPI: no row for {date}")
            return bars, {}, errors
        row = rows.iloc[-1]
        index_row = {
            key: (None if pd.isna(row.get(key)) else float(row.get(key)))
            for key in ("close", "open", "high", "low")
            if key in idx.columns
        }
        return bars, index_row, errors


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


def validate_market_day(
    *,
    date: str,
    tickers: list[str],
    bars: dict[str, dict[str, float | None]],
    index_row: dict[str, float],
    index_signal: dict[str, Any] | None,
    fetch_errors: list[str],
) -> dict[str, Any]:
    errors = list(fetch_errors)
    missing = [t for t in tickers if t not in bars]
    invalid: list[str] = []
    for ticker, bar in bars.items():
        values = [bar.get(key) for key in ("open", "high", "low", "close")]
        if any(value is None or not (float(value) > 0) for value in values):
            invalid.append(ticker)
        volume = bar.get("volume")
        if volume is None or float(volume) < 0:
            invalid.append(ticker)
    if missing:
        errors.append(f"missing ETF rows: {', '.join(sorted(set(missing)))}")
    if invalid:
        errors.append(f"invalid ETF OHLCV: {', '.join(sorted(set(invalid)))}")
    index_close = index_row.get("close")
    if index_close is None or not (float(index_close) > 0):
        errors.append("KOSPI index close missing/invalid")
    if index_signal is None:
        errors.append("KOSPI index history insufficient for MA120/slope20")
    return {
        "ok": not errors,
        "errors": errors,
        "missing_tickers": sorted(set(missing)),
        "invalid_tickers": sorted(set(invalid)),
        "index_close": index_close,
    }


def save_snapshot(paths: Any, day: MarketDay) -> None:
    """Persist a validated market snapshot for audit (never overwritten)."""
    target = paths.snapshots / f"{day.date}.json"
    if target.exists():
        return
    atomic_write_json(
        target,
        {
            "date": day.date,
            "source": day.source,
            "bars": day.bars,
            "index_close": day.index_close,
            "index_signal": day.index_signal,
            "validation": day.validation,
            "stale_tickers": day.stale_tickers,
        },
    )
