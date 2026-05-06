from pathlib import Path
import os

import numpy as np
import pandas as pd
from pykrx import stock

START = "20160101"
END = "20260430"


def load_dotenv(dotenv_path: str | Path | None = None) -> None:
    if dotenv_path is None:
        dotenv_path = Path(__file__).resolve().parent / ".env"
    path = Path(dotenv_path)
    if not path.exists():
        return

    with path.open("r", encoding="utf-8") as f:
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


load_dotenv()
HAS_KRX_CREDENTIALS = bool(os.environ.get("KRX_ID") and os.environ.get("KRX_PW"))
MARKET = "KOSPI"

INITIAL_CASH = 1_000_000
OUTPUT_DIR = Path("outputs_mixed")

ETF_WEIGHT = 0.70
STOCK_WEIGHT = 0.30

BUY_FEE_PCT = 0.00015
SELL_FEE_PCT = 0.00015
SELL_TAX_PCT = 0.0018
ETF_SELL_TAX_PCT = 0.0
SLIPPAGE_PCT = 0.0005

REBALANCE_STEP_DAYS = 20
KOSPI_INDEX_CODE = "1001"
MARKET_MA_DAYS = 120
MARKET_SLOPE_DAYS = 20

# ETF는 장기 생존/유동성 중심으로 구성한다.
ETF_LIST = [
    "069500",  # KODEX 200
    "229200",  # KODEX 코스닥150
    "091160",  # KODEX 반도체
    "102110",  # TIGER 200
    "143850",  # TIGER 미국S&P500선물(H)
    "133690",  # TIGER 미국나스닥100
]
ETF_MAX_POSITIONS = 2
ETF_SELL_RANK_BUFFER = 3

BENCHMARK_TICKER = "069500"  # KODEX 200

# 개별주는 소액 계좌 기준으로 집중하되, 과도한 회전은 피한다.
STOCK_MAX_POSITIONS = 3
TOP_UNIVERSE_SIZE = 600
MIN_PRICE = 3_000
MAX_PRICE = 80_000
MIN_AVG_TRADING_VALUE_20 = 1_000_000_000
STOCK_MIN_HOLD_DAYS = 20
STOCK_SELL_RANK_BUFFER = 6
STOCK_STOP_LOSS_PCT = -0.15
MARKET_RISK_OFF_SELL_AFTER_DAYS = 5


def get_ticker_name(ticker: str) -> str:
    if not HAS_KRX_CREDENTIALS:
        return ticker
    try:
        name = stock.get_market_ticker_name(ticker)
        if name is None:
            return ticker
        if hasattr(name, "empty") and name.empty:
            return ticker
        return str(name)
    except Exception:
        return ticker


def is_excluded_ticker(ticker: str) -> bool:
    name = get_ticker_name(ticker)
    excluded_keywords = ["우", "스팩", "리츠"]
    return any(keyword in name for keyword in excluded_keywords)


def normalize_ohlcv(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["date", "ticker", "open", "close", "volume", "trading_value"])

    df = df.reset_index()
    df["ticker"] = ticker
    df = df.rename(
        columns={
            "날짜": "date",
            "시가": "open",
            "종가": "close",
            "거래량": "volume",
            "거래대금": "trading_value",
        }
    )

    required_columns = ["date", "ticker", "open", "close"]
    missing_columns = [col for col in required_columns if col not in df.columns]
    if missing_columns:
        raise ValueError(f"{ticker}: missing columns {missing_columns}; actual columns={list(df.columns)}")

    if "volume" not in df.columns:
        df["volume"] = 0
    if "trading_value" not in df.columns:
        df["trading_value"] = df["close"] * df["volume"]

    out = df[["date", "ticker", "open", "close", "volume", "trading_value"]].copy()
    out["date"] = pd.to_datetime(out["date"])
    return out


def get_price(ticker: str) -> pd.DataFrame:
    return normalize_ohlcv(stock.get_market_ohlcv_by_date(START, END, ticker), ticker)


