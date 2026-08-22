"""Point-in-time ETF 유니버스 스냅샷 정규화·검증 유틸리티."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
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
        "snapshot_count": len(counts),
        "row_count": len(normalized),
        "unique_ticker_count": len(all_tickers),
        "first_snapshot_date": str(first_date.date()),
        "last_snapshot_date": str(last_date.date()),
        "first_snapshot_ticker_count": len(first_tickers),
        "last_snapshot_ticker_count": len(last_tickers),
        "observed_then_absent_count": len(all_tickers - last_tickers),
        "entered_after_first_count": len(all_tickers - first_tickers),
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


def build_membership_intervals(panel: pd.DataFrame) -> pd.DataFrame:
    """Legacy diagnostic interval view; never use this for PIT eligibility.

    This preserves a descriptive compatibility helper while the eligibility path
    uses :func:`latest_snapshot_as_of` exclusively.

    Returns
    -------
    pd.DataFrame
        ``ticker``, ``first_observed``, ``last_observed`` 컬럼.
    """
    work = panel[["snapshot_date", "ticker"]].copy()
    work["snapshot_date"] = pd.to_datetime(work["snapshot_date"])
    work["ticker"] = work["ticker"].astype(str)
    intervals = (
        work.groupby("ticker")["snapshot_date"]
        .agg(first_observed="min", last_observed="max")
        .reset_index()
    )
    return intervals.sort_values("first_observed")


def _normalize_membership_rows(panel: pd.DataFrame) -> pd.DataFrame:
    required = {"snapshot_date", "ticker"}
    if panel.empty:
        return pd.DataFrame(columns=["snapshot_date", "ticker"])
    missing = required - set(panel.columns)
    if missing:
        raise ValueError(f"PIT membership 필수 컬럼 누락: {sorted(missing)}")
    work = panel[["snapshot_date", "ticker"]].copy()
    work["snapshot_date"] = pd.to_datetime(work["snapshot_date"], errors="coerce")
    work["ticker"] = work["ticker"].astype(str).str.strip()
    if work["snapshot_date"].isna().any() or work["ticker"].eq("").any():
        raise ValueError("PIT membership에 유효하지 않은 snapshot_date 또는 ticker가 있습니다.")
    return work.sort_values(["snapshot_date", "ticker"]).reset_index(drop=True)


def latest_snapshot_as_of(panel: pd.DataFrame, as_of_date: str | pd.Timestamp) -> pd.DataFrame:
    """``as_of_date`` 이전 또는 당일의 최신 snapshot 행만 반환한다.

    첫 snapshot 이전에는 빈 DataFrame을 반환해 PIT eligibility를 fail-closed 한다.
    이후 snapshot의 membership은 현재 날짜로 소급하지 않으며, 미래 snapshot 행은
    membership 선택에 사용하지 않는다.
    """
    normalized = _normalize_membership_rows(panel)
    as_of = pd.Timestamp(as_of_date)
    if normalized.empty:
        return normalized
    eligible_dates = normalized.loc[normalized["snapshot_date"] <= as_of, "snapshot_date"]
    if eligible_dates.empty:
        return normalized.iloc[0:0].copy()
    latest_date = eligible_dates.max()
    return normalized[normalized["snapshot_date"] == latest_date].reset_index(drop=True)


def membership_as_of(
    panel: pd.DataFrame, as_of_date: str | pd.Timestamp
) -> set[str]:
    """Return the ticker membership from the latest snapshot available as of a date."""
    return set(latest_snapshot_as_of(panel, as_of_date)["ticker"].astype(str))


def _preflight_diagnostic(
    category: str,
    *,
    date: pd.Timestamp | None = None,
    ticker: str | None = None,
    detail: str,
) -> dict[str, str | None]:
    return {
        "category": category,
        "date": None if date is None else date.strftime("%Y-%m-%d"),
        "ticker": ticker,
        "detail": detail,
    }


def _raise_preflight_failure(diagnostics: list[dict[str, str | None]]) -> None:
    lines = ["PIT strict preflight failed:"]
    for item in diagnostics:
        lines.append(
            "  [{category}] date={date} ticker={ticker}: {detail}".format(**item)
        )
    raise ValueError("\n".join(lines))


def validate_pit_preflight(
    panel: pd.DataFrame,
    price: pd.DataFrame,
    trading_dates: Sequence[str | pd.Timestamp],
    ticker_groups: Mapping[str, str] | None,
    *,
    decision_dates: Sequence[str | pd.Timestamp] | None = None,
    max_snapshot_age_trading_dates: int = 25,
    allowed_groups: set[str] | None = None,
    historical_classification_tax_verified: bool = False,
) -> dict[str, object]:
    """Run strict PIT-only membership, price, group, and history preflight.

    ``historical_classification_tax_verified`` is intentionally false by default:
    current or inferred-restored labels are not evidence of historical validity.
    It is an explicit seam for future effective-dated source validation and for
    deterministic synthetic coverage tests; the production PIT caller leaves it
    false until that source exists.
    """
    if max_snapshot_age_trading_dates < 0:
        raise ValueError("max_snapshot_age_trading_dates는 0 이상이어야 합니다.")
    allowed = allowed_groups if allowed_groups is not None else {
        "domestic_equity",
        "foreign_investment",
        "commodity",
    }
    calendar = pd.Index(pd.to_datetime(list(trading_dates), errors="coerce"))
    calendar = pd.Index(calendar.dropna().drop_duplicates().sort_values())
    dates = decision_dates if decision_dates is not None else price.get("date", [])
    decision_index = pd.Index(pd.to_datetime(list(dates), errors="coerce"))
    decision_index = pd.Index(decision_index.dropna().drop_duplicates().sort_values())
    if calendar.empty:
        raise ValueError("PIT strict preflight에 거래일 calendar가 없습니다.")

    price_required = {"date", "ticker", "close"}
    missing_price_columns = price_required - set(price.columns)
    if missing_price_columns:
        raise ValueError(f"PIT price coverage 필수 컬럼 누락: {sorted(missing_price_columns)}")
    price_work = price[["date", "ticker", "close"]].copy()
    price_work["date"] = pd.to_datetime(price_work["date"], errors="coerce")
    price_work["ticker"] = price_work["ticker"].astype(str).str.strip()
    close = pd.to_numeric(price_work["close"], errors="coerce")
    usable = close.notna() & close.gt(0) & ~close.isin([float("inf"), float("-inf")])
    price_work = price_work.loc[usable]
    price_by_date = {
        date: set(group["ticker"])
        for date, group in price_work.groupby("date", sort=True)
    }

    diagnostics: list[dict[str, str | None]] = []
    if not historical_classification_tax_verified:
        diagnostics.append(
            _preflight_diagnostic(
                "historical classification/tax coverage unverified",
                detail=(
                    "effective-dated historical classification and tax source is required; "
                    "current/restored labels cannot approve PIT"
                ),
            )
        )

    normalized_panel = _normalize_membership_rows(panel)
    snapshot_dates = pd.Index(normalized_panel["snapshot_date"].drop_duplicates().sort_values())
    groups = {str(ticker): str(group) for ticker, group in (ticker_groups or {}).items()}
    snapshot_report: dict[str, str] = {}
    for date in decision_index:
        if date not in calendar:
            diagnostics.append(
                _preflight_diagnostic(
                    "decision date missing from trading calendar",
                    date=date,
                    detail="supply the complete sorted trading-date calendar",
                )
            )
            continue
        prior = snapshot_dates[snapshot_dates <= date]
        if prior.empty:
            diagnostics.append(
                _preflight_diagnostic(
                    "missing snapshot",
                    date=date,
                    detail=(
                        "no snapshot_date <= decision date; eligibility is empty before "
                        "the first snapshot"
                    ),
                )
            )
            continue
        latest_date = pd.Timestamp(prior.max())
        snapshot_report[date.strftime("%Y-%m-%d")] = latest_date.strftime("%Y-%m-%d")
        if latest_date not in calendar:
            diagnostics.append(
                _preflight_diagnostic(
                    "snapshot date missing from trading calendar",
                    date=date,
                    detail=(
                        f"latest snapshot={latest_date.date()} is not an exact member "
                        "of the supplied trading-date calendar"
                    ),
                )
            )
            continue
        decision_position = int(calendar.get_loc(date))
        snapshot_position = int(calendar.get_loc(latest_date))
        age = decision_position - snapshot_position
        if age > max_snapshot_age_trading_dates:
            diagnostics.append(
                _preflight_diagnostic(
                    "snapshot age exceeds limit",
                    date=date,
                    detail=(
                        f"latest snapshot={latest_date.date()}, age={age} trading dates, "
                        f"limit={max_snapshot_age_trading_dates}"
                    ),
                )
            )

        expected = set(
            normalized_panel.loc[normalized_panel["snapshot_date"] == latest_date, "ticker"]
        )
        usable_tickers = price_by_date.get(date, set())
        for ticker in sorted(expected - usable_tickers):
            diagnostics.append(
                _preflight_diagnostic(
                    "missing usable price coverage",
                    date=date,
                    ticker=ticker,
                    detail="expected as-of eligible ticker has no positive finite close",
                )
            )
        for ticker in sorted(expected):
            group = groups.get(ticker)
            if not group:
                diagnostics.append(
                    _preflight_diagnostic(
                        "missing group coverage",
                        date=date,
                        ticker=ticker,
                        detail="ticker is absent from the PIT group mapping",
                    )
                )
            elif group not in allowed:
                diagnostics.append(
                    _preflight_diagnostic(
                        "unknown group coverage",
                        date=date,
                        ticker=ticker,
                        detail=f"group={group!r}, allowed={sorted(allowed)}",
                    )
                )

    if diagnostics:
        _raise_preflight_failure(diagnostics)
    return {
        "status": "passed",
        "decision_date_count": len(decision_index),
        "max_snapshot_age_trading_dates": int(max_snapshot_age_trading_dates),
        "as_of_snapshot_dates": snapshot_report,
        "historical_classification_tax_verified": bool(
            historical_classification_tax_verified
        ),
    }


def build_pit_ticker_groups(
    current_classification_path: Path | None = None,
    restored_classification_path: Path | None = None,
) -> dict[str, str]:
    """PIT 유니버스 전체(1,370종목)의 티커→그룹 매핑을 구축한다.

    - 현재 상장 종목: `data_cache/etf_tax_classification.parquet`의 `IDX_MKT_CLSS_NM`
    - 상장폐지 종목: `pit_classification_restored.parquet`의 `market` (복원값)

    매핑: 국내 → domestic_equity, 해외/국내&해외 → foreign_investment, 원자재 → commodity.
    """
    groups: dict[str, str] = {}

    if current_classification_path is not None and current_classification_path.exists():
        current = pd.read_parquet(current_classification_path)
        for _, row in current.iterrows():
            ticker = str(row.get("ISU_SRT_CD", "")).strip()
            market = str(row.get("IDX_MKT_CLSS_NM", "")).strip()
            asset_cls = str(row.get("IDX_ASST_CLSS_NM", "")).strip()
            if not ticker:
                continue
            groups[ticker] = market_to_group(market, asset_cls)

    if restored_classification_path is not None and restored_classification_path.exists():
        restored = pd.read_parquet(restored_classification_path)
        for _, row in restored.iterrows():
            ticker = str(row.get("ticker", "")).strip()
            market = str(row.get("market", "")).strip()
            asset_cls = str(row.get("asset_class", "")).strip()
            if not ticker:
                continue
            if ticker in groups:
                continue
            groups[ticker] = market_to_group(market, asset_cls)

    return groups


def market_to_group(market: str, asset_cls: str = "") -> str:
    """KRX 시장/자산군 분류를 전략 그룹으로 매핑한다."""
    if asset_cls == "원자재":
        return "commodity"
    if market == "국내":
        return "domestic_equity"
    return "foreign_investment"


def add_pit_membership_flag(
    price: pd.DataFrame, panel: pd.DataFrame
) -> pd.DataFrame:
    """가격 데이터에 as-of PIT membership 플래그 ``pit_membership_ok``를 추가한다.

    각 가격일에는 해당 날짜 이전의 최신 snapshot membership만 적용한다. 첫
    snapshot 이전과 빈 panel은 fail-closed로 ``False``를 반환한다.
    """
    price = price.copy()
    work = price[["date", "ticker"]].copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work["ticker"] = work["ticker"].astype(str).str.strip()
    normalized = _normalize_membership_rows(panel)
    snapshot_dates = pd.Index(normalized["snapshot_date"].drop_duplicates().sort_values())
    memberships = {
        date: set(group["ticker"])
        for date, group in normalized.groupby("snapshot_date", sort=True)
    }
    flags = []
    for row in work.itertuples(index=False):
        if pd.isna(row.date):
            flags.append(False)
            continue
        prior = snapshot_dates[snapshot_dates <= row.date]
        if prior.empty:
            flags.append(False)
            continue
        flags.append(row.ticker in memberships[pd.Timestamp(prior.max())])
    price["pit_membership_ok"] = pd.Series(flags, index=price.index, dtype=bool)
    return price
