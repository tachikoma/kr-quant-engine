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
ENABLE_TICKER_NAME_LOOKUP = os.environ.get("ENABLE_TICKER_NAME_LOOKUP", "0") == "1"

# KRX 인증 정보 검증
if not HAS_KRX_CREDENTIALS:
    print("⚠️  경고: KRX_ID 또는 KRX_PW 환경 변수가 설정되지 않았습니다.")
    print("   .env 파일을 생성하고 KRX 인증 정보를 설정해주세요.")
    print("   예: cp .env.sample .env && nano .env")
    print()

INITIAL_CASH = 1_000_000
OUTPUT_DIR = Path("outputs_etf_only")

BUY_FEE_PCT = 0.00015
SELL_FEE_PCT = 0.00015
ETF_SELL_TAX_PCT = 0.0
SLIPPAGE_PCT = 0.0005
# 슬리피지 민감도 테스트 옵션(퍼센트 단위, 예: 0.0005 = 5bp)
SLIPPAGE_OPTIONS = [0.0005, 0.001, 0.002, 0.003]

REBALANCE_STEP_DAYS = 20
KOSPI_INDEX_CODE = "1001"
MARKET_MA_DAYS = 120
MARKET_SLOPE_DAYS = 20

# 비교 실험을 위한 시장 필터(risk-on/off) 사용 여부
USE_MARKET_FILTER = True

# ETF 후보군: 장기 생존, 유동성, 국내/해외/섹터 분산을 고려한 기본 리스트.
ETF_LIST = [
    "069500",  # KODEX 200
    "229200",  # KODEX 코스닥150
    "091160",  # KODEX 반도체
    "102110",  # TIGER 200
    "143850",  # TIGER 미국S&P500선물(H)
    "133690",  # TIGER 미국나스닥100
]
ETF_MAX_POSITIONS = 2
REBALANCE_POSITION_OPTIONS = [1, 2, 3]
ETF_SELL_RANK_BUFFER = 3


BENCHMARK_TICKER = "069500"  # KODEX 200


PERIODS = [
    ("2016_2019", "2016-01-01", "2019-12-31"),
    ("2020_2021", "2020-01-01", "2021-12-31"),
    ("2022_2023", "2022-01-01", "2023-12-31"),
    ("2024_2026", "2024-01-01", "2026-04-30"),
]


def get_strategy_config() -> dict:
    """백테스트와 라이브 드라이런 모듈에서 공통으로 쓰는 ETF 전략 설정을 반환한다."""
    return {
        "etf_list": ETF_LIST,
        "max_positions": ETF_MAX_POSITIONS,
        "sell_rank_buffer": ETF_SELL_RANK_BUFFER,
        "rebalance_step_days": REBALANCE_STEP_DAYS,
        "market_index_code": KOSPI_INDEX_CODE,
        "market_ma_days": MARKET_MA_DAYS,
        "market_slope_days": MARKET_SLOPE_DAYS,
        "buy_fee_pct": BUY_FEE_PCT,
        "sell_fee_pct": SELL_FEE_PCT,
        "sell_tax_pct": ETF_SELL_TAX_PCT,
        "default_slippage_pct": SLIPPAGE_PCT,
    }


def get_ticker_name(ticker: str) -> str:
    # pykrx의 종목명 조회는 일부 환경에서 내부 에러 메시지를 직접 출력한다.
    # 백테스트에는 종목명이 필수 정보가 아니므로 기본값은 ticker를 그대로 사용한다.
    # 종목명 저장이 꼭 필요하면 .env에 ENABLE_TICKER_NAME_LOOKUP=1 을 추가한다.
    if not (HAS_KRX_CREDENTIALS and ENABLE_TICKER_NAME_LOOKUP):
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
    try:
        return normalize_ohlcv(stock.get_market_ohlcv_by_date(START, END, ticker), ticker)
    except Exception as e:
        print(f"❌ 오류: 종목 {ticker} 데이터 조회 실패: {str(e)}")
        raise RuntimeError(f"Cannot fetch price data for ticker {ticker}") from e


