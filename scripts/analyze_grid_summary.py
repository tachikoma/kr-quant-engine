#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
그리드 요약 분석 스크립트

- 입력: outputs_grid/grid_summary.csv
- 출력:
  - outputs_grid/grid_summary_by_n.csv
  - outputs_grid/grid_report.json
  - outputs_grid/figures/grid_mdd_by_n.png
  - outputs_grid/figures/grid_cagr_by_n.png

Usage: `uv run scripts/analyze_grid_summary.py`
"""

from pathlib import Path
import pandas as pd
import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
IN_FILE = ROOT / "outputs_grid" / "grid_summary.csv"
OUT_DIR = ROOT / "outputs_grid"
FIG_DIR = OUT_DIR / "figures"
FIG_DIR.mkdir(parents=True, exist_ok=True)

if not IN_FILE.exists():
    print("grid_summary.csv not found at", IN_FILE)
    raise SystemExit(1)

df = pd.read_csv(IN_FILE)

# 기본 통계: 그룹화 by n_candidates
group = df.groupby("n_candidates")
agg = group.agg(
    count=("cagr", "count"),
    mean_cagr=("cagr", "mean"),
    median_cagr=("cagr", "median"),
    std_cagr=("cagr", "std"),
    mean_mdd=("mdd", "mean"),
    median_mdd=("mdd", "median"),
    std_mdd=("mdd", "std"),
    mean_sharpe=("sharpe", "mean"),
    mean_volatility=("volatility", "mean"),
).reset_index()

agg.to_csv(OUT_DIR / "grid_summary_by_n.csv", index=False, encoding="utf-8-sig")

# 상세 리포트 JSON
report = {
    "by_n": agg.to_dict(orient="records"),
    "overall_count": int(len(df)),
}

with open(OUT_DIR / "grid_report.json", "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)

print("Saved:", OUT_DIR / "grid_summary_by_n.csv", OUT_DIR / "grid_report.json")

# Boxplot: MDD by n_candidates
plt.figure(figsize=(6, 4))
df_box = df[["n_candidates", "mdd"]].copy()
df_box["mdd_pct"] = df_box["mdd"] * 100
groups = [grp["mdd_pct"].values for name, grp in df_box.groupby("n_candidates")]
labels = [str(int(name)) for name, grp in df_box.groupby("n_candidates")]
plt.boxplot(groups, labels=labels, showmeans=True)
plt.title("MDD by n_candidates (%)")
plt.ylabel("MDD (%)")
plt.xlabel("n_candidates")
plt.grid(axis="y", linestyle="--", alpha=0.4)
plt.tight_layout()
plt.savefig(FIG_DIR / "grid_mdd_by_n.png")
plt.close()

# Bar: mean CAGR and mean MDD
plt.figure(figsize=(8, 4))
width = 0.35
x = range(len(agg))
plt.bar([i - width/2 for i in x], agg["mean_cagr"] * 100, width=width, label="Mean CAGR (%)")
plt.bar([i + width/2 for i in x], agg["mean_mdd"] * 100, width=width, label="Mean MDD (%)")
plt.xticks(x, agg["n_candidates"].astype(int).astype(str))
plt.ylabel("Percent (%)")
plt.title("Mean CAGR vs Mean MDD by n_candidates")
plt.legend()
plt.grid(axis="y", linestyle="--", alpha=0.4)
plt.tight_layout()
plt.savefig(FIG_DIR / "grid_cagr_by_n.png")
plt.close()

print("Figures saved to:", FIG_DIR)
print("Done.")
