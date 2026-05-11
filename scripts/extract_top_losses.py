#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
상위 손실 트레이드(실현/드로우다운 내 거래)를 추출합니다.

출력:
 - outputs_grid/top_loss_realized.csv
 - outputs_grid/top_loss_drawdown_trades.csv
 - outputs_grid/top_loss_ticker_realized.csv

사용: uv run scripts/extract_top_losses.py
"""
from pathlib import Path
import pandas as pd
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs_grid"


def main():
    OUT.mkdir(exist_ok=True)
    top_n = int(os.environ.get("TOP_N", "20"))

    realized_path = OUT / "realized_trades_detailed.csv"
    drawdown_path = OUT / "drawdown_trades.csv"

    if not realized_path.exists() and not drawdown_path.exists():
        print("realized_trades_detailed.csv 및 drawdown_trades.csv 둘 다 없습니다. 먼저 분석 스크립트를 실행하세요.")
        sys.exit(1)

    if realized_path.exists():
        df = pd.read_csv(realized_path)
        if "realized_pnl" in df.columns:
            top_real = df.sort_values("realized_pnl").head(top_n)
            top_real.to_csv(OUT / "top_loss_realized.csv", index=False, encoding="utf-8-sig")
            print(f"실현 손실 상위 {top_n}개 저장: {OUT / 'top_loss_realized.csv'}")

            # 종목별 집계
            agg = df.groupby("ticker").agg(total_realized_pnl=("realized_pnl", "sum"), trades=("qty", "count")).reset_index()
            agg = agg.sort_values("total_realized_pnl").head(top_n)
            agg.to_csv(OUT / "top_loss_ticker_realized.csv", index=False, encoding="utf-8-sig")
            print(f"종목별 실현 손실 상위 {top_n}개 저장: {OUT / 'top_loss_ticker_realized.csv'}")
        else:
            print("realized_trades_detailed.csv에 'realized_pnl' 컬럼이 없습니다.")

    if drawdown_path.exists():
        ddf = pd.read_csv(drawdown_path)
        if "realized_pnl" in ddf.columns:
            top_draw = ddf.sort_values("realized_pnl").head(top_n)
            top_draw.to_csv(OUT / "top_loss_drawdown_trades.csv", index=False, encoding="utf-8-sig")
            print(f"드로우다운 윈도우 내 손실 상위 {top_n}개 저장: {OUT / 'top_loss_drawdown_trades.csv'}")
        else:
            print("drawdown_trades.csv에 'realized_pnl' 컬럼이 없습니다.")


if __name__ == "__main__":
    main()
