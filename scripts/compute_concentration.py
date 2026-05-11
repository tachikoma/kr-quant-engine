#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
피크 시 포지션 노출(금액)과 포트폴리오 대비 비중을 계산합니다.

출력:
 - outputs_grid/peak_exposure.csv
 - outputs_grid/peak_exposure_summary.json

사용: uv run scripts/compute_concentration.py
"""
from pathlib import Path
import json
import pandas as pd
import sys

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs_grid"


def find_latest_filtered_curve():
    files = sorted(OUT.glob("filtered_curve_*.csv"))
    if not files:
        return None
    return files[-1]


def main():
    OUT.mkdir(exist_ok=True)
    dd_path = OUT / "drawdown_windows.json"
    if not dd_path.exists():
        print("drawdown_windows.json가 없습니다. 먼저 analyze_drawdown_trades.py를 실행하세요.")
        sys.exit(1)

    with dd_path.open("r", encoding="utf-8") as f:
        windows = json.load(f)
    # 가장 큰 드로우다운 윈도우 선택
    if not windows:
        print("drawdown_windows.json에 윈도우가 없습니다.")
        sys.exit(1)
    win = min(windows, key=lambda w: w.get("drawdown", 0))
    peak_date = win.get("peak_date")

    # 노출 데이터
    unreal = win.get("per_ticker_unrealized_pnl", [])
    rows = []
    for r in unreal:
        try:
            t = str(r.get("ticker"))
            qty = int(r.get("qty", 0))
            close_peak = float(r.get("close_peak", 0.0))
            exposure = qty * close_peak
            rows.append({"ticker": t, "qty": qty, "close_peak": close_peak, "exposure": exposure})
        except Exception:
            continue

    df = pd.DataFrame(rows)
    if df.empty:
        print("피크에 보유된 종목 정보가 없습니다.")
        sys.exit(0)

    # 피크 시 포트폴리오 값 조회
    curve_file = find_latest_filtered_curve()
    peak_equity = None
    if curve_file and curve_file.exists():
        curve = pd.read_csv(curve_file, parse_dates=["date"]).sort_values("date")
        try:
            pd_peak = pd.to_datetime(peak_date)
            # 일치하는 날짜가 없으면 직전 날짜 사용
            df_before = curve[curve["date"] <= pd_peak]
            if not df_before.empty:
                peak_equity = float(df_before.iloc[-1]["equity"])
        except Exception:
            peak_equity = None

    df["exposure_pct_of_peak"] = df["exposure"] / (peak_equity if peak_equity else df["exposure"].sum())
    df = df.sort_values("exposure", ascending=False)
    df.to_csv(OUT / "peak_exposure.csv", index=False, encoding="utf-8-sig")

    summary = {
        "peak_date": peak_date,
        "curve_file_used": str(curve_file.name) if curve_file else None,
        "peak_equity": peak_equity,
        "total_exposure": float(df["exposure"].sum()),
        "top_exposures": df.head(10).to_dict(orient="records"),
    }
    with (OUT / "peak_exposure_summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("피크 노출 요약 저장:", OUT / "peak_exposure_summary.json")
    print(df.head(10).to_string(index=False))


if __name__ == "__main__":
    main()
