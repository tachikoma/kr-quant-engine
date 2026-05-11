#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MAX_ASSET_PCT 정책을 적용한 재백테스트 실행 스크립트

환경변수:
 - MAX_ASSET_PCT (예: 0.2)
 - MAX_POSITIONS (옵션)

출력:
 - outputs_grid/cap_filtered_*.csv
 - outputs_grid/cap_filtered_trades_*.csv

사용: uv run scripts/apply_cap_and_retest.py
"""
from pathlib import Path
import os
import sys
import json

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import run_etf_backtest as rtb

OUT = ROOT / "outputs_grid"
OUT.mkdir(exist_ok=True)


def main():
    cap_env = os.environ.get("MAX_ASSET_PCT", "0.2")
    try:
        cap = float(cap_env)
    except Exception:
        print("MAX_ASSET_PCT invalid; set a numeric value like 0.2")
        sys.exit(1)

    # 후보 리스트 로드
    fl = OUT / "filtered_etf_list.json"
    if not fl.exists():
        print("filtered_etf_list.json not found; run filter_candidates.py first")
        sys.exit(1)

    etfs = json.loads(fl.read_text(encoding="utf-8"))
    rtb.ETF_LIST = etfs

    # set env var for run_etf_backtest to pick up
    os.environ["MAX_ASSET_PCT"] = str(cap)

    index_df = rtb.get_index_data()
    common_dates = list(index_df["date"])

    max_positions = int(os.environ.get("MAX_POSITIONS", str(rtb.ETF_MAX_POSITIONS)))

    print(f"Running cap retest: MAX_ASSET_PCT={cap}, max_positions={max_positions}, n_candidates={len(etfs)}")

    result, trades = rtb.run_etf_strategy(rtb.INITIAL_CASH, common_dates, index_df, use_market_filter=True, max_positions=max_positions, slippage=float(rtb.BASE_SLIPPAGE))

    fname = OUT / f"cap_filtered_n{len(etfs)}_cap{cap:.2f}_pos{max_positions}.csv"
    result.to_csv(fname, index=False, encoding="utf-8-sig")
    trades.to_csv(OUT / f"cap_filtered_trades_n{len(etfs)}_cap{cap:.2f}_pos{max_positions}.csv", index=False, encoding="utf-8-sig")

    stats = rtb.calc_stats(result, "equity")
    print("=== CAP 재백테스트 결과 ===")
    print(f"파일: {fname}")
    print(f"CAGR: {stats['cagr']:.2%}")
    print(f"MDD: {stats['mdd']:.2%}")
    print(f"Sharpe: {stats['sharpe']:.3f}")
    print(f"Total Return: {stats['total_return']:.2%}")


if __name__ == "__main__":
    main()