def get_index_data() -> pd.DataFrame:
    idx = stock.get_index_ohlcv_by_date(START, END, KOSPI_INDEX_CODE)
    if idx is None or idx.empty:
        raise RuntimeError("No KOSPI index data returned.")
    idx = idx.reset_index().rename(columns={"날짜": "date", "종가": "close"})
    idx["date"] = pd.to_datetime(idx["date"])
    idx["market_ma"] = idx["close"].rolling(MARKET_MA_DAYS).mean()
    idx["market_ma_slope"] = idx["market_ma"] - idx["market_ma"].shift(MARKET_SLOPE_DAYS)
    idx["risk_on"] = (idx["close"] >= idx["market_ma"]) & (idx["market_ma_slope"] >= 0)
    return idx[["date", "close", "market_ma", "market_ma_slope", "risk_on"]]


def is_risk_on(index_df: pd.DataFrame, date: pd.Timestamp) -> bool:
    rows = index_df[index_df["date"] <= date]
    if rows.empty:
        return True
    last = rows.iloc[-1]
    if pd.isna(last["market_ma"]) or pd.isna(last["market_ma_slope"]):
        return True
    return bool(last["risk_on"])


def safe_get(series: pd.Series, key: str):
    value = series.get(key)
    if value is None or pd.isna(value) or value <= 0:
        return None
    return float(value)


def zscore(series: pd.Series) -> pd.Series:
    series = series.replace([np.inf, -np.inf], np.nan)
    if series.notna().sum() < 2:
        return pd.Series(0.0, index=series.index)
    filled = series.fillna(series.median())
    std = filled.std(ddof=0)
    if std == 0 or np.isnan(std):
        return pd.Series(0.0, index=series.index)
    return (filled - filled.mean()) / std


def apply_buy_cost(price: float) -> float:
    return price * (1 + SLIPPAGE_PCT) * (1 + BUY_FEE_PCT)


def apply_sell_value(price: float, qty: int, sell_tax_pct: float) -> float:
    sell_price = price * (1 - SLIPPAGE_PCT)
    return qty * sell_price * (1 - SELL_FEE_PCT - sell_tax_pct)


# ---------------- ETF 전략 ----------------
def load_etf_price() -> pd.DataFrame:
    frames = []
    failed = []
    empty = []
    for ticker in ETF_LIST:
        try:
            df = get_price(ticker)
            if df.empty:
                empty.append(ticker)
                continue
            frames.append(df)
        except Exception as exc:
            failed.append((ticker, str(exc)))

    if not frames:
        raise RuntimeError(f"No ETF data collected. empty={empty}, failed={failed[:5]}")

    if failed:
        print(f"[WARN] failed ETFs: {failed[:3]}")
    if empty:
        print(f"[WARN] empty ETFs: {empty}")

    price = pd.concat(frames, ignore_index=True)
    price = price.sort_values(["ticker", "date"]).copy()
    g = price.groupby("ticker")
    price["ret_60"] = g["close"].pct_change(60)
    price["ret_120"] = g["close"].pct_change(120)
    price["ma20"] = g["close"].transform(lambda x: x.rolling(20).mean())
    price["ma60"] = g["close"].transform(lambda x: x.rolling(60).mean())
    price["trend_ok"] = (price["close"] > price["ma20"]) & (price["ma20"] > price["ma60"])
    return price


def rank_etfs(snapshot: pd.DataFrame) -> pd.DataFrame:
    df = snapshot.copy()
    df = df[df["ret_60"].notna() & df["ret_120"].notna() & df["trend_ok"]].copy()
    if df.empty:
        return df
    df["score"] = 0.55 * zscore(df["ret_60"]) + 0.45 * zscore(df["ret_120"])
    return df.sort_values("score", ascending=False).reset_index(drop=True)


