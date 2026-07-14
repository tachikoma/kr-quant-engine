from __future__ import annotations

import logging
import os
import numpy as np
import pandas as pd

from config_utils import parse_pct_env
from etf_distributions import add_total_return_price

logger = logging.getLogger(__name__)

BUY_FEE_PCT = 0.00015
SELL_FEE_PCT = 0.00015
ETF_SELL_TAX_PCT = 0.0
ETF_TAXABLE_SELL_TAX_PCT = 0.154


def _parse_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return int(default)
    text = str(raw).strip()
    if "#" in text:
        text = text[: text.index("#")].strip()
    if not text:
        return int(default)
    try:
        return int(text.replace("_", ""))
    except Exception:
        print(f"⚠️ 환경변수 {name} 파싱 실패: '{raw}' — 기본값({default}) 사용")
        return int(default)


def _parse_threshold_dict_env(name: str, default: dict[str, float]) -> dict[str, float]:
    """환경변수에서 'key=value,key2=value2' 형태의 딕셔너리를 파싱합니다."""
    raw = os.environ.get(name)
    if not raw:
        return default
    result = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k or not v:
            continue
        try:
            result[k] = float(v)
        except ValueError:
            continue
    return result or default


# 현재 전략에서 매매차익 과세를 반영할 기타 ETF 후보입니다.
TAXABLE_ETF_TICKERS = {
    "143850",  # KODEX 미국S&P500선물(H)
    "360200",  # ACE 미국S&P500
    "360750",  # TIGER 미국S&P500
    "133690",  # TIGER 미국나스닥100
    "472150",  # TIGER 배당커버드콜액티브
    "486290",  # TIGER 미국나스닥100타겟데일리커버드콜
    "498400",  # KODEX 200타겟위클리커버드콜
    "411060",  # ACE KRX금현물
}

REBALANCE_STEP_DAYS = 10
KOSPI_INDEX_CODE = "1001"
MARKET_MA_DAYS = 120
MARKET_SLOPE_DAYS = 20

ETF_LIST = [
    "069500",  # KODEX 200
    # "122630",  # KODEX 레버리지
    # "229200",  # KODEX 코스닥150
    "091160",  # KODEX 반도체
    "102110",  # TIGER 200
    "0101N0",  # RISE AI전력인프라
    "463250",  # TIGER K방산&우주
    "143850",  # TIGER 미국S&P500선물(H)
    "360200",  # ACE 미국S&P500
    "360750",  # TIGER 미국S&P500
    "133690",  # TIGER 미국나스닥100
    # "243880",  # TIGER 200IT레버리지
    "161510",  # PLUS 고배당주
    "091170",  # KODEX 은행
    "472150",  # TIGER 배당커버드콜액티브
    "486290",  # TIGER 미국나스닥100타겟데일리커버드콜
    "498400",  # KODEX 200타겟위클리커버드콜
    "411060",  # ACE KRX금현물
    "367760",  # RISE 네트워크인프라
]

# 환경변수 `ETF_LIST`가 쉼표로 전달되면 모듈 로드 시점에 기본 후보풀을 재정의합니다.
env_list = os.environ.get("ETF_LIST")
if env_list:
    parsed = [t.strip() for t in env_list.split(",") if t.strip()]
    if parsed:
        ETF_LIST = parsed
        print(f"[etf_shared] ETF_LIST overridden from env: {len(ETF_LIST)} tickers")
        # KRX 분류 데이터로 TAXABLE_ETF_TICKERS 자동 산출 (KRX_ID/KRX_PW 필요)
        try:
            from pykrx_utils import get_taxable_tickers

            taxable = get_taxable_tickers(ticker_subset=set(ETF_LIST))
            if taxable is not None:
                TAXABLE_ETF_TICKERS = taxable
                print(
                    f"[etf_shared] TAXABLE_ETF_TICKERS auto-computed from KRX data: {len(TAXABLE_ETF_TICKERS)} tickers"
                )
            else:
                # KRX API 실패 → hardcoded set에서 ETF_LIST에 있는 것만 유지
                TAXABLE_ETF_TICKERS = {t for t in TAXABLE_ETF_TICKERS if t in ETF_LIST}
                print(
                    f"[etf_shared] TAXABLE_ETF_TICKERS fallback (filtered hardcoded): {len(TAXABLE_ETF_TICKERS)} tickers"
                )
        except Exception:
            TAXABLE_ETF_TICKERS = {t for t in TAXABLE_ETF_TICKERS if t in ETF_LIST}

# TAXABLE_ETF_TICKERS env var가 명시되면 최우선으로 오버라이드
env_taxable = os.environ.get("TAXABLE_ETF_TICKERS")
if env_taxable:
    parsed_taxable = {t.strip() for t in env_taxable.split(",") if t.strip()}
    if parsed_taxable:
        TAXABLE_ETF_TICKERS = parsed_taxable
        print(
            f"[etf_shared] TAXABLE_ETF_TICKERS overridden from env: {len(TAXABLE_ETF_TICKERS)} tickers"
        )
ETF_MAX_POSITIONS = 2
ETF_SELL_RANK_BUFFER = 3

