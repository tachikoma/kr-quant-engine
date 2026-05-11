#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Filtered list에서 특정 티커를 제외하고 재백테스트를 실행하여 MDD/CAGR 개선 여부를 확인합니다.

환경변수:
 - EXCLUDE_TICKERS: 쉼표로 구분된 티커 목록(예: 233740,229200). 기본값은 233740,229200
 - MAX_POSITIONS, REBALANCE_STEP_DAYS 등은 기존 env를 따릅니다.

결과:
 - outputs_grid/retest_excluding_{tickers}_...csv 생성
"""
import os
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run_etf_backtest as rtb
import pandas as pd

OUT = ROOT / "outputs_grid"

def main():
    exclude_env = os.environ.get("EXCLUDE_TICKERS")
    if exclude_env:
        exclude = [t.strip() for t in exclude_env.split(",") if t.strip()]
    else:
        exclude = ["233740", "229200"]

    filtered_path = OUT / "filtered_etf_list.json"
    if not filtered_path.exists():
        print("filtered_etf_list.json 파일을 찾을 수 없습니다. 먼저 필터 스크립트를 실행하세요.")
        sys.exit(1)

    with filtered_path.open("r", encoding="utf-8") as f:
        final_list = json.load(f)

    new_list = [str(t) for t in final_list if str(t) not in exclude]
    print(f"원래 후보 수: {len(final_list)}, 제외 후 후보 수: {len(new_list)} (제외: {exclude})")

    # 인덱스 데이터 및 날짜
    index_df = rtb.get_index_data()
    common_dates = list(index_df["date"])

    # 환경변수로 포지션/리밸런스 설정 가능
    max_positions = int(os.environ.get("MAX_POSITIONS", str(3)))
    rebalance_days = int(os.environ.get("REBALANCE_STEP_DAYS", str(rtb.REBALANCE_STEP_DAYS)))

    rtb.ETF_LIST = new_list
    rtb.REBALANCE_STEP_DAYS = rebalance_days

    print("재백테스트 실행 중... (이 작업은 시간이 걸릴 수 있습니다)")
    result, trades = rtb.run_etf_strategy(
        rtb.INITIAL_CASH,
        common_dates,
        index_df,
        use_market_filter=True,
        max_positions=max_positions,
        slippage=float(rtb.BASE_SLIPPAGE),
    )

    fname = OUT / f"retest_excluding_{'_'.join(exclude)}_n{len(new_list)}_pos{max_positions}_reb{rebalance_days}.csv"
    result.to_csv(fname, index=False, encoding="utf-8-sig")
    trades.to_csv(OUT / f"retest_excluding_{'_'.join(exclude)}_trades_n{len(new_list)}_pos{max_positions}_reb{rebalance_days}.csv", index=False, encoding="utf-8-sig")

    stats = rtb.calc_stats(result, "equity")
    print("=== 재백테스트 결과 ===")
    print(f"파일: {fname}")
    print(f"CAGR: {stats['cagr']:.2%}")
    print(f"MDD: {stats['mdd']:.2%}")
    print(f"Sharpe: {stats['sharpe']:.3f}")
    print(f"Total Return: {stats['total_return']:.2%}")


if __name__ == "__main__":
    main()
