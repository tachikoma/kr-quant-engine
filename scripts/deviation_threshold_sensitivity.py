#!/usr/bin/env python3
"""NAV 괴리율 임계값 민감도 분석

그룹별/티커별 NAV 괴리율 임계값을 변경해 백테스트를 실행하고,
성능 지표(CAGR, MDD, Sharpe, 거래수)를 비교합니다.

사용법:
    uv run scripts/deviation_threshold_sensitivity.py

주의: 시나리오마다 etf_shared 모듈 상수를 직접 수정하므로
run_etf_backtest.py가 add_deviation_flag()를 동적으로 호출해야 합니다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import os

os.environ.setdefault("ETF_USE_CACHE", "1")
os.environ.setdefault("ETF_REFRESH_CACHE", "0")
os.environ.setdefault("ETF_BACKTEST_MODE", "single")
os.environ.setdefault("ETF_RETURN_BASIS", "price")

import pandas as pd

import etf_shared
import run_etf_backtest as rtb

OUTPUT_DIR = Path("outputs_grid")
OUTPUT_DIR.mkdir(exist_ok=True)

# 분석 기간 (속도를 위해 2년으로 제한, 필요시 전체 기간으로 확장 가능)
START_DATE = pd.Timestamp("2023-01-01")
END_DATE = pd.Timestamp("2024-12-31")

SCENARIOS = {
    "A_current": {
        "description": "현재 구현: 국내 2%, 해외 5%, 상품 5%, 커버드콜 4%",
        "group": {
            "domestic_equity": 0.02,
            "foreign_investment": 0.05,
            "commodity": 0.05,
        },
        "ticker": {
            "472150": 0.04,
            "486290": 0.04,
            "498400": 0.04,
        },
    },
    "B_conservative": {
        "description": "보수적: 국내 2%, 해외 3%, 상품 3%, 커버드콜 3%",
        "group": {
            "domestic_equity": 0.02,
            "foreign_investment": 0.03,
            "commodity": 0.03,
        },
        "ticker": {
            "472150": 0.03,
            "486290": 0.03,
            "498400": 0.03,
        },
    },
    "C_middle": {
        "description": "중간: 국내 2%, 해외 4%, 상품 4%, 커버드콜 3%",
        "group": {
            "domestic_equity": 0.02,
            "foreign_investment": 0.04,
            "commodity": 0.04,
        },
        "ticker": {
            "472150": 0.03,
            "486290": 0.03,
            "498400": 0.03,
        },
    },
    "D_baseline": {
        "description": "기준: 전체 2% 단일 임계값",
        "group": {
            "domestic_equity": 0.02,
            "foreign_investment": 0.02,
            "commodity": 0.02,
        },
        "ticker": {
            "472150": 0.02,
            "486290": 0.02,
            "498400": 0.02,
        },
    },
}


def _set_thresholds(group: dict[str, float], ticker: dict[str, float]) -> None:
    """etf_shared 모듈 상수를 직접 교체한다."""
    etf_shared.ETF_DEVIATION_THRESHOLD_BY_GROUP = dict(group)
    etf_shared.ETF_DEVIATION_THRESHOLD_BY_TICKER = dict(ticker)


def run_scenario(name: str, scenario: dict) -> dict:
    print(f"\n[시나리오 {name}] {scenario['description']}")
    _set_thresholds(scenario["group"], scenario["ticker"])

    # 공통 데이터 한 번만 로드 (각 시나리오 간 재사용)
    index_df = rtb.get_index_data()
    common_dates = [
        d for d in index_df["date"] if START_DATE <= d <= END_DATE
    ]

    result, trades = rtb.run_etf_strategy(
        rtb.INITIAL_CASH,
        common_dates,
        index_df,
        use_market_filter=rtb.USE_MARKET_FILTER,
        max_positions=rtb.ETF_MAX_POSITIONS,
        slippage=rtb.BASE_SLIPPAGE,
        risk_off_liquidate=True,
    )

    stats = rtb.calc_stats(result, "equity")
    core_domestic = ["069500", "091160", "102110"]
    core_trades = trades[trades["ticker"].isin(core_domestic)] if len(trades) > 0 else pd.DataFrame()
    foreign_tickers = ["133690", "143850", "360200", "360750"]
    foreign_trades = trades[trades["ticker"].isin(foreign_tickers)] if len(trades) > 0 else pd.DataFrame()
    covered_call = ["472150", "486290", "498400"]
    cc_trades = trades[trades["ticker"].isin(covered_call)] if len(trades) > 0 else pd.DataFrame()

    summary = {
        "scenario": name,
        "description": scenario["description"],
        "domestic_equity": scenario["group"]["domestic_equity"],
        "foreign_investment": scenario["group"]["foreign_investment"],
        "commodity": scenario["group"]["commodity"],
        "covered_call": scenario["ticker"]["472150"],
        "initial": float(stats["initial"]),
        "final": float(stats["final"]),
        "total_return": float(stats["total_return"]),
        "cagr": float(stats["cagr"]),
        "mdd": float(stats["mdd"]),
        "volatility": float(stats["volatility"]),
        "sharpe": None if pd.isna(stats.get("sharpe")) else float(stats["sharpe"]),
        "total_trades": len(trades),
        "core_domestic_trades": len(core_trades),
        "foreign_trades": len(foreign_trades),
        "covered_call_trades": len(cc_trades),
    }

    print(f"  CAGR: {summary['cagr']:.2%}, MDD: {summary['mdd']:.2%}, Sharpe: {summary['sharpe']:.4f}")
    print(f"  거래: 총 {summary['total_trades']}, 국내핵심 {summary['core_domestic_trades']}, 해외 {summary['foreign_trades']}, 커버드콜 {summary['covered_call_trades']}")

    return summary


def main() -> None:
    print("=" * 80)
    print(f"NAV 괴리율 임계값 민감도 분석 ({START_DATE.date()} ~ {END_DATE.date()})")
    print("=" * 80)

    summaries: list[dict] = []
    for name, scenario in SCENARIOS.items():
        try:
            summary = run_scenario(name, scenario)
            summaries.append(summary)
        except Exception as e:
            print(f"  [오류] {name} 실행 실패: {e}")
            import traceback
            traceback.print_exc()

    df = pd.DataFrame(summaries)
    df.to_csv(OUTPUT_DIR / "deviation_threshold_sensitivity.csv", index=False, encoding="utf-8-sig")
    with (OUTPUT_DIR / "deviation_threshold_sensitivity.json").open("w", encoding="utf-8") as f:
        json.dump(summaries, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 80)
    print("[결과 요약]")
    print("=" * 80)
    print(df.to_string(index=False))
    print(f"\n저장 완료: {OUTPUT_DIR}/deviation_threshold_sensitivity.csv")


if __name__ == "__main__":
    main()