# ETF 유동성 필터: 최소 평균 일 거래대금 (원). 리밸런싱 snapshot 시점에 liquidity_ok=False면 랭킹에서 제외.
MIN_AVG_TRADING_VALUE = _parse_int_env("MIN_AVG_TRADING_VALUE", 1_000_000_000)
MIN_LISTING_DAYS = _parse_int_env("MIN_LISTING_DAYS", 60)
MAX_PREMIUM_DISCOUNT = parse_pct_env("MAX_PREMIUM_DISCOUNT", 0.02)
MAX_LIVE_SPREAD_PCT = parse_pct_env("MAX_LIVE_SPREAD_PCT", 0.005)

# 그룹별 NAV 괴리율 임계값
# 기본값은 MAX_PREMIUM_DISCOUNT와 동일한 2%로 설정.
# 해외 ETF/커버드콜의 구조적 괴리를 반영하려면 env var로 완화 가능.
ETF_DEVIATION_THRESHOLD_BY_GROUP: dict[str, float] = _parse_threshold_dict_env(
    "ETF_DEVIATION_THRESHOLD_BY_GROUP",
    {
        "domestic_equity": 0.02,
        "foreign_investment": 0.02,
        "commodity": 0.02,
    },
)

# 티커별 NAV 괴리율 override (커버드콜 등 특수 ETF)
ETF_DEVIATION_THRESHOLD_BY_TICKER: dict[str, float] = _parse_threshold_dict_env(
    "ETF_DEVIATION_THRESHOLD_BY_TICKER",
    {
        "472150": 0.02,  # TIGER 배당커버드콜액티브
        "486290": 0.02,  # TIGER 미국나스닥100타겟데일리커버드콜
        "498400": 0.02,  # KODEX 200타겟위클리커버드콜
    },
)

# ETF 그룹 분류: 그룹별 시장필터 override에 사용
# foreign_investment / commodity 그룹은 KOSPI risk_off여도 보유/매수 가능
ETF_TICKER_GROUPS: dict[str, str] = {
    "069500": "domestic_equity",  # KODEX 200
    "091160": "domestic_equity",  # KODEX 반도체
    "102110": "domestic_equity",  # TIGER 200
    "0101N0": "domestic_equity",  # RISE AI전력인프라
    "463250": "domestic_equity",  # TIGER K방산&우주
    "161510": "domestic_equity",  # PLUS 고배당주
    "091170": "domestic_equity",  # KODEX 은행
    "367760": "domestic_equity",  # RISE 네트워크인프라
    "143850": "foreign_investment",  # TIGER 미국S&P500선물(H)
    "360200": "foreign_investment",  # ACE 미국S&P500
    "360750": "foreign_investment",  # TIGER 미국S&P500
    "133690": "foreign_investment",  # TIGER 미국나스닥100
    "472150": "foreign_investment",  # TIGER 배당커버드콜액티브
    "486290": "foreign_investment",  # TIGER 미국나스닥100타겟데일리커버드콜
    "498400": "foreign_investment",  # KODEX 200타겟위클리커버드콜
    "411060": "commodity",  # ACE KRX금현물
}

# KOSPI risk_off여도 거래를 허용할 그룹 (외국 투자, 원자재는 방어자산 역할)
GROUP_RISK_OVERRIDE: set[str] = {"foreign_investment", "commodity"}


def get_etf_group(ticker: str) -> str:
    return ETF_TICKER_GROUPS.get(ticker, "domestic_equity")


def get_deviation_threshold(ticker: str) -> float:
    """ETF별 NAV 괴리율 임계값을 반환한다.

    우선순위:
    1. 티커별 override (ETF_DEVIATION_THRESHOLD_BY_TICKER)
    2. 그룹별 임계값 (ETF_DEVIATION_THRESHOLD_BY_GROUP)
    3. 전역 기본값 (MAX_PREMIUM_DISCOUNT)
    """
    ticker = str(ticker)
    if ticker in ETF_DEVIATION_THRESHOLD_BY_TICKER:
        return ETF_DEVIATION_THRESHOLD_BY_TICKER[ticker]
    group = get_etf_group(ticker)
    if group in ETF_DEVIATION_THRESHOLD_BY_GROUP:
        return ETF_DEVIATION_THRESHOLD_BY_GROUP[group]
    return max(float(MAX_PREMIUM_DISCOUNT), 0.0)


def is_ticker_risk_on(ticker: str, kospi_risk_on: bool) -> bool:
    if kospi_risk_on:
        return True
    return get_etf_group(ticker) in GROUP_RISK_OVERRIDE


# (기존 .env 로드 로직 제거) 백테스트 전용 환경 변수는
# 백테스트 모듈에서 관리하도록 이동했습니다.


