"""ETF 현금분배금 로드와 분배금 재투자 total-return 계산."""

from __future__ import annotations

import os
import hashlib
from pathlib import Path

import numpy as np
import pandas as pd


DEFAULT_DISTRIBUTIONS_FILE = Path("data/etf_distributions.csv")
REQUIRED_COLUMNS = {"ticker", "ex_date", "amount_per_share"}


def distributions_path() -> Path:
    return Path(os.environ.get("ETF_DISTRIBUTIONS_FILE", str(DEFAULT_DISTRIBUTIONS_FILE)))


def distributions_file_sha256(path: Path | None = None) -> str | None:
    path = path or distributions_path()
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_distributions(path: Path | None = None, required: bool = False) -> pd.DataFrame:
    """정규화한 ETF 분배금 CSV를 읽고 중복 이벤트는 합산한다."""
    path = path or distributions_path()
    if not path.exists():
        if required:
            raise FileNotFoundError(f"분배금 파일이 없습니다: {path}")
        return pd.DataFrame(columns=sorted(REQUIRED_COLUMNS))

    data = pd.read_csv(path, dtype={"ticker": str})
    missing = REQUIRED_COLUMNS - set(data.columns)
    if missing:
        raise ValueError(f"분배금 파일 필수 컬럼 누락: {sorted(missing)}")
    if data.empty:
        if required:
            raise ValueError(f"분배금 파일에 데이터가 없습니다: {path}")
        return data

    data = data.copy()
    data["ticker"] = data["ticker"].astype(str).str.strip().str.zfill(6)
    data["ex_date"] = pd.to_datetime(data["ex_date"], errors="coerce")
    data["amount_per_share"] = pd.to_numeric(data["amount_per_share"], errors="coerce")
    invalid = data[
        data["ticker"].eq("")
        | data["ex_date"].isna()
        | data["amount_per_share"].isna()
        | data["amount_per_share"].le(0)
    ]
    if not invalid.empty:
        raise ValueError(f"분배금 파일에 잘못된 행이 있습니다: rows={invalid.index.tolist()}")

    optional_columns = [column for column in ["payment_date", "source"] if column in data]
    if "payment_date" in data:
        data["payment_date"] = pd.to_datetime(data["payment_date"], errors="coerce")
    aggregations = {"amount_per_share": "sum", **{column: "first" for column in optional_columns}}
    return (
        data.groupby(["ticker", "ex_date"], as_index=False)
        .agg(aggregations)
        .sort_values(["ticker", "ex_date"])
        .reset_index(drop=True)
    )


def add_distributions(price: pd.DataFrame, distributions: pd.DataFrame) -> pd.DataFrame:
    """가격 데이터에 분배락일 기준 주당 분배금을 병합한다."""
    result = price.copy()
    result["date"] = pd.to_datetime(result["date"])
    result["ticker"] = result["ticker"].astype(str).str.zfill(6)
    if distributions.empty:
        result["distribution"] = 0.0
        return result

    events = distributions[["ticker", "ex_date", "amount_per_share"]].rename(
        columns={"ex_date": "date", "amount_per_share": "distribution"}
    )
    events = events.copy()
    events["date"] = pd.to_datetime(events["date"])
    events["ticker"] = events["ticker"].astype(str).str.zfill(6)

    price_keys = result[["ticker", "date"]].drop_duplicates()
    relevant_tickers = set(price_keys["ticker"])
    min_date = price_keys["date"].min()
    max_date = price_keys["date"].max()
    relevant_events = events[
        events["ticker"].isin(relevant_tickers)
        & events["date"].between(min_date, max_date)
    ]
    unmatched = relevant_events.merge(
        price_keys, on=["ticker", "date"], how="left", indicator=True
    )
    unmatched = unmatched[unmatched["_merge"] == "left_only"]
    if not unmatched.empty:
        sample = unmatched[["ticker", "date"]].head(5).to_dict(orient="records")
        raise ValueError(f"가격 거래일과 일치하지 않는 분배락 이벤트가 있습니다: {sample}")

    result = result.merge(relevant_events, on=["ticker", "date"], how="left")
    result["distribution"] = result["distribution"].fillna(0.0).astype(float)
    return result


def add_total_return_price(price: pd.DataFrame) -> pd.DataFrame:
    """분배금을 종가에 즉시 재투자한 가격형 total-return 지수를 만든다."""
    result = price.sort_values(["ticker", "date"]).copy()
    close = pd.to_numeric(result["close"], errors="coerce")
    if "distribution" in result:
        distribution = pd.to_numeric(result["distribution"], errors="coerce").fillna(0.0)
    else:
        distribution = pd.Series(0.0, index=result.index)
    previous_close = close.groupby(result["ticker"]).shift(1)
    valid = close.gt(0) & previous_close.gt(0)
    growth = pd.Series(1.0, index=result.index)
    growth.loc[valid] = (close.loc[valid] + distribution.loc[valid]) / previous_close.loc[valid]
    growth = growth.replace([np.inf, -np.inf], np.nan).fillna(1.0)
    cumulative = growth.groupby(result["ticker"]).cumprod()
    first_close = close.groupby(result["ticker"]).transform("first")
    result["close_total_return"] = first_close * cumulative
    return result


def distribution_cash_for_holdings(
    holdings: dict[str, int], distributions: pd.Series | dict | None, tax_pct: float = 0.0
) -> float:
    """분배락 직전 보유수량에 귀속되는 세후 현금분배금을 계산한다."""
    if distributions is None:
        return 0.0
    total = 0.0
    for ticker, qty in holdings.items():
        try:
            amount = float(distributions.get(str(ticker), 0.0) or 0.0)
        except (AttributeError, TypeError, ValueError):
            amount = 0.0
        if amount > 0 and int(qty) > 0:
            total += int(qty) * amount
    return total * (1 - max(0.0, min(float(tax_pct), 1.0)))
