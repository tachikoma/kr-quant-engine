#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
드로우다운 윈도우 기간 동안 후보 ETF 간 일간 수익률 상관관계 행렬을 계산합니다.

출력:
 - outputs_grid/corr_drawdown_window.csv
 - outputs_grid/corr_drawdown_heatmap.png

사용: uv run scripts/correlation_analysis.py
"""
from pathlib import Path
import json
import os
import sys
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs_grid"
OUT.mkdir(exist_ok=True)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import run_etf_backtest as rtb


def main():
    dd_path = OUT / "drawdown_windows.json"
    etf_list_path = OUT / "filtered_etf_list.json"
    if not dd_path.exists():
        print("drawdown_windows.json not found; run analyze_drawdown_trades.py first")
        sys.exit(1)
    if not etf_list_path.exists():
        print("filtered_etf_list.json not found; run filter_candidates.py first")
        sys.exit(1)

    windows = json.loads(dd_path.read_text(encoding="utf-8"))
    if not windows:
        print("no drawdown windows")
        sys.exit(1)

    # 가장 큰 드로우다운 윈도우 선택
    win = min(windows, key=lambda w: w.get("drawdown", 0))
    peak_date = pd.to_datetime(win["peak_date"]) if win.get("peak_date") else None
    trough_date = pd.to_datetime(win["trough_date"]) if win.get("trough_date") else None
    print(f"Using drawdown window: {peak_date.date()} -> {trough_date.date()} (drawdown={win.get('drawdown')})")

    etfs = json.loads(etf_list_path.read_text(encoding="utf-8"))
    price_frames = []
    good_etfs = []
    for t in etfs:
        try:
            df = rtb.get_price(str(t))
        except Exception as e:
            print(f"price fetch failed for {t}: {e}")
            continue
        if df is None or df.empty:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        mask = (df["date"] >= peak_date) & (df["date"] <= trough_date)
        sub = df.loc[mask, ["date", "close"]].copy()
        if len(sub) < 30:
            print(f"skip {t}: insufficient data in window ({len(sub)} rows)")
            continue
        sub = sub.rename(columns={"close": str(t)})
        sub = sub.set_index("date")
        price_frames.append(sub)
        good_etfs.append(str(t))

    if not price_frames:
        print("no ETF price series available for window")
        sys.exit(1)

    joined = pd.concat(price_frames, axis=1, join="outer").sort_index()
    returns = joined.pct_change().dropna(how="all")
    returns = returns.dropna(axis=1, thresh=5)

    corr = returns.corr()
    corr.to_csv(OUT / "corr_drawdown_window.csv", encoding="utf-8-sig")
    print(f"saved correlation matrix: {OUT / 'corr_drawdown_window.csv'}")

    # heatmap
    fig, ax = plt.subplots(figsize=(8, 6))
    cax = ax.imshow(corr.values, vmin=-1, vmax=1, cmap="coolwarm")
    ax.set_xticks(range(len(corr.columns)))
    ax.set_yticks(range(len(corr.index)))
    ax.set_xticklabels(corr.columns, rotation=90, fontsize=8)
    ax.set_yticklabels(corr.index, fontsize=8)
    fig.colorbar(cax, ax=ax, fraction=0.046, pad=0.04)
    plt.title("ETF Returns Correlation (drawdown window)")
    plt.tight_layout()
    plt.savefig(OUT / "corr_drawdown_heatmap.png", dpi=200)
    plt.close()
    print(f"saved heatmap: {OUT / 'corr_drawdown_heatmap.png'}")

    # summary
    mean_corr = corr.values[np.triu_indices_from(corr.values, k=1)].mean()
    summary = {"num_etfs": len(corr.columns), "mean_pairwise_corr": float(mean_corr)}
    print("Summary:", summary)
    (OUT / "corr_drawdown_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
