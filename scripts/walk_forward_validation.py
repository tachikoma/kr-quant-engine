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
import strategy_freeze
from config_utils import parse_fraction_env, parse_pct_env

OUTPUT_DIR = ROOT / "outputs_walk_forward"

FIXED_POLICY_CURVE_FILENAME = "fixed_policy_oos_equity_curve.csv"
FIXED_POLICY_SUMMARY_FILENAME = "fixed_policy_oos_summary.json"


def parse_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def serialize_stats(stats: dict) -> dict:
    result = {}
    for key, value in stats.items():
        if value is None or (not isinstance(value, str) and pd.isna(value)):
            result[key] = None
        elif isinstance(value, (np.integer, np.floating)):
            result[key] = float(value)
        elif isinstance(value, pd.Timestamp):
            result[key] = str(value.date())
        else:
            result[key] = value
    return result


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
    sample = curve[(curve["date"] >= start) & (curve["date"] <= end)][["date", "equity"]]
    if len(sample) < 2:
        return None
    return rtb.calc_stats(sample, "equity")


def phase_offset(start_index: int, warmup_days: int, step_days: int) -> int:
    """Return the phase needed to continue a full-period schedule."""
    if step_days <= 0:
        raise ValueError("step_days must be positive")
    return (int(start_index) - int(warmup_days)) % int(step_days)


def stitch_state_based_segment(
    segment_equity: pd.Series | list[float],
    *,
    prior_end_equity: float | None = None,
    first_segment_anchor_equity: float | None = None,
) -> list[float]:
    """Convert an absolute state-based segment into returns for stitching."""
    values = pd.Series(segment_equity, dtype=float).reset_index(drop=True)
    if values.empty:
        return []
    if prior_end_equity is not None:
        anchor_equity = float(prior_end_equity)
    elif first_segment_anchor_equity is not None:
        anchor_equity = float(first_segment_anchor_equity)
    else:
        return values.pct_change().fillna(0.0).tolist()
    if not np.isfinite(anchor_equity) or anchor_equity <= 0:
        raise ValueError("segment equity anchor must be finite and positive")
    returns = [float(values.iloc[0] / anchor_equity - 1.0)]
    returns.extend(float(values.iloc[i] / values.iloc[i - 1] - 1.0) for i in range(1, len(values)))
    return returns


