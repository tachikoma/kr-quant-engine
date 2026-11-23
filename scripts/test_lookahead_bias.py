"""P0-A 미래정보 누수(look-ahead) 검증.

운영 신호 경로를 그대로 구동해 다음 4가지를 검증한다.

1. ``signal_prefix_invariance``  시점 T 이후 OHLCV를 추가/변경해도 T까지의 신호 불변
2. ``decision_prefix_invariance`` 리밸런싱 결정(순위/목표/게이트)도 T까지 불변
3. ``asof_signal_recompute``     시점 t 신호는 t까지의 종가로만 재현 가능 (shift(-1) 검출)
4. ``execution_next_open``       t 종가 신호는 t+1 시가에 체결 (당일 종가 체결 금지)

누수 3종을 주입했을 때 위 검증이 실제로 실패하는지도 확인한다.

- shift(-1) 정렬 오류: 전일 행에 당일 값이 실리는 오프바이원 데이터 정렬
- 전체 기간 정규화: ``add_liquidity_flag``가 rolling 대신 전 구간 평균 사용
- 당일 종가 체결: 실행일 시가 자리에 직전 거래일 종가를 사용

네트워크, 실거래 API, 운영 산출물(``outputs_*``, ``runtime_state/``,
``strategy_freeze.json``)은 사용/변경하지 않는다. 모든 가격은 합성 fixture다.

실행::

    uv run python scripts/test_lookahead_bias.py
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --- 환경 고정: 로컬 .env나 셸 설정이 fixture 결과를 바꾸지 못하게 한다. ---
_PINNED_ENV = {
    "ETF_UNIVERSE_MODE": "static",
    "ETF_RETURN_BASIS": "price",
    "ETF_SPREAD_PCT": "0.0005",
    "ETF_BASE_SLIPPAGE": "0.0005",
    "MIN_AVG_TRADING_VALUE": "1000000000",
    "MIN_LISTING_DAYS": "60",
    "MAX_ASSET_PCT": "0.5",
    "ETF_MOMENTUM_WEIGHT_60": "0.55",
    "TARGET_WEIGHT_REBALANCE": "0",
    "TRIM_OVERWEIGHT_POSITIONS": "0",
    "LIQUIDATE_ON_RISK_OFF": "1",
    "ETF_DISTRIBUTION_TAX_PCT": "0",
    "MULTI_INDEX_GATING_MODE": "hybrid",
    "ETF_EXIT_CHECK_DAYS": "0",
    "ETF_TRAILING_STOP_PCT": "0",
    "ETF_PORTFOLIO_TRAILING_STOP_PCT": "0",
    "ETF_USE_CACHE": "0",
    "ETF_ENABLE_BENCHMARK": "0",
    "REBALANCE_STEP_DAYS": "20",
}
os.environ.update(_PINNED_ENV)
# pykrx는 import 시점에 인증을 시도하지 않지만, 네트워크 경로가 열리지 않도록
# 자격증명을 비운다. 모든 가격은 fixture에서만 온다.
os.environ["KRX_ID"] = ""
os.environ["KRX_PW"] = ""

import etf_shared
import run_etf_backtest as backtest

logging.getLogger("etf_shared").setLevel(logging.WARNING)
logging.getLogger("run_etf_backtest").setLevel(logging.WARNING)

INITIAL_CASH = 1_000_000.0
SLIPPAGE = 0.0005
MAX_POSITIONS = 2
TICKERS = ("900101", "900102", "900103", "900104")
N_DAYS = 300
WARMUP_DAYS = max(120, etf_shared.MARKET_MA_DAYS + etf_shared.MARKET_SLOPE_DAYS)
AS_OF_INDEX = 240
DATES = pd.bdate_range("2022-01-04", periods=N_DAYS)
AS_OF = pd.Timestamp(DATES[AS_OF_INDEX])
FUTURE_FROM = AS_OF_INDEX + 1
COLUMNS = [
    "date",
    "ticker",
    "open",
    "close",
    "close_adj",
    "ret_60",
    "ret_120",
    "ma20",
    "ma60",
    "trend_ok",
    "liquidity_ok",
    "avg_trading_value_60",
    "listing_ok",
    "deviation_ok",
    "distribution",
]
DECISION_FIELDS = (
    "decision_date",
    "execution_date",
    "kospi_risk_on",
    "us_risk_on",
    "multi_index_enabled",
    "multi_index_gating_mode",
    "allowed_groups",
    "forced_exit_tickers",
    "n_candidates",
    "ranked_tickers",
    "targets",
    "allow_empty_target_sell",
    "empty_target_protected",
    "pre_holdings",
)
# 시가와 직전 종가의 괴리를 0.4%로 고정한다. 체결가가 시가인지 당일 종가인지
# 구분할 수 있게 하려는 fixture 보장이며, 검증 임계값(1e-6)보다 훨씬 크다.
OPEN_GAP_PCT = 0.004


# --------------------------------------------------------------------------- #
# fixture
# --------------------------------------------------------------------------- #
def _daily_returns(index: int, n: int) -> np.ndarray:
    """티커별 결정적 수익률 시계열. 순위가 구간마다 바뀌도록 설계한다."""
    t = np.arange(n, dtype=float)
    if index == 0:
        drift = np.where(t < 130, 0.0025, -0.0015)
        return drift + 0.0015 * np.sin(0.7 * t + 0.3)
    if index == 1:
        drift = np.where(t < 130, -0.0013, 0.0028)
        return drift + 0.0012 * np.sin(0.5 * t + 1.1)
    if index == 2:
        return 0.0009 + 0.0035 * np.sin(0.31 * t + 2.0)
    return 0.0004 + 0.0025 * np.sin(0.9 * t + 0.5)


def build_raw_frames(perturb_future: bool = False) -> dict[str, pd.DataFrame]:
    """``get_price``가 반환할 원시 OHLCV 프레임을 만든다.

    ``perturb_future=True``이면 기준 시점(``AS_OF``) 이후 구간만 바꾼다.
    정상 구현이라면 이 변경은 기준 시점까지의 신호에 영향을 주지 않아야 한다.
    """
    n = N_DAYS
    t = np.arange(n, dtype=float)
    # 유동성 필터가 구간 중간에 뒤집히도록 임계값(1e9)을 천천히 통과시킨다.
    trading_value_base = 4e8 + 1.2e9 * t / (n - 1)
    frames: dict[str, pd.DataFrame] = {}
    for offset, ticker in enumerate(TICKERS):
        close = 10_000.0 * np.cumprod(1.0 + _daily_returns(offset, n))
        trading_value = trading_value_base + 1e8 * offset
        if perturb_future:
            future = np.arange(n) >= FUTURE_FROM
            # 미래 구간은 급등 + 유동성 급감으로 바꾼다. 전 구간 평균을 쓰는
            # 구현이라면 이 변경이 과거 시점 신호까지 오염시킨다.
            close = np.where(future, close * 1.35, close)
            trading_value = np.where(future, 1e8, trading_value)
        open_ = np.empty_like(close)
        open_[0] = close[0]
        gap = np.where(np.arange(1, n) % 2 == 0, 1.0, -1.0) * OPEN_GAP_PCT
        open_[1:] = close[:-1] * (1.0 + gap)
        frames[ticker] = pd.DataFrame(
            {
                "date": DATES,
                "ticker": ticker,
                "open": open_,
                "high": np.maximum(open_, close) * 1.01,
                "low": np.minimum(open_, close) * 0.99,
                "close": close,
                "volume": np.maximum((trading_value / close).astype(np.int64), 1),
                "trading_value": trading_value,
            }
        )
    return frames


def subset_raw(raw: dict[str, pd.DataFrame], as_of: pd.Timestamp) -> dict[str, pd.DataFrame]:
    return {
        ticker: frame[frame["date"] <= as_of].reset_index(drop=True)
        for ticker, frame in raw.items()
    }


def shift_minus_one(raw: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """누수 주입 1: 전일 행에 당일 값을 채우는 오프바이원 정렬 오류."""
    shifted: dict[str, pd.DataFrame] = {}
    for ticker, frame in raw.items():
        moved = frame.copy()
        for column in ("open", "high", "low", "close", "volume", "trading_value"):
            moved[column] = moved[column].shift(-1)
        shifted[ticker] = moved.dropna(subset=["close"]).reset_index(drop=True)
    return shifted


def stale_open(signals: pd.DataFrame) -> pd.DataFrame:
    """누수 주입 3: 실행일 시가 자리에 직전 거래일 종가(= 결정일 종가)를 넣는다."""
    frame = signals.sort_values(["ticker", "date"]).copy()
    frame["open"] = frame.groupby("ticker")["close"].shift(1)
    frame["open"] = frame["open"].fillna(frame["close"])
    return frame.reset_index(drop=True)


def build_index_fixture(dates: pd.DatetimeIndex | pd.Series) -> pd.DataFrame:
    t = np.arange(len(dates), dtype=float)
    close = 2500.0 * np.exp(0.00025 * t) * (1.0 + 0.05 * np.sin(t / 16.0))
    frame = pd.DataFrame({"date": pd.to_datetime(dates), "close": close})
    frame["market_ma"] = frame["close"].rolling(etf_shared.MARKET_MA_DAYS).mean()
    frame["market_ma_slope"] = frame["market_ma"] - frame["market_ma"].shift(
        etf_shared.MARKET_SLOPE_DAYS
    )
    frame["risk_on"] = (frame["close"] > frame["market_ma"]) & (
        frame["market_ma_slope"] > 0
    )
    return frame


def reference_prices(raw: dict[str, pd.DataFrame]) -> pd.DataFrame:
    frames = [
        frame[["date", "ticker", "open", "close"]].copy() for frame in raw.values()
    ]
    return pd.concat(frames, ignore_index=True).sort_values(["ticker", "date"])


# --------------------------------------------------------------------------- #
# 운영 경로 실행 (패치는 데이터 소스 seam에만 적용)
# --------------------------------------------------------------------------- #
@contextmanager
def _patched(**overrides: Any):
    originals: dict[str, Any] = {}
    try:
        for name, value in overrides.items():
            originals[name] = getattr(backtest, name)
            setattr(backtest, name, value)
        yield
    finally:
        for name, value in originals.items():
            setattr(backtest, name, value)


def _fake_get_price_factory(raw: dict[str, pd.DataFrame]) -> Callable[..., pd.DataFrame]:
    def fake_get_price(ticker: str, listing_dates: dict[str, str] | None = None) -> pd.DataFrame:
        del listing_dates
        if ticker not in raw:
            raise KeyError(f"fixture에 없는 티커: {ticker}")
        return raw[ticker].copy()

    return fake_get_price


def load_signals(raw: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """운영 신호 파이프라인(``load_etf_price``)을 그대로 실행한다."""
    with _patched(
        get_price=_fake_get_price_factory(raw),
        get_listing_dates=lambda **kwargs: {},
        load_distributions=lambda **kwargs: pd.DataFrame(),
    ):
        return backtest.load_etf_price(list(TICKERS))


def leaky_liquidity_flag(price: pd.DataFrame) -> pd.DataFrame:
    """전체 기간 평균을 쓰는 유동성 필터 (미래 통계 참조)."""
    frame = price.sort_values(["ticker", "date"]).copy()
    full_mean = frame.groupby("ticker")["trading_value"].transform("mean")
    frame["liquidity_ok"] = full_mean.notna() & (
        full_mean >= etf_shared.MIN_AVG_TRADING_VALUE
    )
    frame["avg_trading_value_60"] = full_mean
    return frame


class RunResult:
    def __init__(
        self,
        equity: pd.DataFrame,
        trades: pd.DataFrame,
        events: list[dict[str, Any]],
        dates: list[pd.Timestamp],
    ) -> None:
        self.equity = equity
        self.trades = trades
        self.events = events
        self.dates = dates


def run_strategy(
    signals: pd.DataFrame,
    index_df: pd.DataFrame,
    as_of: pd.Timestamp | None = None,
) -> RunResult:
    """운영 백테스트 경로(``run_etf_strategy``)를 실행한다."""
    frame = signals if as_of is None else signals[signals["date"] <= as_of]
    frame = frame.reset_index(drop=True)
    dates = [pd.Timestamp(d) for d in sorted(frame["date"].unique())]
    index = index_df if as_of is None else index_df[index_df["date"] <= as_of]
    events: list[dict[str, Any]] = []
    equity, trades = backtest.run_etf_strategy(
        initial_cash=INITIAL_CASH,
        common_dates=dates,
        index_df=index.reset_index(drop=True),
        use_market_filter=True,
        max_positions=MAX_POSITIONS,
        slippage=SLIPPAGE,
        risk_off_liquidate=True,
        price_data=frame,
        max_asset_pct=0.5,
        target_weight_rebalance=False,
        rebalance_band_pct=0.05,
        trim_overweight_positions=False,
        exit_check_days=0,
        trailing_stop_pct=0.0,
        portfolio_trailing_stop_pct=0.0,
        enable_multi_index_risk=False,
        universe_tickers=list(TICKERS),
        rebalance_observer=events.append,
    )
    return RunResult(equity, trades, events, dates)


# --------------------------------------------------------------------------- #
# 검증기 (주입 방식과 무관하게 산출물만 본다)
# --------------------------------------------------------------------------- #
def _normalize(value: Any) -> Any:
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return str(pd.Timestamp(value))
    if isinstance(value, np.generic):
        return value.item()
    return value


def _assert_equal_series(
    actual: pd.Series,
    expected: pd.Series,
    label: str,
    tolerance: float = 1e-12,
) -> None:
    a = actual.to_numpy(dtype=float)
    e = expected.to_numpy(dtype=float)
    both_nan = np.isnan(a) & np.isnan(e)
    close = np.isclose(a, e, rtol=0.0, atol=tolerance, equal_nan=True)
    bad = ~(close | both_nan)
    if bad.any():
        positions = np.flatnonzero(bad)[:3]
        samples = ", ".join(
            f"row{int(p)}: actual={a[p]!r} expected={e[p]!r}" for p in positions
        )
        raise AssertionError(
            f"{label}: {int(bad.sum())}/{len(a)}행이 다릅니다 ({samples})"
        )


def assert_signal_prefix_invariance(
    prefix: pd.DataFrame,
    full: pd.DataFrame,
    as_of: pd.Timestamp,
    label: str,
) -> None:
    """기준 시점까지의 신호 컬럼이 미래 데이터에 의존하지 않아야 한다."""
    left = (
        prefix[prefix["date"] <= as_of][COLUMNS]
        .sort_values(["ticker", "date"])
        .reset_index(drop=True)
    )
    right = (
        full[full["date"] <= as_of][COLUMNS]
        .sort_values(["ticker", "date"])
        .reset_index(drop=True)
    )
    if len(left) != len(right):
        raise AssertionError(
            f"prefix invariance({label}): 행 수 불일치 prefix={len(left)} full={len(right)}"
        )
    if len(left) == 0:
        raise AssertionError(f"prefix invariance({label}): 비교할 행이 없습니다")
    for column in ("date", "ticker"):
        if not left[column].astype(str).equals(right[column].astype(str)):
            raise AssertionError(
                f"prefix invariance({label}) 컬럼 {column}: 비교 구간의 시계열이 다릅니다"
            )
    for column in COLUMNS:
        if column in {"date", "ticker", "trend_ok", "liquidity_ok", "listing_ok", "deviation_ok"}:
            continue
        _assert_equal_series(
            left[column].astype(float),
            right[column].astype(float),
            f"prefix invariance({label}) 컬럼 {column}",
        )
    for column in ("trend_ok", "liquidity_ok", "listing_ok", "deviation_ok"):
        if not left[column].astype(bool).equals(right[column].astype(bool)):
            diff = int((left[column].astype(bool) != right[column].astype(bool)).sum())
            raise AssertionError(
                f"prefix invariance({label}) 컬럼 {column}: {diff}행 불일치"
            )


def _decision_map(
    events: list[dict[str, Any]], as_of: pd.Timestamp
) -> dict[pd.Timestamp, dict[str, Any]]:
    """``as_of``까지의 데이터로 내릴 수 있었던 결정만 모은다.

    기준 시점 ``as_of``까지 데이터가 있을 때 실행일이 ``as_of`` 이하인 결정은
    prefix 실행에서도 반드시 존재해야 한다. (실행일이 ``as_of``를 넘는 결정은
    prefix 데이터만으로는 체결할 수 없으므로 비교 대상에서 제외한다.)
    """
    mapped: dict[pd.Timestamp, dict[str, Any]] = {}
    for event in events:
        key = pd.Timestamp(event["decision_date"])
        execution_date = pd.Timestamp(event["execution_date"])
        if key <= as_of and execution_date <= as_of:
            mapped[key] = event
    return mapped


def assert_decision_prefix_invariance(
    prefix_events: list[dict[str, Any]],
    full_events: list[dict[str, Any]],
    as_of: pd.Timestamp,
) -> None:
    """미래 데이터의 유무가 기준 시점까지의 리밸런싱 결정을 바꾸지 않아야 한다."""
    left = _decision_map(prefix_events, as_of)
    right = _decision_map(full_events, as_of)
    if not right:
        raise AssertionError("decision prefix invariance: 비교할 결정이 없습니다")
    if len(right) < 3:
        raise AssertionError(
            f"decision prefix invariance: 결정이 {len(right)}건뿐이라 검증력이 부족합니다"
        )
    if set(left) != set(right):
        missing = sorted(set(right) - set(left))
        extra = sorted(set(left) - set(right))
        raise AssertionError(
            f"decision prefix invariance: 결정 시점 집합 불일치 missing={missing[:3]} extra={extra[:3]}"
        )
    for date, expected_event in right.items():
        actual_event = left[date]
        for field in DECISION_FIELDS:
            actual = _normalize(actual_event.get(field))
            expected = _normalize(expected_event.get(field))
            if actual != expected:
                raise AssertionError(
                    f"decision prefix invariance: {date.date()} {field} 불일치 "
                    f"prefix={actual!r} full={expected!r}"
                )
        execution_date = pd.Timestamp(actual_event["execution_date"])
        if execution_date <= date:
            raise AssertionError(
                f"decision prefix invariance: {date.date()} 결정이 같은 날 체결로 기록됨"
            )


def assert_asof_signal_recompute(
    signals: pd.DataFrame,
    reference: pd.DataFrame,
    as_of: pd.Timestamp,
) -> None:
    """시점 t 신호는 t까지의 종가로만 재현 가능해야 한다 (shift(-1) 검출)."""
    frame = (
        signals[signals["date"] <= as_of]
        .sort_values(["ticker", "date"])
        .reset_index(drop=True)
    )
    if frame.empty:
        raise AssertionError("asof recompute: 비교할 행이 없습니다")
    merged = frame[["ticker", "date", "close", "close_adj"]].merge(
        reference[["ticker", "date", "close"]],
        on=["ticker", "date"],
        how="left",
        suffixes=("", "_ref"),
    )
    if merged["close_ref"].isna().any():
        raise AssertionError("asof recompute: 참조 가격이 없는 행이 있습니다")
    _assert_equal_series(
        merged["close"],
        merged["close_ref"],
        "asof recompute: 당일 종가가 참조 종가와 다릅니다(미래 행 참조 의심)",
        tolerance=1e-9,
    )
    _assert_equal_series(
        merged["close_adj"],
        merged["close_ref"],
        "asof recompute: close_adj가 당일 종가와 다릅니다",
        tolerance=1e-9,
    )

    grouped = frame.groupby("ticker")["close_adj"]
    expected_ret_60 = grouped.pct_change(60)
    expected_ret_120 = grouped.pct_change(120)
    expected_ma20 = grouped.transform(lambda series: series.rolling(20).mean())
    expected_ma60 = grouped.transform(lambda series: series.rolling(60).mean())
    _assert_equal_series(frame["ret_60"], expected_ret_60, "asof recompute: ret_60")
    _assert_equal_series(frame["ret_120"], expected_ret_120, "asof recompute: ret_120")
    _assert_equal_series(frame["ma20"], expected_ma20, "asof recompute: ma20")
    _assert_equal_series(frame["ma60"], expected_ma60, "asof recompute: ma60")
    expected_trend = (frame["close_adj"] > expected_ma20) & (
        expected_ma20 > expected_ma60
    )
    if not frame["trend_ok"].astype(bool).equals(expected_trend.astype(bool)):
        diff = int((frame["trend_ok"].astype(bool) != expected_trend.astype(bool)).sum())
        raise AssertionError(f"asof recompute: trend_ok {diff}행 불일치")


def assert_execution_next_open(
    trades: pd.DataFrame,
    reference: pd.DataFrame,
    events: list[dict[str, Any]],
) -> None:
    """t 종가 신호는 t+1 시가에 체결되어야 한다 (당일 종가 체결 금지)."""
    if trades.empty:
        raise AssertionError("execution timing: 체결이 없어 검증할 수 없습니다")
    sides = set(trades["side"].astype(str))
    if not {"BUY", "SELL"}.issubset(sides):
        raise AssertionError(f"execution timing: 매수/매도 체결이 모두 필요합니다 ({sides})")
    ref = reference.set_index(["ticker", "date"])
    decision_by_execution = {
        pd.Timestamp(event["execution_date"]): pd.Timestamp(event["decision_date"])
        for event in events
        if "execution_date" in event
    }
    for row in trades.itertuples(index=False):
        execution_date = pd.Timestamp(row.date)
        key = (str(row.ticker), execution_date)
        if key not in ref.index:
            raise AssertionError(f"execution timing: 참조 가격 없음 {key}")
        open_price = float(ref.loc[key, "open"])
        decision_date = decision_by_execution.get(execution_date)
        if decision_date is None:
            raise AssertionError(
                f"execution timing: {execution_date.date()} 체결에 대응하는 결정이 없습니다"
            )
        if decision_date >= execution_date:
            raise AssertionError(
                f"execution timing: {row.ticker} 결정일 {decision_date.date()} >= 체결일 "
                f"{execution_date.date()}"
            )
        spread = 1.0 + backtest.SPREAD_PCT / 2.0 if row.side == "BUY" else 1.0 - backtest.SPREAD_PCT / 2.0
        expected = open_price * spread
        relative_gap = abs(float(row.price) - expected) / expected
        if relative_gap > 1e-9:
            raise AssertionError(
                f"execution timing: {row.ticker} {execution_date.date()} 체결가 "
                f"{float(row.price):.6f}가 실행일 시가 기준 {expected:.6f}와 다릅니다"
                f"(당일 종가 체결 의심, gap={relative_gap:.2e})"
            )
        # fixture는 시가와 직전 종가를 0.4% 벌려 둔다. 체결가가 결정일 종가와
        # 사실상 같다면 당일 종가 체결로 판정한다.
        close_key = (str(row.ticker), decision_date)
        if close_key in ref.index:
            decision_close = float(ref.loc[close_key, "close"])
            if abs(float(row.price) - decision_close) / decision_close < 1e-5:
                raise AssertionError(
                    f"execution timing: {row.ticker} {execution_date.date()} 체결가가 "
                    "결정일 종가와 동일합니다(당일 종가 체결)"
                )


def assert_decisions_match_shared_ranker(
    signals: pd.DataFrame, events: list[dict[str, Any]]
) -> None:
    """운영 루프의 후보 목록이 공용 랭킹 함수(as-of 스냅샷)와 일치해야 한다."""
    checked = 0
    for event in events:
        decision_date = pd.Timestamp(event["decision_date"])
        snapshot = signals[signals["date"] == decision_date].reset_index(drop=True)
        if snapshot.empty:
            continue
        expected = etf_shared.rank_etfs(snapshot)["ticker"].astype(str).tolist()
        actual = [str(ticker) for ticker in event["ranked_tickers"]]
        if actual != expected:
            raise AssertionError(
                f"shared ranker 불일치: {decision_date.date()} "
                f"strategy={actual} ranker={expected}"
            )
        checked += 1
    if checked < 3:
        raise AssertionError(f"shared ranker 검증 표본이 부족합니다 ({checked}건)")


# --------------------------------------------------------------------------- #
# 검증 실행
# --------------------------------------------------------------------------- #
def evaluate(
    signals: pd.DataFrame,
    reference: pd.DataFrame,
    prefix_signals: pd.DataFrame,
    perturbed_signals: pd.DataFrame,
    events: list[dict[str, Any]],
    prefix_events: list[dict[str, Any]],
    trades: pd.DataFrame,
    as_of: pd.Timestamp,
) -> dict[str, tuple[bool, str]]:
    checks: dict[str, Callable[[], None]] = {
        "signal_prefix_invariance_truncated": lambda: assert_signal_prefix_invariance(
            prefix_signals, signals, as_of, "truncated"
        ),
        "signal_prefix_invariance_future_perturbed": lambda: assert_signal_prefix_invariance(
            perturbed_signals, signals, as_of, "future_perturbed"
        ),
        "decision_prefix_invariance": lambda: assert_decision_prefix_invariance(
            prefix_events, events, as_of
        ),
        "asof_signal_recompute": lambda: assert_asof_signal_recompute(
            signals, reference, as_of
        ),
        "execution_next_open": lambda: assert_execution_next_open(
            trades, reference, events
        ),
        "shared_ranker_consistency": lambda: assert_decisions_match_shared_ranker(
            signals, events
        ),
    }
    results: dict[str, tuple[bool, str]] = {}
    for name, check in checks.items():
        try:
            check()
        except AssertionError as exc:
            results[name] = (False, str(exc))
        else:
            results[name] = (True, "")
    return results


def _report(title: str, results: dict[str, tuple[bool, str]]) -> bool:
    print(f"[{title}]")
    passed = True
    for name, (ok, message) in results.items():
        print(f"  {'PASS' if ok else 'FAIL'} {name}")
        if not ok:
            passed = False
            print(f"       {message}")
    return passed


def _expect_failures(
    title: str,
    results: dict[str, tuple[bool, str]],
    expected_failures: set[str],
) -> bool:
    print(f"[{title}]")
    ok = True
    for name in expected_failures:
        failed = not results[name][0]
        print(f"  {'PASS' if failed else 'FAIL'} detected:{name}")
        if failed:
            print(f"       {results[name][1]}")
        else:
            ok = False
    unexpected = [
        name
        for name, (passed, _) in results.items()
        if not passed and name not in expected_failures
    ]
    for name in unexpected:
        print(f"  NOTE  추가 검출: {name}: {results[name][1]}")
    return ok


def main() -> int:
    raw = build_raw_frames()
    raw_perturbed = build_raw_frames(perturb_future=True)
    raw_prefix = subset_raw(raw, AS_OF)
    reference = reference_prices(raw)
    index_full = build_index_fixture(DATES)
    index_prefix = index_full[index_full["date"] <= AS_OF].reset_index(drop=True)

    signals = load_signals(raw)
    signals_prefix = load_signals(raw_prefix)
    signals_perturbed = load_signals(raw_perturbed)

    full_run = run_strategy(signals, index_full)
    prefix_run = run_strategy(signals_prefix, index_prefix, as_of=AS_OF)
    if not full_run.events:
        raise AssertionError("fixture 오류: 리밸런싱 결정이 한 건도 없습니다")

    clean = evaluate(
        signals,
        reference,
        signals_prefix,
        signals_perturbed,
        full_run.events,
        prefix_run.events,
        full_run.trades,
        AS_OF,
    )
    ok = _report("clean fixture (no injection)", clean)

    shifted_signals = load_signals(shift_minus_one(raw))
    shifted_prefix = load_signals(subset_raw(shift_minus_one(raw), AS_OF))
    shifted_run = run_strategy(shifted_signals, index_full)
    shifted_prefix_run = run_strategy(shifted_prefix, index_prefix, as_of=AS_OF)
    shift_results = evaluate(
        shifted_signals,
        reference,
        shifted_prefix,
        shifted_signals,
        shifted_run.events,
        shifted_prefix_run.events,
        shifted_run.trades,
        AS_OF,
    )
    ok &= _expect_failures(
        "injection 1: shift(-1) 오프바이원 정렬", shift_results, {"asof_signal_recompute"}
    )

    with _patched(add_liquidity_flag=leaky_liquidity_flag):
        leaky_signals = load_signals(raw)
        leaky_prefix = load_signals(raw_prefix)
        leaky_perturbed = load_signals(raw_perturbed)
    leaky_run = run_strategy(leaky_signals, index_full)
    leaky_prefix_run = run_strategy(leaky_prefix, index_prefix, as_of=AS_OF)
    leaky_results = evaluate(
        leaky_signals,
        reference,
        leaky_prefix,
        leaky_perturbed,
        leaky_run.events,
        leaky_prefix_run.events,
        leaky_run.trades,
        AS_OF,
    )
    ok &= _expect_failures(
        "injection 2: 전 구간 평균 정규화(미래 통계 참조)",
        leaky_results,
        {"signal_prefix_invariance_future_perturbed", "decision_prefix_invariance"},
    )

    stale_signals = stale_open(signals)
    stale_prefix = stale_open(signals_prefix)
    stale_run = run_strategy(stale_signals, index_full)
    stale_prefix_run = run_strategy(stale_prefix, index_prefix, as_of=AS_OF)
    stale_results = evaluate(
        stale_signals,
        reference,
        stale_prefix,
        stale_signals,
        stale_run.events,
        stale_prefix_run.events,
        stale_run.trades,
        AS_OF,
    )
    ok &= _expect_failures(
        "injection 3: 당일 종가 신호 → 당일 종가 체결", stale_results, {"execution_next_open"}
    )

    if ok:
        print("\nP0-A look-ahead checks passed")
        return 0
    print("\nP0-A look-ahead checks FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