def get_strategy_config() -> dict:
    """백테스트와 실전에서 공통으로 쓰는 ETF 전략 설정을 반환한다."""
    return {
        "etf_list": ETF_LIST,
        "max_positions": ETF_MAX_POSITIONS,
        "sell_rank_buffer": ETF_SELL_RANK_BUFFER,
        "rebalance_step_days": int(os.environ.get("REBALANCE_STEP_DAYS", str(REBALANCE_STEP_DAYS))),
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
        "return_basis": os.environ.get("ETF_RETURN_BASIS", "price").strip().lower(),
        "min_listing_days": MIN_LISTING_DAYS,
        "max_premium_discount": MAX_PREMIUM_DISCOUNT,
        "deviation_threshold_by_group": dict(ETF_DEVIATION_THRESHOLD_BY_GROUP),
        "deviation_threshold_by_ticker": dict(ETF_DEVIATION_THRESHOLD_BY_TICKER),
        "min_avg_trading_value": MIN_AVG_TRADING_VALUE,
        "max_live_spread_pct": MAX_LIVE_SPREAD_PCT,
        "target_weight_rebalance": os.environ.get("TARGET_WEIGHT_REBALANCE", "0") == "1",
        "rebalance_band_pct": parse_pct_env("REBALANCE_BAND_PCT", 0.05),
        "trim_overweight_positions": os.environ.get("TRIM_OVERWEIGHT_POSITIONS", "0")
        == "1",
        # risk_off 시 전량 매도할지 보유 유지할지 결정
        # True: 전량 매도 (실전 기본), False: 보유 유지 (기존 백테스트 기본)
        "liquidate_on_risk_off": os.environ.get("LIQUIDATE_ON_RISK_OFF", "1") == "1",
    }


def add_liquidity_flag(price: pd.DataFrame) -> pd.DataFrame:
    price = price.sort_values(["ticker", "date"]).copy()
    # look-ahead bias 방지: 각 날짜 기준 trailing 60일 평균(최소 20일)으로 유동성 판단
    trailing_avg_tv = price.groupby("ticker")["trading_value"].transform(
        lambda x: x.rolling(60, min_periods=20).mean()
    )
    # trailing 평균이 MIN_AVG_TRADING_VALUE 이상이면 liquidity_ok=True
    # 초기 20일 미만 데이터는 평균 산출 전이므로 제외한다.
    price["liquidity_ok"] = trailing_avg_tv.notna() & (trailing_avg_tv >= MIN_AVG_TRADING_VALUE)
    price["avg_trading_value_60"] = trailing_avg_tv
    return price


def add_listing_flag(
    price: pd.DataFrame, listing_dates: dict[str, str] | None = None
) -> pd.DataFrame:
    price = price.sort_values(["ticker", "date"]).copy()
    min_days = max(int(MIN_LISTING_DAYS), 0)
    if min_days <= 0:
        price["listing_ok"] = True
        price["listing_days"] = pd.NA
        return price

    listing_dates = listing_dates or {}
    if not listing_dates:
        price["listing_ok"] = True
        price["listing_days"] = pd.NA
        return price

    market_dates = pd.Index(sorted(pd.to_datetime(price["date"].dropna().unique())))
    market_values = market_dates.values.astype("datetime64[ns]")
    date_values = pd.to_datetime(price["date"], errors="coerce").values.astype("datetime64[ns]")
    date_pos = np.searchsorted(market_values, date_values, side="left")

    raw_listing = price["ticker"].astype(str).map(listing_dates)
    listing_ts = pd.to_datetime(raw_listing, format="%Y%m%d", errors="coerce")
    listing_values = listing_ts.values.astype("datetime64[ns]")
    listing_pos = np.searchsorted(market_values, listing_values, side="left")
    trading_age = date_pos - listing_pos + 1

    known_listing = listing_ts.notna()
    price["listing_days"] = pd.NA
    price.loc[known_listing, "listing_days"] = trading_age[known_listing]
    price["listing_ok"] = True
    price.loc[known_listing, "listing_ok"] = trading_age[known_listing] >= min_days
    return price


def add_deviation_flag(price: pd.DataFrame) -> pd.DataFrame:
    price = price.copy()
    price["premium_discount"] = np.nan
    price["deviation_threshold"] = np.nan
    price["deviation_ok"] = True
    if "nav" not in price.columns:
        return price

    close = pd.to_numeric(price.get("close"), errors="coerce")
    nav = pd.to_numeric(price.get("nav"), errors="coerce")
    valid_nav = nav.notna() & (nav > 0) & close.notna()
    if not valid_nav.any():
        return price

    # 티커별/그룹별 임계값 적용
    tickers = price["ticker"].astype(str)
    thresholds = tickers.map(get_deviation_threshold)

    deviation = (close - nav) / nav
    price.loc[valid_nav, "premium_discount"] = deviation[valid_nav]
    price.loc[valid_nav, "deviation_threshold"] = thresholds[valid_nav]
    price.loc[valid_nav, "deviation_ok"] = deviation[valid_nav].abs() <= thresholds[valid_nav]
    return price


