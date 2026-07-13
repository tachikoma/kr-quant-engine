#!/usr/bin/env python3
"""롤링 walk-forward 파라미터 선택 및 표본외 성과 검증."""

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


OUTPUT_DIR = ROOT / "outputs_walk_forward"


def parse_int_list(name: str, default: list[int]) -> list[int]:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        values = sorted({int(value.strip()) for value in raw.split(",") if value.strip()})
    except ValueError as exc:
        raise ValueError(f"{name}은 쉼표로 구분한 정수여야 합니다: {raw}") from exc
    if not values or any(value <= 0 for value in values):
        raise ValueError(f"{name}에는 양의 정수가 하나 이상 필요합니다: {raw}")
    return values


def slice_stats(curve: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> dict | None:
    sample = curve[(curve["date"] >= start) & (curve["date"] <= end)][
        ["date", "equity"]
    ]
    if len(sample) < 2:
        return None
    return rtb.calc_stats(sample, "equity")


def build_folds(
    dates: pd.Series,
    train_years: int,
    test_years: int,
    step_years: int,
    anchored: bool,
) -> list[dict]:
    dates = pd.Series(pd.to_datetime(dates).dropna().sort_values().unique())
    if dates.empty:
        return []
    first_date = pd.Timestamp(dates.iloc[0])
    last_date = pd.Timestamp(dates.iloc[-1])
    folds = []
    train_start = first_date
    train_end = first_date + pd.DateOffset(years=train_years) - pd.Timedelta(days=1)
    previous_test_end: pd.Timestamp | None = None

    while True:
        test_start_cutoff = train_end
        if previous_test_end is not None:
            test_start_cutoff = max(test_start_cutoff, previous_test_end)
        future_dates = dates[dates > test_start_cutoff]
        if future_dates.empty:
            break
        test_start = pd.Timestamp(future_dates.iloc[0])
        requested_test_end = test_start + pd.DateOffset(years=test_years) - pd.Timedelta(days=1)
        if requested_test_end > last_date:
            break
        eligible_test_dates = dates[(dates >= test_start) & (dates <= requested_test_end)]
        if eligible_test_dates.empty:
            break
        test_end = pd.Timestamp(eligible_test_dates.iloc[-1])
        eligible_train_dates = dates[(dates >= train_start) & (dates <= train_end)]
        if len(eligible_train_dates) >= 2:
            folds.append(
                {
                    "fold": len(folds) + 1,
                    "train_start": pd.Timestamp(eligible_train_dates.iloc[0]),
                    "train_end": pd.Timestamp(eligible_train_dates.iloc[-1]),
                    "test_start": test_start,
                    "test_end": test_end,
                }
            )
            previous_test_end = test_end
        train_end = train_end + pd.DateOffset(years=step_years)
        if not anchored:
            train_start = train_start + pd.DateOffset(years=step_years)
    return folds


def selection_key(row: dict) -> tuple[float, float, float]:
    sharpe = row["train_sharpe"]
    return (
        -np.inf if sharpe is None or pd.isna(sharpe) else float(sharpe),
        float(row["train_cagr"]),
        float(row["train_mdd"]),
    )


def run_validation() -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    rebalance_days = parse_int_list("WF_REBALANCE_DAYS", [10, 20, 30])
    max_positions = parse_int_list("WF_MAX_POSITIONS", [1, 2, 3])
    train_years = int(os.environ.get("WF_TRAIN_YEARS", "3"))
    test_years = int(os.environ.get("WF_TEST_YEARS", "1"))
    step_years = int(os.environ.get("WF_STEP_YEARS", "1"))
    anchored = os.environ.get("WF_ANCHORED", "0") == "1"
    boundary_cost_pct = float(os.environ.get("WF_BOUNDARY_COST_PCT", "0.0015"))
    if min(train_years, test_years, step_years) <= 0:
        raise ValueError("WF_TRAIN_YEARS/WF_TEST_YEARS/WF_STEP_YEARS는 양수여야 합니다.")
    if not 0 <= boundary_cost_pct <= 0.1:
        raise ValueError("WF_BOUNDARY_COST_PCT는 0~0.1 범위여야 합니다.")

    print("[walk-forward] 데이터 1회 로드")
    index_df = rtb.get_index_data()
    common_dates = list(index_df["date"])
    price_data = rtb.load_etf_price()
    risk_off_liquidate = rtb.strategy_cfg.get("liquidate_on_risk_off", True)

    original_rebalance_days = rtb.REBALANCE_STEP_DAYS
    scenario_curves: dict[tuple[int, int], pd.DataFrame] = {}
    try:
        for rebalance, positions in product(rebalance_days, max_positions):
            print(f"[walk-forward] 시나리오 실행: rebalance={rebalance}, positions={positions}")
            rtb.REBALANCE_STEP_DAYS = rebalance
            curve, _ = rtb.run_etf_strategy(
                rtb.INITIAL_CASH,
                common_dates,
                index_df,
                use_market_filter=True,
                max_positions=positions,
                slippage=rtb.BASE_SLIPPAGE,
                risk_off_liquidate=risk_off_liquidate,
                price_data=price_data,
            )
            curve = curve[["date", "equity"]].copy()
            curve["date"] = pd.to_datetime(curve["date"])
            curve = curve.sort_values("date").drop_duplicates("date").reset_index(drop=True)
            scenario_curves[(rebalance, positions)] = curve
    finally:
        rtb.REBALANCE_STEP_DAYS = original_rebalance_days

    reference_curve = next(iter(scenario_curves.values()))
    folds = build_folds(reference_curve["date"], train_years, test_years, step_years, anchored)
    if not folds:
        raise RuntimeError("조건을 만족하는 walk-forward 폴드가 없습니다.")

    fold_rows = []
    stitched_rows = []
    stitched_equity = float(rtb.INITIAL_CASH)
    for fold in folds:
        candidates = []
        for (rebalance, positions), curve in scenario_curves.items():
            train_stats = slice_stats(curve, fold["train_start"], fold["train_end"])
            if train_stats is None:
                continue
            candidates.append(
                {
                    "rebalance_step_days": rebalance,
                    "max_positions": positions,
                    "train_cagr": float(train_stats["cagr"]),
                    "train_mdd": float(train_stats["mdd"]),
                    "train_sharpe": None
                    if pd.isna(train_stats["sharpe"])
                    else float(train_stats["sharpe"]),
                }
            )
        if not candidates:
            continue
        selected = max(candidates, key=selection_key)
        selected_key = (selected["rebalance_step_days"], selected["max_positions"])
        selected_curve = scenario_curves[selected_key].copy()
        selected_curve["daily_return"] = selected_curve["equity"].pct_change()
        test = selected_curve[
            (selected_curve["date"] >= fold["test_start"])
            & (selected_curve["date"] <= fold["test_end"])
        ].dropna(subset=["daily_return"])
        if test.empty:
            continue

        fold_initial = stitched_equity
        for row_number, row in enumerate(test.itertuples(index=False)):
            daily_return = float(row.daily_return)
            if row_number == 0:
                daily_return = (1 + daily_return) * (1 - boundary_cost_pct) - 1
            stitched_equity *= 1 + daily_return
            stitched_rows.append(
                {
                    "date": row.date,
                    "equity": stitched_equity,
                    "daily_return": daily_return,
                    "fold": fold["fold"],
                    "rebalance_step_days": selected_key[0],
                    "max_positions": selected_key[1],
                }
            )
        fold_curve = pd.DataFrame(
            [
                {"date": fold["train_end"], "equity": fold_initial},
                *[
                    {"date": row["date"], "equity": row["equity"]}
                    for row in stitched_rows
                    if row["fold"] == fold["fold"]
                ],
            ]
        )
        test_stats = rtb.calc_stats(fold_curve, "equity")
        fold_rows.append(
            {
                **{key: str(value.date()) if isinstance(value, pd.Timestamp) else value for key, value in fold.items()},
                **selected,
                "test_total_return": float(test_stats["total_return"]),
                "test_cagr": float(test_stats["cagr"]),
                "test_mdd": float(test_stats["mdd"]),
                "test_sharpe": None
                if pd.isna(test_stats["sharpe"])
                else float(test_stats["sharpe"]),
            }
        )

    folds_df = pd.DataFrame(fold_rows)
    stitched_df = pd.DataFrame(stitched_rows)
    if stitched_df.empty:
        raise RuntimeError("walk-forward 표본외 수익률을 생성하지 못했습니다.")
    baseline = pd.DataFrame(
        [{"date": folds[0]["train_end"], "equity": float(rtb.INITIAL_CASH)}]
    )
    overall_curve = pd.concat([baseline, stitched_df[["date", "equity"]]], ignore_index=True)
    overall_stats = rtb.calc_stats(overall_curve, "equity")
    summary = {
        "method": "rolling" if not anchored else "anchored",
        "selection_metric": "train_sharpe_then_cagr_then_mdd",
        "train_years": train_years,
        "test_years": test_years,
        "step_years": step_years,
        "boundary_cost_pct": boundary_cost_pct,
        "rebalance_days_grid": rebalance_days,
        "max_positions_grid": max_positions,
        "fold_count": len(folds_df),
        "oos_start": str(stitched_df["date"].iloc[0].date()),
        "oos_end": str(stitched_df["date"].iloc[-1].date()),
        "oos_stats": {
            key: None if pd.isna(value) else float(value) for key, value in overall_stats.items()
        },
    }
    return folds_df, stitched_df, summary


def main() -> None:
    folds_df, stitched_df, summary = run_validation()
    OUTPUT_DIR.mkdir(exist_ok=True)
    folds_df.to_csv(OUTPUT_DIR / "walk_forward_folds.csv", index=False, encoding="utf-8-sig")
    stitched_df.to_csv(
        OUTPUT_DIR / "walk_forward_equity_curve.csv", index=False, encoding="utf-8-sig"
    )
    (OUTPUT_DIR / "walk_forward_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[walk-forward] 저장 완료: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
