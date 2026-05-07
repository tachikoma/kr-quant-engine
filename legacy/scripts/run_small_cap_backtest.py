from pathlib import Path
import os

import numpy as np
import pandas as pd

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

from pykrx import stock

HAS_KRX_CREDENTIALS = bool(os.environ.get("KRX_ID") and os.environ.get("KRX_PW"))
MARKET = "KOSPI"

INITIAL_CASH = 1_000_000
OUTPUT_DIR = Path("outputs_small_cap")

# 소액 계좌용 실전형 기본값: 과도한 분산과 과도한 회전을 피하고 낙폭을 줄인다.
MAX_POSITIONS = 5
REBALANCE_STEP_DAYS = 20  # 약 월 1회
TOP_UNIVERSE_SIZE = 600
MIN_PRICE = 3_000
MAX_PRICE = 80_000
MIN_AVG_TRADING_VALUE_20 = 1_000_000_000
MIN_HOLD_DAYS = 20
SELL_RANK_BUFFER = 6  # 기존 보유 종목은 상위 6위 안이면 유지
MARKET_RISK_OFF_SELL_AFTER_DAYS = 5

BUY_FEE_PCT = 0.00015
SELL_FEE_PCT = 0.00015
SELL_TAX_PCT = 0.0018
SLIPPAGE_PCT = 0.0005
STOP_LOSS_PCT = -0.15

KOSPI_INDEX_CODE = "1001"
MARKET_MA_DAYS = 120
MARKET_SLOPE_DAYS = 20


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


def get_price(ticker: str) -> pd.DataFrame:
    df = stock.get_market_ohlcv_by_date(START, END, ticker)
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
        # pykrx 응답에 거래대금이 없을 수 있어 종가 * 거래량으로 근사한다.
        df["trading_value"] = df["close"] * df["volume"]

    return df[["date", "ticker", "open", "close", "volume", "trading_value"]]


def get_fundamental_snapshot(date: str) -> pd.DataFrame:
    df = stock.get_market_fundamental_by_ticker(date, market=MARKET)
    if df is None or df.empty:
        return pd.DataFrame(columns=["ticker", "per", "pbr", "eps", "bps", "div"])

    df = df.reset_index().rename(
        columns={
            "티커": "ticker",
            "BPS": "bps",
            "PER": "per",
            "PBR": "pbr",
            "EPS": "eps",
            "DIV": "div",
        }
    )
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


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["ticker", "date"]).copy()
    grouped = df.groupby("ticker")

    df["ret_60"] = grouped["close"].pct_change(60)
    df["ret_120"] = grouped["close"].pct_change(120)
    df["ret_180"] = grouped["close"].pct_change(180)
    df["ma20"] = grouped["close"].transform(lambda x: x.rolling(20).mean())
    df["ma60"] = grouped["close"].transform(lambda x: x.rolling(60).mean())
    df["avg_trading_value_20"] = grouped["trading_value"].transform(lambda x: x.rolling(20).mean())
    df["trend_ok"] = (df["close"] > df["ma20"]) & (df["ma20"] > df["ma60"])
    return df


def zscore(series: pd.Series) -> pd.Series:
    series = series.replace([np.inf, -np.inf], np.nan)
    if series.notna().sum() < 2:
        return pd.Series(0.0, index=series.index)
    filled = series.fillna(series.median())
    std = filled.std(ddof=0)
    if std == 0 or np.isnan(std):
        return pd.Series(0.0, index=series.index)
    return (filled - filled.mean()) / std


