from pathlib import Path
import time
import pandas as pd
from tqdm import tqdm
from pykrx import stock


def ensure_dir(path):
    Path(path).mkdir(parents=True, exist_ok=True)


def get_tickers(date: str, market: str = "KOSPI") -> list[str]:
    return stock.get_market_ticker_list(date, market=market)


def fetch_ohlcv_one(start: str, end: str, ticker: str) -> pd.DataFrame:
    df = stock.get_market_ohlcv_by_date(start, end, ticker)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.reset_index()
    df["ticker"] = ticker
    df = df.rename(columns={
        "날짜": "date",
        "시가": "open",
        "고가": "high",
        "저가": "low",
        "종가": "close",
        "거래량": "volume",
        "거래대금": "trading_value",
        "등락률": "change_pct",
    })
    keep = ["date", "ticker", "open", "high", "low", "close", "volume", "trading_value"]
    df = df[[c for c in keep if c in df.columns]]
    if "trading_value" not in df.columns and {"close", "volume"}.issubset(df.columns):
        df["trading_value"] = df["close"] * df["volume"]
    return df


def load_or_fetch_ohlcv(start: str, end: str, tickers: list[str], cache_dir: str, sleep_sec: float = 0.25) -> pd.DataFrame:
    ensure_dir(cache_dir)
    cache_path = Path(cache_dir) / f"ohlcv_{start}_{end}_{len(tickers)}.parquet"

    if cache_path.exists():
        return pd.read_parquet(cache_path)

    frames = []
    for ticker in tqdm(tickers, desc="fetch ohlcv"):
        try:
            df = fetch_ohlcv_one(start, end, ticker)
            if not df.empty:
                frames.append(df)
        except Exception as e:
            print(f"[WARN] OHLCV failed: {ticker} {e}")
        time.sleep(sleep_sec)

    if not frames:
        raise RuntimeError("No OHLCV data fetched.")

    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"])
    out.to_parquet(cache_path, index=False)
    return out


def fetch_fundamental_snapshot(date: str, market: str = "KOSPI") -> pd.DataFrame:
    df = stock.get_market_fundamental_by_ticker(date, market=market)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.reset_index().rename(columns={
        "티커": "ticker",
        "BPS": "bps",
        "PER": "per",
        "PBR": "pbr",
        "EPS": "eps",
        "DIV": "div",
        "DPS": "dps",
    })
    if "ticker" not in df.columns:
        df = df.rename(columns={df.columns[0]: "ticker"})
    return df


def load_or_fetch_fundamentals(dates: list[pd.Timestamp], market: str, cache_dir: str, sleep_sec: float = 0.25) -> dict:
    ensure_dir(cache_dir)
    out = {}

    for dt in tqdm(dates, desc="fetch fundamentals"):
        key = dt.strftime("%Y%m%d")
        cache_path = Path(cache_dir) / f"fundamental_{market}_{key}.parquet"

        if cache_path.exists():
            out[dt] = pd.read_parquet(cache_path)
            continue

        try:
            df = fetch_fundamental_snapshot(key, market=market)
            if not df.empty:
                df.to_parquet(cache_path, index=False)
                out[dt] = df
        except Exception as e:
            print(f"[WARN] fundamental failed: {key} {e}")
        time.sleep(sleep_sec)

    return out


def fetch_index_ohlcv(start: str, end: str, index_code: str = "1001") -> pd.DataFrame:
    """
    index_code:
    - 1001: KOSPI
    - 2001: KOSDAQ
    """
    df = stock.get_index_ohlcv_by_date(start, end, index_code)
    df = df.reset_index().rename(columns={
        "날짜": "date",
        "시가": "open",
        "고가": "high",
        "저가": "low",
        "종가": "close",
        "거래량": "volume",
        "거래대금": "trading_value",
    })
    df["date"] = pd.to_datetime(df["date"])
    return df