def add_price_basis_columns(price: pd.DataFrame) -> pd.DataFrame:
    """``close_adj`` 컬럼을 ``ETF_RETURN_BASIS``에 따라 설정한다.

    - ``price``: 원래 종가 그대로 사용.
    - ``nav``: NAV가 있으면 NAV, 없으면 종가로 폴백.
    - ``total_return``: 분배금 즉시 재투자 total-return 지수를 사용.
    """
    price = price.copy()
    basis = os.environ.get("ETF_RETURN_BASIS", "price").strip().lower()
    if basis == "total_return":
        price = add_total_return_price(price)
        price["close_adj"] = price["close_total_return"]
        logger.info("[etf_shared] return basis = total_return (현금분배금 즉시 재투자)")
        return price
    if basis == "nav" and "nav" in price.columns:
        nav = pd.to_numeric(price["nav"], errors="coerce")
        if nav.notna().any():
            close = pd.to_numeric(price["close"], errors="coerce")
            price["close_adj"] = nav.where(nav.notna() & (nav > 0), close)
            logger.info("[etf_shared] return basis = NAV (총수익률 근사, 분배형 ETF는 한계 있음)")
            return price
        logger.info("[etf_shared] ETF_RETURN_BASIS=nav 이나 NAV 데이터 없음 \u2192 price 폴백")
        price["close_adj"] = price["close"]
        return price

    if basis != "price":
        raise ValueError(f"지원하지 않는 ETF_RETURN_BASIS={basis!r}; price|nav|total_return 사용")
    price["close_adj"] = price["close"]
    return price


def update_last_valid_prices(
    last_valid_prices: dict[str, float], prices: pd.Series | dict | None
) -> None:
    """양수인 최신 가격만 직전 유효가격 저장소에 반영한다."""
    if prices is None:
        return
    items = prices.items() if hasattr(prices, "items") else []
    for ticker, raw_price in items:
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            continue
        if pd.notna(price) and price > 0:
            last_valid_prices[str(ticker)] = price


def get_valuation_price(
    ticker: str, current_prices: pd.Series | dict | None, last_valid_prices: dict[str, float]
) -> float | None:
    """현재 가격을 우선 사용하고, 결측이면 직전 유효가격을 반환한다."""
    raw_price = None
    if current_prices is not None:
        try:
            raw_price = current_prices.get(ticker)
        except AttributeError:
            raw_price = None
    try:
        price = float(raw_price)
    except (TypeError, ValueError):
        price = None
    if price is not None and pd.notna(price) and price > 0:
        last_valid_prices[str(ticker)] = price
        return price
    return last_valid_prices.get(str(ticker))


def zscore(series: pd.Series) -> pd.Series:
    # replace에서의 암묵적 다운캐스팅 경고를 피하기 위해 infer_objects를 적용합니다.
    series = series.replace([np.inf, -np.inf], np.nan).infer_objects(copy=False)
    if series.notna().sum() < 2:
        return pd.Series(0.0, index=series.index)
    filled = series.fillna(series.median())
    std = filled.std(ddof=0)
    if std == 0 or np.isnan(std):
        return pd.Series(0.0, index=series.index)
    return (filled - filled.mean()) / std


def rank_etfs(snapshot: pd.DataFrame) -> pd.DataFrame:
    df = snapshot.copy()
    n = len(df)
    steps: list[str] = []

    if "liquidity_ok" in df.columns:
        n_before = len(df)
        dropped = sorted(df.loc[~df["liquidity_ok"], "ticker"].tolist())
        df = df[df["liquidity_ok"]].copy()
        logger.debug(f"  [필터] liquidity: {n_before}\u2192{len(df)}")
        if dropped:
            logger.debug(f"    탈락: {dropped}")
        steps.append(f"liquidity: {n_before}\u2192{len(df)}")

    if "listing_ok" in df.columns:
        n_before = len(df)
        dropped = sorted(df.loc[~df["listing_ok"], "ticker"].tolist())
        df = df[df["listing_ok"]].copy()
        logger.debug(f"  [필터] listing: {n_before}\u2192{len(df)}")
        if dropped:
            logger.debug(f"    탈락: {dropped}")
        steps.append(f"listing: {n_before}\u2192{len(df)}")

    if "deviation_ok" in df.columns:
        n_before = len(df)
        dropped = sorted(df.loc[~df["deviation_ok"], "ticker"].tolist())
        df = df[df["deviation_ok"]].copy()
        logger.debug(f"  [필터] deviation: {n_before}\u2192{len(df)}")
        if dropped:
            logger.debug(f"    탈락: {dropped}")
        steps.append(f"deviation: {n_before}\u2192{len(df)}")

    n_before = len(df)
    mask = df["ret_60"].notna() & df["ret_120"].notna() & df["trend_ok"]
    dropped = sorted(df.loc[~mask, "ticker"].tolist())
    df = df[mask].copy()
    logger.debug(f"  [필터] trend/return: {n_before}\u2192{len(df)}")
    if dropped:
        logger.debug(f"    탈락: {dropped}")
    steps.append(f"trend/return: {n_before}\u2192{len(df)}")

    logger.info(f"[필터] {n}개 \u2192 {' | '.join(steps)}")

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


def is_taxable_etf(ticker: str, taxable_tickers: set[str] | None = None) -> bool:
    target_set = taxable_tickers if taxable_tickers is not None else TAXABLE_ETF_TICKERS
    return str(ticker) in target_set


def apply_sell_value(
    price: float,
    qty: int,
    sell_tax_pct: float,
    slippage: float,
    cost_basis_per_share: float | None = None,
) -> float:
    sell_price = price * (1 - slippage)
    gross_proceeds = qty * sell_price
    taxable_gain = 0.0
    if cost_basis_per_share is not None:
        taxable_gain = max(0.0, gross_proceeds - qty * float(cost_basis_per_share))
    estimated_tax = taxable_gain * max(float(sell_tax_pct or 0.0), 0.0)
    return gross_proceeds * (1 - SELL_FEE_PCT) - estimated_tax


