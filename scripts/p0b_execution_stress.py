"""P0-B: 거래비용·유동성·운용규모 스트레스 검증.

기존 엔진만 재사용한다(새 체결 엔진 없음).

- 비용: legacy 경로 ``run_etf_strategy`` + 수수료/세금 배수 패치
- 유동성/규모: ``execution_mode="ohlcv_capacity"`` 부분체결·이월·취소 정책
- 순위 안정성: ``scripts/parameter_stability.run_stability`` 재사용

산출물은 ``outputs_p0b/``에만 쓰고, 입력 캐시(data_cache)는 읽기만 한다.

실행::

    uv run python scripts/p0b_execution_stress.py
    uv run python scripts/p0b_execution_stress.py --phases cost,capacity
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_SCRIPTS = ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

os.environ.setdefault("ETF_USE_CACHE", "1")
os.environ.setdefault("ETF_REFRESH_CACHE", "0")

import parameter_stability as stability

import etf_shared
import pykrx_utils
import run_etf_backtest as rtb

OUTPUT_DIR = ROOT / "outputs_p0b"
DEFAULT_START = "20160105"
DEFAULT_END = "20260928"

BASE_BUY_FEE = etf_shared.BUY_FEE_PCT
BASE_SELL_FEE = etf_shared.SELL_FEE_PCT
BASE_TAX = rtb.ETF_TAXABLE_SELL_TAX_PCT
BASE_SLIPPAGE = rtb.BASE_SLIPPAGE

# 단일 변수 + 대표 복합 조건 (전수 조합은 실행하지 않는다)
COST_SCENARIOS: tuple[tuple[str, float, float], ...] = (
    ("slip5_fee1x", 0.0005, 1.0),
    ("slip10_fee1x", 0.0010, 1.0),
    ("slip20_fee1x", 0.0020, 1.0),
    ("slip30_fee1x", 0.0030, 1.0),
    ("slip50_fee1x", 0.0050, 1.0),
    ("slip5_fee2x", 0.0005, 2.0),
    ("slip5_fee3x", 0.0005, 3.0),
    ("slip20_fee2x", 0.0020, 2.0),
    ("slip50_fee3x", 0.0050, 3.0),
)
AUMS: tuple[int, ...] = (5_000_000, 10_000_000, 50_000_000, 100_000_000, 1_000_000_000)
PARTICIPATION_RATES: tuple[float, ...] = (0.01, 0.05, 0.10)


# --------------------------------------------------------------------------- #
# 공통 유틸
# --------------------------------------------------------------------------- #
@contextmanager
def patched_costs(multiplier: float, slippage: float | None = None) -> Iterator[None]:
    """수수료·세금 배수를 적용한다(슬리피지/스프레드는 별도 축).

    spread는 주문 참조가격에 이미 반영되어 있고, slippage는 파라미터로 전달되므로
    여기서 배수를 곱하지 않는다(중복 차감 방지).
    """
    etf_shared.BUY_FEE_PCT = BASE_BUY_FEE * multiplier
    etf_shared.SELL_FEE_PCT = BASE_SELL_FEE * multiplier
    rtb.ETF_TAXABLE_SELL_TAX_PCT = BASE_TAX * multiplier
    if slippage is not None:
        rtb.BASE_SLIPPAGE = slippage
    try:
        yield
    finally:
        etf_shared.BUY_FEE_PCT = BASE_BUY_FEE
        etf_shared.SELL_FEE_PCT = BASE_SELL_FEE
        rtb.ETF_TAXABLE_SELL_TAX_PCT = BASE_TAX
        rtb.BASE_SLIPPAGE = BASE_SLIPPAGE


def evaluation_curve(aum: float, dates: list[pd.Timestamp], curve: pd.DataFrame) -> pd.DataFrame:
    """초기 자본 시점을 포함한 평가 곡선(기존 script 관례)."""
    return pd.concat(
        [
            pd.DataFrame([{"date": pd.Timestamp(dates[0]), "equity": float(aum)}]),
            curve[["date", "equity"]],
        ],
        ignore_index=True,
    ).drop_duplicates("date", keep="last")


def performance(aum: float, dates: list[pd.Timestamp], curve: pd.DataFrame) -> dict[str, Any]:
    stats = rtb.calc_stats(evaluation_curve(aum, dates, curve), "equity")
    return {
        "final": float(stats["final"]),
        "total_return": float(stats["total_return"]),
        "cagr": float(stats["cagr"]),
        "mdd": float(stats["mdd"]),
        "sharpe": None if pd.isna(stats["sharpe"]) else float(stats["sharpe"]),
        "calmar": None if pd.isna(stats["calmar"]) else float(stats["calmar"]),
    }


def trading_stats(curve: pd.DataFrame, trades: pd.DataFrame) -> dict[str, Any]:
    stats = rtb.calc_trading_stats(trades, curve["equity"], curve["date"])
    return {
        "trade_count": len(trades),
        "turnover": float(stats["gross_turnover"]),
        "annual_turnover": float(stats["annual_gross_turnover"]),
        "buy_value": float(stats["buy_value"]),
        "sell_value": float(stats["sell_value"]),
    }


def cost_breakdown(trades: pd.DataFrame, slippage: float, multiplier: float) -> dict[str, Any]:
    """체결 내역에서 비용 구성요소를 분해한다(추정치).

    - fee: 체결 참조가격 x 수량 x (기본 수수료 x 배수)
    - tax: 과세 ETF 매도차익 추정치(체결 내역의 estimated_tax)
    - slippage: 체결 참조가격 x 수량 x 슬리피지
    - spread: 참조가격에 이미 포함되어 있으므로 총비용에는 더하지 않고 참고값으로만 기록
    """
    if trades.empty:
        return {
            "fee_cost": 0.0,
            "tax_cost": 0.0,
            "slippage_cost": 0.0,
            "spread_cost_informational": 0.0,
            "traded_notional": 0.0,
            "total_run_rate_cost": 0.0,
            "cost_pct_of_notional": 0.0,
        }
    qty = pd.to_numeric(trades["qty"], errors="coerce").fillna(0.0)
    price = pd.to_numeric(trades["price"], errors="coerce").fillna(0.0)
    notional = qty * price
    is_buy = trades["side"].astype(str).str.upper().eq("BUY")
    fee = notional.where(is_buy, 0.0) * BASE_BUY_FEE * multiplier + notional.where(
        ~is_buy, 0.0
    ) * BASE_SELL_FEE * multiplier
    tax = pd.to_numeric(trades.get("estimated_tax", 0.0), errors="coerce").fillna(0.0)
    slip = notional * slippage
    spread = notional * rtb.SPREAD_PCT / 2.0
    traded = float(notional.sum())
    total = float(fee.sum() + tax.sum() + slip.sum())
    return {
        "fee_cost": float(fee.sum()),
        "tax_cost": float(tax.sum()),
        "slippage_cost": float(slip.sum()),
        "spread_cost_informational": float(spread.sum()),
        "traded_notional": traded,
        "total_run_rate_cost": total,
        "cost_pct_of_notional": (total / traded) if traded > 0 else 0.0,
    }


def assert_legacy_reconciled(
    curve: pd.DataFrame, trades: pd.DataFrame, aum: float, label: str
) -> dict[str, Any]:
    """현금·평가액·거래현금흐름 검산."""
    identity = np.allclose(
        curve["equity"].to_numpy(dtype=float),
        (curve["cash"] + curve["market_value"]).to_numpy(dtype=float),
        rtol=0.0,
        atol=1e-6,
    )
    distribution_total = float(curve.get("distribution_cash", pd.Series(dtype=float)).sum())
    cash_flow = float(pd.to_numeric(trades.get("cash_flow", 0.0), errors="coerce").fillna(0.0).sum())
    expected_cash = aum + cash_flow + distribution_total
    final_cash = float(curve["cash"].iloc[-1])
    # 잔여 현금은 수백만원 규모의 유입/유출이 상쇄된 값이라, 허용오차를 거래 규모 기준으로
    # 잡아야 float 누적오차를 오탐하지 않는다.
    scale = abs(aum) + abs(cash_flow) + abs(distribution_total)
    cash_ok = abs(final_cash - expected_cash) <= max(1e-6, scale * 1e-12)
    if not identity:
        raise AssertionError(f"{label}: equity != cash + market_value")
    if not cash_ok:
        raise AssertionError(
            f"{label}: 현금 불일치 final_cash={final_cash} expected={expected_cash} "
            f"delta={final_cash - expected_cash}"
        )
    return {"equity_identity_ok": True, "cash_reconciled": True}


# --------------------------------------------------------------------------- #
# 입력/메타데이터
# --------------------------------------------------------------------------- #
def preflight_cache(start: str, end: str) -> dict[str, Any]:
    """네트워크 조회·캐시 쓰기가 발생할 수 있는 조건을 먼저 차단한다."""
    if Path.cwd().resolve() != ROOT:
        raise SystemExit(
            "[p0b] data_cache는 cwd 기준입니다. 저장소 루트에서 실행하세요: " + str(ROOT)
        )
    if os.environ.get("ETF_REFRESH_CACHE", "0") == "1":
        raise SystemExit("[p0b] ETF_REFRESH_CACHE=1은 캐시를 덮어쓰고 네트워크를 사용합니다.")
    if os.environ.get("ETF_USE_CACHE", "1") == "0":
        raise SystemExit("[p0b] ETF_USE_CACHE=0은 네트워크 조회를 강제합니다.")
    classification = ROOT / "data_cache" / "etf_tax_classification.parquet"
    if not classification.exists():
        raise SystemExit(
            "[p0b] etf_tax_classification.parquet이 없어 상장일 조회가 네트워크로 나갈 수 있습니다."
        )

    start_ts = pd.to_datetime(start)
    end_ts = pd.to_datetime(end)
    problems: list[str] = []
    with network_blocked():
        listing_dates = rtb.get_listing_dates(ticker_subset=set(etf_shared.ETF_LIST))
    for ticker in etf_shared.ETF_LIST:
        path = ROOT / "data_cache" / f"{ticker}.parquet"
        if not path.exists():
            problems.append(f"{ticker}: 캐시 없음")
            continue
        dates = pd.to_datetime(pd.read_parquet(path, columns=["date"])["date"])
        if dates.max() < end_ts:
            problems.append(f"{ticker}: 캐시 종료 {dates.max().date()} < {end_ts.date()}")
            continue
        if dates.min() <= start_ts:
            continue
        # 캐시가 요청 시작보다 늦게 시작하면 좌측 증분 조회가 발생한다.
        # 상장일이 요청 시작 이후로 확인되는 경우에만 허용한다.
        listed = listing_dates.get(str(ticker))
        listed_ts = pd.to_datetime(listed, errors="coerce") if listed else pd.NaT
        if pd.isna(listed_ts) or listed_ts <= start_ts:
            problems.append(
                f"{ticker}: 캐시 시작 {dates.min().date()} > {start_ts.date()} (좌측 조회 필요)"
            )
        else:
            print(f"[p0b] {ticker} 상장일 {listed_ts.date()} 이후만 존재 — 좌측 조회 없음")
    index_path = ROOT / "data_cache" / "index.parquet"
    if not index_path.exists():
        problems.append("index.parquet 없음")
    else:
        index_dates = pd.to_datetime(pd.read_parquet(index_path, columns=["date"])["date"])
        if index_dates.min() > start_ts:
            problems.append(
                f"index: 캐시 시작 {index_dates.min().date()} > {start_ts.date()} (좌측 조회 필요)"
            )
        if index_dates.max() < end_ts:
            problems.append(f"index: 캐시 종료 {index_dates.max().date()} < {end_ts.date()}")
    if problems:
        raise SystemExit(
            "[p0b] 캐시가 요청 구간을 덮지 않습니다(네트워크/쓰기 방지). --start/--end를 조정하세요:\n  "
            + "\n  ".join(problems)
        )
    return {
        "data_cache_digest": data_cache_digest(),
        "universe_size": len(etf_shared.ETF_LIST),
        "cache_files": len(cache_file_digests()),
    }


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


@contextmanager
def network_blocked() -> Iterator[None]:
    """캐시 미스가 네트워크 조회/캐시 쓰기로 이어지지 않도록 즉시 실패시킨다."""
    original_fetch = rtb.fetch_etf_ohlcv_with_nav
    original_stock = rtb.get_stock
    original_yf = rtb.yf
    original_tax_fetch = pykrx_utils.fetch_and_cache_tax_classification

    def _blocked(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("P0-B: 네트워크 조회가 차단되었습니다(캐시 전용 실행)")

    rtb.fetch_etf_ohlcv_with_nav = _blocked
    rtb.get_stock = _blocked
    rtb.yf = None
    pykrx_utils.fetch_and_cache_tax_classification = _blocked
    try:
        yield
    finally:
        rtb.fetch_etf_ohlcv_with_nav = original_fetch
        rtb.get_stock = original_stock
        rtb.yf = original_yf
        pykrx_utils.fetch_and_cache_tax_classification = original_tax_fetch


FREEZE_PATH = ROOT / "strategy_freeze.json"


def cache_file_digests() -> dict[str, str]:
    digests: dict[str, str] = {}
    for ticker in etf_shared.ETF_LIST:
        path = ROOT / "data_cache" / f"{ticker}.parquet"
        if path.exists():
            digests[str(ticker)] = hashlib.sha256(path.read_bytes()).hexdigest()
    index_path = ROOT / "data_cache" / "index.parquet"
    if index_path.exists():
        digests["__index__"] = hashlib.sha256(index_path.read_bytes()).hexdigest()
    classification = ROOT / "data_cache" / "etf_tax_classification.parquet"
    if classification.exists():
        digests["__tax_classification__"] = hashlib.sha256(
            classification.read_bytes()
        ).hexdigest()
    distributions = ROOT / "data" / "etf_distributions.csv"
    if distributions.exists():
        digests["__distributions__"] = hashlib.sha256(distributions.read_bytes()).hexdigest()
    return digests


def data_cache_digest() -> str:
    return hashlib.sha256(
        json.dumps(cache_file_digests(), sort_keys=True).encode("utf-8")
    ).hexdigest()


APPLIED: dict[str, Any] = {}


def configure_frozen_settings(cap: float | None) -> dict[str, Any]:
    """동결 설정을 환경/모듈에 강제 적용하고, 적용값이 freeze와 같은지 검증한다."""
    from strategy_freeze import load_frozen_strategy

    frozen = load_frozen_strategy(FREEZE_PATH)
    params = frozen["parameters"]
    applied_cap = float(params["max_asset_pct"]) if cap is None else float(cap)

    os.environ.update(
        {
            "ETF_RETURN_BASIS": str(params["return_basis"]),
            "ETF_DISTRIBUTION_TAX_PCT": str(float(params["distribution_tax_pct"])),
            "MIN_LISTING_DAYS": str(int(params["min_listing_days"])),
            "MIN_AVG_TRADING_VALUE": str(int(params["min_avg_trading_value"])),
            "MAX_PREMIUM_DISCOUNT": str(float(params["max_premium_discount"])),
            "MAX_ASSET_PCT": str(applied_cap),
            "TARGET_WEIGHT_REBALANCE": "1" if params["target_weight_rebalance"] else "0",
            "REBALANCE_BAND_PCT": str(float(params["rebalance_band_pct"])),
            "TRIM_OVERWEIGHT_POSITIONS": "1" if params["trim_overweight_positions"] else "0",
            "ETF_EXIT_CHECK_DAYS": str(int(params["exit_check_days"])),
            "ETF_TRAILING_STOP_PCT": str(float(params["trailing_stop_pct"])),
            "ETF_PORTFOLIO_TRAILING_STOP_PCT": str(
                float(params["portfolio_trailing_stop_pct"])
            ),
            "LIQUIDATE_ON_RISK_OFF": "1" if params["liquidate_on_risk_off"] else "0",
            "ENABLE_MULTI_INDEX_RISK": "1" if params["enable_multi_index_risk"] else "0",
            "MULTI_INDEX_GATING_MODE": str(params["multi_index_gating_mode"]),
            "US_RISK_PROXY": str(params["us_risk_proxy"]),
            "US_MARKET_MA_DAYS": str(int(params["us_market_ma_days"])),
            "US_MARKET_SLOPE_DAYS": str(int(params["us_market_slope_days"])),
            "REBALANCE_STEP_DAYS": str(int(params["rebalance_step_days"])),
        }
    )
    # import 시점에 상수로 고정되는 값은 모듈 속성도 함께 맞춘다.
    etf_shared.MIN_LISTING_DAYS = int(params["min_listing_days"])
    etf_shared.MIN_AVG_TRADING_VALUE = int(params["min_avg_trading_value"])
    etf_shared.MAX_PREMIUM_DISCOUNT = float(params["max_premium_discount"])
    etf_shared.ETF_DEVIATION_THRESHOLD_BY_GROUP = dict(params["deviation_threshold_by_group"])
    etf_shared.ETF_DEVIATION_THRESHOLD_BY_TICKER = dict(params["deviation_threshold_by_ticker"])
    etf_shared.ETF_SELL_RANK_BUFFER = int(params["sell_rank_buffer"])
    etf_shared.REBALANCE_STEP_DAYS = int(params["rebalance_step_days"])
    rtb.ETF_SELL_RANK_BUFFER = int(params["sell_rank_buffer"])
    rtb.ETF_MAX_POSITIONS = int(params["max_positions"])
    rtb.REBALANCE_STEP_DAYS = int(params["rebalance_step_days"])
    rtb.MARKET_MA_DAYS = int(params["market_ma_days"])
    rtb.MARKET_SLOPE_DAYS = int(params["market_slope_days"])
    rtb.BASE_SLIPPAGE = float(params["slippage"])
    rtb.SPREAD_PCT = float(params["spread_pct"])
    rtb.ENABLE_MULTI_INDEX_RISK = bool(params["enable_multi_index_risk"])
    rtb.MULTI_INDEX_GATING_MODE = str(params["multi_index_gating_mode"])
    rtb.US_RISK_PROXY = str(params["us_risk_proxy"])
    rtb.US_MARKET_MA_DAYS = int(params["us_market_ma_days"])
    rtb.US_MARKET_SLOPE_DAYS = int(params["us_market_slope_days"])
    # 엔진은 import 시점에 캡처한 모듈 전역 strategy_cfg를 참조하므로,
    # 환경변수만 바꾸면 target_weight/trim/band/liquidate가 갱신되지 않는다.
    rtb.strategy_cfg = rtb.get_strategy_config(initialize_universe=False)

    applied: dict[str, Any] = {
        "return_basis": os.environ["ETF_RETURN_BASIS"],
        "distribution_tax_pct": float(os.environ["ETF_DISTRIBUTION_TAX_PCT"]),
        "min_listing_days": int(etf_shared.MIN_LISTING_DAYS),
        "max_premium_discount": float(etf_shared.MAX_PREMIUM_DISCOUNT),
        "deviation_threshold_by_group": dict(etf_shared.ETF_DEVIATION_THRESHOLD_BY_GROUP),
        "deviation_threshold_by_ticker": dict(etf_shared.ETF_DEVIATION_THRESHOLD_BY_TICKER),
        "min_avg_trading_value": int(etf_shared.MIN_AVG_TRADING_VALUE),
        "max_asset_pct": applied_cap,
        "target_weight_rebalance": bool(rtb.strategy_cfg["target_weight_rebalance"]),
        "rebalance_band_pct": float(rtb.strategy_cfg["rebalance_band_pct"]),
        "trim_overweight_positions": bool(rtb.strategy_cfg["trim_overweight_positions"]),
        "exit_check_days": int(os.environ["ETF_EXIT_CHECK_DAYS"]),
        "trailing_stop_pct": float(os.environ["ETF_TRAILING_STOP_PCT"]),
        "portfolio_trailing_stop_pct": float(os.environ["ETF_PORTFOLIO_TRAILING_STOP_PCT"]),
        "liquidate_on_risk_off": bool(rtb.strategy_cfg["liquidate_on_risk_off"]),
        "slippage": float(rtb.BASE_SLIPPAGE),
        "spread_pct": float(rtb.SPREAD_PCT),
        "rebalance_step_days": int(rtb.REBALANCE_STEP_DAYS),
        "market_ma_days": int(rtb.MARKET_MA_DAYS),
        "market_slope_days": int(rtb.MARKET_SLOPE_DAYS),
        "enable_multi_index_risk": bool(rtb.ENABLE_MULTI_INDEX_RISK),
        "multi_index_gating_mode": str(rtb.MULTI_INDEX_GATING_MODE),
        "us_risk_proxy": str(rtb.US_RISK_PROXY),
        "us_market_ma_days": int(rtb.US_MARKET_MA_DAYS),
        "us_market_slope_days": int(rtb.US_MARKET_SLOPE_DAYS),
        "max_positions": int(rtb.ETF_MAX_POSITIONS),
        "sell_rank_buffer": int(rtb.ETF_SELL_RANK_BUFFER),
        "use_market_filter": True,
        "universe": [str(t) for t in etf_shared.ETF_LIST],
        "taxable_tickers": sorted(str(t) for t in etf_shared.TAXABLE_ETF_TICKERS),
        "ticker_groups": dict(etf_shared.ETF_TICKER_GROUPS),
    }
    diffs: list[str] = []
    for key, actual in applied.items():
        if key not in params:
            continue
        if key == "max_asset_pct":
            # 실험 변수로 의도한 차이는 intentional_deviations에 기록한다.
            continue
        expected = params[key]
        if isinstance(actual, float) and isinstance(expected, (int, float)):
            same = abs(actual - float(expected)) < 1e-12
        else:
            same = actual == expected
        if not same:
            diffs.append(f"{key}: runtime={actual!r} freeze={expected!r}")
    if diffs:
        raise SystemExit("[p0b] 동결 설정과 불일치합니다: " + "; ".join(diffs))

    APPLIED.clear()
    APPLIED.update(applied)

    intentional: list[str] = []
    if applied_cap != float(params["max_asset_pct"]):
        intentional.append(
            f"max_asset_pct: freeze={params['max_asset_pct']} -> experiment={applied_cap}"
        )
    return {
        "freeze_sha256": frozen["sha256"],
        "freeze_date": frozen.get("freeze_date"),
        "oos_start_date": frozen.get("oos_start_date"),
        "applied_cap": applied_cap,
        "frozen_cap": float(params["max_asset_pct"]),
        "spread_pct": float(rtb.SPREAD_PCT),
        "slippage": float(rtb.BASE_SLIPPAGE),
        "applied_settings": applied,
        "config_diffs": diffs,
        "intentional_deviations": intentional,
        "not_frozen_momentum_weight_60": float(
            os.environ.get("ETF_MOMENTUM_WEIGHT_60", "0.55")
        ),
    }


def load_inputs() -> tuple[pd.DataFrame, pd.DataFrame, list[pd.Timestamp]]:
    with network_blocked():
        index_df = rtb.get_index_data()
        price = rtb.load_etf_price()
    return price, index_df, [pd.Timestamp(d) for d in index_df["date"]]


def run_legacy(
    price: pd.DataFrame,
    index_df: pd.DataFrame,
    dates: list[pd.Timestamp],
    *,
    aum: float,
    slippage: float,
    fee_multiplier: float,
    max_asset_pct: float,
    max_positions: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    with patched_costs(fee_multiplier):
        curve, trades = rtb.run_etf_strategy(
            aum,
            dates,
            index_df,
            use_market_filter=True,
            max_positions=int(
                max_positions if max_positions is not None else APPLIED["max_positions"]
            ),
            slippage=slippage,
            risk_off_liquidate=True,
            price_data=price,
            max_asset_pct=max_asset_pct,
        )
    assert_legacy_reconciled(curve, trades, float(aum), "legacy run")
    return curve, trades


# --------------------------------------------------------------------------- #
# A. 비용/슬리피지 민감도
# --------------------------------------------------------------------------- #
def run_cost_phase(
    price: pd.DataFrame, index_df: pd.DataFrame, dates: list[pd.Timestamp], cap: float
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    aum = float(rtb.INITIAL_CASH)
    for label, slippage, multiplier in COST_SCENARIOS:
        curve, trades = run_legacy(
            price,
            index_df,
            dates,
            aum=aum,
            slippage=slippage,
            fee_multiplier=multiplier,
            max_asset_pct=cap,
        )
        recon = assert_legacy_reconciled(curve, trades, aum, f"cost:{label}")
        row: dict[str, Any] = {
            "scenario": label,
            "slippage_bps": round(slippage * 10000),
            "cost_multiplier": multiplier,
            "initial": aum,
            **performance(aum, dates, curve),
            **trading_stats(curve, trades),
            **cost_breakdown(trades, slippage, multiplier),
            **recon,
        }
        row["est_gross_final"] = row["final"] + row["total_run_rate_cost"]
        rows.append(row)
        print(
            f"[p0b] cost {label:16s} cagr={row['cagr']:.3%} mdd={row['mdd']:.1%} "
            f"sharpe={row['sharpe']:.3f} cost={row['total_run_rate_cost']:,.0f}"
        )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# B. 유동성/운용규모 (OHLCV capacity)
# --------------------------------------------------------------------------- #
def run_capacity_phase(
    price: pd.DataFrame, index_df: pd.DataFrame, dates: list[pd.Timestamp], cap: float
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    summaries: list[dict[str, Any]] = []
    diagnostics_all: list[dict[str, Any]] = []
    reconciliations: list[dict[str, Any]] = []
    scenario_meta: list[dict[str, Any]] = []
    input_hash = rtb._execution_sha256(
        {"start": rtb.START, "end": rtb.END, "universe": list(etf_shared.ETF_LIST)}
    )
    for aum in AUMS:
        legacy_curve, legacy_trades = run_legacy(
            price,
            index_df,
            dates,
            aum=float(aum),
            slippage=BASE_SLIPPAGE,
            fee_multiplier=1.0,
            max_asset_pct=cap,
        )
        legacy_stats = performance(float(aum), dates, legacy_curve)
        print(
            f"[p0b] legacy aum={aum:,} cagr={legacy_stats['cagr']:.3%} "
            f"final={legacy_stats['final']:,.0f}"
        )
        for rate in PARTICIPATION_RATES:
            config_hash = rtb._execution_sha256(
                {
                    "aum": aum,
                    "participation_rate": rate,
                    "slippage": BASE_SLIPPAGE,
                    "spread_pct": rtb.SPREAD_PCT,
                }
            )
            curve, trades, state = rtb.run_etf_strategy(
                float(aum),
                dates,
                index_df,
                use_market_filter=True,
                max_positions=int(APPLIED["max_positions"]),
                slippage=BASE_SLIPPAGE,
                risk_off_liquidate=True,
                price_data=price,
                max_asset_pct=cap,
                execution_mode="ohlcv_capacity",
                participation_rate=rate,
                return_final_state=True,
            )
            summary, diag, _trade_rows, recon = rtb._execution_capacity_scenario(
                aum=int(aum),
                result=curve,
                trades=trades,
                final_state=state,
                input_hash=input_hash,
                config_hash=config_hash,
                participation_rate=rate,
            )
            label = f"aum{aum}_p{int(rate * 100)}"
            diag_df = pd.DataFrame(diag)
            decisions = (
                diag_df["decision"].astype(str).value_counts()
                if not diag_df.empty
                else pd.Series(dtype=int)
            )
            requested = float(summary["requested_qty_total"])
            filled = float(summary["filled_qty_total"])
            cancel_mask = diag_df.get("decision", pd.Series(dtype=str)).astype(str).isin(
                {"PARTIAL_CANCEL", "CARRY_CANCELLED", "CARRY_EXPIRED"}
            )
            cancel_qty = (
                float(pd.to_numeric(diag_df.loc[cancel_mask, "remaining_qty"]).sum())
                if not diag_df.empty and cancel_mask.any()
                else 0.0
            )
            partial_cancel_mask = (
                diag_df.get("decision", pd.Series(dtype=str)).astype(str) == "PARTIAL_CANCEL"
            )
            partial_cancel_qty = (
                float(pd.to_numeric(diag_df.loc[partial_cancel_mask, "remaining_qty"]).sum())
                if not diag_df.empty and partial_cancel_mask.any()
                else 0.0
            )
            original_requested = (
                float(
                    pd.to_numeric(
                        diag_df.loc[
                            pd.to_numeric(diag_df["carry_age"], errors="coerce").fillna(0) == 0,
                            "requested_qty",
                        ]
                    ).sum()
                )
                if not diag_df.empty and "carry_age" in diag_df.columns
                else requested
            )
            cap_stats = performance(float(aum), dates, curve)
            summary_row = {
                "scenario": label,
                **summary,
                **cap_stats,
                "fill_rate_qty": (filled / requested) if requested > 0 else None,
                "original_requested_qty_total": original_requested,
                "fill_rate_qty_original": (
                    filled / original_requested if original_requested > 0 else None
                ),
                "full_order_count": int(decisions.get("FULL", 0)),
                "partial_carry_count": int(decisions.get("PARTIAL_CARRY", 0)),
                "partial_cancel_count": int(decisions.get("PARTIAL_CANCEL", 0)),
                "partial_cancel_remaining_qty": partial_cancel_qty,
                "carry_cancelled_count": int(decisions.get("CARRY_CANCELLED", 0)),
                "cash_limited_count": int(decisions.get("CASH_LIMITED", 0)),
                "cancel_event_count": int(cancel_mask.sum()) if not diag_df.empty else 0,
                "cancel_remaining_qty": cancel_qty,
                "unfilled_qty_total": requested - filled,
                "decision_breakdown": json.dumps(
                    {str(k): int(v) for k, v in decisions.items()}, ensure_ascii=False
                ),
                "legacy_final_equity": legacy_stats["final"],
                "legacy_cagr": legacy_stats["cagr"],
                # 최종 자산 차이(비율)와 CAGR 차이(%p)는 서로 다른 지표다.
                "final_vs_legacy_pct": (
                    (float(summary["final_equity"]) / legacy_stats["final"] - 1.0)
                    if legacy_stats["final"]
                    else None
                ),
                "cagr_diff_pp": (cap_stats["cagr"] - legacy_stats["cagr"]) * 100.0,
            }
            summaries.append(summary_row)
            diagnostics_all.extend({**row, "scenario": label} for row in diag)
            reconciliations.extend({**row, "scenario": label} for row in recon)
            scenario_meta.append(
                {
                    "scenario": label,
                    "aum": aum,
                    "participation_rate": rate,
                    "legacy_trade_count": len(legacy_trades),
                }
            )
            print(
                f"[p0b] capacity {label:20s} fill={summary_row['fill_rate_qty']} "
                f"cagr={summary_row['cagr']:.3%} reconciled={summary['reconciled']}"
            )
    summary_df = pd.DataFrame(summaries)
    if not summary_df["reconciled"].all():
        raise AssertionError("capacity reconciliation 실패")
    return (
        summary_df,
        pd.DataFrame(diagnostics_all),
        pd.DataFrame(reconciliations),
    )


# --------------------------------------------------------------------------- #
# C. 파라미터 순위 안정성 (기존 parameter_stability 재사용)
# --------------------------------------------------------------------------- #
@contextmanager
def reconciled_strategy_runs(label: str, counter: dict[str, int]) -> Iterator[None]:
    """run_etf_strategy 호출을 감싸 모든 실행의 회계 검산을 강제한다."""
    original = rtb.run_etf_strategy

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        curve, trades = result[0], result[1]
        aum = float(kwargs.get("initial_cash", args[0] if args else rtb.INITIAL_CASH))
        counter["runs"] = counter.get("runs", 0) + 1
        assert_legacy_reconciled(curve, trades, aum, f"{label}#{counter['runs']}")
        return result

    rtb.run_etf_strategy = wrapper
    try:
        yield
    finally:
        rtb.run_etf_strategy = original


def _stability_grid(cap: float) -> tuple[pd.DataFrame, dict[str, Any]]:
    os.environ["STABILITY_REBALANCE_DAYS"] = "15,20,25"
    os.environ["STABILITY_MAX_POSITIONS"] = "1,2,3"
    # 비중 한도는 동결값으로 고정하고 리밸런싱 주기·보유종목만 인접 변동한다.
    os.environ["STABILITY_MAX_ASSET_PCT"] = str(cap)
    os.environ["MAX_ASSET_PCT"] = str(cap)
    return stability.run_stability()


def run_stability_phase(cap: float) -> tuple[pd.DataFrame, int]:
    counter: dict[str, int] = {"runs": 0}
    base_label = "slip5_fee1x"
    stress_slippage = 0.005
    # run_stability() 내부에서 지수/가격을 다시 로드하므로 캐시 전용 가드를 함께 적용한다.
    with network_blocked():
        with reconciled_strategy_runs("stability", counter):
            base_results, _ = _stability_grid(cap)
        with patched_costs(3.0, slippage=stress_slippage), reconciled_strategy_runs(
            "stability_stress", counter
        ):
            stress_results, _ = _stability_grid(cap)
    stress_label = "slip50_fee3x"

    key = ["rebalance_step_days", "max_positions", "max_asset_pct"]
    base = base_results.copy()
    base["cost_scenario"] = base_label
    stress = stress_results.copy()
    stress["cost_scenario"] = stress_label
    combined = pd.concat([base, stress], ignore_index=True)
    combined["rank_cagr"] = combined.groupby("cost_scenario")["cagr"].rank(
        ascending=False, method="min"
    )
    combined["rank_sharpe"] = combined.groupby("cost_scenario")["sharpe"].rank(
        ascending=False, method="min"
    )

    base_rank = combined[combined["cost_scenario"] == base_label].set_index(key)
    stress_rank = combined[combined["cost_scenario"] == stress_label].set_index(key)
    delta = pd.DataFrame(
        {
            "rank_cagr_base": base_rank["rank_cagr"],
            "rank_cagr_stress": stress_rank["rank_cagr"],
            "rank_sharpe_base": base_rank["rank_sharpe"],
            "rank_sharpe_stress": stress_rank["rank_sharpe"],
        }
    ).reset_index()
    delta["cagr_rank_change"] = delta["rank_cagr_stress"] - delta["rank_cagr_base"]
    delta["sharpe_rank_change"] = delta["rank_sharpe_stress"] - delta["rank_sharpe_base"]
    out = combined.merge(
        delta[["rebalance_step_days", "max_positions", "max_asset_pct",
               "cagr_rank_change", "sharpe_rank_change"]],
        on=["rebalance_step_days", "max_positions", "max_asset_pct"],
        how="left",
    )
    central = out[
        (out["rebalance_step_days"] == 20)
        & (out["max_positions"] == 2)
        & np.isclose(out["max_asset_pct"], cap)
    ]
    for row in central.itertuples():
        print(f"[p0b] stability central {row.cost_scenario}: cagr={row.cagr:.3%}")
    print(f"[p0b] stability 회계 검산 {counter['runs']}회 통과")
    return out, int(counter["runs"])


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def _central_rank(frame: pd.DataFrame, column: str) -> float:
    mask = (frame.index.get_level_values("rebalance_step_days") == 20) & (
        frame.index.get_level_values("max_positions") == 2
    )
    return float(frame[mask][column].iloc[0])


def write_summary(
    metadata: dict[str, Any],
    cost_df: pd.DataFrame | None,
    capacity_df: pd.DataFrame | None,
    stability_df: pd.DataFrame | None,
) -> dict[str, Any]:
    """헤드라인 지표만 모은 요약(원자료 CSV는 별도 보존)."""
    summary: dict[str, Any] = {
        "phase": "P0-B",
        "period": metadata["period"],
        "max_asset_pct": metadata.get("max_asset_pct"),
        "spread_pct": metadata.get("spread_pct"),
        "freeze_sha256": metadata.get("freeze_sha256"),
        "git_commit": metadata["git_commit"],
        "data_cache_digest": metadata["data_cache_digest"],
        "diagnostic_only": True,
        "executable_fill_claim": False,
    }
    if cost_df is not None and not cost_df.empty:
        metrics = ("cagr", "mdd", "sharpe", "calmar", "final", "total_run_rate_cost")

        def _row(frame_row: pd.Series) -> dict[str, Any]:
            return {
                key: (None if pd.isna(frame_row[key]) else float(frame_row[key]))
                for key in metrics
            }

        base = cost_df[cost_df["scenario"] == "slip5_fee1x"].iloc[0]
        worst = cost_df.loc[cost_df["cagr"].idxmin()]
        summary["cost"] = {
            "base": _row(base),
            "worst": {"scenario": str(worst["scenario"]), **_row(worst)},
            "cagr_drop_bp": float((base["cagr"] - worst["cagr"]) * 10000),
            "sharpe_drop": float(base["sharpe"] - worst["sharpe"]),
            "cost_mix_base": {
                "fee": float(base["fee_cost"]),
                "tax": float(base["tax_cost"]),
                "slippage": float(base["slippage_cost"]),
            },
        }
    if capacity_df is not None and not capacity_df.empty:
        by_aum: dict[str, Any] = {}
        for aum, group in capacity_df.groupby("aum"):
            worst = group.loc[group["final_vs_legacy_pct"].idxmin()]
            by_aum[str(int(aum))] = {
                "min_fill_rate": float(group["fill_rate_qty"].min()),
                "min_fill_rate_original": float(group["fill_rate_qty_original"].min()),
                "worst_participation_rate": float(worst["participation_rate"]),
                "worst_final_vs_legacy_pct": float(worst["final_vs_legacy_pct"]),
                "worst_cagr_diff_pp": float(worst["cagr_diff_pp"]),
            }
        summary["capacity"] = {
            "min_fill_rate": float(capacity_df["fill_rate_qty"].min()),
            "all_reconciled": bool(capacity_df["reconciled"].all()),
            "by_aum": by_aum,
        }
    if stability_df is not None and not stability_df.empty:
        key = ["rebalance_step_days", "max_positions", "max_asset_pct"]
        base = stability_df[stability_df["cost_scenario"] == "slip5_fee1x"].set_index(key)
        stress = stability_df[stability_df["cost_scenario"] == "slip50_fee3x"].set_index(key)
        joined = base.join(stress, lsuffix="_base", rsuffix="_stress")
        summary["stability"] = {
            "scenario_count": len(base),
            # scipy 없이 순위 피어슨(=스피어만) 상관을 계산한다.
            "spearman_cagr": float(
                base["cagr"].rank().corr(stress["cagr"].rank())
            ),
            "spearman_sharpe": float(
                base["sharpe"].rank().corr(stress["sharpe"].rank())
            ),
            "max_abs_cagr_rank_change": float(
                (joined["rank_cagr_stress"] - joined["rank_cagr_base"]).abs().max()
            ),
            "max_abs_sharpe_rank_change": float(
                (joined["rank_sharpe_stress"] - joined["rank_sharpe_base"]).abs().max()
            ),
            "central_rank_cagr": {
                "base": float(_central_rank(base, "rank_cagr")),
                "stress": float(_central_rank(stress, "rank_cagr")),
            },
        }
    return summary


def _parse_phases(raw: str) -> set[str]:
    allowed = {"cost", "capacity", "stability"}
    phases = {part.strip() for part in raw.split(",") if part.strip()}
    unknown = phases - allowed
    if unknown:
        raise SystemExit(f"알 수 없는 phase: {sorted(unknown)} (가능: {sorted(allowed)})")
    return phases or allowed


def main() -> int:
    parser = argparse.ArgumentParser(description="P0-B execution stress MVP")
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--phases", default="cost,capacity,stability")
    parser.add_argument(
        "--cap",
        type=float,
        default=None,
        help="자산별 비중 한도(기본: strategy_freeze.json 값). 0.5 비교 실행용으로만 사용.",
    )
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    args = parser.parse_args()

    phases = _parse_phases(args.phases)
    frozen_cfg = configure_frozen_settings(args.cap)
    cap = float(frozen_cfg["applied_cap"])
    output_dir = Path(args.output_dir).resolve()
    digest_before = data_cache_digest()
    preflight = preflight_cache(args.start, args.end)
    rtb.START = args.start
    rtb.END = args.end

    output_dir.mkdir(parents=True, exist_ok=True)
    price, index_df, dates = load_inputs()
    loaded = set(price["ticker"].astype(str).unique())
    missing = sorted({str(t) for t in etf_shared.ETF_LIST} - loaded)
    if missing:
        raise SystemExit(f"[p0b] 필수 종목이 로딩되지 않았습니다: {missing}")

    metadata: dict[str, Any] = {
        "phase": "P0-B",
        "period": {"start": args.start, "end": args.end},
        "trading_days": len(dates),
        "universe": len(etf_shared.ETF_LIST),
        "loaded_tickers": len(loaded),
        "price_basis": os.environ.get("ETF_RETURN_BASIS", "price"),
        "base_slippage": BASE_SLIPPAGE,
        "spread_pct": frozen_cfg["spread_pct"],
        "buy_fee": BASE_BUY_FEE,
        "sell_fee": BASE_SELL_FEE,
        "taxable_sell_tax": BASE_TAX,
        "max_positions": int(rtb.ETF_MAX_POSITIONS),
        "max_asset_pct": cap,
        "frozen": frozen_cfg,
        "output_dir": str(output_dir),
        "git_commit": git_commit(),
        **preflight,
    }

    cost_df: pd.DataFrame | None = None
    capacity_df: pd.DataFrame | None = None
    stability_df: pd.DataFrame | None = None

    if "cost" in phases:
        cost_df = run_cost_phase(price, index_df, dates, cap)
        cost_df.to_csv(output_dir / "cost_sensitivity.csv", index=False, encoding="utf-8-sig")

    if "capacity" in phases:
        summary_df, diag_df, recon_df = run_capacity_phase(price, index_df, dates, cap)
        capacity_df = summary_df
        summary_df.to_csv(output_dir / "capacity_summary.csv", index=False, encoding="utf-8-sig")
        diag_df.to_csv(output_dir / "capacity_diagnostics.csv", index=False, encoding="utf-8-sig")
        recon_df.to_csv(
            output_dir / "capacity_reconciliation.csv", index=False, encoding="utf-8-sig"
        )

    if "stability" in phases:
        stability_df, stability_runs = run_stability_phase(cap)
        metadata["stability_reconciled_runs"] = stability_runs
        stability_df.to_csv(
            output_dir / "stability_ranking.csv", index=False, encoding="utf-8-sig"
        )

    if data_cache_digest() != digest_before:
        raise SystemExit("[p0b] 실행 중 data_cache가 변경되었습니다(캐시 전용 실행 위반).")
    metadata["data_cache_unchanged"] = True

    (output_dir / "p0b_summary.json").write_text(
        json.dumps(
            write_summary(metadata, cost_df, capacity_df, stability_df),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    metadata["artifacts"] = sorted(path.name for path in output_dir.glob("*"))
    (output_dir / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"[p0b] 완료: {output_dir} (cap={cap})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
