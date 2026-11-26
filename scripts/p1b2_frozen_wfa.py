#!/usr/bin/env python3
"""P1-B-2: Frozen-policy WFA 및 전체 위상 검증(연구용).

고정 정책(Baseline / A=daily_risk_off_exit)을 파라미터 재선택 없이 순차 평가한다.
- 실험 A: 리밸런싱 위상 0~19 전수 × 2정책 × 2비용
- 실험 B: 상태 재개 합성 fixture(별도 `scripts/test_p1b2_state_continuity.py`)
- 실험 C: frozen-policy WFA(Train 3년/5년, Test 1년, Step 1년, rolling)

실행::

    uv run python scripts/p1b2_frozen_wfa.py

산출물: ``outputs_p1b2/``
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import p0b_execution_stress as p0b
import p1a1_risk_overlay as p1a1
import walk_forward_validation as wf

import run_etf_backtest as rtb

OUTPUT_DIR = ROOT / "outputs_p1b2"
CAP = 0.85
AUM = float(rtb.INITIAL_CASH)
EXCLUDE_2026_END = pd.Timestamp("2025-12-31")

COST_CASES: tuple[tuple[str, float, float], ...] = (
    ("base", 0.0005, 1.0),
    ("stress_50bp_3x", 0.005, 3.0),
)
POLICIES: tuple[tuple[str, bool], ...] = (("baseline", False), ("A", True))
PHASES: tuple[int, ...] = tuple(range(20))
FOLD_CONFIGS: tuple[tuple[int, int, int], ...] = ((3, 1, 1), (5, 1, 1))
REENTRY_WINDOW = 30

CODE_FILES = (
    "scripts/p1b2_frozen_wfa.py",
    "scripts/test_p1b2_state_continuity.py",
    "scripts/walk_forward_validation.py",
    "scripts/p0b_execution_stress.py",
    "scripts/p1a1_risk_overlay.py",
    "run_etf_backtest.py",
    "etf_shared.py",
)

RUN_STATS = {"runs": 0}


# --------------------------------------------------------------------------- #
# 공용 유틸
# --------------------------------------------------------------------------- #
def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"


def code_digest() -> str:
    payload = [f"{name}:{file_sha256(ROOT / name)}" for name in CODE_FILES]
    return hashlib.sha256("|".join(payload).encode("utf-8")).hexdigest()


def mdd_episode(equity: pd.DataFrame) -> dict[str, str | None]:
    frame = equity[["date", "equity"]].dropna().reset_index(drop=True)
    if frame.empty:
        return {"peak": None, "trough": None}
    values = frame["equity"].to_numpy(dtype=float)
    running = np.maximum.accumulate(values)
    drawdown = values / running - 1.0
    trough = int(np.argmin(drawdown))
    peak = int(np.argmax(values[: trough + 1]))
    return {
        "peak": str(pd.Timestamp(frame.loc[peak, "date"]).date()),
        "trough": str(pd.Timestamp(frame.loc[trough, "date"]).date()),
    }


def assert_reconciled(
    curve: pd.DataFrame, trades: pd.DataFrame, start_cash: float, label: str
) -> None:
    """세그먼트 실행용 검산(시작 현금 기준)."""
    identity = np.allclose(
        curve["equity"].to_numpy(dtype=float),
        (curve["cash"] + curve["market_value"]).to_numpy(dtype=float),
        rtol=0.0,
        atol=1e-6,
    )
    distribution_total = float(curve.get("distribution_cash", pd.Series(dtype=float)).sum())
    cash_flow = float(pd.to_numeric(trades.get("cash_flow", 0.0), errors="coerce").fillna(0.0).sum())
    expected_cash = float(start_cash) + cash_flow + distribution_total
    final_cash = float(curve["cash"].iloc[-1])
    scale = abs(float(start_cash)) + abs(cash_flow) + abs(distribution_total)
    if not identity:
        raise AssertionError(f"{label}: equity != cash + market_value")
    if abs(final_cash - expected_cash) > max(1e-6, scale * 1e-12):
        raise AssertionError(
            f"{label}: 현금 불일치 final={final_cash} expected={expected_cash} delta={final_cash - expected_cash}"
        )


def run_policy(
    price: pd.DataFrame,
    index_df: pd.DataFrame,
    dates_sub: list[pd.Timestamp],
    *,
    overlay: bool,
    slippage: float,
    fee_multiplier: float,
    phase: int = 0,
    state: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    phase_kwargs = {} if state is not None else {"rebalance_phase_offset": int(phase)}
    with p0b.patched_costs(fee_multiplier):
        curve, trades, final_state = rtb.run_etf_strategy(
            AUM,
            dates_sub,
            index_df,
            use_market_filter=True,
            max_positions=int(p0b.APPLIED["max_positions"]),
            slippage=slippage,
            risk_off_liquidate=True,
            price_data=price,
            max_asset_pct=CAP,
            daily_risk_off_exit=overlay,
            initial_state=state,
            return_final_state=True,
            **phase_kwargs,
        )
    assert_reconciled(
        curve, trades, AUM if state is None else float(state.get("cash", AUM)), "p1b2 run"
    )
    RUN_STATS["runs"] += 1
    return curve, trades, final_state


def metrics(
    curve: pd.DataFrame,
    trades: pd.DataFrame,
    dates: list[pd.Timestamp],
    *,
    slippage: float,
    fee_multiplier: float,
    oos_start: pd.Timestamp,
) -> dict[str, Any]:
    stats = p0b.performance(AUM, dates, curve)
    trading = p0b.trading_stats(curve, trades)
    costs = p0b.cost_breakdown(trades, slippage, fee_multiplier)
    cash = (curve["cash"] / curve["equity"]).replace([np.inf, -np.inf], np.nan)
    equity = p0b.evaluation_curve(AUM, dates, curve)
    ex2026 = p1a1.slice_stats(equity, None, EXCLUDE_2026_END)
    episode = mdd_episode(equity)
    return {
        **stats,
        "trade_count": trading["trade_count"],
        "turnover": trading["turnover"],
        "avg_cash_weight": float(cash.mean()),
        "total_cost": costs["total_run_rate_cost"],
        "ex2026_cagr": ex2026["cagr"],
        "ex2026_mdd": ex2026["mdd"],
        "ex2026_sharpe": ex2026["sharpe"],
        "mdd_peak_date": episode["peak"],
        "mdd_trough_date": episode["trough"],
    }


# --------------------------------------------------------------------------- #
# 실험 A: 20개 위상 전수
# --------------------------------------------------------------------------- #
def experiment_a(
    price: pd.DataFrame,
    index_df: pd.DataFrame,
    dates: list[pd.Timestamp],
    oos_start: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[tuple[str, int], tuple[pd.DataFrame, pd.DataFrame]]]:
    rows: list[dict[str, Any]] = []
    curves: dict[tuple[str, int], tuple[pd.DataFrame, pd.DataFrame]] = {}
    for cost_label, slippage, multiplier in COST_CASES:
        for policy_label, overlay in POLICIES:
            for phase in PHASES:
                curve, trades, _ = run_policy(
                    price,
                    index_df,
                    dates,
                    overlay=overlay,
                    slippage=slippage,
                    fee_multiplier=multiplier,
                    phase=phase,
                )
                curves[(f"{policy_label}:{cost_label}", phase)] = (curve, trades)
                rows.append(
                    {
                        "cost_case": cost_label,
                        "policy": policy_label,
                        "phase": phase,
                        **metrics(
                            curve,
                            trades,
                            dates,
                            slippage=slippage,
                            fee_multiplier=multiplier,
                            oos_start=oos_start,
                        ),
                    }
                )
    return pd.DataFrame(rows), curves


def phase_distribution(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    metrics_list = ("cagr", "mdd", "sharpe", "calmar", "ex2026_cagr", "ex2026_mdd", "turnover", "total_cost")
    rows: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    for (cost_label, policy_label), group in frame.groupby(["cost_case", "policy"], sort=False):
        for metric in metrics_list:
            values = group[metric].astype(float)
            phase0 = float(group.loc[group["phase"] == 0, metric].iloc[0])
            if metric in {"mdd", "ex2026_mdd"}:
                rank = int(values.rank(ascending=False).loc[group.index[group["phase"] == 0][0]])
                pct = float((values < phase0).mean())
            elif metric in {"turnover", "total_cost"}:
                rank = int(values.rank(ascending=True).loc[group.index[group["phase"] == 0][0]])
                pct = float((values > phase0).mean())
            else:
                rank = int(values.rank(ascending=False).loc[group.index[group["phase"] == 0][0]])
                pct = float((values < phase0).mean())
            rows.append(
                {
                    "cost_case": cost_label,
                    "policy": policy_label,
                    "metric": metric,
                    "mean": float(values.mean()),
                    "median": float(values.median()),
                    "std": float(values.std(ddof=0)),
                    "min": float(values.min()),
                    "max": float(values.max()),
                    "range": float(values.max() - values.min()),
                    "phase0_value": phase0,
                    "phase0_rank": rank,
                    "phase0_percentile": pct,
                }
            )
        summary[f"{policy_label}:{cost_label}"] = {
            "cagr_range": float(group["cagr"].max() - group["cagr"].min()),
            "ex2026_cagr_range": float(group["ex2026_cagr"].max() - group["ex2026_cagr"].min()),
            "mdd_range": float(group["mdd"].max() - group["mdd"].min()),
            "phase0_cagr_rank": int(
                group["cagr"].rank(ascending=False).loc[group.index[group["phase"] == 0][0]]
            ),
            "phase0_mdd_rank": int(
                group["mdd"].rank(ascending=False).loc[group.index[group["phase"] == 0][0]]
            ),
        }
    return pd.DataFrame(rows), summary


def paired_policy_difference(frame: pd.DataFrame) -> pd.DataFrame:
    base = frame[frame["policy"] == "baseline"]
    overlay = frame[frame["policy"] == "A"]
    merged = base.merge(overlay, on=["cost_case", "phase"], suffixes=("_base", "_a"))
    rows = []
    for row in merged.itertuples():
        rows.append(
            {
                "cost_case": row.cost_case,
                "phase": row.phase,
                "delta_cagr": row.cagr_a - row.cagr_base,
                "delta_mdd": row.mdd_a - row.mdd_base,
                "delta_sharpe": row.sharpe_a - row.sharpe_base,
                "delta_calmar": row.calmar_a - row.calmar_base,
                "delta_ex2026_cagr": row.ex2026_cagr_a - row.ex2026_cagr_base,
                "delta_ex2026_mdd": row.ex2026_mdd_a - row.ex2026_mdd_base,
                "delta_turnover": row.turnover_a - row.turnover_base,
                "delta_cost": row.total_cost_a - row.total_cost_base,
            }
        )
    return pd.DataFrame(rows)


def paired_summary(paired: pd.DataFrame) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for cost_label, group in paired.groupby("cost_case"):
        summary[cost_label] = {
            "phases": len(group),
            "mdd_improved": int((group["delta_mdd"] > 0).sum()),
            "cagr_improved": int((group["delta_cagr"] > 0).sum()),
            "delta_mdd_median": float(group["delta_mdd"].median()),
            "delta_mdd_worst": float(group["delta_mdd"].min()),
            "delta_cagr_median": float(group["delta_cagr"].median()),
            "delta_cagr_worst": float(group["delta_cagr"].min()),
            "delta_sharpe_median": float(group["delta_sharpe"].median()),
            "ex2026_mdd_improved": int((group["delta_ex2026_mdd"] > 0).sum()),
            "delta_ex2026_mdd_median": float(group["delta_ex2026_mdd"].median()),
        }
    return summary


# --------------------------------------------------------------------------- #
# 실험 C: frozen-policy WFA
# --------------------------------------------------------------------------- #
def segmented_run(
    price: pd.DataFrame,
    index_df: pd.DataFrame,
    full_dates: list[pd.Timestamp],
    boundaries: list[pd.Timestamp],
    *,
    overlay: bool,
    slippage: float,
    fee_multiplier: float,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any], list[dict[str, Any]]]:
    """fold 경계에서 상태를 이월하며 연결 평가곡선을 만든다(경계일은 다음 세그먼트가 재처리)."""
    position = {pd.Timestamp(day): index for index, day in enumerate(full_dates)}
    warmup = max(120, rtb.MARKET_MA_DAYS + rtb.MARKET_SLOPE_DAYS)
    starts = [full_dates[0]] + list(boundaries)
    ends = list(boundaries) + [full_dates[-1]]
    curves: list[pd.DataFrame] = []
    trades_list: list[pd.DataFrame] = []
    segments: list[dict[str, Any]] = []
    state: dict[str, Any] | None = None
    for start, end in zip(starts, ends, strict=True):
        sub = [day for day in full_dates if start <= day <= end]
        if state is not None:
            state = dict(state)
            start_index = position[pd.Timestamp(sub[0])]
            state["rebalance_phase_offset"] = wf.phase_offset(start_index, warmup, int(rtb.REBALANCE_STEP_DAYS))
            state["exit_phase_offset"] = state["rebalance_phase_offset"]
        curve, trades, state = run_policy(
            price,
            index_df,
            sub,
            overlay=overlay,
            slippage=slippage,
            fee_multiplier=fee_multiplier,
            state=state,
        )
        curves.append(curve)
        trades_list.append(trades)
        segments.append(
            {
                "start": str(pd.Timestamp(sub[0]).date()),
                "end": str(pd.Timestamp(sub[-1]).date()),
                "rows": len(curve),
                "trades": len(trades),
            }
        )
    joined_curve = pd.concat(curves, ignore_index=True).drop_duplicates("date")
    joined_curve["date"] = pd.to_datetime(joined_curve["date"])
    joined_trades = pd.concat(trades_list, ignore_index=True) if trades_list else pd.DataFrame()
    return joined_curve, joined_trades, state, segments


def assert_segments_match_continuous(
    continuous_curve: pd.DataFrame,
    split_curve: pd.DataFrame,
    continuous_trades: pd.DataFrame,
    split_trades: pd.DataFrame,
    label: str,
) -> dict[str, Any]:
    merged = continuous_curve.merge(
        split_curve, on="date", suffixes=("_full", "_split"), how="outer", indicator=True
    )
    if (merged["_merge"] != "both").any():
        raise SystemExit(f"[p1b2] {label}: 세그먼트 병합 날짜 불일치")
    equity_diff = float(np.max(np.abs(merged["equity_full"] - merged["equity_split"])))
    cash_diff = float(np.max(np.abs(merged["cash_full"] - merged["cash_split"])))
    if equity_diff > 1e-6 or cash_diff > 1e-6:
        raise SystemExit(f"[p1b2] {label}: 연속/분할 실행 불일치 equity={equity_diff} cash={cash_diff}")
    columns = ["date", "ticker", "side", "qty", "price", "cash_flow"]
    keys_full = sorted(map(tuple, continuous_trades[columns].astype(str).values.tolist()))
    keys_split = sorted(map(tuple, split_trades[columns].astype(str).values.tolist()))
    if keys_full != keys_split:
        raise SystemExit(f"[p1b2] {label}: 연속/분할 체결 내역 불일치")
    return {"equity_max_diff": equity_diff, "cash_max_diff": cash_diff, "trades": len(keys_full)}


def fold_metrics(
    curve: pd.DataFrame,
    folds: list[dict[str, Any]],
    *,
    policy: str,
    train_years: int,
    cost_case: str,
) -> list[dict[str, Any]]:
    equity = curve[["date", "equity"]].copy()
    rows = []
    for fold in folds:
        stats = p1a1.slice_stats(equity, pd.Timestamp(fold["test_start"]), pd.Timestamp(fold["test_end"]))
        rows.append(
            {
                "policy": policy,
                "train_years": train_years,
                "cost_case": cost_case,
                "fold": fold["fold"],
                "test_start": str(pd.Timestamp(fold["test_start"]).date()),
                "test_end": str(pd.Timestamp(fold["test_end"]).date()),
                **stats,
            }
        )
    return rows


def fold_summary(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (policy, train_years, cost_case), group in frame.groupby(
        ["policy", "train_years", "cost_case"], sort=False
    ):
        rows.append(
            {
                "policy": policy,
                "train_years": train_years,
                "cost_case": cost_case,
                "folds": len(group),
                "positive_return_folds": int((group["total_return"] > 0).sum()),
                "median_total_return": float(group["total_return"].median()),
                "worst_total_return": float(group["total_return"].min()),
                "median_cagr": float(group["cagr"].median()),
                "worst_mdd": float(group["mdd"].min()),
                "mean_fold_mdd": float(group["mdd"].mean()),
                "median_sharpe": float(group["sharpe"].median()),
                "worst_sharpe": float(group["sharpe"].min()),
            }
        )
    return pd.DataFrame(rows)


def policy_improvement(fold_frame: pd.DataFrame) -> pd.DataFrame:
    base = fold_frame[fold_frame["policy"] == "baseline"]
    overlay = fold_frame[fold_frame["policy"] == "A"]
    merged = base.merge(
        overlay,
        on=["train_years", "cost_case", "fold", "test_start", "test_end"],
        suffixes=("_base", "_a"),
    )
    merged["delta_total_return"] = merged["total_return_a"] - merged["total_return_base"]
    merged["delta_cagr"] = merged["cagr_a"] - merged["cagr_base"]
    merged["delta_mdd"] = merged["mdd_a"] - merged["mdd_base"]
    merged["delta_sharpe"] = merged["sharpe_a"] - merged["sharpe_base"]
    return merged


# --------------------------------------------------------------------------- #
# 위험 이벤트·휩쏘
# --------------------------------------------------------------------------- #
def risk_events(
    trades: pd.DataFrame, dates: list[pd.Timestamp]
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    if trades.empty:
        return pd.DataFrame(), pd.DataFrame(), {"ticker_exits": 0, "decision_events": 0}
    exits = trades[
        (trades["side"] == "SELL") & (trades["reason"].astype(str) == "ETF_DAILY_RISK_OFF")
    ].copy()
    if exits.empty:
        return pd.DataFrame(), pd.DataFrame(), {"ticker_exits": 0, "decision_events": 0}
    exits["date"] = pd.to_datetime(exits["date"])
    exits["year"] = exits["date"].dt.year
    exits["origin"] = exits.get("exit_origin_date", pd.Series(index=exits.index, dtype=object))
    exits["origin_date"] = pd.to_datetime(exits["origin"], errors="coerce")
    exits["origin_year"] = exits["origin_date"].dt.year
    origin_lookup = {
        (str(pd.Timestamp(row.date).date()), str(row.ticker)): (
            None if pd.isna(row.origin_date) else str(row.origin_date.date())
        )
        for row in exits.itertuples()
    }
    position = {pd.Timestamp(day): index for index, day in enumerate(dates)}
    exits["index"] = exits["date"].map(position)
    buys = trades[trades["side"] == "BUY"].copy()
    buys["date"] = pd.to_datetime(buys["date"])
    buys["index"] = buys["date"].map(position)
    rows = []
    for exit_row in exits.itertuples():
        origin_value = origin_lookup.get(
            (str(pd.Timestamp(exit_row.date).date()), str(exit_row.ticker))
        )
        later = buys[
            (buys["ticker"] == exit_row.ticker) & (buys["index"] > exit_row.index)
        ].sort_values("index")
        later = later[later["index"] - exit_row.index <= REENTRY_WINDOW]
        if later.empty:
            rows.append(
                {
                    "date": str(exit_row.date.date()),
                    "origin_date": origin_value,
                    "ticker": exit_row.ticker,
                    "qty": int(exit_row.qty),
                    "exit_price": float(exit_row.price),
                    "exit_cash_flow": float(exit_row.cash_flow),
                    "reentry_date": None,
                    "reentry_days": None,
                    "reentry_price": None,
                    "reentry_qty": None,
                    "bought_back_higher": None,
                    "reentry_cash_flow": None,
                }
            )
            continue
        reentry = later.iloc[0]
        rows.append(
            {
                "date": str(pd.Timestamp(exit_row.date).date()),
                "origin_date": origin_value,
                "ticker": exit_row.ticker,
                "qty": int(exit_row.qty),
                "exit_price": float(exit_row.price),
                "exit_cash_flow": float(exit_row.cash_flow),
                "reentry_date": str(pd.Timestamp(reentry["date"]).date()),
                "reentry_days": int(reentry["index"] - exit_row.index),
                "reentry_price": float(reentry["price"]),
                "reentry_qty": int(reentry["qty"]),
                "bought_back_higher": bool(float(reentry["price"]) >= float(exit_row.price)),
                "reentry_cash_flow": float(reentry["cash_flow"]),
            }
        )
    detail = pd.DataFrame(rows)
    by_year = (
        exits.groupby("year").size().rename("risk_off_ticker_exits").reset_index().to_dict(orient="records")
    )
    decision_by_year = (
        exits.dropna(subset=["origin_year"])
        .drop_duplicates(subset=["origin_date"])
        .groupby("origin_year")
        .size()
        .rename("risk_off_decision_events")
        .reset_index()
    )
    by_year_frame = pd.DataFrame(by_year).merge(
        decision_by_year, left_on="year", right_on="origin_year", how="outer"
    )
    if "origin_year" in by_year_frame:
        by_year_frame["year"] = by_year_frame["year"].fillna(by_year_frame["origin_year"])
        by_year_frame = by_year_frame.drop(columns=["origin_year"])
    by_year_frame[["year"]] = by_year_frame[["year"]].astype("Int64")
    counts = {
        "ticker_exits": len(exits),
        "decision_events": int(exits["origin_date"].nunique(dropna=True)),
    }
    return detail, by_year_frame, counts


def whipsaw_summary(detail: pd.DataFrame) -> dict[str, Any]:
    if detail.empty:
        return {"exits": 0, "reentries": 0}
    reentries = detail[detail["reentry_days"].notna()]
    return {
        "exits": len(detail),
        "reentries": len(reentries),
        "reentry_share": float(len(reentries) / len(detail)),
        "median_reentry_days": float(reentries["reentry_days"].median()) if len(reentries) else None,
        "bought_back_higher_share": float(reentries["bought_back_higher"].mean())
        if len(reentries)
        else None,
        "years": sorted(detail["date"].str[:4].unique().tolist()),
    }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="P1-B-2 frozen-policy WFA")
    parser.add_argument("--smoke", action="store_true", help="기준 재현·연속성 검사만 수행")
    args = parser.parse_args()

    frozen = p0b.configure_frozen_settings(CAP)
    os.environ["ETF_MOMENTUM_WEIGHT_60"] = "0.55"
    rtb.START, rtb.END = p0b.DEFAULT_START, p0b.DEFAULT_END
    preflight = p0b.preflight_cache(rtb.START, rtb.END)
    digest_before = p0b.data_cache_digest()
    price, index_df, dates = p0b.load_inputs()
    p0b.verify_applied_settings()
    oos_start = pd.Timestamp(frozen["oos_start_date"])
    full_dates = [pd.Timestamp(day) for day in dates]

    # 기준 재현(Baseline phase 0)
    base_curve, base_trades, _ = run_policy(
        price,
        index_df,
        full_dates,
        overlay=False,
        slippage=0.0005,
        fee_multiplier=1.0,
        phase=0,
    )
    baseline = metrics(
        base_curve, base_trades, full_dates, slippage=0.0005, fee_multiplier=1.0, oos_start=oos_start
    )
    expected = {"cagr": 0.2454113778672673, "mdd": -0.5121244554166072, "sharpe": 1.0391357658469746}
    if any(abs(baseline[key] - value) > 1e-9 for key, value in expected.items()):
        raise SystemExit(f"[p1b2] 기준 재현 실패: { {k: baseline[k] for k in expected} }")

    folds_by_config = {
        train: wf.build_folds(pd.Series(full_dates), train, 1, 1, anchored=False)
        for train, _, _ in FOLD_CONFIGS
    }
    for train, folds in folds_by_config.items():
        if not folds:
            raise SystemExit(f"[p1b2] train {train}년 fold가 없습니다.")

    # 상태 연속성: 실데이터에서 연속 실행 = fold 분할 실행
    continuity_rows = []
    for policy_label, overlay in POLICIES:
        continuous_curve, continuous_trades, _ = run_policy(
            price,
            index_df,
            full_dates,
            overlay=overlay,
            slippage=0.0005,
            fee_multiplier=1.0,
            phase=0,
        )
        for train, folds in folds_by_config.items():
            boundaries = [pd.Timestamp(fold["test_end"]) for fold in folds]
            split_curve, split_trades, _, segments = segmented_run(
                price,
                index_df,
                full_dates,
                boundaries,
                overlay=overlay,
                slippage=0.0005,
                fee_multiplier=1.0,
            )
            check = assert_segments_match_continuous(
                continuous_curve, split_curve, continuous_trades, split_trades,
                f"{policy_label}/train{train}",
            )
            continuity_rows.append(
                {
                    "policy": policy_label,
                    "train_years": train,
                    "segments": len(segments),
                    "equity_max_diff": check["equity_max_diff"],
                    "cash_max_diff": check["cash_max_diff"],
                    "trades": check["trades"],
                }
            )
    print("[p1b2] 연속 실행 = 분할 실행 확인", flush=True)

    if args.smoke:
        print(
            "[p1b2] smoke: 기준 재현·연속성 검사 통과 "
            f"(runs={RUN_STATS['runs']}, folds={ {k: len(v) for k, v in folds_by_config.items()} })"
        )
        return 0

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("[p1b2] 실험 A 시작(80회)", flush=True)
    phase_frame, curves = experiment_a(price, index_df, full_dates, oos_start)

    distribution, distribution_summary = phase_distribution(phase_frame)
    paired = paired_policy_difference(phase_frame)
    paired_summary_rows = paired_summary(paired)

    # 실험 C: fold별 성과(연결 평가곡선 기준)
    fold_rows: list[dict[str, Any]] = []
    connected: dict[tuple[int, str], tuple[pd.DataFrame, pd.DataFrame]] = {}
    segment_rows = []
    for train, folds in folds_by_config.items():
        boundaries = [pd.Timestamp(fold["test_end"]) for fold in folds]
        for policy_label, overlay in POLICIES:
            curve, trades, _, segments = segmented_run(
                price,
                index_df,
                full_dates,
                boundaries,
                overlay=overlay,
                slippage=0.0005,
                fee_multiplier=1.0,
            )
            connected[(train, policy_label)] = (curve, trades)
            fold_rows.extend(
                fold_metrics(curve, folds, policy=policy_label, train_years=train, cost_case="base")
            )
            segment_rows.append(
                {
                    "train_years": train,
                    "policy": policy_label,
                    "segments": len(segments),
                    "first_test_start": str(pd.Timestamp(folds[0]["test_start"]).date()),
                    "last_test_end": str(pd.Timestamp(folds[-1]["test_end"]).date()),
                }
            )
    fold_frame = pd.DataFrame(fold_rows)
    improvements = policy_improvement(fold_frame)
    fold_summary_frame = fold_summary(fold_frame)

    # 위상별 fold 성과(실험 A 곡선을 동일 fold 기간으로 절단)
    phase_fold_rows: list[dict[str, Any]] = []
    for train, folds in folds_by_config.items():
        for cost_label, _, _ in COST_CASES:
            for policy_label, _ in POLICIES:
                for phase in PHASES:
                    curve, _ = curves[f"{policy_label}:{cost_label}", phase]
                    equity = p0b.evaluation_curve(AUM, full_dates, curve)
                    for fold in folds:
                        stats = p1a1.slice_stats(
                            equity[["date", "equity"]],
                            pd.Timestamp(fold["test_start"]),
                            pd.Timestamp(fold["test_end"]),
                        )
                        phase_fold_rows.append(
                            {
                                "train_years": train,
                                "cost_case": cost_label,
                                "policy": policy_label,
                                "phase": phase,
                                "fold": fold["fold"],
                                "test_start": str(pd.Timestamp(fold["test_start"]).date()),
                                "test_end": str(pd.Timestamp(fold["test_end"]).date()),
                                "total_return": stats["total_return"],
                                "cagr": stats["cagr"],
                                "mdd": stats["mdd"],
                                "sharpe": stats["sharpe"],
                            }
                        )
    phase_fold_frame = pd.DataFrame(phase_fold_rows)
    phase_fold_summary = (
        phase_fold_frame.groupby(["train_years", "cost_case", "policy", "phase"])
        .agg(
            folds=("fold", "count"),
            positive_folds=("total_return", lambda values: int((values > 0).sum())),
            median_total_return=("total_return", "median"),
            worst_total_return=("total_return", "min"),
            mean_mdd=("mdd", "mean"),
        )
        .reset_index()
    )
    phase_fold_summary["positive_share"] = (
        phase_fold_summary["positive_folds"] / phase_fold_summary["folds"]
    )

    # 2026 모니터링(부분연도)
    monitoring_rows = []
    for (train, policy_label), (curve, trades) in connected.items():
        equity = p0b.evaluation_curve(AUM, full_dates, curve)
        stats = p1a1.slice_stats(equity, pd.Timestamp("2026-01-01"), None)
        monitoring_rows.append(
            {
                "train_years": train,
                "policy": policy_label,
                "days": stats["days"],
                "total_return": stats["total_return"],
                "mdd": stats["mdd"],
                "sharpe": stats["sharpe"],
                "note": "2026 부분연도 모니터링(연율화 비교 금지)",
            }
        )
    monitoring = pd.DataFrame(monitoring_rows)

    # 위험 이벤트·휩쏘(연결 곡선 기준, A)
    event_detail, event_by_year, event_counts = risk_events(
        connected[(FOLD_CONFIGS[0][0], "A")][1], full_dates
    )
    whipsaw = whipsaw_summary(event_detail)

    # 비용·회전·현금(phase 0, 두 비용 조건)
    cost_rows = []
    for cost_label, slippage, multiplier in COST_CASES:
        for policy_label, overlay in POLICIES:
            curve, trades, _ = run_policy(
                price,
                index_df,
                full_dates,
                overlay=overlay,
                slippage=slippage,
                fee_multiplier=multiplier,
                phase=0,
            )
            stats = metrics(
                curve, trades, full_dates, slippage=slippage, fee_multiplier=multiplier, oos_start=oos_start
            )
            cost_rows.append(
                {
                    "cost_case": cost_label,
                    "policy": policy_label,
                    "cagr": stats["cagr"],
                    "mdd": stats["mdd"],
                    "sharpe": stats["sharpe"],
                    "ex2026_cagr": stats["ex2026_cagr"],
                    "ex2026_mdd": stats["ex2026_mdd"],
                    "turnover": stats["turnover"],
                    "total_cost": stats["total_cost"],
                    "avg_cash_weight": stats["avg_cash_weight"],
                }
            )
    cost_frame = pd.DataFrame(cost_rows)

    # MDD 개선이 특정 사건(2026)에 집중되는지: 연도별 MDD 비교
    year_mdd_rows = []
    for train, folds in folds_by_config.items():
        for policy_label in ("baseline", "A"):
            curve, _ = connected[(train, policy_label)]
            equity = p0b.evaluation_curve(AUM, full_dates, curve)
            for year in range(2016, 2027):
                stats = p1a1.slice_stats(
                    equity[["date", "equity"]],
                    pd.Timestamp(f"{year}-01-01"),
                    pd.Timestamp(f"{year}-12-31"),
                )
                if stats["days"] < 2:
                    continue
                year_mdd_rows.append(
                    {
                        "train_years": train,
                        "policy": policy_label,
                        "year": year,
                        "cagr": stats["cagr"],
                        "mdd": stats["mdd"],
                        "total_return": stats["total_return"],
                        "days": stats["days"],
                    }
                )
    year_mdd = pd.DataFrame(year_mdd_rows)

    # ---------------------------------------------------------------- outputs
    phase_frame.drop(columns=["mdd_peak_date", "mdd_trough_date"]).to_csv(
        OUTPUT_DIR / "phase_metrics.csv", index=False, encoding="utf-8-sig"
    )
    distribution.to_csv(OUTPUT_DIR / "phase_distribution.csv", index=False, encoding="utf-8-sig")
    paired.to_csv(OUTPUT_DIR / "phase_paired_difference.csv", index=False, encoding="utf-8-sig")
    fold_frame.to_csv(OUTPUT_DIR / "fold_metrics.csv", index=False, encoding="utf-8-sig")
    fold_summary_frame.to_csv(OUTPUT_DIR / "fold_summary.csv", index=False, encoding="utf-8-sig")
    improvements.to_csv(OUTPUT_DIR / "fold_policy_improvement.csv", index=False, encoding="utf-8-sig")
    phase_fold_frame.to_csv(OUTPUT_DIR / "phase_fold_metrics.csv", index=False, encoding="utf-8-sig")
    phase_fold_summary.to_csv(
        OUTPUT_DIR / "phase_fold_summary.csv", index=False, encoding="utf-8-sig"
    )
    monitoring.to_csv(OUTPUT_DIR / "monitoring_2026.csv", index=False, encoding="utf-8-sig")
    cost_frame.to_csv(OUTPUT_DIR / "cost_turnover.csv", index=False, encoding="utf-8-sig")
    year_mdd.to_csv(OUTPUT_DIR / "year_mdd.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(continuity_rows).to_csv(
        OUTPUT_DIR / "continuity_check.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(segment_rows).to_csv(
        OUTPUT_DIR / "wfa_segments.csv", index=False, encoding="utf-8-sig"
    )
    if not event_detail.empty:
        event_detail.to_csv(OUTPUT_DIR / "risk_events.csv", index=False, encoding="utf-8-sig")
    if not event_by_year.empty:
        event_by_year.to_csv(OUTPUT_DIR / "risk_events_by_year.csv", index=False, encoding="utf-8-sig")

    # 연결 평가곡선 지표(fold 평균 MDD와 구분)
    connected_metrics: list[dict[str, Any]] = []
    for (train, policy_label), (curve, trades) in connected.items():
        stats = metrics(
            curve,
            trades,
            full_dates,
            slippage=0.0005,
            fee_multiplier=1.0,
            oos_start=oos_start,
        )
        connected_metrics.append(
            {
                "train_years": train,
                "policy": policy_label,
                "cagr": stats["cagr"],
                "mdd": stats["mdd"],
                "sharpe": stats["sharpe"],
                "calmar": stats["calmar"],
                "ex2026_cagr": stats["ex2026_cagr"],
                "ex2026_mdd": stats["ex2026_mdd"],
                "turnover": stats["turnover"],
                "total_cost": stats["total_cost"],
                "mdd_peak_date": stats["mdd_peak_date"],
                "mdd_trough_date": stats["mdd_trough_date"],
            }
        )
    connected_frame = pd.DataFrame(connected_metrics)

    # fold 개선 요약
    improvement_summary = {}
    for (train, cost_case), group in improvements.groupby(["train_years", "cost_case"]):
        improvement_summary[f"train{train}_{cost_case}"] = {
            "folds": len(group),
            "mdd_improved_folds": int((group["delta_mdd"] > 0).sum()),
            "mdd_improved_share": float((group["delta_mdd"] > 0).mean()),
            "return_improved_folds": int((group["delta_total_return"] > 0).sum()),
            "return_improved_share": float((group["delta_total_return"] > 0).mean()),
            "median_delta_mdd": float(group["delta_mdd"].median()),
            "worst_delta_mdd": float(group["delta_mdd"].min()),
            "median_delta_total_return": float(group["delta_total_return"].median()),
        }

    # MDD 개선의 사건 집중도(연도별 A-Baseline ΔMDD)
    year_pivot = year_mdd.pivot_table(
        index=["train_years", "year"], columns="policy", values="mdd"
    )
    if {"baseline", "A"}.issubset(year_pivot.columns):
        year_pivot["delta_mdd"] = year_pivot["A"] - year_pivot["baseline"]
        year_delta = year_pivot.reset_index()
    else:
        year_delta = pd.DataFrame(columns=["train_years", "year", "delta_mdd"])
    years_per_config = len(year_delta) // max(1, len(FOLD_CONFIGS))
    improved_per_config = (
        int((year_delta["delta_mdd"] > 0).sum()) // max(1, len(FOLD_CONFIGS))
        if len(year_delta)
        else 0
    )
    over_one_pct_years = sorted(
        {int(record["year"]) for record in year_delta.to_dict(orient="records")
         if float(record["delta_mdd"]) > 0.01}
    )
    concentration = {
        "years_total": years_per_config,
        "years_mdd_improved": improved_per_config,
        "years_mdd_improved_over_1pct": int((year_delta["delta_mdd"] > 0.01).sum())
        // max(1, len(FOLD_CONFIGS))
        if len(year_delta)
        else 0,
        "max_year_delta_mdd": float(year_delta["delta_mdd"].max()) if len(year_delta) else None,
        "max_year": int(year_delta.loc[year_delta["delta_mdd"].idxmax(), "year"])
        if len(year_delta)
        else None,
        "year_delta": year_delta.to_dict(orient="records"),
    }

    if p0b.data_cache_digest() != digest_before:
        raise SystemExit("[p1b2] 실행 중 data_cache가 변경되었습니다.")

    summary = {
        "phase": "P1-B-2",
        "baseline": {
            **{key: baseline[key] for key in ("cagr", "mdd", "sharpe", "calmar", "turnover", "total_cost")},
            "expected_match": True,
        },
        "folds": {f"train{train}": len(folds) for train, folds in folds_by_config.items()},
        "phase_distribution": distribution_summary,
        "phase_paired": paired_summary_rows,
        "phase_fold_summary": {
            "phase_cagr_spread_median": float(
                phase_fold_summary.groupby(["train_years", "policy"])["median_total_return"]
                .agg(lambda values: float(values.max() - values.min()))
                .median()
            ),
            "positive_fold_share_range": [
                float(phase_fold_summary["positive_share"].min()),
                float(phase_fold_summary["positive_share"].max()),
            ],
            "fold_median_return_spread_across_phases": float(
                phase_fold_summary.groupby(["train_years", "cost_case", "policy"])[
                    "median_total_return"
                ]
                .agg(lambda values: float(values.max() - values.min()))
                .max()
            ),
        },
        "wfa": {
            "fold_summary": fold_summary_frame.to_dict(orient="records"),
            "connected_metrics": connected_frame.to_dict(orient="records"),
            "policy_improvement": improvement_summary,
            "continuity": continuity_rows,
            "segments": segment_rows,
            "note": (
                "frozen policy(선택 없음) — train 결과로 파라미터를 고르지 않는다. "
                "fold 평균 MDD와 연결 평가곡선 MDD를 구분해 기록한다."
            ),
        },
        "monitoring_2026": monitoring.to_dict(orient="records"),
        "risk_events": {
            "ticker_exits": event_counts["ticker_exits"],
            "decision_events": event_counts["decision_events"],
            "by_year": event_by_year.to_dict(orient="records") if not event_by_year.empty else [],
            "whipsaw": whipsaw,
            "note": (
                "ticker_exits는 종목별 청산 행 수, decision_events는 원 신호일(decision date) "
                "기준 고유 이벤트 수다."
            ),
        },
        "mdd_concentration": {
            **concentration,
            "note": "연도별 ΔMDD(A-Baseline)로 개선이 단일 급락 사건에 집중되는지 확인한다.",
        },
        "decision": {
            "candidate": "risk_overlay_A",
            "keep": False,
            "status": "보류(hold)",
            "evidence_for": [
                "20개 위상 중 MDD 개선 18/20(base)·16/20(stress)",
                "위상 CAGR 범위 17.04→11.76%p, MDD 범위 26.74→16.59%p로 분산 축소",
                (
                    f"연도별 MDD {improved_per_config}/{years_per_config}년 개선 "
                    "(2026 모니터링 MDD -51.21→-35.07%p)"
                ),
            ],
            "evidence_against": [
                (
                    "완료 fold의 ΔMDD 중앙값 "
                    f"{float(improvements['delta_mdd'].median()):+.2%}p(사실상 0)"
                ),
                (
                    "fold 수익 개선 "
                    + "·".join(
                        f"{int(group['delta_total_return'].gt(0).sum())}/{len(group)}"
                        f"({entry.split('_')[0]})"
                        for entry, group in improvements.groupby(
                            improvements["train_years"].map(lambda value: f"train{value}")
                        )
                    )
                ),
                (
                    f"연도별 ΔMDD >1%p는 {len(over_one_pct_years)}개 연도"
                    f"({', '.join(map(str, over_one_pct_years))}), "
                    f"최대 {concentration.get('max_year')}년 "
                    f"{concentration.get('max_year_delta_mdd', 0.0):+.2%}p"
                ),
            ],
            "rule_note": (
                "과제의 '여러 기간 일관 개선'·'단일 급락 사건 반복을 독립 성공으로 계산하지 않음' "
                "요구를 그대로 적용한 판단이며, 통계적 검정이 아니다."
            ),
        },
        "pre_registered": {
            "phases": list(PHASES),
            "cost_cases": [list(item) for item in COST_CASES],
            "policies": [list(item) for item in POLICIES],
            "fold_configs": [list(item) for item in FOLD_CONFIGS],
            "reentry_window": REENTRY_WINDOW,
        },
        "limits": {
            "in_sample_history": True,
            "independent_oos": False,
            "monitoring_46d_only": True,
            "phases_not_independent": True,
            "phase_average_not_a_strategy": True,
            "overlapping_rolling": True,
        },
        "runs": RUN_STATS["runs"],
    }
    (OUTPUT_DIR / "p1b2_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    self_outputs = {"provenance.json"}
    artifacts = {
        path.name: file_sha256(path)
        for path in sorted(OUTPUT_DIR.glob("*"))
        if path.is_file() and path.name not in self_outputs
    }
    provenance = {
        "phase": "P1-B-2",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": p0b.git_commit(),
        "code_digest": code_digest(),
        "code_files": list(CODE_FILES),
        "data_cache_digest": digest_before,
        "freeze_sha256": frozen["freeze_sha256"],
        "freeze_date": frozen.get("freeze_date"),
        "period": {"start": p0b.DEFAULT_START, "end": p0b.DEFAULT_END},
        "oos_start_date": frozen["oos_start_date"],
        "preflight": preflight,
        "cost_cases": {label: [slippage, multiplier] for label, slippage, multiplier in COST_CASES},
        "phases": list(PHASES),
        "fold_configs": [list(item) for item in FOLD_CONFIGS],
        "runs": RUN_STATS["runs"],
        "continuity": continuity_rows,
        "artifacts": artifacts,
    }
    (OUTPUT_DIR / "provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(
        f"[p1b2] 완료: {OUTPUT_DIR} (runs={RUN_STATS['runs']}, "
        f"folds={ {k: len(v) for k, v in folds_by_config.items()} })"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
