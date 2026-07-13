#!/usr/bin/env python3
"""비용 포함 현금흐름으로 완결 거래와 종목별 실현성과를 분해한다."""

from __future__ import annotations

import json
import os
from collections import defaultdict, deque
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRADE_FILE = ROOT / "outputs_etf_only" / "etf_trades.csv"
OUTPUT_DIR = ROOT / "outputs_trade_analysis"


def _net_value(row: pd.Series) -> tuple[float, bool]:
    if "net_value" in row and pd.notna(row["net_value"]):
        return abs(float(row["net_value"])), True
    return abs(float(row["qty"]) * float(row["price"])), False


def attribute_trades(trades: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    required = {"date", "ticker", "side", "qty", "price"}
    missing = required - set(trades.columns)
    if missing:
        raise ValueError(f"거래 파일 필수 컬럼 누락: {sorted(missing)}")
    data = trades.copy()
    data["date"] = pd.to_datetime(data["date"])
    data["ticker"] = data["ticker"].astype(str).str.zfill(6)
    data["side"] = data["side"].astype(str).str.upper()
    data["_sequence"] = range(len(data))
    data = data[data["side"].isin(["BUY", "SELL"])].sort_values(
        ["date", "_sequence"], kind="stable"
    )
    ticker_names = (
        data.drop_duplicates("ticker").set_index("ticker")["name"].to_dict()
        if "name" in data
        else {}
    )

    lots: dict[str, deque] = defaultdict(deque)
    realized = []
    cost_aware = True
    trade_id = 0
    for _, row in data.iterrows():
        ticker = row["ticker"]
        qty = int(row["qty"])
        if qty <= 0:
            continue
        value, row_cost_aware = _net_value(row)
        cost_aware = cost_aware and row_cost_aware
        unit_value = value / qty
        if row["side"] == "BUY":
            lots[ticker].append(
                {"qty": qty, "unit_cost": unit_value, "buy_date": row["date"]}
            )
            continue

        remaining = qty
        while remaining > 0 and lots[ticker]:
            lot = lots[ticker][0]
            matched_qty = min(remaining, lot["qty"])
            allocated_cost = matched_qty * lot["unit_cost"]
            allocated_proceeds = matched_qty * unit_value
            pnl = allocated_proceeds - allocated_cost
            trade_id += 1
            realized.append(
                {
                    "trade_id": trade_id,
                    "ticker": ticker,
                    "name": ticker_names.get(ticker, ticker),
                    "buy_date": lot["buy_date"],
                    "sell_date": row["date"],
                    "holding_days": int((row["date"] - lot["buy_date"]).days),
                    "qty": matched_qty,
                    "cost": allocated_cost,
                    "proceeds": allocated_proceeds,
                    "net_pnl": pnl,
                    "return_pct": pnl / allocated_cost if allocated_cost > 0 else np.nan,
                }
            )
            lot["qty"] -= matched_qty
            remaining -= matched_qty
            if lot["qty"] == 0:
                lots[ticker].popleft()
        if remaining:
            raise ValueError(f"매수 이력보다 많은 매도: ticker={ticker}, unmatched_qty={remaining}")

    realized_df = pd.DataFrame(realized)
    if realized_df.empty:
        raise RuntimeError("완결된 매수-매도 거래가 없습니다.")
    ticker_df = (
        realized_df.groupby(["ticker", "name"], as_index=False)
        .agg(
            completed_lots=("trade_id", "count"),
            total_cost=("cost", "sum"),
            net_pnl=("net_pnl", "sum"),
            avg_return_pct=("return_pct", "mean"),
            win_rate=("net_pnl", lambda values: float((values > 0).mean())),
            avg_holding_days=("holding_days", "mean"),
        )
        .sort_values("net_pnl", ascending=False)
    )
    wins = realized_df.loc[realized_df["net_pnl"] > 0, "net_pnl"]
    losses = realized_df.loc[realized_df["net_pnl"] < 0, "net_pnl"]
    open_positions = {
        ticker: int(sum(lot["qty"] for lot in ticker_lots))
        for ticker, ticker_lots in lots.items()
        if ticker_lots
    }
    summary = {
        "cost_aware": cost_aware,
        "completed_lots": len(realized_df),
        "net_realized_pnl": float(realized_df["net_pnl"].sum()),
        "win_rate": float((realized_df["net_pnl"] > 0).mean()),
        "profit_factor": float(wins.sum() / abs(losses.sum())) if not losses.empty else None,
        "avg_return_pct": float(realized_df["return_pct"].mean()),
        "median_return_pct": float(realized_df["return_pct"].median()),
        "avg_holding_days": float(realized_df["holding_days"].mean()),
        "best_trade_pnl": float(realized_df["net_pnl"].max()),
        "worst_trade_pnl": float(realized_df["net_pnl"].min()),
        "open_positions_qty": open_positions,
        "note": "실현손익만 포함하며 미실현손익과 현금분배금은 별도입니다.",
    }
    return realized_df, ticker_df, summary


def main() -> None:
    trade_file = Path(os.environ.get("TRADE_FILE", str(DEFAULT_TRADE_FILE)))
    trades = pd.read_csv(trade_file, dtype={"ticker": str})
    realized, by_ticker, summary = attribute_trades(trades)
    OUTPUT_DIR.mkdir(exist_ok=True)
    realized.to_csv(OUTPUT_DIR / "realized_trade_lots.csv", index=False, encoding="utf-8-sig")
    by_ticker.to_csv(OUTPUT_DIR / "performance_by_ticker.csv", index=False, encoding="utf-8-sig")
    (OUTPUT_DIR / "trade_performance_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