def get_index_data() -> pd.DataFrame:
    """KOSPI 지수 데이터를 조회하고 기술적 지표를 계산한다."""
    if not HAS_KRX_CREDENTIALS:
        raise RuntimeError(
            "KRX 인증 정보가 필요합니다. KOSPI 지수 데이터를 조회할 수 없습니다.\n"
            "다음 단계를 따르세요:\n"
            "1. .env.sample을 참고하여 .env 파일을 생성하세요\n"
            "2. KRX_ID와 KRX_PW를 설정하세요\n"
            "3. 다시 실행하세요"
        )
    
    try:
        idx = stock.get_index_ohlcv_by_date(START, END, KOSPI_INDEX_CODE)
    except Exception as e:
        raise RuntimeError(
            f"KOSPI 지수 데이터 조회 중 오류 발생: {str(e)}\n"
            "KRX 인증 정보를 확인하고 다시 시도하세요."
        ) from e
    
    if idx is None or idx.empty:
        raise RuntimeError("No KOSPI index data returned.")

    try:
        idx = idx.reset_index().rename(columns={"날짜": "date", "종가": "close"})
    except Exception as e:
        raise RuntimeError(
            f"KOSPI 지수 데이터 포맷 오류: {str(e)}\n"
            "조회한 데이터 구조: {list(idx.columns)}"
        ) from e
    
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


def apply_buy_cost(price: float, slippage: float) -> float:
    return price * (1 + slippage) * (1 + BUY_FEE_PCT)


def apply_sell_value(price: float, qty: int, sell_tax_pct: float, slippage: float) -> float:
    sell_price = price * (1 - slippage)
    return qty * sell_price * (1 - SELL_FEE_PCT - sell_tax_pct)


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
        print(f"[경고] 수집 실패 ETF: {failed[:3]}")
    if empty:
        print(f"[경고] 데이터가 비어 있는 ETF: {empty}")

    price = pd.concat(frames, ignore_index=True)
    price = price.sort_values(["ticker", "date"]).copy()
    grouped = price.groupby("ticker")
    price["ret_60"] = grouped["close"].pct_change(60)
    price["ret_120"] = grouped["close"].pct_change(120)
    price["ma20"] = grouped["close"].transform(lambda x: x.rolling(20).mean())
    price["ma60"] = grouped["close"].transform(lambda x: x.rolling(60).mean())
    price["trend_ok"] = (price["close"] > price["ma20"]) & (price["ma20"] > price["ma60"])
    return price



def rank_etfs(snapshot: pd.DataFrame) -> pd.DataFrame:
    df = snapshot.copy()
    df = df[df["ret_60"].notna() & df["ret_120"].notna() & df["trend_ok"]].copy()
    if df.empty:
        return df

    df["score"] = 0.55 * zscore(df["ret_60"]) + 0.45 * zscore(df["ret_120"])
    return df.sort_values("score", ascending=False).reset_index(drop=True)


def select_target_etfs(snapshot: pd.DataFrame, max_positions: int = ETF_MAX_POSITIONS) -> list[str]:
    """검증된 랭킹 로직으로 최신 스냅샷에서 목표 ETF 티커를 반환한다."""
    ranked = rank_etfs(snapshot)
    if ranked.empty:
        return []
    return ranked.head(max_positions)["ticker"].tolist()