def score_candidates(snapshot: pd.DataFrame, fundamental: pd.DataFrame) -> pd.DataFrame:
    df = snapshot.merge(fundamental, on="ticker", how="left").copy()

    df = df[
        (df["close"] >= MIN_PRICE)
        & (df["close"] <= MAX_PRICE)
        & (df["avg_trading_value_20"] >= MIN_AVG_TRADING_VALUE_20)
        & (df["trend_ok"])
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
    momentum_score = (
        0.35 * zscore(df["ret_60"])
        + 0.35 * zscore(df["ret_120"])
        + 0.30 * zscore(df["ret_180"])
    )

    # 기본 전략을 소액에 맞춘 형태: 모멘텀 비중은 높이되 가치/퀄리티를 함께 본다.
    df["score"] = (
        0.45 * momentum_score
        + 0.25 * value_score
        + 0.20 * quality_score
        + 0.10 * dividend_score
    )

    return df.sort_values("score", ascending=False).reset_index(drop=True)


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


def backtest():
    tickers = get_market_cap_universe(END)
    if not tickers:
        raise RuntimeError(f"No tickers returned for END={END}. Check market/date settings.")
    print(f"Loaded market-cap universe: {len(tickers)} tickers")

    frames = []
    failed = []
    empty = []
    for ticker in tickers:
        try:
            price_df = get_price(ticker)
            if price_df.empty:
                empty.append(ticker)
                continue
            frames.append(price_df)
        except Exception as exc:
            failed.append((ticker, str(exc)))

    if not frames:
        raise RuntimeError(
            "No price data was collected. "
            f"tickers={len(tickers)}, empty={len(empty)}, failed={len(failed)}, "
            f"sample_failed={failed[:5]}"
        )

    if failed:
        print(f"[WARN] failed tickers: {len(failed)} / {len(tickers)}; sample={failed[:3]}")
    if empty:
        print(f"[WARN] empty tickers: {len(empty)} / {len(tickers)}; sample={empty[:10]}")

    price = pd.concat(frames, ignore_index=True)
    price["date"] = pd.to_datetime(price["date"])
    price = compute_features(price)

    dates = sorted(price["date"].unique())
    price_by_date = {dt: day.set_index("ticker") for dt, day in price.groupby("date")}
    index_df = get_index_data()

    fundamental = get_fundamental_snapshot(END)

    cash = float(INITIAL_CASH)
    holdings = {}  # 티커 -> 수량
    avg_price = {}
    entry_date = {}
    trades = []
    equity_curve = []

    warmup_days = max(180, MARKET_MA_DAYS + MARKET_SLOPE_DAYS)
    for i in range(warmup_days, len(dates) - 1):
        dt = pd.Timestamp(dates[i])
        next_dt = pd.Timestamp(dates[i + 1])

        today = price_by_date.get(dt, pd.DataFrame())
        next_day = price_by_date.get(next_dt, pd.DataFrame())
        if today.empty or next_day.empty:
            continue

        today_close = today["close"]
        next_open = next_day["open"]
        next_close = next_day["close"]

        # 매일 손절 체크: 신호는 당일 종가, 체결은 다음 거래일 시가.
        for ticker in list(holdings.keys()):
            close_price = safe_get(today_close, ticker)
            open_price = safe_get(next_open, ticker)
            if close_price is None or open_price is None:
                continue
            pnl = close_price / avg_price[ticker] - 1
            if pnl <= STOP_LOSS_PCT:
                qty = holdings.pop(ticker)
                sell_price = open_price * (1 - SLIPPAGE_PCT)
                proceeds = qty * sell_price * (1 - SELL_FEE_PCT - SELL_TAX_PCT)
                cash += proceeds
                avg_price.pop(ticker, None)
                entry_date.pop(ticker, None)
                trades.append(
                    {
                        "date": next_dt,
                        "ticker": ticker,
                        "name": get_ticker_name(ticker),
                        "side": "SELL",
                        "reason": "STOP_LOSS",
                        "price": sell_price,
                        "qty": qty,
                        "cash_after": cash,
                    }
                )

        should_rebalance = (i - warmup_days) % REBALANCE_STEP_DAYS == 0
        if should_rebalance:
            risk_on = is_risk_on(index_df, dt)
            snapshot = today.reset_index()
            ranked = score_candidates(snapshot, fundamental)

            target_rank = dict(zip(ranked["ticker"], ranked.index + 1)) if not ranked.empty else {}
            target_tickers = ranked.head(MAX_POSITIONS)["ticker"].tolist() if risk_on else []

            # 기존 보유 종목은 최소 보유 기간을 지키고, 상위 버퍼 안이면 유지한다.
            for ticker in list(holdings.keys()):
                held_days = (dt - entry_date.get(ticker, dt)).days
                rank = target_rank.get(ticker)
                keep_by_rank = rank is not None and rank <= SELL_RANK_BUFFER and risk_on
                keep_by_min_hold = held_days < MIN_HOLD_DAYS
                if not risk_on:
                    # 시장 위험 구간에서는 최소 며칠만 지나도 현금화한다.
                    if held_days < MARKET_RISK_OFF_SELL_AFTER_DAYS:
                        continue
                elif keep_by_rank or keep_by_min_hold:
                    continue

                open_price = safe_get(next_open, ticker)
                if open_price is None:
                    continue

                qty = holdings.pop(ticker)
                sell_price = open_price * (1 - SLIPPAGE_PCT)
                proceeds = qty * sell_price * (1 - SELL_FEE_PCT - SELL_TAX_PCT)
                cash += proceeds
                avg_price.pop(ticker, None)
                entry_date.pop(ticker, None)
                trades.append(
                    {
                        "date": next_dt,
                        "ticker": ticker,
                        "name": get_ticker_name(ticker),
                        "side": "SELL",
                        "reason": "REBALANCE",
                        "price": sell_price,
                        "qty": qty,
                        "cash_after": cash,
                    }
                )

            # 목표 종목 중 미보유 종목만 매수한다. 전량 교체하지 않는다.
            current_positions = len(holdings)
            slots = max(MAX_POSITIONS - current_positions, 0)
            buy_list = [ticker for ticker in target_tickers if ticker not in holdings][:slots]

            if buy_list and cash > 0:
                budget = cash / len(buy_list)
                for ticker in buy_list:
                    open_price = safe_get(next_open, ticker)
                    if open_price is None:
                        continue
                    buy_price = open_price * (1 + SLIPPAGE_PCT)
                    total_unit_cost = buy_price * (1 + BUY_FEE_PCT)
                    qty = int(budget // total_unit_cost)
                    if qty <= 0:
                        continue
                    cost = qty * total_unit_cost
                    if cost > cash:
                        qty = int(cash // total_unit_cost)
                        cost = qty * total_unit_cost
                    if qty <= 0:
                        continue
                    holdings[ticker] = holdings.get(ticker, 0) + qty
                    avg_price[ticker] = buy_price
                    entry_date[ticker] = next_dt
                    cash -= cost
                    trades.append(
                        {
                            "date": next_dt,
                            "ticker": ticker,
                            "name": get_ticker_name(ticker),
                            "side": "BUY",
                            "reason": "REBALANCE",
                            "price": buy_price,
                            "qty": qty,
                            "cash_after": cash,
                        }
                    )

        market_value = 0.0
        missing_price_count = 0
        for ticker, qty in holdings.items():
            close_price = safe_get(next_close, ticker)
            if close_price is None:
                missing_price_count += 1
                close_price = avg_price.get(ticker, 0)
            market_value += qty * close_price

        equity = cash + market_value
        equity_curve.append(
            {
                "date": next_dt,
                "equity": equity,
                "cash": cash,
                "market_value": market_value,
                "positions": len(holdings),
                "missing_price_count": missing_price_count,
                "holdings": ",".join(sorted(holdings.keys())),
            }
        )

    equity_df = pd.DataFrame(equity_curve)
    trades_df = pd.DataFrame(trades)
    return equity_df, trades_df


def summarize(result: pd.DataFrame, trades: pd.DataFrame):
    result = result.copy()
    result["date"] = pd.to_datetime(result["date"])
    result["daily_ret"] = result["equity"].pct_change().fillna(0)

    total_return = result["equity"].iloc[-1] / result["equity"].iloc[0] - 1
    years = max((result["date"].iloc[-1] - result["date"].iloc[0]).days / 365.25, 1 / 365.25)
    cagr = (result["equity"].iloc[-1] / result["equity"].iloc[0]) ** (1 / years) - 1
    drawdown = result["equity"] / result["equity"].cummax() - 1
    mdd = drawdown.min()
    volatility = result["daily_ret"].std() * np.sqrt(252)
    sharpe = np.nan if volatility == 0 else result["daily_ret"].mean() * 252 / volatility
    turnover_per_year = len(trades) / years if years > 0 else np.nan
    invested_ratio = (result["market_value"] / result["equity"]).mean()

    print(result.tail())
    print("\n=== Performance ===")
    print(f"initial_equity: {result['equity'].iloc[0]:,.0f}")
    print(f"final_equity: {result['equity'].iloc[-1]:,.0f}")
    print(f"total_return: {total_return:.4f}")
    print(f"cagr: {cagr:.4f}")
    print(f"max_drawdown: {mdd:.4f}")
    print(f"annualized_volatility: {volatility:.4f}")
    print(f"sharpe_ratio: {sharpe:.4f}" if not np.isnan(sharpe) else "sharpe_ratio: nan")
    print(f"trades: {len(trades)}")
    print(f"trades_per_year: {turnover_per_year:.2f}")
    print(f"avg_invested_ratio: {invested_ratio:.4f}")
    if not trades.empty:
        print("\n=== Trade count by reason ===")
        print(trades.groupby(["side", "reason"]).size())


if __name__ == "__main__":
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    result, trades = backtest()
    if result.empty:
        raise RuntimeError("Backtest produced no equity rows.")

    result.to_csv(OUTPUT_DIR / "equity_curve.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUTPUT_DIR / "trades.csv", index=False, encoding="utf-8-sig")

    summarize(result, trades)
    print(f"saved: {OUTPUT_DIR / 'equity_curve.csv'}")
    print(f"saved: {OUTPUT_DIR / 'trades.csv'}")