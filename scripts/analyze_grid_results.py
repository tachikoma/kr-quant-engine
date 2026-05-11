#!/usr/bin/env python3
"""
그리드 백테스트 결과 자동 분석 및 시각화

- outputs_grid에 저장된 `grid_summary.csv`, `curve_*.csv`, `trades_*.csv`를 읽어
  누적수익/드로우다운 플롯을 생성합니다.
- 각 시나리오별 턴오버(연간 거래수, 거래금액 대비 회전율) 통계를 계산합니다.
- 상위 시나리오(기본: CAGR 기준 상위 3개)에 대해 슬리피지 민감도(근사치)를 계산하고
  슬리피지에 따른 CAGR/MDD/Sharpe 변화를 플롯으로 저장합니다.
"""
from __future__ import annotations

import sys
from pathlib import Path
import os
import json

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

import run_etf_backtest as rtb


# 출력 경로
OUT = Path("outputs_grid")
FIGDIR = OUT / "figures"
FIGDIR.mkdir(exist_ok=True)


def load_grid_summary(path: Path) -> pd.DataFrame:
    return pd.read_csv(path)


def plot_equity_and_drawdown(df: pd.DataFrame, title: str, outpath: Path):
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    eq = df["equity"]
    norm = eq / eq.iloc[0]
    drawdown = eq / eq.cummax() - 1

    fig, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    axes[0].plot(df["date"], norm, label="Normalized Equity")
    axes[0].set_ylabel("Cumulative")
    axes[0].set_title(title)
    axes[0].grid(True)

    axes[1].plot(df["date"], drawdown, color="tab:red", label="Drawdown")
    axes[1].set_ylabel("Drawdown")
    axes[1].set_xlabel("Date")
    axes[1].grid(True)

    fig.tight_layout()
    fig.savefig(outpath)
    plt.close(fig)


def compute_turnover_stats(curve_df: pd.DataFrame, trades_df: pd.DataFrame) -> dict:
    """단순한 턴오버 지표를 계산합니다.

    - trades_per_year: 연평균 트레이드 건수
    - total_traded_value: 전체 거래 금액 합계(매수+매도)
    - turnover_pct: 전체 거래금액 / 평균자산
    """
    if trades_df is None or trades_df.empty:
        return {
            "trades": 0,
            "trades_per_year": 0.0,
            "total_traded_value": 0.0,
            "turnover_pct": 0.0,
            "avg_trade_value": 0.0,
            "unique_tickers": 0,
        }

    trades = trades_df.copy()
    trades["price"] = trades["price"].astype(float)
    trades["qty"] = trades["qty"].astype(float)
    trades["trade_value"] = (trades["price"] * trades["qty"]).abs()

    total_traded_value = trades["trade_value"].sum()

    period = rtb.get_backtest_period(curve_df, "equity")
    years = period.get("years", 1.0)
    trades_per_year = len(trades) / years if years > 0 else float(len(trades))

    avg_equity = float(curve_df["equity"].mean()) if not curve_df.empty else 0.0
    turnover_pct = total_traded_value / avg_equity if avg_equity > 0 else 0.0

    return {
        "trades": len(trades),
        "trades_per_year": trades_per_year,
        "total_traded_value": total_traded_value,
        "turnover_pct": turnover_pct,
        "avg_trade_value": total_traded_value / len(trades) if len(trades) > 0 else 0.0,
        "unique_tickers": trades["ticker"].nunique(),
    }


def approx_slippage_adjusted_curve(curve_df: pd.DataFrame, trades_df: pd.DataFrame, base_slip: float, new_slip: float) -> pd.DataFrame:
    """근사 방식으로 기존 트레이드에 대해 슬리피지 차이를 적용한 지분 곡선을 반환합니다.

    논리: 각 트레이드에서 슬리피지 차이에 따른 비용(또는 수익 감소)을 계산하고,
    해당 거래일 이후의 지분에서 누적 차감을 적용합니다.
    이 방법은 체결 타이밍·수량이 동일하다고 가정한 근사치입니다.
    """
    base = float(base_slip)
    new = float(new_slip)

    res = curve_df.copy().sort_values("date").reset_index(drop=True)
    res["date"] = pd.to_datetime(res["date"])

    if trades_df is None or trades_df.empty:
        res["equity_adj"] = res["equity"].copy()
        return res

    trades = trades_df.copy()
    trades["date"] = pd.to_datetime(trades["date"])
    trades["price"] = trades["price"].astype(float)
    trades["qty"] = trades["qty"].astype(float)

    buy_fee = rtb.BUY_FEE_PCT
    sell_fee = rtb.SELL_FEE_PCT
    sell_tax = rtb.ETF_SELL_TAX_PCT

    deltas = []
    for _, r in trades.iterrows():
        side = str(r.get("side", "")).upper()
        price = float(r["price"])
        qty = float(r["qty"])
        if side == "BUY":
            base_cost = qty * price * (1 + base) * (1 + buy_fee)
            new_cost = qty * price * (1 + new) * (1 + buy_fee)
            delta = new_cost - base_cost  # 추가 비용
        else:  # SELL
            base_val = qty * price * (1 - base) * (1 - sell_fee - sell_tax)
            new_val = qty * price * (1 - new) * (1 - sell_fee - sell_tax)
            delta = base_val - new_val  # 수익 감소

        deltas.append((pd.to_datetime(r["date"]), delta))

    if not deltas:
        res["equity_adj"] = res["equity"].copy()
        return res

    deltas_df = pd.DataFrame(deltas, columns=["date", "delta_on_date"])
    grouped = deltas_df.groupby("date").sum().reset_index()

    merged = res.merge(grouped, on="date", how="left")
    merged["delta_on_date"] = merged["delta_on_date"].fillna(0.0)
    merged["cum_delta"] = merged["delta_on_date"].cumsum()
    merged["equity_adj"] = merged["equity"] - merged["cum_delta"]

    return merged


