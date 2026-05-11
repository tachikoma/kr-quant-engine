#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
분석 스크립트: `outputs_grid/filtered_curve_*.csv` 파일을 읽어
요약 통계와 그래프(누적수익, 드로우다운, 일별수익 히스토그램)를 생성합니다.

생성물:
- outputs_grid/filtered_summary.json
- outputs_grid/figures/filtered_equity_curve.png
- outputs_grid/figures/filtered_drawdown.png
- outputs_grid/figures/filtered_daily_returns_hist.png

사용법: `python3 scripts/analyze_filtered_results.py`
"""

from pathlib import Path
import pandas as pd
import numpy as np
import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import sys

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs_grid"
FIG = OUT / "figures"
FIG.mkdir(parents=True, exist_ok=True)

# 가장 최신의 filtered_curve 파일 선택
files = sorted(OUT.glob("filtered_curve_*.csv"))
if not files:
    print("ERROR: filtered_curve 파일을 찾을 수 없습니다.")
    sys.exit(1)

fpath = files[-1]
print("Loading:", fpath)
df = pd.read_csv(fpath, parse_dates=["date"]) 

# 날짜 범위 및 기간
start_date = df["date"].iloc[0]
end_date = df["date"].iloc[-1]
days = (end_date - start_date).days
years = days / 365.25 if days > 0 else 1.0/252

# 지표 계산
equity = df["equity"].astype(float)
start_equity = float(equity.iloc[0])
end_equity = float(equity.iloc[-1])

total_return = end_equity / start_equity - 1.0
cagr = (end_equity / start_equity) ** (1.0 / years) - 1.0 if years>0 else float('nan')

# 일별 수익률 기반
daily_ret = equity.pct_change().dropna()
ann_vol = float(daily_ret.std() * (252 ** 0.5))
ann_ret = float(daily_ret.mean() * 252)
sharpe = float(ann_ret / ann_vol) if ann_vol>0 else float('nan')

# 최대 낙폭
running_max = equity.cummax()
drawdown = (equity - running_max) / running_max
max_dd = float(drawdown.min())

stats = {
    "file": str(fpath.name),
    "start_date": str(start_date.date()),
    "end_date": str(end_date.date()),
    "days": int(days),
    "years": float(years),
    "start_equity": start_equity,
    "end_equity": end_equity,
    "total_return": total_return,
    "cagr": cagr,
    "annual_return": ann_ret,
    "annual_volatility": ann_vol,
    "sharpe": sharpe,
    "max_drawdown": max_dd,
}

# 저장
with open(OUT / "filtered_summary.json", "w", encoding="utf-8") as f:
    json.dump(stats, f, ensure_ascii=False, indent=2)

print("Saved summary to:", OUT / "filtered_summary.json")
print(stats)

# 누적 수익 곡선
plt.figure(figsize=(10, 5))
plt.plot(df["date"], equity, lw=1.5)
plt.title("Filtered Equity Curve")
plt.xlabel("Date")
plt.ylabel("Equity")
plt.grid(True)
plt.tight_layout()
plt.savefig(FIG / "filtered_equity_curve.png")
plt.close()

# 드로우다운
plt.figure(figsize=(10, 5))
plt.plot(df["date"], drawdown, lw=1.5)
plt.title("Drawdown")
plt.xlabel("Date")
plt.ylabel("Drawdown")
plt.grid(True)
plt.tight_layout()
plt.savefig(FIG / "filtered_drawdown.png")
plt.close()

# 일별 수익 히스토그램
plt.figure(figsize=(6, 4))
daily_ret.hist(bins=50)
plt.title("Daily Returns Histogram")
plt.tight_layout()
plt.savefig(FIG / "filtered_daily_returns_hist.png")
plt.close()

print("Plots saved to:", FIG)
print("Done.")