def build_rebalance_orders(
    current_holdings: dict[str, int],
    target_tickers: list[str],
    latest_prices: dict[str, float],
    available_cash: float,
    max_positions: int = ETF_MAX_POSITIONS,
    sell_rank_buffer: int = ETF_SELL_RANK_BUFFER,
    slippage: float = SLIPPAGE_PCT,
) -> list[dict]:
    """
    드라이런 리밸런싱 주문 목록을 생성한다.

    current_holdings: ticker -> 수량
    target_tickers: 선호 순위가 반영된 목표 티커 목록(상위 -> 하위)
    latest_prices: ticker -> 체결 기준 가격
    available_cash: 리밸런싱 전 사용 가능 현금

    이 함수는 실제 주문을 실행하지 않고, 의도된 SELL/BUY 주문만 반환한다.
    """
    orders = []
    holdings = dict(current_holdings)
    cash = float(available_cash)
    target_set = set(target_tickers[:max_positions])
    target_rank = {ticker: idx + 1 for idx, ticker in enumerate(target_tickers)}

    for ticker, qty in list(holdings.items()):
        rank = target_rank.get(ticker)
        keep_by_rank = rank is not None and rank <= sell_rank_buffer
        if keep_by_rank:
            continue

        price = latest_prices.get(ticker)
        if price is None or pd.isna(price) or price <= 0:
            continue

        estimated_value = apply_sell_value(price, qty, ETF_SELL_TAX_PCT, slippage)
        cash += estimated_value
        orders.append(
            {
                "side": "SELL",
                "ticker": ticker,
                "qty": qty,
                "reference_price": price,
                "estimated_value": estimated_value,
                "reason": "ETF_REBALANCE",
            }
        )
        holdings.pop(ticker, None)

    slots = max(max_positions - len(holdings), 0)
    buy_list = [ticker for ticker in target_tickers if ticker in target_set and ticker not in holdings][:slots]
    if not buy_list or cash <= 0:
        return orders

    budget = cash / len(buy_list)
    for ticker in buy_list:
        price = latest_prices.get(ticker)
        if price is None or pd.isna(price) or price <= 0:
            continue

        unit_cost = apply_buy_cost(price, slippage)
        qty = int(budget // unit_cost)
        if qty <= 0:
            continue

        cost = qty * unit_cost
        if cost > cash:
            qty = int(cash // unit_cost)
            cost = qty * unit_cost
        if qty <= 0:
            continue

        cash -= cost
        orders.append(
            {
                "side": "BUY",
                "ticker": ticker,
                "qty": qty,
                "reference_price": price,
                "estimated_value": cost,
                "reason": "ETF_REBALANCE",
            }
        )

    return orders


def run_etf_strategy(initial_cash: float, common_dates: list[pd.Timestamp], index_df: pd.DataFrame, use_market_filter: bool = True, max_positions: int = ETF_MAX_POSITIONS, slippage: float = SLIPPAGE_PCT):
    price = load_etf_price()
    price_by_date = {dt: day.set_index("ticker") for dt, day in price.groupby("date")}

    cash = float(initial_cash)
    holdings = {}
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
            risk_on = is_risk_on(index_df, dt) if use_market_filter else True
            ranked = rank_etfs(today.reset_index())
            target_rank = dict(zip(ranked["ticker"], ranked.index + 1)) if not ranked.empty else {}
            targets = ranked.head(max_positions)["ticker"].tolist() if risk_on else []

            for ticker in list(holdings.keys()):
                rank = target_rank.get(ticker)
                keep_by_rank = rank is not None and rank <= ETF_SELL_RANK_BUFFER and risk_on
                if keep_by_rank:
                    continue

                open_price = safe_get(next_open, ticker)
                if open_price is None:
                    continue

                qty = holdings.pop(ticker)
                cash += apply_sell_value(open_price, qty, ETF_SELL_TAX_PCT, slippage)
                trades.append(
                    {
                        "date": next_dt,
                        "ticker": ticker,
                        "name": get_ticker_name(ticker),
                        "side": "SELL",
                        "reason": "ETF_REBALANCE",
                        "qty": qty,
                        "price": open_price,
                        "cash_after": cash,
                    }
                )

            slots = max(max_positions - len(holdings), 0)
            buy_list = [ticker for ticker in targets if ticker not in holdings][:slots]
            if buy_list and cash > 0:
                budget = cash / len(buy_list)
                for ticker in buy_list:
                    open_price = safe_get(next_open, ticker)
                    if open_price is None:
                        continue

                    unit_cost = apply_buy_cost(open_price, slippage)
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
                    cash -= cost
                    trades.append(
                        {
                            "date": next_dt,
                            "ticker": ticker,
                            "name": get_ticker_name(ticker),
                            "side": "BUY",
                            "reason": "ETF_REBALANCE",
                            "qty": qty,
                            "price": open_price,
                            "cash_after": cash,
                        }
                    )

        market_value = 0.0
        for ticker, qty in holdings.items():
            close_price = safe_get(next_close, ticker)
            if close_price is not None:
                market_value += qty * close_price

        equity_rows.append(
            {
                "date": next_dt,
                "equity": cash + market_value,
                "cash": cash,
                "market_value": market_value,
                "holdings": ",".join(sorted(holdings.keys())),
            }
        )

    return pd.DataFrame(equity_rows), pd.DataFrame(trades)


def run_kodex200_buy_and_hold(initial_cash: float, common_dates: list[pd.Timestamp]) -> pd.DataFrame:
    price = get_price(BENCHMARK_TICKER)
    if price.empty:
        raise RuntimeError(f"No benchmark data for {BENCHMARK_TICKER}")

    price_by_date = {dt: day.set_index("ticker") for dt, day in price.groupby("date")}
    cash = float(initial_cash)
    qty = 0
    bought = False
    equity_rows = []

    for i, _dt in enumerate(common_dates[:-1]):
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
            unit_cost = apply_buy_cost(open_price, SLIPPAGE_PCT)
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


def calc_period_stats(df: pd.DataFrame, equity_col: str, period_name: str, start: str, end: str) -> dict | None:
    temp = df[["date", equity_col]].dropna().copy()
    temp["date"] = pd.to_datetime(temp["date"])
    start_dt = pd.Timestamp(start)
    end_dt = pd.Timestamp(end)
    temp = temp[(temp["date"] >= start_dt) & (temp["date"] <= end_dt)].sort_values("date")

    if len(temp) < 2:
        return None

    temp["daily_ret"] = temp[equity_col].pct_change().fillna(0)
    total_return = temp[equity_col].iloc[-1] / temp[equity_col].iloc[0] - 1
    years = max((temp["date"].iloc[-1] - temp["date"].iloc[0]).days / 365.25, 1 / 365.25)
    cagr = (temp[equity_col].iloc[-1] / temp[equity_col].iloc[0]) ** (1 / years) - 1
    drawdown = temp[equity_col] / temp[equity_col].cummax() - 1
    mdd = drawdown.min()
    volatility = temp["daily_ret"].std() * np.sqrt(252)
    sharpe = np.nan if volatility == 0 else temp["daily_ret"].mean() * 252 / volatility

    return {
        "period": period_name,
        "start": str(temp["date"].iloc[0].date()),
        "end": str(temp["date"].iloc[-1].date()),
        "strategy": equity_col,
        "initial": temp[equity_col].iloc[0],
        "final": temp[equity_col].iloc[-1],
        "total_return": total_return,
        "cagr": cagr,
        "mdd": mdd,
        "volatility": volatility,
        "sharpe": sharpe,
    }


def build_period_comparison(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for period_name, start, end in PERIODS:
        benchmark_stats = calc_period_stats(df, "equity_kodex200_bh", period_name, start, end)
        if benchmark_stats is not None:
            benchmark_stats["strategy"] = "KODEX200_BuyHold"
            rows.append(benchmark_stats)

        for slip in SLIPPAGE_OPTIONS:
            label = f"slip_{int(slip * 10000)}bp"
            equity_col = f"equity_{label}"
            if equity_col not in df.columns:
                continue
            slip_stats = calc_period_stats(df, equity_col, period_name, start, end)
            if slip_stats is not None:
                slip_stats["strategy"] = f"ETF_{label}"
                rows.append(slip_stats)

    if not rows:
        return pd.DataFrame()

    comparison = pd.DataFrame(rows)
    comparison = comparison[
        [
            "period",
            "strategy",
            "start",
            "end",
            "initial",
            "final",
            "total_return",
            "cagr",
            "mdd",
            "volatility",
            "sharpe",
        ]
    ]
    return comparison


def run():
    index_df = get_index_data()
    common_dates = list(index_df["date"])

    results = []
    trades_dict = {}

    for slip in SLIPPAGE_OPTIONS:
        curve, trades = run_etf_strategy(INITIAL_CASH, common_dates, index_df, True, 2, slip)
        label = f"slip_{int(slip*10000)}bp"
        curve = curve.rename(
            columns={
                "equity": f"equity_{label}",
                "market_value": f"market_value_{label}",
            }
        )
        curve = curve[["date", f"equity_{label}", f"market_value_{label}"]]
        results.append(curve)
        trades_dict[label] = trades

    benchmark_curve = run_kodex200_buy_and_hold(INITIAL_CASH, common_dates)
    benchmark_curve = benchmark_curve[["date", "equity_kodex200_bh"]]

    result = benchmark_curve
    for curve in results:
        result = pd.merge(result, curve, on="date", how="outer")

    result = result.sort_values("date")

    for slip in SLIPPAGE_OPTIONS:
        label = f"slip_{int(slip*10000)}bp"
        result[f"equity_{label}"] = result[f"equity_{label}"].ffill().fillna(INITIAL_CASH)
        result[f"market_value_{label}"] = result[f"market_value_{label}"].ffill().fillna(0)

    result["equity_kodex200_bh"] = result["equity_kodex200_bh"].ffill().fillna(INITIAL_CASH)

    return result, trades_dict



def summarize(df: pd.DataFrame, trades_dict: dict):
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])

    rows = []
    benchmark_stats = calc_stats(df, "equity_kodex200_bh")
    rows.append({"strategy": "KODEX200_BuyHold", **benchmark_stats})

    for slip in SLIPPAGE_OPTIONS:
        label = f"slip_{int(slip*10000)}bp"
        stats = calc_stats(df, f"equity_{label}")
        rows.append({"strategy": f"ETF_{label}", **stats})

    comparison = pd.DataFrame(rows)

    print(df.tail())
    print("\n=== 슬리피지 민감도 테스트 ===")
    display_cols = ["strategy", "final", "total_return", "cagr", "mdd", "volatility", "sharpe"]
    print(comparison[display_cols].to_string(index=False, float_format=lambda x: f"{x:,.4f}"))

    for slip in SLIPPAGE_OPTIONS:
        label = f"slip_{int(slip*10000)}bp"
        trades = trades_dict[label]
        invested_ratio = (df[f"market_value_{label}"] / df[f"equity_{label}"]).mean()

        print(f"\n=== ETF {label} 상세 ===")
        print(f"거래 수: {len(trades)}")
        print(f"평균 투자 비중: {invested_ratio:.4f}")
def main():
    OUTPUT_DIR.mkdir(exist_ok=True)

    try:
        result, trades_dict = run()
    except RuntimeError as e:
        print(f"\n❌ 실행 중 오류 발생:\n{str(e)}")
        exit(1)
    except Exception as e:
        print(f"\n❌ 예기치 않은 오류 발생: {str(e)}")
        import traceback
        traceback.print_exc()
        exit(1)
    
    if result.empty:
        raise RuntimeError("ETF-only backtest produced no result rows.")

    result.to_csv(OUTPUT_DIR / "etf_equity_curve.csv", index=False, encoding="utf-8-sig")
    # 필요 시 슬리피지별 체결 내역 저장
    for slip in SLIPPAGE_OPTIONS:
        label = f"slip_{int(slip*10000)}bp"
        trades_dict[label].to_csv(OUTPUT_DIR / f"etf_trades_{label}.csv", index=False, encoding="utf-8-sig")
    cols = ["date", "equity_kodex200_bh"] + [f"equity_slip_{int(s*10000)}bp" for s in SLIPPAGE_OPTIONS]
    result[cols].to_csv(OUTPUT_DIR / "slippage_comparison.csv", index=False, encoding="utf-8-sig")

    summarize(result, trades_dict)
    print(f"저장 완료: {OUTPUT_DIR / 'etf_equity_curve.csv'}")
    for slip in SLIPPAGE_OPTIONS:
        label = f"slip_{int(slip*10000)}bp"
        print(f"저장 완료: {OUTPUT_DIR / f'etf_trades_{label}.csv'}")
    print(f"저장 완료: {OUTPUT_DIR / 'slippage_comparison.csv'}")


if __name__ == "__main__":
    main()