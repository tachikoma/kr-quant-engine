#!/usr/bin/env python3
"""현재 운용 파라미터 주변의 성과 안정성을 검증한다."""

from __future__ import annotations

import json
import os
import sys
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run_etf_backtest as rtb
from config_utils import parse_fraction_env

OUTPUT_DIR = ROOT / "outputs_stability"


def parse_values(name: str, default: list, cast) -> list:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        values = sorted({cast(value.strip()) for value in raw.split(",") if value.strip()})
    except ValueError as exc:
        raise ValueError(f"{name} 파싱 실패: {raw}") from exc
    if not values:
        raise ValueError(f"{name} 값이 비어 있습니다.")
    return values


def run_stability() -> tuple[pd.DataFrame, dict]:
    central_rebalance = int(rtb.REBALANCE_STEP_DAYS)
    central_positions = int(rtb.ETF_MAX_POSITIONS)
    central_cap = parse_fraction_env("MAX_ASSET_PCT", 0.50)
    rebalances = parse_values(
        "STABILITY_REBALANCE_DAYS",
        sorted({max(1, central_rebalance - 5), central_rebalance, central_rebalance + 5}),
        int,
    )
    positions = parse_values(
        "STABILITY_MAX_POSITIONS",
        sorted({max(1, central_positions - 1), central_positions, central_positions + 1}),
        int,
    )
    caps = parse_values(
        "STABILITY_MAX_ASSET_PCT",
        sorted({max(0.0, central_cap - 0.15), central_cap, min(1.0, central_cap + 0.15)}),
        float,
    )
    if any(value <= 0 for value in rebalances + positions):
        raise ValueError("리밸런싱 주기와 보유 종목 수는 양수여야 합니다.")
    if any(value < 0 or value > 1 for value in caps):
        raise ValueError("자산별 비중 한도는 0~1 범위여야 합니다.")

    print("[stability] 데이터 1회 로드")
    index_df = rtb.get_index_data()
    common_dates = list(index_df["date"])
    price_data = rtb.load_etf_price()
    risk_off_liquidate = rtb.strategy_cfg.get("liquidate_on_risk_off", True)

    rows = []
    original_rebalance = rtb.REBALANCE_STEP_DAYS
    try:
        for rebalance, max_positions, cap in product(rebalances, positions, caps):
            print(f"[stability] rebalance={rebalance}, positions={max_positions}, cap={cap:.2f}")
            rtb.REBALANCE_STEP_DAYS = rebalance
            curve, trades = rtb.run_etf_strategy(
                rtb.INITIAL_CASH,
                common_dates,
                index_df,
                use_market_filter=True,
                max_positions=max_positions,
                slippage=rtb.BASE_SLIPPAGE,
                risk_off_liquidate=risk_off_liquidate,
                price_data=price_data,
                max_asset_pct=cap,
            )
            evaluation_curve = pd.concat(
                [
                    pd.DataFrame(
                        [{"date": pd.Timestamp(common_dates[0]), "equity": rtb.INITIAL_CASH}]
                    ),
                    curve[["date", "equity"]],
                ],
                ignore_index=True,
            ).drop_duplicates("date", keep="last")
            stats = rtb.calc_stats(evaluation_curve, "equity")
            rows.append(
                {
                    "rebalance_step_days": rebalance,
                    "max_positions": max_positions,
                    "max_asset_pct": cap,
                    "is_central": rebalance == central_rebalance
                    and max_positions == central_positions
                    and np.isclose(cap, central_cap),
                    "cagr": float(stats["cagr"]),
                    "mdd": float(stats["mdd"]),
                    "volatility": float(stats["volatility"]),
                    "sharpe": None if pd.isna(stats["sharpe"]) else float(stats["sharpe"]),
                    "final": float(stats["final"]),
                    "trade_count": len(trades),
                }
            )
    finally:
        rtb.REBALANCE_STEP_DAYS = original_rebalance

    results = pd.DataFrame(rows).sort_values(
        ["rebalance_step_days", "max_positions", "max_asset_pct"]
    )
    central = results[results["is_central"]]
    if central.empty:
        raise RuntimeError("주변값 격자에 현재 중앙 설정이 없습니다.")
    central_row = central.iloc[0]
    summary = {
        "central": {
            "rebalance_step_days": central_rebalance,
            "max_positions": central_positions,
            "max_asset_pct": central_cap,
            "cagr": float(central_row["cagr"]),
            "mdd": float(central_row["mdd"]),
            "sharpe": float(central_row["sharpe"]),
            "cagr_percentile": float(results["cagr"].rank(pct=True).loc[central_row.name]),
        },
        "grid": {
            "scenario_count": len(results),
            "rebalance_days": rebalances,
            "max_positions": positions,
            "max_asset_pct": caps,
        },
        "robustness": {
            "profitable_ratio": float((results["cagr"] > 0).mean()),
            "positive_sharpe_ratio": float((results["sharpe"] > 0).mean()),
            "cagr_median": float(results["cagr"].median()),
            "cagr_min": float(results["cagr"].min()),
            "cagr_max": float(results["cagr"].max()),
            "cagr_std": float(results["cagr"].std(ddof=0)),
            "mdd_median": float(results["mdd"].median()),
            "mdd_worst": float(results["mdd"].min()),
            "sharpe_median": float(results["sharpe"].median()),
            "sharpe_min": float(results["sharpe"].min()),
        },
    }
    return results, summary


def main() -> None:
    results, summary = run_stability()
    OUTPUT_DIR.mkdir(exist_ok=True)
    results.to_csv(OUTPUT_DIR / "parameter_stability.csv", index=False, encoding="utf-8-sig")
    (OUTPUT_DIR / "parameter_stability_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