def run_etf_strategy(initial_cash: float, common_dates: list[pd.Timestamp], index_df: pd.DataFrame):
    price = load_etf_price()
    price_by_date = {dt: day.set_index("ticker") for dt, day in price.groupby("date")}

    cash = float(initial_cash)
    holdings = {}
    entry_date = {}
    trades = []
    equity_rows = []

    warmup_days = max(120, MARKET_MA_DAYS + MARKET_SLOPE_DAYS)
    for i, dt in enumerate(common_dates[:-1]):
        if i < warmup_days:
            continue
        next_dt = common_dates[i + 1]
        today = price_by_date.get(dt, pd.DataFrame())
        next_day = price_by_date.get(next_dt, pd.DataFrame())
        if today.empty or next_day.empty:
            continue

        next_open = next_day["open"]
        next_close = next_day["close"]
        should_rebalance = (i - warmup_days) % REBALANCE_STEP_DAYS == 0

        if should_rebalance:
            risk_on = is_risk_on(index_df, dt)
            ranked = rank_etfs(today.reset_index())
            target_rank = dict(zip(ranked["ticker"], ranked.index + 1)) if not ranked.empty else {}
            targets = ranked.head(ETF_MAX_POSITIONS)["ticker"].tolist() if risk_on else []

            for ticker in list(holdings.keys()):
                rank = target_rank.get(ticker)
                keep_by_rank = rank is not None and rank <= ETF_SELL_RANK_BUFFER and risk_on
                if keep_by_rank:
                    continue
                open_price = safe_get(next_open, ticker)
                if open_price is None:
                    continue
                qty = holdings.pop(ticker)
                cash += apply_sell_value(open_price, qty, ETF_SELL_TAX_PCT)
                entry_date.pop(ticker, None)
                trades.append({"date": next_dt, "ticker": ticker, "name": get_ticker_name(ticker), "side": "SELL", "reason": "ETF_REBALANCE", "qty": qty, "price": open_price, "cash_after": cash})

            slots = max(ETF_MAX_POSITIONS - len(holdings), 0)
            buy_list = [ticker for ticker in targets if ticker not in holdings][:slots]
            if buy_list and cash > 0:
                budget = cash / len(buy_list)
                for ticker in buy_list:
                    open_price = safe_get(next_open, ticker)
                    if open_price is None:
                        continue
                    unit_cost = apply_buy_cost(open_price)
                    qty = int(budget // unit_cost)
                    if qty <= 0:
                        continue
                    cost = qty * unit_cost
                    if cost > cash:
                        qty = int(cash // unit_cost)
                        cost = qty * unit_cost
                    if qty <= 0:
                        continue
                    holdings[ticker] = holdings.get(ticker, 0) + qty
                    entry_date[ticker] = next_dt
                    cash -= cost
                    trades.append({"date": next_dt, "ticker": ticker, "name": get_ticker_name(ticker), "side": "BUY", "reason": "ETF_REBALANCE", "qty": qty, "price": open_price, "cash_after": cash})

        market_value = 0.0
        for ticker, qty in holdings.items():
            close_price = safe_get(next_close, ticker)
            if close_price is not None:
                market_value += qty * close_price
        equity_rows.append({"date": next_dt, "equity_etf": cash + market_value, "cash_etf": cash, "market_value_etf": market_value, "holdings_etf": ",".join(sorted(holdings.keys()))})

    return pd.DataFrame(equity_rows), pd.DataFrame(trades)

# ---------------- 추가 벤치마크/ETF-only 전략 ----------------

def run_kodex200_buy_and_hold(initial_cash: float, common_dates: list[pd.Timestamp]) -> pd.DataFrame:
    price = get_price(BENCHMARK_TICKER)
    if price.empty:
        raise RuntimeError(f"No benchmark data for {BENCHMARK_TICKER}")

    price_by_date = {dt: day.set_index("ticker") for dt, day in price.groupby("date")}
    cash = float(initial_cash)
    qty = 0
    bought = False
    equity_rows = []

    for i, dt in enumerate(common_dates[:-1]):
        next_dt = common_dates[i + 1]
        next_day = price_by_date.get(next_dt, pd.DataFrame())
        if next_day.empty:
            continue

        next_open = next_day["open"]
        next_close = next_day["close"]

        if not bought:
            open_price = safe_get(next_open, BENCHMARK_TICKER)
            if open_price is None:
                continue
            unit_cost = apply_buy_cost(open_price)
            qty = int(cash // unit_cost)
            if qty > 0:
                cash -= qty * unit_cost
                bought = True

        close_price = safe_get(next_close, BENCHMARK_TICKER)
        market_value = qty * close_price if close_price is not None else 0.0
        equity_rows.append(
            {
                "date": next_dt,
                "equity_kodex200_bh": cash + market_value,
                "cash_kodex200_bh": cash,
                "market_value_kodex200_bh": market_value,
            }
        )

    return pd.DataFrame(equity_rows)


def run_etf_only_strategy(initial_cash: float, common_dates: list[pd.Timestamp], index_df: pd.DataFrame):
    curve, trades = run_etf_strategy(initial_cash, common_dates, index_df)
    curve = curve.rename(
        columns={
            "equity_etf": "equity_etf_only",
            "cash_etf": "cash_etf_only",
            "market_value_etf": "market_value_etf_only",
            "holdings_etf": "holdings_etf_only",
        }
    )
    return curve, trades

# ---------------- 개별주 전략 ----------------
def get_fundamental_snapshot(date: str) -> pd.DataFrame:
    df = stock.get_market_fundamental_by_ticker(date, market=MARKET)
    if df is None or df.empty:
        return pd.DataFrame(columns=["ticker", "per", "pbr", "eps", "bps", "div"])

    df = df.reset_index().rename(columns={"티커": "ticker", "BPS": "bps", "PER": "per", "PBR": "pbr", "EPS": "eps", "DIV": "div"})
    if "ticker" not in df.columns:
        df = df.rename(columns={df.columns[0]: "ticker"})
    for col in ["per", "pbr", "eps", "bps", "div"]:
        if col not in df.columns:
            df[col] = np.nan
    return df[["ticker", "per", "pbr", "eps", "bps", "div"]]


def get_market_cap_universe(date: str) -> list[str]:
    cap = stock.get_market_cap_by_ticker(date, market=MARKET)
    if cap is None or cap.empty:
        raise RuntimeError(f"No market cap data returned for date={date}, market={MARKET}")
    cap = cap.reset_index().rename(columns={"티커": "ticker", "시가총액": "market_cap"})
    if "ticker" not in cap.columns:
        cap = cap.rename(columns={cap.columns[0]: "ticker"})
    cap = cap.sort_values("market_cap", ascending=False)

    tickers = []
    for ticker in cap["ticker"].tolist():
        if is_excluded_ticker(ticker):
            continue
        tickers.append(ticker)
        if len(tickers) >= TOP_UNIVERSE_SIZE:
            break
    return tickers


def load_stock_price() -> pd.DataFrame:
    tickers = get_market_cap_universe(END)
    print(f"Loaded stock market-cap universe: {len(tickers)} tickers")
    frames = []
    failed = []
    empty = []
    for ticker in tickers:
        try:
            df = get_price(ticker)
            if df.empty:
                empty.append(ticker)
                continue
            frames.append(df)
        except Exception as exc:
            failed.append((ticker, str(exc)))

    if not frames:
        raise RuntimeError(f"No stock price data collected. empty={len(empty)}, failed={len(failed)}, sample_failed={failed[:5]}")
    if failed:
        print(f"[WARN] failed stocks: {len(failed)} / {len(tickers)}; sample={failed[:3]}")
    if empty:
        print(f"[WARN] empty stocks: {len(empty)} / {len(tickers)}; sample={empty[:10]}")

    price = pd.concat(frames, ignore_index=True)
    price = price.sort_values(["ticker", "date"]).copy()
    g = price.groupby("ticker")
    price["ret_60"] = g["close"].pct_change(60)
    price["ret_120"] = g["close"].pct_change(120)
    price["ret_180"] = g["close"].pct_change(180)
    price["ma20"] = g["close"].transform(lambda x: x.rolling(20).mean())
    price["ma60"] = g["close"].transform(lambda x: x.rolling(60).mean())
    price["avg_trading_value_20"] = g["trading_value"].transform(lambda x: x.rolling(20).mean())
    price["trend_ok"] = (price["close"] > price["ma20"]) & (price["ma20"] > price["ma60"])
    return price


def rank_stocks(snapshot: pd.DataFrame, fundamental: pd.DataFrame) -> pd.DataFrame:
    df = snapshot.merge(fundamental, on="ticker", how="left").copy()
    df = df[
        (df["close"] >= MIN_PRICE)
        & (df["close"] <= MAX_PRICE)
        & (df["avg_trading_value_20"] >= MIN_AVG_TRADING_VALUE_20)
        & df["trend_ok"]
        & df["ret_60"].notna()
        & df["ret_120"].notna()
        & df["ret_180"].notna()
    ].copy()

    if df.empty:
        return df

    roe_proxy = df["eps"] / df["bps"].replace(0, np.nan)
    value_score = -zscore(df["per"]) - zscore(df["pbr"])
    quality_score = zscore(roe_proxy)
    dividend_score = zscore(df["div"])
    momentum_score = 0.35 * zscore(df["ret_60"]) + 0.35 * zscore(df["ret_120"]) + 0.30 * zscore(df["ret_180"])

    df["score"] = 0.45 * momentum_score + 0.25 * value_score + 0.20 * quality_score + 0.10 * dividend_score
    return df.sort_values("score", ascending=False).reset_index(drop=True)


def run_stock_strategy(initial_cash: float, common_dates: list[pd.Timestamp], index_df: pd.DataFrame):
    price = load_stock_price()
    price_by_date = {dt: day.set_index("ticker") for dt, day in price.groupby("date")}
    fundamental = get_fundamental_snapshot(END)

    cash = float(initial_cash)
    holdings = {}
    avg_price = {}
    entry_date = {}
    trades = []
    equity_rows = []

    warmup_days = max(180, MARKET_MA_DAYS + MARKET_SLOPE_DAYS)
    for i, dt in enumerate(common_dates[:-1]):
        if i < warmup_days:
            continue
        next_dt = common_dates[i + 1]
        today = price_by_date.get(dt, pd.DataFrame())
        next_day = price_by_date.get(next_dt, pd.DataFrame())
        if today.empty or next_day.empty:
            continue

        today_close = today["close"]
        next_open = next_day["open"]
        next_close = next_day["close"]

        for ticker in list(holdings.keys()):
            close_price = safe_get(today_close, ticker)
            open_price = safe_get(next_open, ticker)
            if close_price is None or open_price is None:
                continue
            pnl = close_price / avg_price[ticker] - 1
            if pnl <= STOCK_STOP_LOSS_PCT:
                qty = holdings.pop(ticker)
                cash += apply_sell_value(open_price, qty, SELL_TAX_PCT)
                avg_price.pop(ticker, None)
                entry_date.pop(ticker, None)
                trades.append({"date": next_dt, "ticker": ticker, "name": get_ticker_name(ticker), "side": "SELL", "reason": "STOCK_STOP_LOSS", "qty": qty, "price": open_price, "cash_after": cash})

        should_rebalance = (i - warmup_days) % REBALANCE_STEP_DAYS == 0
        if should_rebalance:
            risk_on = is_risk_on(index_df, dt)
            ranked = rank_stocks(today.reset_index(), fundamental)
            target_rank = dict(zip(ranked["ticker"], ranked.index + 1)) if not ranked.empty else {}
            targets = ranked.head(STOCK_MAX_POSITIONS)["ticker"].tolist() if risk_on else []

            for ticker in list(holdings.keys()):
                held_days = (dt - entry_date.get(ticker, dt)).days
                rank = target_rank.get(ticker)
                keep_by_rank = rank is not None and rank <= STOCK_SELL_RANK_BUFFER and risk_on
                keep_by_min_hold = held_days < STOCK_MIN_HOLD_DAYS
                if not risk_on:
                    if held_days < MARKET_RISK_OFF_SELL_AFTER_DAYS:
                        continue
                elif keep_by_rank or keep_by_min_hold:
                    continue

                open_price = safe_get(next_open, ticker)
                if open_price is None:
                    continue
                qty = holdings.pop(ticker)
                cash += apply_sell_value(open_price, qty, SELL_TAX_PCT)
                avg_price.pop(ticker, None)
                entry_date.pop(ticker, None)
                trades.append({"date": next_dt, "ticker": ticker, "name": get_ticker_name(ticker), "side": "SELL", "reason": "STOCK_REBALANCE", "qty": qty, "price": open_price, "cash_after": cash})

            slots = max(STOCK_MAX_POSITIONS - len(holdings), 0)
            buy_list = [ticker for ticker in targets if ticker not in holdings][:slots]
            if buy_list and cash > 0:
                budget = cash / len(buy_list)
                for ticker in buy_list:
                    open_price = safe_get(next_open, ticker)
                    if open_price is None:
                        continue
                    unit_cost = apply_buy_cost(open_price)
                    qty = int(budget // unit_cost)
                    if qty <= 0:
                        continue
                    cost = qty * unit_cost
                    if cost > cash:
                        qty = int(cash // unit_cost)
                        cost = qty * unit_cost
                    if qty <= 0:
                        continue
                    holdings[ticker] = holdings.get(ticker, 0) + qty
                    avg_price[ticker] = open_price * (1 + SLIPPAGE_PCT)
                    entry_date[ticker] = next_dt
                    cash -= cost
                    trades.append({"date": next_dt, "ticker": ticker, "name": get_ticker_name(ticker), "side": "BUY", "reason": "STOCK_REBALANCE", "qty": qty, "price": open_price, "cash_after": cash})

        market_value = 0.0
        for ticker, qty in holdings.items():
            close_price = safe_get(next_close, ticker)
            if close_price is not None:
                market_value += qty * close_price
        equity_rows.append({"date": next_dt, "equity_stock": cash + market_value, "cash_stock": cash, "market_value_stock": market_value, "holdings_stock": ",".join(sorted(holdings.keys()))})

    return pd.DataFrame(equity_rows), pd.DataFrame(trades)


# ---------------- 혼합 전략 ----------------
def run():
    index_df = get_index_data()
    common_dates = list(index_df["date"])

    etf_cash = INITIAL_CASH * ETF_WEIGHT
    stock_cash = INITIAL_CASH * STOCK_WEIGHT

    benchmark_curve = run_kodex200_buy_and_hold(INITIAL_CASH, common_dates)
    etf_only_curve, etf_only_trades = run_etf_only_strategy(INITIAL_CASH, common_dates, index_df)
    etf_curve, etf_trades = run_etf_strategy(etf_cash, common_dates, index_df)
    stock_curve, stock_trades = run_stock_strategy(stock_cash, common_dates, index_df)

    merged = pd.merge(etf_curve, stock_curve, on="date", how="outer").sort_values("date")
    merged["equity_etf"] = merged["equity_etf"].ffill().fillna(etf_cash)
    merged["equity_stock"] = merged["equity_stock"].ffill().fillna(stock_cash)
    merged["cash_etf"] = merged["cash_etf"].ffill().fillna(etf_cash)
    merged["cash_stock"] = merged["cash_stock"].ffill().fillna(stock_cash)
    merged["market_value_etf"] = merged["market_value_etf"].ffill().fillna(0)
    merged["market_value_stock"] = merged["market_value_stock"].ffill().fillna(0)
    merged["holdings_etf"] = merged["holdings_etf"].ffill().fillna("")
    merged["holdings_stock"] = merged["holdings_stock"].ffill().fillna("")
    merged["equity"] = merged["equity_etf"] + merged["equity_stock"]
    merged["cash"] = merged["cash_etf"] + merged["cash_stock"]
    merged["market_value"] = merged["market_value_etf"] + merged["market_value_stock"]

    merged = pd.merge(merged, benchmark_curve, on="date", how="outer").sort_values("date")
    merged = pd.merge(merged, etf_only_curve, on="date", how="outer").sort_values("date")
    merged["equity"] = merged["equity"].ffill()
    merged["equity_kodex200_bh"] = merged["equity_kodex200_bh"].ffill().fillna(INITIAL_CASH)
    merged["equity_etf_only"] = merged["equity_etf_only"].ffill().fillna(INITIAL_CASH)

    trades = pd.concat(
        [
            etf_trades.assign(strategy="MIXED_ETF"),
            stock_trades.assign(strategy="MIXED_STOCK"),
            etf_only_trades.assign(strategy="ETF_ONLY"),
        ],
        ignore_index=True,
    )
    return merged, trades



def calc_stats(df: pd.DataFrame, equity_col: str) -> dict:
    temp = df[["date", equity_col]].dropna().copy()
    temp["date"] = pd.to_datetime(temp["date"])
    temp = temp.sort_values("date")
    temp["daily_ret"] = temp[equity_col].pct_change().fillna(0)

    total_return = temp[equity_col].iloc[-1] / temp[equity_col].iloc[0] - 1
    years = max((temp["date"].iloc[-1] - temp["date"].iloc[0]).days / 365.25, 1 / 365.25)
    cagr = (temp[equity_col].iloc[-1] / temp[equity_col].iloc[0]) ** (1 / years) - 1
    drawdown = temp[equity_col] / temp[equity_col].cummax() - 1
    mdd = drawdown.min()
    volatility = temp["daily_ret"].std() * np.sqrt(252)
    sharpe = np.nan if volatility == 0 else temp["daily_ret"].mean() * 252 / volatility

    return {
        "initial": temp[equity_col].iloc[0],
        "final": temp[equity_col].iloc[-1],
        "total_return": total_return,
        "cagr": cagr,
        "mdd": mdd,
        "volatility": volatility,
        "sharpe": sharpe,
    }


def summarize(df: pd.DataFrame, trades: pd.DataFrame):
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["daily_ret"] = df["equity"].pct_change().fillna(0)

    mixed_stats = calc_stats(df, "equity")
    kodex_stats = calc_stats(df, "equity_kodex200_bh")
    etf_only_stats = calc_stats(df, "equity_etf_only")

    years = max((df["date"].iloc[-1] - df["date"].iloc[0]).days / 365.25, 1 / 365.25)
    trades_per_year = len(trades) / years if years > 0 else np.nan
    invested_ratio = (df["market_value"] / df["equity"]).mean()

    comparison = pd.DataFrame(
        [
            {"strategy": "KODEX200_BuyHold", **kodex_stats},
            {"strategy": "ETF_Only", **etf_only_stats},
            {"strategy": "Mixed_ETF70_Stock30", **mixed_stats},
        ]
    )

    print(df.tail())
    print("\n=== BENCHMARK COMPARISON ===")
    display_cols = ["strategy", "initial", "final", "total_return", "cagr", "mdd", "volatility", "sharpe"]
    print(comparison[display_cols].to_string(index=False, float_format=lambda x: f"{x:,.4f}"))

    print("\n=== MIXED PERFORMANCE DETAIL ===")
    print(f"trades: {len(trades)}")
    print(f"trades_per_year: {trades_per_year:.2f}")
    print(f"avg_invested_ratio: {invested_ratio:.4f}")
    print(f"mixed_vs_kodex200_cagr_diff: {mixed_stats['cagr'] - kodex_stats['cagr']:.4f}")
    print(f"mixed_vs_etf_only_cagr_diff: {mixed_stats['cagr'] - etf_only_stats['cagr']:.4f}")
    print(f"mixed_vs_kodex200_mdd_diff: {mixed_stats['mdd'] - kodex_stats['mdd']:.4f}")
    print(f"mixed_vs_etf_only_mdd_diff: {mixed_stats['mdd'] - etf_only_stats['mdd']:.4f}")

    if not trades.empty:
        print("\n=== Trade count by strategy/reason ===")
        print(trades.groupby(["strategy", "side", "reason"]).size())


if __name__ == "__main__":
    OUTPUT_DIR.mkdir(exist_ok=True)

    result, trades = run()
    if result.empty:
        raise RuntimeError("Mixed backtest produced no result rows.")

    result.to_csv(OUTPUT_DIR / "mixed_equity_curve.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUTPUT_DIR / "mixed_trades.csv", index=False, encoding="utf-8-sig")

    benchmark_cols = ["date", "equity_kodex200_bh", "equity_etf_only", "equity"]
    result[benchmark_cols].to_csv(OUTPUT_DIR / "benchmark_comparison.csv", index=False, encoding="utf-8-sig")

    summarize(result, trades)
    print(f"saved: {OUTPUT_DIR / 'mixed_equity_curve.csv'}")
    print(f"saved: {OUTPUT_DIR / 'mixed_trades.csv'}")
    print(f"saved: {OUTPUT_DIR / 'benchmark_comparison.csv'}")