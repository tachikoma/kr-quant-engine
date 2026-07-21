"""Point-in-time ETF 유니버스 스냅샷 정규화·검증 유틸리티."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd


RAW_COLUMN_MAP = {
    "ISU_SRT_CD": "ticker",
    "ISU_CD": "isin",
    "SECUGRP_ID": "security_group",
    "ISU_ABBRV": "name",
    "IDX_IND_NM": "index_name",
    "TDD_CLSPRC": "close",
    "TDD_OPNPRC": "open",
    "TDD_HGPRC": "high",
    "TDD_LWPRC": "low",
    "ACC_TRDVOL": "volume",
    "ACC_TRDVAL": "trading_value",
    "NAV": "nav",
    "MKTCAP": "market_cap",
    "INVSTASST_NETASST_TOTAMT": "net_asset_value",
    "LIST_SHRS": "listed_shares",
    "OBJ_STKPRC_IDX": "underlying_index_value",
}

NUMERIC_COLUMNS = (
    "close",
    "open",
    "high",
    "low",
    "volume",
    "trading_value",
    "nav",
    "market_cap",
    "net_asset_value",
    "listed_shares",
    "underlying_index_value",
)


def build_snapshot_dates(
    trading_dates: pd.Series,
    *,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    warmup_days: int,
    step_days: int,
    include_terminal: bool = True,
) -> pd.DataFrame:
    """Backtest 리밸런싱 스케줄과 종료일 스냅샷 날짜를 생성한다."""
    if warmup_days < 0:
        raise ValueError("warmup_days는 0 이상이어야 합니다.")
    if step_days <= 0:
        raise ValueError("step_days는 양수여야 합니다.")

    dates = pd.Series(pd.to_datetime(trading_dates, errors="coerce")).dropna()
    dates = dates[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))]
    dates = dates.drop_duplicates().sort_values().reset_index(drop=True)
    if len(dates) <= warmup_days + 1:
        raise ValueError("스냅샷 생성에 필요한 거래일이 부족합니다.")

    rows = []
    for index in range(warmup_days, len(dates) - 1, step_days):
        rows.append(
            {
                "snapshot_date": pd.Timestamp(dates.iloc[index]),
                "is_rebalance_snapshot": True,
                "trading_date_index": int(index),
            }
        )

    terminal = pd.Timestamp(dates.iloc[-1])
    if include_terminal and all(row["snapshot_date"] != terminal for row in rows):
        rows.append(
            {
                "snapshot_date": terminal,
                "is_rebalance_snapshot": False,
                "trading_date_index": int(len(dates) - 1),
            }
        )
    return pd.DataFrame(rows).sort_values("snapshot_date").reset_index(drop=True)


def _parse_numeric(series: pd.Series) -> pd.Series:
    cleaned = series.astype(str).str.replace(",", "", regex=False).str.strip()
    cleaned = cleaned.replace({"": None, "-": None, "nan": None, "None": None})
    return pd.to_numeric(cleaned, errors="coerce")


def normalize_krx_etf_snapshot(raw: pd.DataFrame, snapshot_date: str) -> pd.DataFrame:
    """KRX ETF 전종목 시세 응답을 일관된 스키마로 변환한다."""
    if raw is None or raw.empty:
        raise ValueError(f"빈 ETF 스냅샷: {snapshot_date}")
    missing = {"ISU_SRT_CD", "ISU_ABBRV"} - set(raw.columns)
    if missing:
        raise ValueError(f"KRX ETF 스냅샷 필수 컬럼 누락: {sorted(missing)}")

    available_map = {key: value for key, value in RAW_COLUMN_MAP.items() if key in raw.columns}
    snapshot = raw[list(available_map)].rename(columns=available_map).copy()
    snapshot.insert(0, "snapshot_date", pd.Timestamp(snapshot_date))
    snapshot["ticker"] = snapshot["ticker"].astype(str).str.strip()
    for column in ("isin", "security_group", "name", "index_name"):
        if column not in snapshot:
            snapshot[column] = None
        snapshot[column] = snapshot[column].astype("string").str.strip()
    for column in NUMERIC_COLUMNS:
        if column not in snapshot:
            snapshot[column] = None
        snapshot[column] = _parse_numeric(snapshot[column])

    snapshot = snapshot[snapshot["ticker"] != ""].copy()
    duplicate_count = int(snapshot.duplicated(["snapshot_date", "ticker"]).sum())
    if duplicate_count:
        raise ValueError(f"{snapshot_date} ETF 티커 중복: {duplicate_count}건")
    return snapshot.sort_values("ticker").reset_index(drop=True)


def normalize_krx_etf_history(raw: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """KRX 개별 ETF 기간 시세 응답을 일별 표준 스키마로 변환한다."""
    if raw is None or raw.empty:
        return pd.DataFrame(
            columns=[
                "date",
                "ticker",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "trading_value",
                "nav",
                "base_index",
            ]
        )
    mapping = {
        "TRD_DD": "date",
        "TDD_OPNPRC": "open",
        "TDD_HGPRC": "high",
        "TDD_LWPRC": "low",
        "TDD_CLSPRC": "close",
        "ACC_TRDVOL": "volume",
        "ACC_TRDVAL": "trading_value",
        "LST_NAV": "nav",
        "OBJ_STKPRC_IDX": "base_index",
    }
    missing = {"TRD_DD", "TDD_OPNPRC", "TDD_CLSPRC"} - set(raw.columns)
    if missing:
        raise ValueError(f"KRX ETF 기간 시세 필수 컬럼 누락: {sorted(missing)}")
    available = {key: value for key, value in mapping.items() if key in raw.columns}
    history = raw[list(available)].rename(columns=available).copy()
    history["date"] = pd.to_datetime(history["date"], errors="coerce")
    history.insert(1, "ticker", str(ticker))
    for column in (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "trading_value",
        "nav",
        "base_index",
    ):
        if column not in history:
            history[column] = None
        history[column] = _parse_numeric(history[column])
    history = history.dropna(subset=["date"]).drop_duplicates("date", keep="last")
    return history.sort_values("date").reset_index(drop=True)


def membership_sha256(snapshot: pd.DataFrame) -> str:
    """Snapshot date와 티커 membership의 정규화 SHA-256을 반환한다."""
    pairs = snapshot[["snapshot_date", "ticker"]].copy()
    pairs["snapshot_date"] = pd.to_datetime(pairs["snapshot_date"]).dt.strftime("%Y-%m-%d")
    records = pairs.sort_values(["snapshot_date", "ticker"]).to_dict(orient="records")
    payload = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def validate_snapshot_panel(
    panel: pd.DataFrame,
    expected_dates: pd.Series | None = None,
) -> dict:
    """Consolidated snapshot panel 불변식을 검증한다."""
    required = {"snapshot_date", "ticker", "name"}
    missing = required - set(panel.columns)
    if missing:
        raise ValueError(f"PIT panel 필수 컬럼 누락: {sorted(missing)}")
    if panel.empty:
        raise ValueError("PIT panel이 비어 있습니다.")

    normalized = panel.copy()
    normalized["snapshot_date"] = pd.to_datetime(normalized["snapshot_date"])
    duplicate_count = int(normalized.duplicated(["snapshot_date", "ticker"]).sum())
    if duplicate_count:
        raise ValueError(f"PIT panel 날짜·티커 중복: {duplicate_count}건")

    counts = normalized.groupby("snapshot_date")["ticker"].nunique().sort_index()
    if (counts <= 0).any():
        raise ValueError("종목 수가 0인 PIT snapshot이 있습니다.")
    missing_dates: list[str] = []
    if expected_dates is not None:
        expected = set(pd.to_datetime(expected_dates))
        observed = set(counts.index)
        missing_dates = sorted(timestamp.strftime("%Y-%m-%d") for timestamp in expected - observed)
        if missing_dates:
            raise ValueError(f"PIT snapshot 누락 날짜: {missing_dates[:10]}")

    first_date = counts.index.min()
    last_date = counts.index.max()
    first_tickers = set(normalized.loc[normalized["snapshot_date"] == first_date, "ticker"])
    last_tickers = set(normalized.loc[normalized["snapshot_date"] == last_date, "ticker"])
    all_tickers = set(normalized["ticker"])
    return {
        "snapshot_count": int(len(counts)),
        "row_count": int(len(normalized)),
        "unique_ticker_count": int(len(all_tickers)),
        "first_snapshot_date": str(first_date.date()),
        "last_snapshot_date": str(last_date.date()),
        "first_snapshot_ticker_count": int(len(first_tickers)),
        "last_snapshot_ticker_count": int(len(last_tickers)),
        "observed_then_absent_count": int(len(all_tickers - last_tickers)),
        "entered_after_first_count": int(len(all_tickers - first_tickers)),
        "min_snapshot_ticker_count": int(counts.min()),
        "max_snapshot_ticker_count": int(counts.max()),
        "missing_dates": missing_dates,
        "membership_sha256": membership_sha256(normalized),
    }


def build_membership_events(panel: pd.DataFrame) -> pd.DataFrame:
    """인접한 snapshot 사이의 진입·이탈 membership 이벤트를 생성한다."""
    normalized = panel[["snapshot_date", "ticker", "name"]].copy()
    normalized["snapshot_date"] = pd.to_datetime(normalized["snapshot_date"])
    snapshots = {
        date: group.set_index("ticker")["name"].astype(str).to_dict()
        for date, group in normalized.groupby("snapshot_date")
    }
    rows = []
    previous: dict[str, str] = {}
    previous_date: pd.Timestamp | None = None
    for date in sorted(snapshots):
        current = snapshots[date]
        entered = sorted(set(current) - set(previous))
        exited = sorted(set(previous) - set(current)) if previous_date is not None else []
        for ticker in entered:
            rows.append(
                {
                    "event_snapshot_date": date,
                    "previous_snapshot_date": previous_date,
                    "ticker": ticker,
                    "name": current[ticker],
                    "event": "ENTER",
                    "is_initial_snapshot": previous_date is None,
                }
            )
        for ticker in exited:
            rows.append(
                {
                    "event_snapshot_date": date,
                    "previous_snapshot_date": previous_date,
                    "ticker": ticker,
                    "name": previous[ticker],
                    "event": "EXIT",
                    "is_initial_snapshot": False,
                }
            )
        previous = current
        previous_date = date
    return pd.DataFrame(rows)


def load_cached_snapshot_files(paths: list[Path]) -> pd.DataFrame:
    """Cached snapshot parquet 파일을 하나의 panel로 병합한다."""
    frames = [pd.read_parquet(path) for path in paths]
    if not frames:
        return pd.DataFrame()
    panel = pd.concat(frames, ignore_index=True)
    panel["snapshot_date"] = pd.to_datetime(panel["snapshot_date"])
    return panel.sort_values(["snapshot_date", "ticker"]).reset_index(drop=True)
