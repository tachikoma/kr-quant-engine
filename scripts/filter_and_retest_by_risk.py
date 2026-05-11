#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
리스크 기반 필터 적용 후 재백테스트

기능:
 - outputs_grid/filtered_etf_list.json을 읽어 각 티커의 최근 N년(기본 3년) 변동성과 최근 MDD를 계산
 - 환경변수 임계값(MAX_VOL, MIN_RECENT_MDD)에 따라 취약 티커를 제외
 - 제외된 리스트로 백테스트 실행 및 결과 저장/출력

환경변수:
 - RECENT_YEARS (기본 3)
 - MAX_VOL (연환산 변동성, 기본 0.6)
 - MIN_RECENT_MDD (음수, 예: -0.5 => 허용 최저 드로우다운; 기본 -0.6)
"""
from pathlib import Path
import os
import sys
import json

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import numpy as np
import run_etf_backtest as rtb

OUT = ROOT / "outputs_grid"


def mkdir_out():
    OUT.mkdir(exist_ok=True)


def load_filtered_list():
    p = OUT / "filtered_etf_list.json"
    if not p.exists():
        raise RuntimeError("filtered_etf_list.json not found; run filter_candidates first")
    with p.open("r", encoding="utf-8") as f:
        return [str(t) for t in json.load(f)]


def compute_metrics(ticker: str, recent_years: int):
    try:
        df = rtb.get_price(str(ticker))
    except Exception as e:
        return None
    if df is None or df.empty:
        return None
    df = df.sort_values("date").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    end = df["date"].max()
    cutoff = end - pd.Timedelta(days=int(recent_years * 365))
    recent = df[df["date"] >= cutoff].copy()
    if recent.empty:
        return None
    returns = recent["close"].pct_change().dropna()
    vol = float(returns.std(ddof=0) * np.sqrt(252)) if not returns.empty else 0.0
    dd = recent["close"] / recent["close"].cummax() - 1
    recent_mdd = float(dd.min()) if not dd.empty else 0.0
    return {"ticker": str(ticker), "vol": vol, "recent_mdd": recent_mdd, "days": len(recent)}


def main():
    mkdir_out()
    recent_years = int(os.environ.get("RECENT_YEARS", "3"))
    max_vol = float(os.environ.get("MAX_VOL", "0.6"))
    min_recent_mdd = float(os.environ.get("MIN_RECENT_MDD", "-0.6"))

    try:
        candidates = load_filtered_list()
    except Exception as e:
        print("오류: 필터된 리스트를 찾지 못했습니다:", e)
        sys.exit(1)

    print(f"리스크 필터: RECENT_YEARS={recent_years}, MAX_VOL={max_vol}, MIN_RECENT_MDD={min_recent_mdd}")
    metrics = []
    for t in candidates:
        m = compute_metrics(t, recent_years)
        if m is None:
            print(f"  {t}: 데이터 부족 또는 조회 실패")
            continue
        metrics.append(m)
        print(f"  {t}: vol={m['vol']:.3f}, recent_mdd={m['recent_mdd']:.2%}, days={m['days']}")

    metrics_path = OUT / "risk_filter_metrics.json"
    with metrics_path.open("w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)

    # 플래그: vol 초과 또는 최근 mdd가 더 낮으면(더 큰 손실) 제외
    flagged = [m["ticker"] for m in metrics if (m["vol"] > max_vol or m["recent_mdd"] < min_recent_mdd)]
    kept = [t for t in candidates if t not in flagged]

    print(f"총 후보: {len(candidates)}, 제외 대상: {flagged}, 유지: {len(kept)}")
    with (OUT / "risk_filtered_etf_list.json").open("w", encoding="utf-8") as f:
        json.dump(kept, f, ensure_ascii=False, indent=2)

    if not kept:
        print("유지된 후보가 없습니다. 임계값을 완화하세요.")
        sys.exit(1)

    # 재백테스트
    index_df = rtb.get_index_data()
    common_dates = list(index_df["date"])
    rtb.ETF_LIST = kept
    rebalance_days = int(os.environ.get("REBALANCE_STEP_DAYS", str(rtb.REBALANCE_STEP_DAYS)))
    max_positions = int(os.environ.get("MAX_POSITIONS", "3"))

    result, trades = rtb.run_etf_strategy(
        rtb.INITIAL_CASH,
        common_dates,
        index_df,
        use_market_filter=True,
        max_positions=max_positions,
        slippage=float(rtb.BASE_SLIPPAGE),
    )

    fname = OUT / f"risk_filtered_n{len(kept)}_pos{max_positions}_reb{rebalance_days}_vol{max_vol}_mdd{min_recent_mdd}.csv"
    result.to_csv(fname, index=False, encoding="utf-8-sig")
    trades.to_csv(OUT / f"risk_filtered_trades_n{len(kept)}_pos{max_positions}_reb{rebalance_days}.csv", index=False, encoding="utf-8-sig")

    stats = rtb.calc_stats(result, "equity")
    print("=== 리스크 필터 재백테스트 결과 ===")
    print(f"파일: {fname}")
    print(f"CAGR: {stats['cagr']:.2%}")
    print(f"MDD: {stats['mdd']:.2%}")
    print(f"Sharpe: {stats['sharpe']:.3f}")
    print(f"Total Return: {stats['total_return']:.2%}")


if __name__ == "__main__":
    main()