def _build_target_weight_rebalance_orders(
    *,
    holdings: dict[str, int],
    targets: list[str],
    target_rank: dict[str, int],
    buy_prices: dict[str, float | None],
    sell_prices: dict[str, float | None],
    cash: float,
    cost_basis_map: dict[str, float],
    max_positions: int,
    sell_rank_buffer: int,
    slippage: float,
    sell_tax_pct: float,
    taxable_tickers: set[str] | None,
    max_asset_pct: float | None,
    rebalance_band_pct: float,
    market_order_margin_rate: float,
    display_name,
) -> list[dict]:
    """전체 포트폴리오 평가액을 기준으로 목표비중 주문을 생성한다."""
    orders: list[dict] = []
    planned_holdings = dict(holdings)
    working_cash = float(cash)
    buy_list = targets[:max_positions]

    current_values: dict[str, float] = {}
    for ticker, qty in planned_holdings.items():
        price = sell_prices.get(ticker)
        if price is None or pd.isna(price) or price <= 0:
            continue
        current_values[ticker] = int(qty) * float(price)

    current_equity = working_cash + sum(current_values.values())
    if current_equity <= 0:
        return []

    max_allowed_per_asset = None
    if max_asset_pct is not None and float(max_asset_pct) > 0:
        max_allowed_per_asset = float(max_asset_pct) * current_equity

    band_value = max(float(rebalance_band_pct), 0.0) * current_equity
    buffer_only_value = 0.0
    for ticker, value in current_values.items():
        if (
            ticker not in buy_list
            and target_rank.get(ticker) is not None
            and target_rank[ticker] <= sell_rank_buffer
        ):
            buffer_only_value += (
                min(value, max_allowed_per_asset)
                if max_allowed_per_asset is not None
                else value
            )
    allocatable_equity = max(current_equity - buffer_only_value, 0.0)
    equal_target_value = allocatable_equity / len(buy_list) if buy_list else 0.0
    if max_allowed_per_asset is not None:
        equal_target_value = min(equal_target_value, max_allowed_per_asset)

    def append_sell(ticker: str, qty: int) -> None:
        nonlocal working_cash
        if qty <= 0:
            return
        price = sell_prices.get(ticker)
        if price is None or pd.isna(price) or price <= 0:
            logger.info(
                f"[주문계산][목표비중][매도스킵] {display_name(ticker)} 매도가격 없음"
            )
            return

        cost_basis_per_share = cost_basis_map.get(ticker)
        tax_rate = 0.0
        if float(sell_tax_pct or 0.0) > 0:
            if taxable_tickers is None or ticker in taxable_tickers:
                tax_rate = float(sell_tax_pct)
        estimated_value = apply_sell_value(
            float(price),
            qty,
            tax_rate,
            slippage,
            cost_basis_per_share=cost_basis_per_share,
        )
        sell_price_adj = float(price) * (1 - slippage)
        gross_proceeds_adj = qty * sell_price_adj
        taxable_gain = 0.0
        if cost_basis_per_share is not None:
            taxable_gain = max(0.0, gross_proceeds_adj - qty * cost_basis_per_share)
        estimated_tax = taxable_gain * max(tax_rate, 0.0)

        working_cash += float(estimated_value)
        remaining_qty = max(int(planned_holdings.get(ticker, 0)) - qty, 0)
        if remaining_qty > 0:
            planned_holdings[ticker] = remaining_qty
        else:
            planned_holdings.pop(ticker, None)
        orders.append(
            {
                "side": "SELL",
                "ticker": ticker,
                "display_name": display_name(ticker),
                "qty": qty,
                "reference_price": float(price),
                "estimated_value": float(estimated_value),
                "estimated_tax": float(estimated_tax),
                "reason": "ETF_REBALANCE",
            }
        )

    # 이탈 종목은 전량 매도하고, 유지 종목은 목표비중 또는 하드캡 초과분만 줄인다.
    for ticker, qty in list(planned_holdings.items()):
        qty_i = int(qty or 0)
        if qty_i <= 0:
            continue
        price = sell_prices.get(ticker)
        if price is None or pd.isna(price) or price <= 0:
            continue
        current_value = qty_i * float(price)
        rank = target_rank.get(ticker)
        keep_by_rank = rank is not None and rank <= sell_rank_buffer
        if not keep_by_rank:
            append_sell(ticker, qty_i)
            continue

        desired_value = current_value
        if ticker in buy_list and current_value > equal_target_value + band_value:
            desired_value = equal_target_value
        if max_allowed_per_asset is not None and current_value > max_allowed_per_asset:
            desired_value = min(desired_value, max_allowed_per_asset)
        if desired_value >= current_value:
            continue

        desired_qty = max(int(desired_value // float(price)), 0)
        append_sell(ticker, qty_i - desired_qty)

    # 매도 후 목표비중보다 부족한 상위 종목만 추가 매수한다.
    for ticker in buy_list:
        buy_price = buy_prices.get(ticker)
        sell_price = sell_prices.get(ticker)
        if buy_price is None or pd.isna(buy_price) or buy_price <= 0:
            continue
        valuation_price = sell_price if sell_price is not None and sell_price > 0 else buy_price
        held_qty = int(planned_holdings.get(ticker, 0))
        held_value = held_qty * float(valuation_price)
        if held_value >= equal_target_value - band_value:
            continue

        unit_cost = apply_buy_cost(float(buy_price), slippage) * (
            1 + market_order_margin_rate
        )
        if unit_cost <= 0:
            continue
        deficit = max(equal_target_value - held_value, 0.0)
        qty = min(int(deficit // unit_cost), int(working_cash // unit_cost))
        if qty <= 0:
            continue

        cost = qty * unit_cost
        working_cash -= cost
        planned_holdings[ticker] = held_qty + qty
        orders.append(
            {
                "side": "BUY",
                "ticker": ticker,
                "display_name": display_name(ticker),
                "qty": qty,
                "reference_price": float(buy_price),
                "estimated_value": float(cost),
                "reason": "ETF_REBALANCE",
            }
        )

    logger.info(
        "[주문계산][목표비중] 완료 | "
        f"목표={equal_target_value:,.0f}, 밴드={band_value:,.0f}, "
        f"매도={sum(1 for order in orders if order.get('side') == 'SELL')}건, "
        f"매수={sum(1 for order in orders if order.get('side') == 'BUY')}건"
    )
    return orders


def build_rebalance_orders(
    current_holdings: dict[str, int],
    target_tickers: list[str],
    latest_prices: dict[str, float],
    available_cash: float,
    latest_buy_prices: dict[str, float] | None = None,
    latest_sell_prices: dict[str, float] | None = None,
    current_cost_basis: dict[str, float] | None = None,
    max_positions: int = ETF_MAX_POSITIONS,
    sell_rank_buffer: int = ETF_SELL_RANK_BUFFER,
    slippage: float = 0.0005,
    sell_tax_pct: float = ETF_SELL_TAX_PCT,
    taxable_tickers: set[str] | None = None,
    allow_empty_target_sell: bool = False,
    generate_orders: bool = True,
    max_asset_pct: float | None = None,
    ticker_names: dict[str, str] | None = None,
    market_order_margin_rate: float = 0.0,
    target_weight_rebalance: bool = False,
    rebalance_band_pct: float = 0.05,
    trim_overweight_positions: bool = False,
) -> list[dict]:
    """리밸런싱 주문 목록을 생성한다.

    market_order_margin_rate: 시장가 주문 시 증권사가 추가로 확보하는 증거금 할증률
        (예: 0.20 = 20%). Kiwoom MARKET(시장가) 주문 시 필요.
        LIMIT 주문이나 KIS는 0.0으로 유지.
    """
    orders: list[dict] = []
    # 로그/display_name용: ticker_names가 있으면 '종목명(코드)' 형태, 없으면 코드 그대로
    _dn = lambda t: (ticker_names or {}).get(t, t)  # noqa: E731

    # --- 입력 정규화 및 방어 로직 ---
    # current_holdings: 키를 문자열로, 값은 정수로 변환
    holdings: dict[str, int] = {}
    if current_holdings:
        try:
            for k, v in current_holdings.items():
                if v is None:
                    continue
                try:
                    holdings[str(k)] = int(v)
                except Exception:
                    # 변환 불가 시 스킵
                    continue
        except Exception:
            holdings = {str(k): int(v) for k, v in dict(current_holdings).items() if v is not None}

    # 현금
    try:
        cash = float(available_cash or 0.0)
    except Exception:
        cash = 0.0

    # 가격 소스는 dict 또는 pandas.Series를 허용하도록 변환
    def _to_price_dict(src) -> dict:
        if src is None:
            return {}
        if isinstance(src, pd.Series):
            return {
                str(k): (float(v) if (v is not None and not pd.isna(v)) else None)
                for k, v in src.to_dict().items()
            }
        try:
            return {
                str(k): (float(v) if (v is not None and not pd.isna(v)) else None)
                for k, v in dict(src).items()
            }
        except Exception:
            return {}

    def _to_float_dict(src) -> dict[str, float]:
        if src is None:
            return {}
        try:
            items = src.items() if hasattr(src, "items") else dict(src).items()
            result: dict[str, float] = {}
            for k, v in items:
                if v is None or pd.isna(v):
                    continue
                result[str(k)] = float(v)
            return result
        except Exception:
            return {}

    base_prices = _to_price_dict(latest_prices)
    buy_prices = _to_price_dict(latest_buy_prices) if latest_buy_prices is not None else base_prices
    sell_prices = (
        _to_price_dict(latest_sell_prices) if latest_sell_prices is not None else base_prices
    )
    cost_basis_map = _to_float_dict(current_cost_basis)

    # 대상 티커 목록을 문자열 리스트로 정리
    targets: list[str] = [str(t) for t in target_tickers] if target_tickers else []
    target_rank = {ticker: idx + 1 for idx, ticker in enumerate(targets)}

    # generate_orders=False이면 주문 생성 건너뜀
    if not generate_orders:
        logger.info("[주문계산] generate_orders=False \u2014 리밸런싱 미실행으로 주문 생성 생략")
        return []

    # 빈 타겟 보호 로직
    if not targets and not allow_empty_target_sell:
        logger.info(
            "[주문계산] target이 비어있고 빈 target에서 매도 허용이 아니므로 주문 생성 생략"
        )
        return []

    logger.info(
        f"[주문계산] 시작 | 보유={len(holdings)}개, 목표={len(targets)}개, max_positions={max_positions}, 예수금={cash:,.0f}"
    )

    if target_weight_rebalance:
        return _build_target_weight_rebalance_orders(
            holdings=holdings,
            targets=targets,
            target_rank=target_rank,
            buy_prices=buy_prices,
            sell_prices=sell_prices,
            cash=cash,
            cost_basis_map=cost_basis_map,
            max_positions=max_positions,
            sell_rank_buffer=sell_rank_buffer,
            slippage=slippage,
            sell_tax_pct=sell_tax_pct,
            taxable_tickers=taxable_tickers,
            max_asset_pct=max_asset_pct,
            rebalance_band_pct=rebalance_band_pct,
            market_order_margin_rate=market_order_margin_rate,
            display_name=_dn,
        )

    # --- 매도 로직 ---
    for ticker, qty in list(holdings.items()):
        # 수량 정수 보장
        try:
            qty_i = int(qty or 0)
        except Exception:
            qty_i = 0

        if qty_i <= 0:
            # 비정상 수량은 무시
            continue

        rank = target_rank.get(ticker)
        keep_by_rank = rank is not None and rank <= sell_rank_buffer
        if keep_by_rank:
            logger.info(
                f"[주문계산][매도스킵] {_dn(ticker)} 보유유지 (랭크={rank}, 버퍼={sell_rank_buffer})"
            )
            continue

        price = sell_prices.get(ticker)
        if price is None or pd.isna(price) or price <= 0:
            logger.info(
                f"[주문계산][매도스킵] {_dn(ticker)} 매도가격 없음/비정상 (sell_price={price})"
            )
            continue

        try:
            cost_basis_per_share = cost_basis_map.get(ticker)
            tax_rate = 0.0
            if float(sell_tax_pct or 0.0) > 0:
                if taxable_tickers is None:
                    tax_rate = float(sell_tax_pct)
                elif ticker in taxable_tickers:
                    tax_rate = float(sell_tax_pct)

            estimated_value = apply_sell_value(
                float(price),
                qty_i,
                tax_rate,
                slippage,
                cost_basis_per_share=cost_basis_per_share,
            )
            # estimated_tax: 주문 기록용 참고값 (estimated_value에 이미 반영됨)
            sell_price_adj = float(price) * (1 - slippage)
            gross_proceeds_adj = qty_i * sell_price_adj
            taxable_gain = 0.0
            if cost_basis_per_share is not None:
                taxable_gain = max(0.0, gross_proceeds_adj - qty_i * cost_basis_per_share)
            estimated_tax = taxable_gain * max(tax_rate, 0.0)
        except Exception as e:
            logger.info(f"[주문계산][매도오류] {_dn(ticker)} estimated_value 계산 실패: {e}")
            continue

        cash += float(estimated_value)
        orders.append(
            {
                "side": "SELL",
                "ticker": ticker,
                "display_name": _dn(ticker),
                "qty": qty_i,
                "reference_price": float(price),
                "estimated_value": float(estimated_value),
                "estimated_tax": float(estimated_tax),
                "reason": "ETF_REBALANCE",
            }
        )
        holdings.pop(ticker, None)

    # --- 보유 가치 계산 (매도 후 기준) ---
    current_market_value = 0.0
    for t, q in holdings.items():
        p = sell_prices.get(t)
        if p is None or pd.isna(p):
            continue
        try:
            current_market_value += int(q) * float(p)
        except Exception:
            continue

    current_equity = cash + current_market_value
    max_allowed_per_asset = None
    try:
        if max_asset_pct is not None and float(max_asset_pct) > 0:
            max_allowed_per_asset = float(max_asset_pct) * float(current_equity)
    except Exception:
        max_allowed_per_asset = None

    # 비대칭 hard-cap: 승자 보유 로직은 유지하고 상한 초과분만 부분매도한다.
    if trim_overweight_positions and max_allowed_per_asset is not None:
        for ticker, qty in list(holdings.items()):
            qty_i = int(qty or 0)
            price = sell_prices.get(ticker)
            if qty_i <= 0 or price is None or pd.isna(price) or price <= 0:
                continue
            current_value = qty_i * float(price)
            if current_value <= max_allowed_per_asset:
                continue

            desired_qty = max(int(max_allowed_per_asset // float(price)), 0)
            sell_qty = qty_i - desired_qty
            if sell_qty <= 0:
                continue

            cost_basis_per_share = cost_basis_map.get(ticker)
            tax_rate = 0.0
            if float(sell_tax_pct or 0.0) > 0:
                if taxable_tickers is None or ticker in taxable_tickers:
                    tax_rate = float(sell_tax_pct)
            estimated_value = apply_sell_value(
                float(price),
                sell_qty,
                tax_rate,
                slippage,
                cost_basis_per_share=cost_basis_per_share,
            )
            sell_price_adj = float(price) * (1 - slippage)
            gross_proceeds_adj = sell_qty * sell_price_adj
            taxable_gain = 0.0
            if cost_basis_per_share is not None:
                taxable_gain = max(
                    0.0,
                    gross_proceeds_adj - sell_qty * cost_basis_per_share,
                )
            estimated_tax = taxable_gain * max(tax_rate, 0.0)

            cash += float(estimated_value)
            holdings[ticker] = desired_qty
            current_market_value -= sell_qty * float(price)
            orders.append(
                {
                    "side": "SELL",
                    "ticker": ticker,
                    "display_name": _dn(ticker),
                    "qty": sell_qty,
                    "reference_price": float(price),
                    "estimated_value": float(estimated_value),
                    "estimated_tax": float(estimated_tax),
                    "reason": "ETF_REBALANCE_CAP_TRIM",
                }
            )
            logger.info(
                f"[주문계산][hard-cap] {_dn(ticker)} {qty_i}→{desired_qty}주 "
                f"(평가액={current_value:,.0f}, 상한={max_allowed_per_asset:,.0f})"
            )

    # --- 매수 후보 선정 ---
    buy_list = targets[:max_positions]
    buy_count = len(buy_list)
    budget = cash / buy_count if buy_count > 0 else 0
    logger.info(
        f"[주문계산] 매수 종목={[_dn(t) for t in buy_list]} (균등분배, 종목당 약 {budget:,.0f})"
    )
    if not buy_list or cash <= 0:
        if not buy_list:
            logger.info("[주문계산] 매수 대상 없음 \u2192 주문 생성 종료")
        if cash <= 0:
            logger.info(f"[주문계산] 예수금 부족(cash={cash:,.0f}) \u2192 주문 생성 종료")
        return orders

    # 기존 보유 종목의 평가액을 미리 계산 (cap이 total exposure를 제한하도록)
    existing_value_for_target: dict[str, float] = {}
    for ticker in buy_list:
        held_qty = int(holdings.get(ticker, 0))
        if held_qty > 0:
            held_price = sell_prices.get(ticker)
            if held_price is not None and not pd.isna(held_price):
                existing_value_for_target[ticker] = held_qty * float(held_price)
            else:
                existing_value_for_target[ticker] = 0.0
        else:
            existing_value_for_target[ticker] = 0.0

    for ticker in buy_list:
        price = buy_prices.get(ticker)
        if price is None or pd.isna(price) or price <= 0:
            logger.info(
                f"[주문계산][매수스킵] {_dn(ticker)} 매수가격 없음/비정상 (buy_price={price})"
            )
            continue

        try:
            unit_cost = apply_buy_cost(float(price), slippage) * (1 + market_order_margin_rate)
        except Exception as e:
            logger.info(f"[주문계산][매수오류] {_dn(ticker)} unit_cost 계산 실패: {e}")
            continue

        if unit_cost <= 0:
            logger.info(f"[주문계산][매수스킵] {_dn(ticker)} 단가 비정상 (unit_cost={unit_cost})")
            continue

        qty = int(budget // unit_cost)
        if qty <= 0:
            logger.info(
                f"[주문계산][매수스킵] {_dn(ticker)} 수량 0 "
                f"(budget={budget:,.0f}, unit_cost={unit_cost:,.0f})"
            )
            continue

        # 자산별 최대 노출 제한 적용 (기존 보유분 포함 total exposure 기준)
        if max_allowed_per_asset is not None:
            existing_value = existing_value_for_target.get(ticker, 0.0)
            remaining_allowed = max_allowed_per_asset - existing_value
            allowed_qty = max(0, int(remaining_allowed // unit_cost))
            if allowed_qty <= 0:
                logger.info(
                    f"[주문계산][cap] {_dn(ticker)} cap 초과 (기존 {existing_value:,.0f} + 신규 불가, max={max_allowed_per_asset:,.0f})"
                )
                continue
            if qty > allowed_qty:
                logger.info(
                    f"[주문계산][cap] {_dn(ticker)} cap enforced: qty {qty} -> {allowed_qty} (기존 {existing_value:,.0f} + 신규 {allowed_qty * unit_cost:,.0f} <= {max_allowed_per_asset:,.0f})"
                )
                qty = allowed_qty

        cost = qty * unit_cost
        if cost > cash:
            qty = int(cash // unit_cost)
            cost = qty * unit_cost
        if qty <= 0:
            logger.info(
                f"[주문계산][매수스킵] {_dn(ticker)} 잔여예수금 부족 "
                f"(cash={cash:,.0f}, unit_cost={unit_cost:,.0f})"
            )
            continue

        cash -= cost
        orders.append(
            {
                "side": "BUY",
                "ticker": ticker,
                "display_name": _dn(ticker),
                "qty": qty,
                "reference_price": float(price),
                "estimated_value": float(cost),
                "reason": "ETF_REBALANCE",
            }
        )

    logger.info(
        f"[주문계산] 완료 | 매도={sum(1 for o in orders if o.get('side') == 'SELL')}건, "
        f"매수={sum(1 for o in orders if o.get('side') == 'BUY')}건"
    )

    return orders
