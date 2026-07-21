#!/usr/bin/env python3
"""KRX 날짜별 ETF 전종목 시세로 point-in-time membership를 구축한다."""

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
    """Project .env를 pykrx import 전에 로드한다."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


load_dotenv_before_pykrx()

from pit_universe import (
    build_membership_events,
    build_snapshot_dates,
    load_cached_snapshot_files,
    membership_sha256,
    normalize_krx_etf_snapshot,
    validate_snapshot_panel,
)


DEFAULT_CACHE_DIR = ROOT / "data_cache" / "pit_universe"
DEFAULT_OUTPUT_DIR = ROOT / "outputs_universe_bias"
DEFAULT_INDEX_CACHE = ROOT / "data_cache" / "index.parquet"


class SnapshotApi(Protocol):
    def fetch(self, date: str) -> pd.DataFrame: ...


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2016-01-06")
    parser.add_argument("--end", default=None)
    parser.add_argument("--warmup-days", type=int, default=140)
    parser.add_argument("--step-days", type=int, default=20)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--index-cache", type=Path, default=DEFAULT_INDEX_CACHE)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--max-snapshots", type=int, default=None)
    return parser.parse_args()


def fetch_snapshot_with_retry(
    api: SnapshotApi,
    date: pd.Timestamp,
    *,
    max_retries: int,
    request_delay: float,
) -> pd.DataFrame:
    date_text = date.strftime("%Y%m%d")
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            raw = api.fetch(date_text)
            snapshot = normalize_krx_etf_snapshot(raw, date_text)
            if request_delay > 0:
                time.sleep(request_delay)
            return snapshot
        except Exception as exc:
            last_error = exc
            if attempt >= max_retries:
                break
            wait_seconds = min(2 ** (attempt - 1), 8)
            print(f"[PIT] {date_text} 실패: {exc}; {wait_seconds}초 후 재시도")
            time.sleep(wait_seconds)
    raise RuntimeError(f"{date_text} PIT snapshot 수집 실패") from last_error


def attach_current_classification(panel: pd.DataFrame) -> pd.DataFrame:
    """Current classification은 매칭 여부만 표시하고 과거 분류로 간주하지 않는다."""
    path = ROOT / "data_cache" / "etf_tax_classification.parquet"
    if not path.exists():
        panel["has_current_classification"] = False
        return panel
    current = pd.read_parquet(path)
    current["ticker"] = current["ISU_SRT_CD"].astype(str).str.strip()
    selected = current[
        [
            "ticker",
            "LIST_DD",
            "ETF_REPLICA_METHD_TP_CD",
            "IDX_CALC_INST_NM2",
            "IDX_MKT_CLSS_NM",
            "IDX_ASST_CLSS_NM",
            "TAX_TP_CD",
        ]
    ].drop_duplicates("ticker")
    selected = selected.rename(
        columns={
            column: f"current_{column.lower()}"
            for column in selected.columns
            if column != "ticker"
        }
    )
    merged = panel.merge(selected, on="ticker", how="left")
    merged["has_current_classification"] = merged["current_list_dd"].notna()
    return merged


def main() -> None:
    args = parse_args()
    if not args.index_cache.exists():
        raise FileNotFoundError(f"KOSPI 거래일 캐시가 없습니다: {args.index_cache}")
    index_df = pd.read_parquet(args.index_cache)
    if "date" not in index_df:
        raise ValueError(f"KOSPI 캐시에 date 컬럼이 없습니다: {args.index_cache}")
    end = args.end or str(pd.to_datetime(index_df["date"]).max().date())
    schedule = build_snapshot_dates(
        index_df["date"],
        start=args.start,
        end=end,
        warmup_days=args.warmup_days,
        step_days=args.step_days,
        include_terminal=True,
    )
    if args.max_snapshots is not None:
        if args.max_snapshots <= 0:
            raise ValueError("--max-snapshots는 양수여야 합니다.")
        schedule = schedule.head(args.max_snapshots).copy()

    args.cache_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    max_retries = int(os.environ.get("PIT_SNAPSHOT_MAX_RETRIES", "3"))
    request_delay = float(os.environ.get("PIT_SNAPSHOT_REQUEST_DELAY", "0.10"))
    api: SnapshotApi | None = None
    if not args.offline:
        from pykrx.website.krx.etx.core import 전종목시세_ETF

        api = 전종목시세_ETF()
    paths: list[Path] = []

    for number, row in enumerate(schedule.itertuples(index=False), start=1):
        date = pd.Timestamp(row.snapshot_date)
        path = args.cache_dir / f"{date.strftime('%Y%m%d')}.parquet"
        paths.append(path)
        if path.exists() and not args.refresh:
            snapshot = pd.read_parquet(path)
            print(
                f"[PIT] {number}/{len(schedule)} {date.date()} 캐시 재사용 "
                f"({len(snapshot)}종목)"
            )
            continue
        if args.offline:
            raise FileNotFoundError(f"오프라인 모드 snapshot 누락: {path}")
        if api is None:
            raise RuntimeError("KRX PIT snapshot API가 초기화되지 않았습니다.")
        snapshot = fetch_snapshot_with_retry(
            api,
            date,
            max_retries=max_retries,
            request_delay=request_delay,
        )
        snapshot["is_rebalance_snapshot"] = bool(row.is_rebalance_snapshot)
        snapshot["trading_date_index"] = int(row.trading_date_index)
        snapshot.to_parquet(path, index=False)
        print(f"[PIT] {number}/{len(schedule)} {date.date()} 수집 ({len(snapshot)}종목)")

    panel = load_cached_snapshot_files(paths)
    panel = attach_current_classification(panel)
    coverage = validate_snapshot_panel(panel, schedule["snapshot_date"])
    coverage["current_classification_row_ratio"] = float(
        panel["has_current_classification"].mean()
    )
    coverage["current_classification_ticker_count"] = int(
        panel.loc[panel["has_current_classification"], "ticker"].nunique()
    )
    coverage["unknown_historical_ticker_count"] = int(
        panel.loc[~panel["has_current_classification"], "ticker"].nunique()
    )

    events = build_membership_events(panel)
    snapshot_counts = (
        panel.groupby("snapshot_date")
        .agg(
            ticker_count=("ticker", "nunique"),
            current_classified_count=("has_current_classification", "sum"),
        )
        .reset_index()
    )
    snapshot_counts["unknown_classification_count"] = (
        snapshot_counts["ticker_count"] - snapshot_counts["current_classified_count"]
    )
    schedule_export = schedule.copy()
    schedule_export["snapshot_date"] = schedule_export["snapshot_date"].dt.strftime("%Y-%m-%d")

    panel_path = args.cache_dir / "pit_universe_snapshots.parquet"
    events_path = args.cache_dir / "pit_universe_membership_events.parquet"
    manifest_path = args.cache_dir / "pit_universe_manifest.json"
    panel.to_parquet(panel_path, index=False)
    events.to_parquet(events_path, index=False)
    snapshot_counts.to_csv(
        args.output_dir / "pit_universe_snapshot_counts.csv",
        index=False,
        encoding="utf-8-sig",
    )
    events.to_csv(
        args.output_dir / "pit_universe_membership_events.csv",
        index=False,
        encoding="utf-8-sig",
    )
    manifest = {
        "source": "KRX MDCSTAT04301 ETF 전종목 시세",
        "source_scope": "지정 거래일의 실제 ETF 거래 종목 membership",
        "start": args.start,
        "end": end,
        "warmup_days": args.warmup_days,
        "step_days": args.step_days,
        "schedule": schedule_export.to_dict(orient="records"),
        "coverage": coverage,
        "panel_membership_sha256": membership_sha256(panel),
        "limitations": [
            "날짜별 membership는 point-in-time이지만 과거 자산군·복제방법 분류는 아님",
            "current_* 분류 컬럼은 현재 캐시 매칭 결과일 뿐 과거 시점 분류로 사용하면 안 됨",
            "snapshot 사이의 ENTER/EXIT는 정확한 상장·상장폐지일이 아닌 관찰 구간을 의미함",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== PIT ETF membership coverage ===")
    print(json.dumps(coverage, ensure_ascii=False, indent=2))
    print(f"panel: {panel_path}")
    print(f"events: {events_path}")
    print(f"manifest: {manifest_path}")


if __name__ == "__main__":
    main()