def run_analysis():
    summary_path = OUT / "grid_summary.csv"
    if not summary_path.exists():
        raise SystemExit(f"grid summary not found: {summary_path}")

    grid = load_grid_summary(summary_path)

    turnover_rows = []

    # 각 시나리오에 대해 플롯 및 턴오버 계산
    for _, row in grid.iterrows():
        n = int(row["n_candidates"])
        reb = int(row["rebalance_step_days"])
        pos = int(row["max_positions"])

        curve_file = OUT / f"curve_n{n}_reb{reb}_pos{pos}.csv"
        trades_file = OUT / f"trades_n{n}_reb{reb}_pos{pos}.csv"

        if not curve_file.exists():
            print(f"곡선 파일 없음: {curve_file} — 스킵")
            continue

        curve = pd.read_csv(curve_file)
        if "equity" not in curve.columns:
            # 일부 파일명/포맷 변동에 대비
            equity_cols = [c for c in curve.columns if c.startswith("equity")]
            if not equity_cols:
                print(f"equity 컬럼을 찾을 수 없음: {curve_file} — 스킵")
                continue
            curve = curve.rename(columns={equity_cols[0]: "equity"})

        trades = pd.read_csv(trades_file) if trades_file.exists() else pd.DataFrame()

        title = f"n={n} reb={reb} pos={pos}"
        figpath = FIGDIR / f"curve_n{n}_reb{reb}_pos{pos}.png"
        plot_equity_and_drawdown(curve, title, figpath)

        tstats = compute_turnover_stats(curve, trades)
        turnover_rows.append({"n_candidates": n, "rebalance_step_days": reb, "max_positions": pos, **tstats})

    turnover_df = pd.DataFrame(turnover_rows)
    turnover_df.to_csv(OUT / "turnover_summary.csv", index=False, encoding="utf-8-sig")

    # 슬리피지 민감도: CAGR 기준 상위 3개 시나리오에 대해 근사 슬리피지 민감도 계산
    top_n = 3
    top_scenarios = grid.sort_values("cagr", ascending=False).head(top_n)
    slippage_options = [0.0005, 0.001, 0.002, 0.003, 0.005]

    for _, row in top_scenarios.iterrows():
        n = int(row["n_candidates"])
        reb = int(row["rebalance_step_days"])
        pos = int(row["max_positions"])

        curve_file = OUT / f"curve_n{n}_reb{reb}_pos{pos}.csv"
        trades_file = OUT / f"trades_n{n}_reb{reb}_pos{pos}.csv"
        curve = pd.read_csv(curve_file)
        if "equity" not in curve.columns:
            eqcols = [c for c in curve.columns if c.startswith("equity")]
            curve = curve.rename(columns={eqcols[0]: "equity"})

        trades = pd.read_csv(trades_file) if trades_file.exists() else pd.DataFrame()

        base_slip = float(rtb.BASE_SLIPPAGE)
        rows = []
        # 각 슬리피지에 대해 근사 적용
        for slip in slippage_options:
            adj = approx_slippage_adjusted_curve(curve, trades, base_slip, slip)
            # 중복된 'equity' 컬럼이 생기는 것을 방지하기 위해 원본 'equity'는 제외하고 'equity_adj'만 전달
            tmp = adj[["date", "equity_adj"]].rename(columns={"equity_adj": "equity"})
            stats = rtb.calc_stats(tmp, "equity")
            rows.append({"slippage": slip, "cagr": stats["cagr"], "mdd": stats["mdd"], "sharpe": stats["sharpe"], "final": stats["final"]})

        sframe = pd.DataFrame(rows)
        sframe.to_csv(OUT / f"slippage_n{n}_reb{reb}_pos{pos}.csv", index=False, encoding="utf-8-sig")

        # 시각화: CAGR/MDD/Sharpe vs 슬리피지
        fig, ax1 = plt.subplots(figsize=(8, 4))
        ax1.plot(sframe["slippage"] * 10000, sframe["cagr"] * 100, marker="o", label="CAGR(%)", color="tab:blue")
        ax1.set_xlabel("Slippage (bp)")
        ax1.set_ylabel("CAGR (%)", color="tab:blue")
        ax1.tick_params(axis="y", labelcolor="tab:blue")

        ax2 = ax1.twinx()
        ax2.plot(sframe["slippage"] * 10000, sframe["mdd"] * 100, marker="x", label="MDD(%)", color="tab:red")
        ax2.set_ylabel("MDD (%)", color="tab:red")
        ax2.tick_params(axis="y", labelcolor="tab:red")

        fig.suptitle(f"Slippage Sensitivity: n={n} reb={reb} pos={pos}")
        fig.tight_layout()
        fig.savefig(FIGDIR / f"slippage_n{n}_reb{reb}_pos{pos}.png")
        plt.close(fig)

    print("분석 완료: 그림 및 요약 파일은 outputs_grid에 저장되었습니다.")


if __name__ == "__main__":
    run_analysis()
