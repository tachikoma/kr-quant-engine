from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

MA_DAYS = 120
SLOPE_DAYS = 20
KOSPI_YF_TICKER = "^KS11"


def load_dotenv() -> None:
    dotenv_path = ROOT / ".env"
    if not dotenv_path.exists():
        return
    try:
        with dotenv_path.open("r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.lower().startswith("export "):
                    line = line[7:].lstrip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        pass


def compute_signal(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    ma = close.rolling(MA_DAYS).mean()
    slope = ma - ma.shift(SLOPE_DAYS)
    risk_on = ((close >= ma) & (slope >= 0)).fillna(False)
    return ma, slope, risk_on


def download_index(ticker: str, start: str, end: str) -> pd.DataFrame:
    """Download index data via yfinance with parquet caching."""
    try:
        import yfinance as yf
    except Exception:
        raise RuntimeError("yfinance를 사용할 수 없습니다.")

    cache_dir = ROOT / "data_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe = ticker.replace("^", "").replace(".", "_")
    cache_file = cache_dir / f"proxy_signal_{safe}.parquet"
    use_cache = os.environ.get("ETF_USE_CACHE", "1") != "0"
    force_refresh = os.environ.get("ETF_REFRESH_CACHE", "0") == "1"

    req_start = pd.to_datetime(start)
    req_end = pd.to_datetime(end)

    if use_cache and cache_file.exists() and not force_refresh:
        try:
            cached = pd.read_parquet(cache_file)
            cached["date"] = pd.to_datetime(cached["date"])
            out = cached[(cached["date"] >= req_start) & (cached["date"] <= req_end)].copy()
            if not out.empty:
                return out[["date", "close", "risk_on"]]
        except Exception:
            pass

    end_dt = req_end + pd.Timedelta(days=1)
    raw = yf.download(ticker, start=req_start, end=end_dt, progress=False, auto_adjust=False)
    if raw is None or raw.empty:
        raise RuntimeError(f"{ticker} 데이터가 없습니다.")
    raw = raw.reset_index()

    if getattr(raw.columns, "nlevels", 1) > 1:
        flat = []
        for col in raw.columns:
            if isinstance(col, tuple):
                flat.append("_".join(str(x) for x in col if x))
            else:
                flat.append(str(col))
        raw.columns = flat

    close_col = None
    for cand in ["Adj Close", "Close", "Adj_Close"]:
        if cand in raw.columns:
            close_col = cand
            break
    if close_col is None:
        for c in raw.columns:
            cl = str(c).lower().replace(" ", "_")
            if "adj_close" in cl or cl.startswith("close"):
                close_col = c
                break
    if close_col is None:
        raise RuntimeError(f"{ticker} 종가 컬럼을 찾지 못했습니다.")

    date_col = "Date" if "Date" in raw.columns else ("date" if "date" in raw.columns else None)
    if date_col is None:
        raise RuntimeError(f"{ticker} 날짜 컬럼을 찾지 못했습니다.")

    close_series = raw[close_col]
    if isinstance(close_series, pd.DataFrame):
        close_series = close_series.iloc[:, 0]

    df = pd.DataFrame({
        "date": pd.to_datetime(raw[date_col]),
        "close": pd.to_numeric(close_series, errors="coerce"),
    })
    df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    _, _, risk_on = compute_signal(df["close"])
    df["risk_on"] = risk_on

    if use_cache:
        try:
            tmp = cache_file.with_suffix(".parquet.tmp")
            df.to_parquet(tmp)
            os.replace(tmp, cache_file)
        except Exception:
            pass

    return df[(df["date"] >= req_start) & (df["date"] <= req_end)][["date", "close", "risk_on"]]


def align_signal_to_dates(us_df: pd.DataFrame, target_dates: pd.DatetimeIndex) -> pd.Series:
    """Forward-fill US risk_on signal to Korean trading dates.

    us_df must have 'date' and 'risk_on' columns.
    Returns a Series indexed by target_dates with risk_on values (ffilled).
    """
    us = us_df.set_index("date")["risk_on"]
    idx = pd.Index(target_dates)
    aligned = us.reindex(idx, method="ffill")
    # For dates before the first US signal, default to True (no gating)
    aligned = aligned.fillna(True)
    return aligned


def pick_equity_column(df: pd.DataFrame) -> str:
    """Find the equity column in backtest output. Single mode uses 'equity'."""
    for col in ("equity", "equity_strategy", "equity_liquidate", "equity_hold"):
        if col in df.columns:
            return col
    raise KeyError(f"No equity column found. Available: {list(df.columns)}")
