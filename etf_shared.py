from __future__ import annotations

import numpy as np
import pandas as pd

BUY_FEE_PCT = 0.00015
SELL_FEE_PCT = 0.00015
ETF_SELL_TAX_PCT = 0.0

REBALANCE_STEP_DAYS = 20
KOSPI_INDEX_CODE = "1001"
MARKET_MA_DAYS = 120
MARKET_SLOPE_DAYS = 20

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


# (기존 .env 로드 로직 제거) 백테스트 전용 환경 변수는
# 백테스트 모듈에서 관리하도록 이동했습니다.


def get_strategy_config() -> dict:
    """백테스트와 실전에서 공통으로 쓰는 ETF 전략 설정을 반환한다."""
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
        # 기본 슬리피지/스프레드는 백테스트와 실전에서 공통으로 참고할 수 있게 노출합니다.
        # 단위: 비율 (예: 0.0005 == 5bp)
        "default_slippage_pct": 0.0005,
        "spread_pct": 0.0005,
    }


def zscore(series: pd.Series) -> pd.Series:
    series = series.replace([np.inf, -np.inf], np.nan)
    if series.notna().sum() < 2:
        return pd.Series(0.0, index=series.index)
    filled = series.fillna(series.median())
    std = filled.std(ddof=0)
    if std == 0 or np.isnan(std):
        return pd.Series(0.0, index=series.index)
    return (filled - filled.mean()) / std


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


def apply_buy_cost(price: float, slippage: float) -> float:
    return price * (1 + slippage) * (1 + BUY_FEE_PCT)


def apply_sell_value(price: float, qty: int, sell_tax_pct: float, slippage: float) -> float:
    sell_price = price * (1 - slippage)
    return qty * sell_price * (1 - SELL_FEE_PCT - sell_tax_pct)


def build_rebalance_orders(
    current_holdings: dict[str, int],
    target_tickers: list[str],
    latest_prices: dict[str, float],
    available_cash: float,
    latest_buy_prices: dict[str, float] | None = None,
    latest_sell_prices: dict[str, float] | None = None,
    max_positions: int = ETF_MAX_POSITIONS,
    sell_rank_buffer: int = ETF_SELL_RANK_BUFFER,
    slippage: float = 0.0005,
    allow_empty_target_sell: bool = False,
    generate_orders: bool = True,
) -> list[dict]:
    """리밸런싱 주문 목록을 생성한다."""
    orders = []
    holdings = dict(current_holdings)
    cash = float(available_cash)
    buy_prices = latest_buy_prices or latest_prices
    sell_prices = latest_sell_prices or latest_prices
    target_set = set(target_tickers[:max_positions])
    target_rank = {ticker: idx + 1 for idx, ticker in enumerate(target_tickers)}

    # generate_orders=False이면 리밸런싱이 비활성화된 상태이므로 주문 생성을 건너뜁니다.
    if not generate_orders:
        print("[주문계산] generate_orders=False — 리밸런싱 미실행으로 주문 생성 생략")
        return []

    # 빈 target에 대해 '빈 target이면 전량매도'로 해석되는 것을 방지하기 위한 방어 로직
    if not target_tickers and not allow_empty_target_sell:
        print("[주문계산] target이 비어있고 빈 target에서 매도 허용이 아니므로 주문 생성 생략")
        return []

    print(
        f"[주문계산] 시작 | 보유={len(holdings)}개, 목표={len(target_tickers)}개, "
        f"max_positions={max_positions}, 예수금={cash:,.0f}"
    )

    for ticker, qty in list(holdings.items()):
        rank = target_rank.get(ticker)
        keep_by_rank = rank is not None and rank <= sell_rank_buffer
        if keep_by_rank:
            print(f"[주문계산][매도스킵] {ticker} 보유유지 (랭크={rank}, 버퍼={sell_rank_buffer})")
            continue

        price = sell_prices.get(ticker)
        if price is None or pd.isna(price) or price <= 0:
            print(f"[주문계산][매도스킵] {ticker} 매도가격 없음/비정상 (sell_price={price})")
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
    print(f"[주문계산] 매수 슬롯={slots}, 매수후보={buy_list}")
    if not buy_list or cash <= 0:
        if not buy_list:
            print("[주문계산] 매수 대상 없음 → 주문 생성 종료")
        if cash <= 0:
            print(f"[주문계산] 예수금 부족(cash={cash:,.0f}) → 주문 생성 종료")
        return orders

    budget = cash / len(buy_list)
    print(f"[주문계산] 종목당 예산={budget:,.0f}")
    for ticker in buy_list:
        price = buy_prices.get(ticker)
        if price is None or pd.isna(price) or price <= 0:
            print(f"[주문계산][매수스킵] {ticker} 매수가격 없음/비정상 (buy_price={price})")
            continue

        unit_cost = apply_buy_cost(price, slippage)
        qty = int(budget // unit_cost)
        if qty <= 0:
            print(
                f"[주문계산][매수스킵] {ticker} 수량 0 "
                f"(budget={budget:,.0f}, unit_cost={unit_cost:,.0f})"
            )
            continue

        cost = qty * unit_cost
        if cost > cash:
            qty = int(cash // unit_cost)
            cost = qty * unit_cost
        if qty <= 0:
            print(
                f"[주문계산][매수스킵] {ticker} 잔여예수금 부족 "
                f"(cash={cash:,.0f}, unit_cost={unit_cost:,.0f})"
            )
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

    print(
        f"[주문계산] 완료 | 매도={sum(1 for o in orders if o.get('side') == 'SELL')}건, "
        f"매수={sum(1 for o in orders if o.get('side') == 'BUY')}건"
    )

    return orders