def load_frozen_payload() -> dict:
    """동결 스냅샷을 검증해 로드한다. 누락/위조 시 fail-closed."""
    try:
        return strategy_freeze.load_frozen_strategy()
    except (FileNotFoundError, TypeError) as exc:
        raise RuntimeError(f"전략 동결 스냅샷 로드 실패: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError(f"전략 동결 스냅샷 검증 실패: {exc}") from exc


def resolve_fixed_policy_params(payload: dict) -> dict:
    """동결 parameters 블록을 run_etf_strategy 호출 인자로 매핑한다."""
    params_block = payload.get("parameters")
    if not isinstance(params_block, dict):
        raise TypeError("전략 동결 스냅샷에 parameters 블록이 없습니다.")
    required = ("rebalance_step_days", "max_positions", "max_asset_pct")
    missing = [key for key in required if key not in params_block]
    if missing:
        raise RuntimeError(f"동결 파라미터 누락: {', '.join(missing)}")
    return {
        "rebalance_step_days": int(params_block["rebalance_step_days"]),
        "max_positions": int(params_block["max_positions"]),
        "max_asset_pct": float(params_block["max_asset_pct"]),
        "target_weight_rebalance": bool(params_block.get("target_weight_rebalance", False)),
        "rebalance_band_pct": float(params_block.get("rebalance_band_pct", 0.05)),
        "trim_overweight_positions": bool(params_block.get("trim_overweight_positions", False)),
        "exit_check_days": int(params_block.get("exit_check_days", 0)),
        "trailing_stop_pct": float(params_block.get("trailing_stop_pct", 0.0)),
        "portfolio_trailing_stop_pct": float(params_block.get("portfolio_trailing_stop_pct", 0.0)),
        "risk_off_liquidate": bool(params_block.get("liquidate_on_risk_off", True)),
        "slippage": float(params_block.get("slippage", rtb.BASE_SLIPPAGE)),
        "use_market_filter": bool(params_block.get("use_market_filter", True)),
        "enable_multi_index_risk": bool(params_block.get("enable_multi_index_risk", False)),
        "multi_index_gating_mode": str(params_block.get("multi_index_gating_mode", "hybrid")),
        "us_risk_proxy": str(params_block.get("us_risk_proxy", "SPY")).upper(),
        "us_market_ma_days": int(params_block.get("us_market_ma_days", rtb.MARKET_MA_DAYS)),
        "us_market_slope_days": int(
            params_block.get("us_market_slope_days", rtb.MARKET_SLOPE_DAYS)
        ),
    }


def filter_oos_slice(curve: pd.DataFrame, oos_start: pd.Timestamp | str) -> pd.DataFrame:
    """oos_start(포함) 이후 행만 반환한다."""
    oos_start_ts = pd.Timestamp(oos_start)
    sliced = curve[pd.to_datetime(curve["date"]) >= oos_start_ts].copy()
    sliced["date"] = pd.to_datetime(sliced["date"])
    return sliced.sort_values("date").drop_duplicates("date").reset_index(drop=True)


def build_fixed_policy_summary(
    oos_curve: pd.DataFrame,
    payload: dict,
    applied_params: dict,
) -> dict:
    """고정(frozen) 정책 OOS 요약을 생성한다. adaptive 요약과 절대 합치지 않는다."""
    if oos_curve.empty or len(oos_curve) < 2:
        raise RuntimeError("동결 OOS 시작일 이후 데이터가 부족합니다.")
    stats = rtb.calc_stats(oos_curve[["date", "equity"]], "equity")
    return {
        "policy_type": "frozen_fixed",
        "freeze_date": str(payload.get("freeze_date")),
        "oos_start": str(pd.Timestamp(payload["oos_start_date"]).date()),
        "oos_end": str(oos_curve["date"].iloc[-1].date()),
        "row_count": len(oos_curve),
        "applied_params": applied_params,
        "oos_stats": serialize_stats(stats),
    }


def run_fixed_policy_oos(
    index_df: pd.DataFrame,
    common_dates: list,
    price_data: pd.DataFrame,
) -> tuple[pd.DataFrame, dict]:
    """strategy_freeze.json 기준 고정 파라미터로 전 기간 연속 실행 후 OOS 구간만 보고한다."""
    payload = load_frozen_payload()
    applied = resolve_fixed_policy_params(payload)
    oos_start_raw = payload.get("oos_start_date")
    if not oos_start_raw:
        raise RuntimeError("전략 동결 스냅샷에 oos_start_date가 없습니다.")

    original_step_days = rtb.REBALANCE_STEP_DAYS
    multi_index_keys = (
        "ENABLE_MULTI_INDEX_RISK",
        "MULTI_INDEX_GATING_MODE",
        "US_RISK_PROXY",
        "US_MARKET_MA_DAYS",
        "US_MARKET_SLOPE_DAYS",
    )
    original_multi_index = {key: getattr(rtb, key, None) for key in multi_index_keys}
    us_index_df = None
    try:
        rtb.REBALANCE_STEP_DAYS = applied["rebalance_step_days"]
        if applied["enable_multi_index_risk"]:
            rtb.ENABLE_MULTI_INDEX_RISK = True
            rtb.MULTI_INDEX_GATING_MODE = applied["multi_index_gating_mode"]
            rtb.US_RISK_PROXY = applied["us_risk_proxy"]
            rtb.US_MARKET_MA_DAYS = applied["us_market_ma_days"]
            rtb.US_MARKET_SLOPE_DAYS = applied["us_market_slope_days"]
            us_index_df = rtb.get_us_index_data()
        print(
            "[fixed-policy] 고정 파라미터 실행: "
            f"rebalance={applied['rebalance_step_days']}, positions={applied['max_positions']}"
        )
        curve, _ = rtb.run_etf_strategy(
            rtb.INITIAL_CASH,
            list(common_dates),
            index_df,
            use_market_filter=applied["use_market_filter"],
            max_positions=applied["max_positions"],
            slippage=applied["slippage"],
            risk_off_liquidate=applied["risk_off_liquidate"],
            price_data=price_data,
            max_asset_pct=applied["max_asset_pct"],
            target_weight_rebalance=applied["target_weight_rebalance"],
            rebalance_band_pct=applied["rebalance_band_pct"],
            trim_overweight_positions=applied["trim_overweight_positions"],
            exit_check_days=applied["exit_check_days"],
            trailing_stop_pct=applied["trailing_stop_pct"],
            portfolio_trailing_stop_pct=applied["portfolio_trailing_stop_pct"],
            us_index_df=us_index_df,
            enable_multi_index_risk=applied["enable_multi_index_risk"],
        )
    finally:
        rtb.REBALANCE_STEP_DAYS = original_step_days
        for key, value in original_multi_index.items():
            if value is None:
                if hasattr(rtb, key):
                    delattr(rtb, key)
            else:
                setattr(rtb, key, value)

    curve = curve[["date", "equity"]].copy()
    curve["date"] = pd.to_datetime(curve["date"])
    curve = curve.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    oos_curve = filter_oos_slice(curve, pd.Timestamp(oos_start_raw))
    summary = build_fixed_policy_summary(oos_curve, payload, applied)
    return oos_curve, summary


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


def load_common_data() -> tuple[pd.DataFrame, list, pd.DataFrame]:
    """인덱스/가격 데이터를 1회 로드한다."""
    print("[walk-forward] 데이터 1회 로드")
    index_df = rtb.get_index_data()
    common_dates = list(index_df["date"])
    price_data = rtb.load_etf_price()
    return index_df, common_dates, price_data


def run_validation(
    *,
    index_df: pd.DataFrame | None = None,
    common_dates: list | None = None,
    price_data: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    rebalance_days = parse_int_list("WF_REBALANCE_DAYS", [10, 20, 30])
    max_positions = parse_int_list("WF_MAX_POSITIONS", [1, 2, 3])
    train_years = int(os.environ.get("WF_TRAIN_YEARS", "3"))
    test_years = int(os.environ.get("WF_TEST_YEARS", "1"))
    step_years = int(os.environ.get("WF_STEP_YEARS", "1"))
    anchored = os.environ.get("WF_ANCHORED", "0") == "1"
    boundary_cost_pct = float(os.environ.get("WF_BOUNDARY_COST_PCT", "0.0015"))
    enable_multi_index_risk = parse_bool_env("ENABLE_MULTI_INDEX_RISK", False)
    multi_index_gating_mode = (
        os.environ.get("MULTI_INDEX_GATING_MODE", "hybrid").strip().lower() or "hybrid"
    )
    us_risk_proxy = os.environ.get("US_RISK_PROXY", "SPY").strip().upper() or "SPY"
    us_market_ma_days = int(os.environ.get("US_MARKET_MA_DAYS", str(rtb.MARKET_MA_DAYS)))
    us_market_slope_days = int(os.environ.get("US_MARKET_SLOPE_DAYS", str(rtb.MARKET_SLOPE_DAYS)))
    target_weight_rebalance = parse_bool_env(
        "WF_TARGET_WEIGHT_REBALANCE",
        bool(rtb.strategy_cfg.get("target_weight_rebalance", False)),
    )
    trim_overweight_positions = parse_bool_env(
        "WF_TRIM_OVERWEIGHT_POSITIONS",
        bool(rtb.strategy_cfg.get("trim_overweight_positions", False)),
    )
    max_asset_pct = parse_fraction_env(
        "WF_MAX_ASSET_PCT",
        parse_fraction_env("MAX_ASSET_PCT", 0.50),
    )
    rebalance_band_pct = parse_pct_env(
        "WF_REBALANCE_BAND_PCT",
        float(rtb.strategy_cfg.get("rebalance_band_pct", 0.05)),
    )
    exit_check_days = int(os.environ.get("WF_EXIT_CHECK_DAYS", "0"))
    trailing_stop_pct = parse_fraction_env("WF_TRAILING_STOP_PCT", 0.0)
    portfolio_trailing_stop_pct = parse_fraction_env("WF_PORTFOLIO_TRAILING_STOP_PCT", 0.0)
    if exit_check_days < 0:
        raise ValueError("WF_EXIT_CHECK_DAYS는 0 이상이어야 합니다.")
    if min(train_years, test_years, step_years) <= 0:
        raise ValueError("WF_TRAIN_YEARS/WF_TEST_YEARS/WF_STEP_YEARS는 양수여야 합니다.")
    if not 0 <= boundary_cost_pct <= 0.1:
        raise ValueError("WF_BOUNDARY_COST_PCT는 0~0.1 범위여야 합니다.")

    if index_df is None or common_dates is None or price_data is None:
        index_df, common_dates, price_data = load_common_data()
    risk_off_liquidate = rtb.strategy_cfg.get("liquidate_on_risk_off", True)
    if enable_multi_index_risk:
        rtb.ENABLE_MULTI_INDEX_RISK = True
        rtb.MULTI_INDEX_GATING_MODE = multi_index_gating_mode
        rtb.US_RISK_PROXY = us_risk_proxy
        rtb.US_MARKET_MA_DAYS = us_market_ma_days
        rtb.US_MARKET_SLOPE_DAYS = us_market_slope_days
    us_index_df = rtb.get_us_index_data() if enable_multi_index_risk else None

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
                max_asset_pct=max_asset_pct,
                target_weight_rebalance=target_weight_rebalance,
                rebalance_band_pct=rebalance_band_pct,
                trim_overweight_positions=trim_overweight_positions,
                exit_check_days=exit_check_days,
                trailing_stop_pct=trailing_stop_pct,
                portfolio_trailing_stop_pct=portfolio_trailing_stop_pct,
                us_index_df=us_index_df,
                enable_multi_index_risk=enable_multi_index_risk,
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

    state_based = os.environ.get("WF_STATE_BASED", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }
    full_dates = list(pd.to_datetime(common_dates))
    warmup_full = max(120, rtb.MARKET_MA_DAYS + rtb.MARKET_SLOPE_DAYS)

    fold_rows = []
    stitched_rows = []
    stitched_equity = float(rtb.INITIAL_CASH)
    carry_state: dict | None = None
    previous_segment_final_equity: float | None = None
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
        rtb.REBALANCE_STEP_DAYS = selected_key[0]

        if state_based:
            prior_carry_state = carry_state
            prior_end_equity: float | None = None
            if prior_carry_state is not None:
                candidate_equity = prior_carry_state.get("final_equity")
                if candidate_equity is not None and np.isfinite(float(candidate_equity)):
                    prior_end_equity = float(candidate_equity)
                else:
                    prior_end_equity = previous_segment_final_equity
            test_start_ts = pd.Timestamp(fold["test_start"])
            test_end_ts = pd.Timestamp(fold["test_end"])
            start_index = next(
                (i for i, d in enumerate(full_dates) if d >= test_start_ts),
                0,
            )
            if carry_state is None:
                # 첫 폴드: 기간 시작부터 fold의 test_end까지 연속 실행
                segment_dates = [d for d in full_dates if d <= test_end_ts]
                run_state = None
            else:
                segment_dates = [d for d in full_dates if test_start_ts <= d <= test_end_ts]
                run_state = dict(carry_state)
                run_state["rebalance_phase_offset"] = phase_offset(
                    start_index, warmup_full, selected_key[0]
                )
                if exit_check_days > 0:
                    run_state["exit_phase_offset"] = phase_offset(
                        start_index, warmup_full, exit_check_days
                    )
            curve, _, end_state = rtb.run_etf_strategy(
                rtb.INITIAL_CASH,
                segment_dates,
                index_df,
                use_market_filter=True,
                max_positions=selected_key[1],
                slippage=rtb.BASE_SLIPPAGE,
                risk_off_liquidate=risk_off_liquidate,
                price_data=price_data,
                max_asset_pct=max_asset_pct,
                target_weight_rebalance=target_weight_rebalance,
                rebalance_band_pct=rebalance_band_pct,
                trim_overweight_positions=trim_overweight_positions,
                exit_check_days=exit_check_days,
                trailing_stop_pct=trailing_stop_pct,
                portfolio_trailing_stop_pct=portfolio_trailing_stop_pct,
                us_index_df=us_index_df,
                enable_multi_index_risk=enable_multi_index_risk,
                initial_state=run_state,
                return_final_state=True,
            )
            curve["date"] = pd.to_datetime(curve["date"])
            test = curve[(curve["date"] >= test_start_ts) & (curve["date"] <= test_end_ts)].copy()
            carry_state = end_state
            final_equity = end_state.get("final_equity")
            if (
                (final_equity is None or not np.isfinite(float(final_equity)))
                and not curve.empty
                and "equity" in curve.columns
            ):
                final_equity = float(curve["equity"].iloc[-1])
            previous_segment_final_equity = (
                float(final_equity) if final_equity is not None else None
            )
        else:
            selected_curve = scenario_curves[selected_key].copy()
            selected_curve["daily_return"] = selected_curve["equity"].pct_change()
            test = selected_curve[
                (selected_curve["date"] >= fold["test_start"])
                & (selected_curve["date"] <= fold["test_end"])
            ].dropna(subset=["daily_return"])
        if test.empty:
            continue

        fold_initial = stitched_equity
        if state_based:
            # 상태 기반 세그먼트 실행의 일간 수익률을 스티치 에쿼티에 적용한다.
            # 세그먼트 절대 평가액(INITIAL_CASH 기반)과 스티치 레벨은 다르므로
            # 수익률만 이전 스티치 레벨에 합성한다. 첫 행은 폴드 경계(직전
            # 실행 종료) 시점의 수익률로 계산해 경계 전환 수익률을 보존한다.
            seg_equity = pd.Series(
                [float(row.equity) for row in test.itertuples(index=False)], dtype=float
            )
            if prior_carry_state is None:
                # 폴드 1: 전체 curve에서 test_start 직전 행을 앵커로 삼아 경계 수익률 유지
                full_curve_sorted = curve.sort_values("date").reset_index(drop=True)
                anchor_rows = full_curve_sorted[full_curve_sorted["date"] < test_start_ts]
                if not anchor_rows.empty:
                    anchor_equity = float(anchor_rows["equity"].iloc[-1])
                    seg_returns = stitch_state_based_segment(
                        seg_equity, first_segment_anchor_equity=anchor_equity
                    )
                else:
                    seg_returns = stitch_state_based_segment(seg_equity)
            else:
                seg_returns = stitch_state_based_segment(
                    seg_equity, prior_end_equity=prior_end_equity
                )
            for row, seg_return in zip(test.itertuples(index=False), seg_returns, strict=False):
                stitched_equity *= 1 + float(seg_return)
                stitched_rows.append(
                    {
                        "date": row.date,
                        "equity": stitched_equity,
                        "daily_return": float(seg_return),
                        "fold": fold["fold"],
                        "rebalance_step_days": selected_key[0],
                        "max_positions": selected_key[1],
                    }
                )
        else:
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
                **{
                    key: str(value.date()) if isinstance(value, pd.Timestamp) else value
                    for key, value in fold.items()
                },
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
    baseline = pd.DataFrame([{"date": folds[0]["train_end"], "equity": float(rtb.INITIAL_CASH)}])
    overall_curve = pd.concat([baseline, stitched_df[["date", "equity"]]], ignore_index=True)
    overall_stats = rtb.calc_stats(overall_curve, "equity")
    summary = {
        "policy_type": "adaptive_fold_selected",
        "method": "rolling" if not anchored else "anchored",
        "selection_metric": "train_sharpe_then_cagr_then_mdd",
        "state_based": state_based,
        "boundary_cost_pct": boundary_cost_pct,
        "train_years": train_years,
        "target_weight_rebalance": target_weight_rebalance,
        "trim_overweight_positions": trim_overweight_positions,
        "max_asset_pct": max_asset_pct,
        "rebalance_band_pct": rebalance_band_pct,
        "exit_check_days": exit_check_days,
        "trailing_stop_pct": trailing_stop_pct,
        "portfolio_trailing_stop_pct": portfolio_trailing_stop_pct,
        "enable_multi_index_risk": enable_multi_index_risk,
        "multi_index_gating_mode": multi_index_gating_mode,
        "us_risk_proxy": us_risk_proxy,
        "us_market_ma_days": us_market_ma_days,
        "us_market_slope_days": us_market_slope_days,
        "rebalance_days_grid": rebalance_days,
        "max_positions_grid": max_positions,
        "fold_count": len(folds_df),
        "oos_start": str(stitched_df["date"].iloc[0].date()),
        "oos_end": str(stitched_df["date"].iloc[-1].date()),
        "oos_stats": serialize_stats(overall_stats),
    }
    return folds_df, stitched_df, summary


def write_walk_forward_outputs(
    output_dir: Path,
    folds_df: pd.DataFrame,
    stitched_df: pd.DataFrame,
    summary: dict,
    fixed_curve: pd.DataFrame | None = None,
    fixed_summary: dict | None = None,
) -> None:
    """adaptive/fixed-policy 산출물을 분리 저장한다."""
    output_dir.mkdir(parents=True, exist_ok=True)
    folds_df.to_csv(output_dir / "walk_forward_folds.csv", index=False, encoding="utf-8-sig")
    stitched_df.to_csv(
        output_dir / "walk_forward_equity_curve.csv", index=False, encoding="utf-8-sig"
    )
    (output_dir / "walk_forward_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if fixed_curve is not None and fixed_summary is not None:
        fixed_curve.to_csv(
            output_dir / FIXED_POLICY_CURVE_FILENAME, index=False, encoding="utf-8-sig"
        )
        (output_dir / FIXED_POLICY_SUMMARY_FILENAME).write_text(
            json.dumps(fixed_summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def main() -> None:
    index_df, common_dates, price_data = load_common_data()
    folds_df, stitched_df, summary = run_validation(
        index_df=index_df, common_dates=common_dates, price_data=price_data
    )
    output_dir_raw = os.environ.get("WF_OUTPUT_DIR", str(OUTPUT_DIR))
    output_dir = Path(output_dir_raw)
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir

    fixed_curve: pd.DataFrame | None = None
    fixed_summary: dict | None = None
    if parse_bool_env("WF_FIXED_POLICY_OOS", True):
        fixed_curve, fixed_summary = run_fixed_policy_oos(index_df, common_dates, price_data)

    write_walk_forward_outputs(
        output_dir, folds_df, stitched_df, summary, fixed_curve, fixed_summary
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if fixed_summary is not None:
        print(json.dumps(fixed_summary, ensure_ascii=False, indent=2))
    print(f"[walk-forward] 저장 완료: {output_dir}")


if __name__ == "__main__":
    main()
