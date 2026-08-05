#!/usr/bin/env python3
"""전략 구성 요소(팩터)별 단독 효과를 분리하는 factorial ablation.

기준(baseline) 설정에서 각 팩터를 하나씩 제거/변경해 재실행하고,
성과 차이(팩터 기여도)와 상호작용을 보고한다.

팩터:
  - kospi_filter:   KOSPI 120/20 필터 (`use_market_filter`)
  - group_override: risk_off 시 그룹 override (해외/원자재 유지) + 청산 여부
  - multi_index:    멀티 인덱스 리스크 (KOSPI + US)
  - ma_window:      시장 필터 MA/SLOPE 기간
  - momentum:       모멘텀 스코어 가중치 (ret_60 vs ret_120)

사용: `uv run scripts/factorial_ablation.py`
출력: `outputs_ablation/factorial_ablation.csv`, `.json`
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run_etf_backtest as rtb
from config_utils import parse_fraction_env


OUTPUT_DIR = ROOT / "outputs_ablation"


def recompute_index_df(
    base_index_df: pd.DataFrame,
    ma_days: int,
    slope_days: int,
) -> pd.DataFrame:
    """캐시된 KOSPI 종가로 MA/SLOPE를 다시 계산한다 (네트워크 불필요)."""
    df = base_index_df[["date", "close"]].copy().sort_values("date").reset_index(drop=True)
    df["market_ma"] = df["close"].rolling(ma_days).mean()
    df["market_ma_slope"] = df["market_ma"] - df["market_ma"].shift(slope_days)
    df["risk_on"] = (df["close"] >= df["market_ma"]) & (df["market_ma_slope"] >= 0)
    return df


def run_scenario(
    *,
    common_dates: list[pd.Timestamp],
    index_df: pd.DataFrame,
    price_data: pd.DataFrame,
    us_index_df: pd.DataFrame | None,
    label: str,
    use_market_filter: bool,
    risk_off_liquidate: bool,
    enable_multi_index_risk: bool,
    max_positions: int,
    rebalance_step_days: int,
    momentum_weight_60: float,
    universe_tickers: list[str] | None,
) -> dict:
    original_step = rtb.REBALANCE_STEP_DAYS
    try:
        rtb.REBALANCE_STEP_DAYS = rebalance_step_days
        curve, trades = rtb.run_etf_strategy(
            rtb.INITIAL_CASH,
            common_dates,
            index_df,
            use_market_filter=use_market_filter,
            max_positions=max_positions,
            slippage=rtb.BASE_SLIPPAGE,
            risk_off_liquidate=risk_off_liquidate,
            price_data=price_data,
            enable_multi_index_risk=enable_multi_index_risk,
            us_index_df=us_index_df,
            universe_tickers=universe_tickers,
        )
    finally:
        rtb.REBALANCE_STEP_DAYS = original_step

    curve = curve.sort_values("date").reset_index(drop=True)
    stats = rtb.calc_stats(curve, "equity")
    return {
        "label": label,
        "trading_days": int(len(curve)),
        "final_equity": float(curve["equity"].iloc[-1]),
        "total_return": float(stats["total_return"]),
        "cagr": float(stats["cagr"]),
        "mdd": float(stats["mdd"]),
        "sharpe": (
            None if pd.isna(stats["sharpe"]) else float(stats["sharpe"])
        ),
        "n_trades": int(len(trades)),
    }


def main() -> int:
    os.environ.setdefault("ETF_USE_CACHE", "1")
    baseline_ma = int(os.environ.get("MARKET_MA_DAYS", "120"))
    baseline_slope = int(os.environ.get("MARKET_SLOPE_DAYS", "20"))
    max_positions = int(os.environ.get("ETF_MAX_POSITIONS", "2"))
    rebalance_step_days = int(os.environ.get("REBALANCE_STEP_DAYS", "20"))
    baseline_momentum = parse_fraction_env("ETF_MOMENTUM_WEIGHT_60", 0.55)
    universe_tickers = None

    print("[ablation] 데이터 로드 (1회)")
    index_df = rtb.get_index_data()
    common_dates = list(pd.to_datetime(index_df["date"]))
    price_data = rtb.load_etf_price()
    us_index_df = rtb.get_us_index_data() if os.environ.get("ENABLE_MULTI_INDEX_RISK") == "1" else None

    # 기준: 현재 기본값
    baseline = run_scenario(
        common_dates=common_dates,
        index_df=index_df,
        price_data=price_data,
        us_index_df=us_index_df,
        label="baseline",
        use_market_filter=True,
        risk_off_liquidate=True,
        enable_multi_index_risk=os.environ.get("ENABLE_MULTI_INDEX_RISK") == "1",
        max_positions=max_positions,
        rebalance_step_days=rebalance_step_days,
        momentum_weight_60=baseline_momentum,
        universe_tickers=universe_tickers,
    )
    rows = [baseline]

    scenarios = [
        {
            "label": "no_kospi_filter",
            "use_market_filter": False,
            "risk_off_liquidate": True,
            "enable_multi_index_risk": False,
            "momentum_weight_60": baseline_momentum,
            "index_df": index_df,
            "us_index_df": None,
        },
        {
            "label": "no_group_override",
            "use_market_filter": True,
            "risk_off_liquidate": False,
            "enable_multi_index_risk": False,
            "momentum_weight_60": baseline_momentum,
            "index_df": index_df,
            "us_index_df": None,
        },
        {
            "label": "enable_multi_index",
            "use_market_filter": True,
            "risk_off_liquidate": True,
            "enable_multi_index_risk": True,
            "momentum_weight_60": baseline_momentum,
            "index_df": index_df,
            "us_index_df": us_index_df,
        },
        {
            "label": "ma_90",
            "use_market_filter": True,
            "risk_off_liquidate": True,
            "enable_multi_index_risk": False,
            "momentum_weight_60": baseline_momentum,
            "index_df": recompute_index_df(index_df, 90, baseline_slope),
            "us_index_df": None,
        },
        {
            "label": "ma_180",
            "use_market_filter": True,
            "risk_off_liquidate": True,
            "enable_multi_index_risk": False,
            "momentum_weight_60": baseline_momentum,
            "index_df": recompute_index_df(index_df, 180, baseline_slope),
            "us_index_df": None,
        },
        {
            "label": "momentum_60_30",
            "use_market_filter": True,
            "risk_off_liquidate": True,
            "enable_multi_index_risk": False,
            "momentum_weight_60": 0.30,
            "index_df": index_df,
            "us_index_df": None,
        },
        {
            "label": "momentum_60_100",
            "use_market_filter": True,
            "risk_off_liquidate": True,
            "enable_multi_index_risk": False,
            "momentum_weight_60": 1.00,
            "index_df": index_df,
            "us_index_df": None,
        },
    ]

    for sc in scenarios:
        os.environ["ETF_MOMENTUM_WEIGHT_60"] = str(sc["momentum_weight_60"])
        print(f"[ablation] 실행: {sc['label']}")
        row = run_scenario(
            common_dates=common_dates,
            index_df=sc["index_df"],
            price_data=price_data,
            us_index_df=sc["us_index_df"],
            label=sc["label"],
            use_market_filter=sc["use_market_filter"],
            risk_off_liquidate=sc["risk_off_liquidate"],
            enable_multi_index_risk=sc["enable_multi_index_risk"],
            max_positions=max_positions,
            rebalance_step_days=rebalance_step_days,
            momentum_weight_60=sc["momentum_weight_60"],
            universe_tickers=universe_tickers,
        )
        rows.append(row)
    os.environ.pop("ETF_MOMENTUM_WEIGHT_60", None)

    df = pd.DataFrame(rows)
    # 기준 대비 기여도 (CAGR/MDD)
    baseline_cagr = baseline["cagr"]
    baseline_mdd = baseline["mdd"]
    df["cagr_delta"] = df["cagr"] - baseline_cagr
    df["mdd_delta"] = df["mdd"] - baseline_mdd

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT_DIR / "factorial_ablation.csv", index=False, encoding="utf-8-sig")
    report = {
        "baseline": {k: (None if pd.isna(v) else v) for k, v in baseline.items()},
        "factors": df.to_dict(orient="records"),
        "config": {
            "baseline_ma": baseline_ma,
            "baseline_slope": baseline_slope,
            "max_positions": max_positions,
            "rebalance_step_days": rebalance_step_days,
            "momentum_weight_60": baseline_momentum,
        },
    }
    (OUTPUT_DIR / "factorial_ablation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print()
    print(df[["label", "cagr", "mdd", "sharpe", "cagr_delta", "mdd_delta"]].to_string(index=False))
    print(f"\n[ablation] 저장 완료: {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
