"""Hash-pinned warm-up bundle: validated once at initialization, immutable after.

The bundle holds the ETF/KOSPI history up to the trading day before the first
observation plus the listing dates used by the frozen listing filter.  Daily
runs read only this pinned copy, so a different seed file cannot silently change
the strategy result.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import pandas as pd

from forward_shadow.market import (
    BAR_KEYS,
    MAX_DAILY_CHANGE,
    bar_problems,
    content_sha256,
)
from forward_shadow.store import IntegrityError, atomic_write_json, read_json
from forward_shadow.trading_calendar import COVERAGE_START, TradingCalendar


class WarmupError(RuntimeError):
    """Warm-up input is incomplete or invalid (initialization is refused)."""


@dataclass
class WarmupInput:
    etf_frames: dict[str, pd.DataFrame]
    index_frame: pd.DataFrame
    listing_dates: dict[str, str]
    source: str


def _normalized(frame: pd.DataFrame, end: pd.Timestamp, label: str) -> pd.DataFrame:
    if frame is None or "date" not in frame.columns or "close" not in frame.columns:
        raise WarmupError(f"{label}: date/close columns required")
    data = frame.copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    if data["date"].isna().any():
        raise WarmupError(f"{label}: unparseable dates")
    # Rows after the warm-up end belong to the observation period, not warm-up.
    data = data[data["date"] <= end].sort_values("date").reset_index(drop=True)
    if data.empty:
        raise WarmupError(f"{label}: no rows up to {end.date()}")
    if data["date"].duplicated().any():
        raise WarmupError(f"{label}: duplicate dates")
    return data


def build_bundle(
    warmup: WarmupInput,
    *,
    universe: list[str],
    observation_start: dt.date,
    calendar: TradingCalendar,
    min_rows: int,
) -> dict[str, Any]:
    """Validate ``warmup`` and return the canonical JSON bundle."""
    end_day = calendar.previous_trading_day(observation_start)
    end = pd.Timestamp(end_day)

    index = _normalized(warmup.index_frame, end, "KOSPI")
    index["close"] = pd.to_numeric(index["close"], errors="coerce")
    if not (index["close"].notna() & (index["close"] > 0)).all() or (
        index["close"] == float("inf")
    ).any():
        raise WarmupError("KOSPI: close must be finite and positive")
    if index["date"].iloc[-1] != end:
        raise WarmupError(
            f"KOSPI: warm-up ends {index['date'].iloc[-1].date()}, "
            f"must end on {end_day} (trading day before observation start)"
        )
    if len(index) < min_rows:
        raise WarmupError(f"KOSPI: {len(index)} rows, at least {min_rows} required")
    if (index["close"].pct_change().abs() > MAX_DAILY_CHANGE).any():
        raise WarmupError("KOSPI: implausible daily move")
    # Inside calendar coverage the index dates must be exactly the trading days.
    # Earlier dates cannot be checked against the calendar and serve as the
    # reference set for the ETF series.
    covered_start = max(index["date"].iloc[0].date(), COVERAGE_START)
    if end_day >= covered_start:
        expected = set(calendar.trading_days(covered_start, end_day))
        actual = {d.date() for d in index["date"] if d.date() >= covered_start}
        if expected != actual:
            raise WarmupError(
                "KOSPI: dates differ from the trading calendar; "
                f"missing={sorted(map(str, expected - actual))[:5]}, "
                f"unexpected={sorted(map(str, actual - expected))[:5]}"
            )
    gaps = index["date"].diff().dt.days
    if (gaps > 12).any():
        raise WarmupError("KOSPI: calendar gap longer than 12 days")
    index_dates = list(index["date"])

    listing: dict[str, str] = {}
    etf_rows: dict[str, list[dict[str, Any]]] = {}
    for ticker in universe:
        raw = str(warmup.listing_dates.get(ticker, ""))
        digits = "".join(ch for ch in raw if ch.isdigit())
        listed = pd.to_datetime(digits, format="%Y%m%d", errors="coerce")
        if len(digits) != 8 or pd.isna(listed):
            raise WarmupError(f"{ticker}: listing date missing/invalid ({raw!r})")
        listing[ticker] = digits

        data = _normalized(warmup.etf_frames.get(ticker), end, ticker)
        data = data[data["date"] >= index_dates[0]].reset_index(drop=True)
        required = [d for d in index_dates if d >= listed]
        missing = sorted(set(required) - set(data["date"]))
        unexpected = sorted(set(data["date"]) - set(required))
        if missing or unexpected:
            raise WarmupError(
                f"{ticker}: warm-up series mismatch; "
                f"missing={[str(d.date()) for d in missing[:5]]} ({len(missing)}), "
                f"unexpected={[str(d.date()) for d in unexpected[:5]]} ({len(unexpected)})"
            )
        rows = []
        for record in data.to_dict("records"):
            bar = {
                key: None if pd.isna(record.get(key)) else float(record[key]) for key in BAR_KEYS
            }
            problems = bar_problems(bar)
            if problems:
                raise WarmupError(
                    f"{ticker} {record['date'].date()}: " + ", ".join(problems)
                )
            rows.append({"date": str(record["date"].date()), **bar})
        closes = pd.Series([row["close"] for row in rows])
        if (closes.pct_change().abs() > MAX_DAILY_CHANGE).any():
            raise WarmupError(f"{ticker}: implausible daily move (split or bad data)")
        etf_rows[ticker] = rows

    return {
        "source": warmup.source,
        "start": str(index_dates[0].date()),
        "end": str(end_day),
        "listing_dates": listing,
        "index": [
            {"date": str(d.date()), "close": float(c)}
            for d, c in zip(index["date"], index["close"], strict=True)
        ],
        "etf": etf_rows,
    }


def bundle_summary(bundle: dict[str, Any], retrieved_at: str) -> dict[str, Any]:
    """Manifest entry that pins the bundle."""
    return {
        "sha256": content_sha256(bundle),
        "source": bundle["source"],
        "retrieved_at": retrieved_at,
        "start": bundle["start"],
        "end": bundle["end"],
        "index_rows": len(bundle["index"]),
        "etf_rows": {ticker: len(rows) for ticker, rows in bundle["etf"].items()},
    }


def save_bundle(paths: Any, bundle: dict[str, Any]) -> None:
    atomic_write_json(paths.warmup, bundle)


def load_bundle(paths: Any, expected_sha: str) -> dict[str, Any]:
    try:
        bundle = read_json(paths.warmup)
        actual = content_sha256(bundle)
    except (OSError, ValueError) as exc:
        raise IntegrityError(f"warm-up bundle unreadable: {exc}") from exc
    if actual != expected_sha:
        raise IntegrityError(
            f"warm-up bundle hash mismatch: expected={expected_sha}, actual={actual}"
        )
    return bundle
