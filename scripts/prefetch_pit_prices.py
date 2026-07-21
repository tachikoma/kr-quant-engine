#!/usr/bin/env python3
"""PIT membership의 ISIN으로 상장 기간 ETF OHLCV·NAV를 캐시한다."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Protocol

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_dotenv_before_pykrx() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv_before_pykrx()

from pit_universe import normalize_krx_etf_history


PIT_PANEL = ROOT / "data_cache" / "pit_universe" / "pit_universe_snapshots.parquet"
PRICE_DIR = ROOT / "data_cache" / "pit_prices"


class HistoryApi(Protocol):
    def fetch(self, start: str, end: str, isin: str) -> pd.DataFrame: ...


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scope",
        choices=["unknown", "historical_absent", "all"],
        default="unknown",
    )
    parser.add_argument("--max-tickers", type=int, default=None)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--price-dir", type=Path, default=PRICE_DIR)
    return parser.parse_args()


def build_ticker_master(panel: pd.DataFrame) -> pd.DataFrame:
    work = panel.copy()
    work["snapshot_date"] = pd.to_datetime(work["snapshot_date"])
    final_date = work["snapshot_date"].max()
    final_tickers = set(work.loc[work["snapshot_date"] == final_date, "ticker"])
    master = (
        work.sort_values("snapshot_date")
        .groupby("ticker", as_index=False)
        .agg(
            isin=("isin", "last"),
            name=("name", "last"),
            first_observed=("snapshot_date", "min"),
            last_observed=("snapshot_date", "max"),
            has_current_classification=("has_current_classification", "max"),
        )
    )
    master["present_at_final"] = master["ticker"].isin(final_tickers)
    master["fetch_start"] = pd.Timestamp("2015-07-01")
    master["fetch_end"] = master["last_observed"] + pd.Timedelta(days=45)
    master.loc[master["present_at_final"], "fetch_end"] = final_date
    return master.sort_values(["first_observed", "ticker"]).reset_index(drop=True)


def fetch_with_retry(
    api: HistoryApi,
    row: object,
    *,
    retries: int,
) -> pd.DataFrame:
    last_error: Exception | None = None
    start = pd.Timestamp(row.fetch_start).strftime("%Y%m%d")
    end = pd.Timestamp(row.fetch_end).strftime("%Y%m%d")
    for attempt in range(1, retries + 1):
        try:
            raw = api.fetch(start, end, str(row.isin))
            return normalize_krx_etf_history(raw, str(row.ticker))
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(2 ** (attempt - 1), 8))
    raise RuntimeError(f"{row.ticker} {start}~{end} 수집 실패") from last_error


def main() -> None:
    args = parse_args()
    if not PIT_PANEL.exists():
        raise FileNotFoundError(f"PIT membership panel이 없습니다: {PIT_PANEL}")
    panel = pd.read_parquet(PIT_PANEL)
    master = build_ticker_master(panel)
    if args.scope == "unknown":
        selected = master[~master["has_current_classification"]].copy()
    elif args.scope == "historical_absent":
        selected = master[~master["present_at_final"]].copy()
    else:
        selected = master.copy()
    if args.max_tickers is not None:
        if args.max_tickers <= 0:
            raise ValueError("--max-tickers는 양수여야 합니다.")
        selected = selected.head(args.max_tickers).copy()

    args.price_dir.mkdir(parents=True, exist_ok=True)
    api: HistoryApi | None = None
    if not args.offline:
        from pykrx.website.krx.etx.core import 개별종목시세_ETF

        api = 개별종목시세_ETF()
    retries = int(os.environ.get("PIT_PRICE_MAX_RETRIES", "3"))
    status_rows = []
    for number, row in enumerate(selected.itertuples(index=False), start=1):
        path = args.price_dir / f"{row.ticker}.parquet"
        if path.exists() and not args.refresh:
            history = pd.read_parquet(path)
            source = "cache"
        else:
            if args.offline:
                status_rows.append({"ticker": row.ticker, "status": "MISSING_CACHE"})
                continue
            if api is None:
                raise RuntimeError("KRX ETF 기간 시세 API가 초기화되지 않았습니다.")
            history = fetch_with_retry(api, row, retries=retries)
            history.to_parquet(path, index=False)
            source = "network"
        first = None if history.empty else str(pd.to_datetime(history["date"]).min().date())
        last = None if history.empty else str(pd.to_datetime(history["date"]).max().date())
        status_rows.append(
            {
                "ticker": row.ticker,
                "name": row.name,
                "status": "EMPTY" if history.empty else "OK",
                "source": source,
                "row_count": int(len(history)),
                "first_date": first,
                "last_date": last,
            }
        )
        print(
            f"[PIT price] {number}/{len(selected)} {row.ticker} "
            f"{source} {len(history)}행"
        )

    status = pd.DataFrame(status_rows)
    status_path = args.price_dir / f"prefetch_status_{args.scope}.csv"
    status.to_csv(status_path, index=False, encoding="utf-8-sig")
    summary = {
        "scope": args.scope,
        "selected_ticker_count": int(len(selected)),
        "ok_count": int((status.get("status") == "OK").sum()),
        "empty_count": int((status.get("status") == "EMPTY").sum()),
        "missing_cache_count": int((status.get("status") == "MISSING_CACHE").sum()),
        "status_file": str(status_path),
    }
    (args.price_dir / f"prefetch_summary_{args.scope}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
