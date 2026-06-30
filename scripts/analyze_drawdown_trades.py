#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
드로우다운 원인 분석 및 종목별 손익/거래 로그 상세 분석
입력:
 - outputs_grid/filtered_curve_*.csv
 - outputs_grid/filtered_trades_*.csv
출력:
 - outputs_grid/analysis_per_ticker.json
 - outputs_grid/drawdown_windows.json
 - outputs_grid/drawdown_trades.csv
 - outputs_grid/figures/top_ticker_pnl.png

사용: uv run scripts/analyze_drawdown_trades.py
"""

from pathlib import Path
import pandas as pd
import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import sys

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs_grid"
FIG = OUT / "figures"
FIG.mkdir(parents=True, exist_ok=True)

# 프로젝트 루트를 sys.path에 추가하여 로컬 모듈(run_etf_backtest) 임포트 허용
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import run_etf_backtest as rtb

# 최신 파일 찾기
curve_files = sorted(OUT.glob("filtered_curve_*.csv"))
trade_files = sorted(OUT.glob("filtered_trades_*.csv"))
if not curve_files or not trade_files:
    print("filtered_curve or filtered_trades files not found in outputs_grid")
    sys.exit(1)

curve_path = curve_files[-1]
trades_path = trade_files[-1]
print("Using:", curve_path.name, trades_path.name)

# 로드
curve = pd.read_csv(curve_path, parse_dates=["date"]).sort_values("date").reset_index(drop=True)
trades = pd.read_csv(trades_path, parse_dates=["date"]).sort_values("date").reset_index(drop=True)

# 기본 포트폴리오 드로우다운 계산
equity = curve["equity"].astype(float)
running_max = equity.cummax()
drawdown = (equity - running_max) / running_max
curve = curve.assign(drawdown=drawdown)

# 주요 드로우다운 트로프(하위 3개 최소값)
n_troughs = 3
troughs = curve.nsmallest(n_troughs, "drawdown")[ ["date", "drawdown"] ]
troughs = troughs.reset_index()

# 각 트로프에 대해 피크(시작) 날짜 찾기
windows = []
for _, row in troughs.iterrows():
    idx = int(row["index"])  # index in curve
    trough_date = row["date"]
    # peak index: find the first index where the running max reached the peak value
    peak_val = running_max.iloc[: idx + 1].max()
    peak_candidates = running_max.iloc[: idx + 1][running_max.iloc[: idx + 1] == peak_val]
    if not peak_candidates.empty:
        peak_idx = int(peak_candidates.index[0])
    else:
        peak_idx = 0
    peak_date = curve.loc[peak_idx, "date"]
    windows.append({
        "peak_idx": int(peak_idx),
        "peak_date": str(pd.to_datetime(peak_date).date()),
        "trough_idx": int(idx),
        "trough_date": str(pd.to_datetime(trough_date).date()),
        "drawdown": float(row["drawdown"]),
    })

# FIFO 매칭으로 실현 PnL 계산
trades = trades.rename(columns={c: c.strip() for c in trades.columns})
# Ensure required cols
for c in ["date", "ticker", "side", "qty", "price"]:
    if c not in trades.columns:
        print("Missing column in trades:", c)
        sys.exit(1)

trades["qty"] = trades["qty"].astype(float)
trades["price"] = trades["price"].astype(float)
trades["side"] = trades["side"].astype(str).str.upper()

# prepare structures
buy_lots = {}  # ticker -> list of dicts {qty, price, date}
realized_records = []

for i, t in trades.iterrows():
    ticker = t["ticker"]
    side = t["side"]
    qty = int(round(float(t["qty"])))
    price = float(t["price"])
    date = t["date"]

    if ticker not in buy_lots:
        buy_lots[ticker] = []

    if qty == 0:
        continue

    if side == "BUY":
        buy_lots[ticker].append({"qty": qty, "price": price, "date": date})
    elif side == "SELL":
        qty_to_sell = qty
        while qty_to_sell > 0 and buy_lots[ticker]:
            lot = buy_lots[ticker][0]
            m = min(qty_to_sell, lot["qty"]) 
            pnl = m * (price - lot["price"])  # basic realized PnL
            holding_days = (pd.to_datetime(date) - pd.to_datetime(lot["date"])).days
            realized_records.append({
                "sell_date": pd.to_datetime(date).date().isoformat(),
                "ticker": ticker,
                "qty": int(m),
                "sell_price": price,
                "buy_price": lot["price"],
                "buy_date": pd.to_datetime(lot["date"]).date().isoformat(),
                "realized_pnl": float(pnl),
                "holding_days": int(holding_days),
            })
            # decrease lot qty
            lot["qty"] -= m
            qty_to_sell -= m
            if lot["qty"] == 0:
                buy_lots[ticker].pop(0)
        if qty_to_sell > 0:
            # unmatched sell (short) - record as negative
            realized_records.append({
                "sell_date": pd.to_datetime(date).date().isoformat(),
                "ticker": ticker,
                "qty": int(qty_to_sell),
                "sell_price": price,
                "buy_price": None,
                "buy_date": None,
                "realized_pnl": float(-qty_to_sell * price),
                "holding_days": None,
            })

# DataFrame of realized records
realized_df = pd.DataFrame(realized_records)
if realized_df.empty:
    print("No realized trades found")
else:
    realized_df.to_csv(OUT / "realized_trades_detailed.csv", index=False, encoding="utf-8-sig")

# Aggregate per ticker
agg = realized_df.groupby("ticker").agg(
    realized_pnl=("realized_pnl", "sum"),
    trade_count=("qty", "count"),
    total_qty=("qty", "sum"),
    avg_holding_days=("holding_days", "mean"),
).reset_index()

# For drawdown windows, compute realized pnl for sells in window and unrealized (MTM) pnl
for w in windows:
    peak_date = pd.to_datetime(w["peak_date"]).date()
    trough_date = pd.to_datetime(w["trough_date"]).date()

    # 실현 PnL: 윈도우 내에 발생한 매도(실현) 합계
    mask = (
        (pd.to_datetime(realized_df["sell_date"]).dt.date >= peak_date)
        & (pd.to_datetime(realized_df["sell_date"]).dt.date <= trough_date)
    )
    df_win = realized_df[mask]
    per_ticker_real_df = df_win.groupby("ticker").agg(
        window_realized_pnl=("realized_pnl", "sum"), trades=("qty", "count")
    ).reset_index()
    w["per_ticker_realized_pnl"] = per_ticker_real_df.sort_values("window_realized_pnl").to_dict(orient="records")

    # 미실현 PnL (마크투마켓): 피크일 보유 수량을 재구성하고 피크->트로프 종가 차이로 계산
    trades_up_to_peak = trades[pd.to_datetime(trades["date"]).dt.date <= peak_date].copy()
    trades_up_to_peak["side"] = trades_up_to_peak["side"].astype(str).str.upper()
    holdings_at_peak = {}
    for tkn, grp in trades_up_to_peak.groupby("ticker"):
        buys = grp.loc[grp["side"] == "BUY", "qty"].sum()
        sells = grp.loc[grp["side"] == "SELL", "qty"].sum()
        net = int(round(float(buys) - float(sells)))
        if net > 0:
            holdings_at_peak[str(tkn)] = net

    per_ticker_unreal = []
    unreal_map = {}
    # 가격 데이터 조회 및 피크-트로프 간 종가 차이로 미실현 PnL 계산
    for tkn, qty in holdings_at_peak.items():
        try:
            price_df = rtb.get_price(str(tkn))
            price_df = price_df.sort_values("date")

            def _last_close_on_or_before(df, d):
                df2 = df[pd.to_datetime(df["date"]).dt.date <= d]
                if df2.empty:
                    return None
                return float(df2.iloc[-1]["close"])

            close_peak = _last_close_on_or_before(price_df, peak_date)
            close_trough = _last_close_on_or_before(price_df, trough_date)
            if close_peak is None or close_trough is None:
                continue
            unreal = float(qty) * (close_trough - close_peak)
            per_ticker_unreal.append(
                {
                    "ticker": tkn,
                    "qty": int(qty),
                    "close_peak": float(close_peak),
                    "close_trough": float(close_trough),
                    "unrealized_pnl": float(unreal),
                }
            )
            unreal_map[str(tkn)] = float(unreal)
        except Exception as e:
            print(f"[analyze] 가격 조회 실패: {tkn} -> {e}")
            continue

    w["per_ticker_unrealized_pnl"] = sorted(per_ticker_unreal, key=lambda x: x["unrealized_pnl"]) if per_ticker_unreal else []

    # 총 기여: 실현 + 미실현
    real_map = {str(int(r["ticker"])) if isinstance(r["ticker"], (int, float)) else str(r["ticker"]): float(r["window_realized_pnl"]) for r in per_ticker_real_df.to_dict(orient="records")} if not per_ticker_real_df.empty else {}
    all_keys = set(list(real_map.keys()) + list(unreal_map.keys()))
    per_ticker_total = []
    for k in all_keys:
        r = real_map.get(k, 0.0)
        u = unreal_map.get(k, 0.0)
        total = float(r) + float(u)
        per_ticker_total.append({"ticker": k, "realized_pnl": float(r), "unrealized_pnl": float(u), "total_pnl": float(total)})

    w["per_ticker_total_pnl"] = sorted(per_ticker_total, key=lambda x: x["total_pnl"]) if per_ticker_total else []

# Save outputs
analysis = {
    "ticker_summary": agg.sort_values("realized_pnl").to_dict(orient="records"),
}
with open(OUT / "analysis_per_ticker.json", "w", encoding="utf-8") as f:
    json.dump(analysis, f, ensure_ascii=False, indent=2)

with open(OUT / "drawdown_windows.json", "w", encoding="utf-8") as f:
    json.dump(windows, f, ensure_ascii=False, indent=2)

# Save drawdown trades (sells within windows)
all_window_trades = []
for w in windows:
    peak_date = pd.to_datetime(w["peak_date"]).date()
    trough_date = pd.to_datetime(w["trough_date"]).date()
    mask = (pd.to_datetime(realized_df["sell_date"]).dt.date >= peak_date) & (pd.to_datetime(realized_df["sell_date"]).dt.date <= trough_date)
    df_win = realized_df[mask].copy()
    df_win["window_peak_date"] = w["peak_date"]
    df_win["window_trough_date"] = w["trough_date"]
    all_window_trades.append(df_win)

if all_window_trades:
    pd.concat(all_window_trades, ignore_index=True).to_csv(OUT / "drawdown_trades.csv", index=False, encoding="utf-8-sig")

# Plot: top N tickers by realized pnl (overall)
if not agg.empty:
    top = agg.sort_values("realized_pnl").tail(20)
    plt.figure(figsize=(8, 6))
    plt.barh(top["ticker"].astype(str), top["realized_pnl"])
    plt.title("Top 20 tickers by realized PnL")
    plt.xlabel("Realized PnL")
    plt.tight_layout()
    plt.savefig(FIG / "top_ticker_pnl.png")
    plt.close()

print("Analysis saved:", OUT / "analysis_per_ticker.json", OUT / "drawdown_windows.json", OUT / "drawdown_trades.csv")
print("Done.")
