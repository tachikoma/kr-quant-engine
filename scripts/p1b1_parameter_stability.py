#!/usr/bin/env python3
"""P1-B-1: 파라미터 안정성 및 리밸런싱 위상 검증(연구용).

새 전략·최적화 탐색이 아니라 **동결 전략 주변의 성과 안정 영역(plateau) 존재 여부**를
확인한다. 사전에 고정한 값만 실행하며, 결과 확인 후 범위를 확장하거나 최고 조합 주변을
재최적화하지 않는다.

실험:
- A: 개별 파라미터 민감도(모멘텀 가중치·리밸런싱 주기·보유 종목수·시장 MA·slope)
- B: 리밸런싱 시작 위상 0/5/10/15
- C: 사전 고정 18개 교차 조합(가중치 3 × 주기 3 × 종목수 2)
- D: 위험 관리 후보 A(`daily_risk_off_exit`)의 3개 구성 × 4개 위상 강건성

실행::

    uv run python scripts/p1b1_parameter_stability.py

산출물: ``outputs_p1b1/``
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
import p0d_factor_random_baseline as p0d
import p1a1_risk_overlay as p1a1

import run_etf_backtest as rtb

OUTPUT_DIR = ROOT / "outputs_p1b1"
CAP = 0.85
AUM = float(rtb.INITIAL_CASH)
EXCLUDE_2026_END = pd.Timestamp("2025-12-31")

COST_CASES: tuple[tuple[str, float, float], ...] = (
    ("base", 0.0005, 1.0),
    ("stress_50bp_3x", 0.005, 3.0),
)

# 사전 고정 값(결과 확인 후 확장·변경 금지)
MOMENTUM_WEIGHTS = (0.0, 0.25, 0.55, 0.75, 1.0)
REBALANCE_STEPS = (15, 20, 25)
POSITION_COUNTS = (1, 2, 3)
MA_WINDOWS = (100, 120, 140)
SLOPE_WINDOWS = (15, 20, 25)
PHASES = (0, 5, 10, 15)
CROSS_WEIGHTS = (0.0, 0.25, 0.55)
CROSS_REBALANCE = (15, 20, 25)
CROSS_POSITIONS = (2, 3)
RISK_CONFIGS: tuple[tuple[str, float], ...] = (
    ("frozen", 0.55),
    ("mom120_only", 0.0),
    ("w0.25_0.75", 0.25),
)

CENTRAL = {
    "momentum_weight_60": 0.55,
    "rebalance_step_days": 20,
    "max_positions": 2,
    "market_ma_days": 120,
    "market_slope_days": 20,
    "phase": 0,
    "daily_risk_off_exit": False,
}
# 워밍업 차이를 제거한 공통 평가 시작 인덱스(가장 긴 지표 요구 = 140 + 25).
# 각 변형은 자연 워밍업으로 실행하고, 지표는 이 인덱스 이후 구간에서 비교한다.
COMMON_WINDOW_START_INDEX = max(120, max(MA_WINDOWS) + max(SLOPE_WINDOWS))
NATURAL_WARMUP = max(120, CENTRAL["market_ma_days"] + CENTRAL["market_slope_days"])

# 사전 등록한 안정 영역 판정 기준(결과 확인 후 조정 금지)
PLATEAU = {
    "cagr_delta_min": -0.02,
    "sharpe_delta_min": -0.10,
    "mdd_worsen_max": 0.05,
}
# 후보 게이트는 plateau 기준을 **포함하는** 상위 집합(더 엄격)이어야 한다.
CANDIDATE_GATES = {
    "cagr_delta_min": PLATEAU["cagr_delta_min"],
    "sharpe_delta_min": PLATEAU["sharpe_delta_min"],
    "mdd_worsen_max": PLATEAU["mdd_worsen_max"],
    "stress_rank_top_half": True,
    "ex2026_cagr_delta_min": 0.0,
    "phase_range_max_multiple_of_central": 1.0,
    "phase_range_floor": 0.01,
    "neighborhood_cagr_delta_min": -0.02,
}

# 과제 요구("특정 가중치·위상에서만 개선되는지")를 기계화한 관대한 스크리닝.
# 확인적 검정이 아니라 후보 전달 여부를 판단하는 스크리닝이며, 결과와 무관하게 단순한 값으로 고정했다.
OVERLAY_SCREEN = {
    "mdd_improve_share_min": 0.80,
    "delta_cagr_median_min": -0.005,
}

CODE_FILES = (
    "scripts/p1b1_parameter_stability.py",
    "scripts/p0b_execution_stress.py",
    "scripts/p0c_regime_drawdown.py",
    "scripts/p0d_factor_random_baseline.py",
    "scripts/p1a1_risk_overlay.py",
    "run_etf_backtest.py",
    "etf_shared.py",
)

RUN_CACHE: dict[tuple, dict[str, Any]] = {}
CURVES: dict[tuple, tuple[pd.DataFrame, pd.DataFrame]] = {}
INDEX_CACHE: dict[tuple[int, int], pd.DataFrame] = {}
CACHE_STATS = {"runs": 0, "reused": 0}


# --------------------------------------------------------------------------- #
# 공용 유틸
# --------------------------------------------------------------------------- #
def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"


def code_digest() -> str:
    payload = []
    for name in CODE_FILES:
        path = ROOT / name
        payload.append(f"{name}:{file_sha256(path)}")
    return hashlib.sha256("|".join(payload).encode("utf-8")).hexdigest()


def rebuild_index(base: pd.DataFrame, ma_days: int, slope_days: int) -> pd.DataFrame:
    """동일 원본 종가에서 시장 MA·slope·risk_on을 재계산한다(캐시 지표 재사용 금지)."""
    key = (int(ma_days), int(slope_days))
    if key in INDEX_CACHE:
        return INDEX_CACHE[key]
    frame = base[["date", "close"]].copy()
    frame["market_ma"] = frame["close"].rolling(int(ma_days)).mean()
    frame["market_ma_slope"] = frame["market_ma"] - frame["market_ma"].shift(int(slope_days))
    frame["risk_on"] = (frame["close"] >= frame["market_ma"]) & (
        frame["market_ma_slope"] >= 0
    )
    INDEX_CACHE[key] = frame
    return frame


def spearman(left: pd.Series, right: pd.Series) -> float:
    """순위 기반 상관(중복 순위는 평균 순위). scipy 미사용."""
    a = left.rank(method="average").to_numpy(dtype=float)
    b = right.rank(method="average").to_numpy(dtype=float)
    if len(a) < 3 or np.std(a) == 0 or np.std(b) == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def _config_key(**kwargs: Any) -> tuple:
    return (
        bool(kwargs.get("omit_phase", False)),
        round(float(kwargs["momentum_weight_60"]), 4),
        int(kwargs["rebalance_step_days"]),
        int(kwargs["max_positions"]),
        int(kwargs["market_ma_days"]),
        int(kwargs["market_slope_days"]),
        int(kwargs["phase"]),
        bool(kwargs["daily_risk_off_exit"]),
        float(kwargs["slippage"]),
        float(kwargs["fee_multiplier"]),
    )


# --------------------------------------------------------------------------- #
# 시나리오 실행
# --------------------------------------------------------------------------- #
def mdd_episode(equity: pd.DataFrame) -> dict[str, str | None]:
    """최대 낙폭 구간의 peak/trough 날짜(동일 에피소드 조건부 비교용)."""
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


def run_scenario(
    price: pd.DataFrame,
    base_index: pd.DataFrame,
    dates: list[pd.Timestamp],
    *,
    momentum_weight_60: float,
    rebalance_step_days: int,
    max_positions: int,
    market_ma_days: int,
    market_slope_days: int,
    phase: int = 0,
    daily_risk_off_exit: bool = False,
    omit_phase: bool = False,
    slippage: float = 0.0005,
    fee_multiplier: float = 1.0,
    oos_start: pd.Timestamp,
) -> dict[str, Any]:
    key = _config_key(
        momentum_weight_60=momentum_weight_60,
        rebalance_step_days=rebalance_step_days,
        max_positions=max_positions,
        market_ma_days=market_ma_days,
        market_slope_days=market_slope_days,
        phase=phase,
        daily_risk_off_exit=daily_risk_off_exit,
        omit_phase=omit_phase,
        slippage=slippage,
        fee_multiplier=fee_multiplier,
    )
    if key in RUN_CACHE:
        CACHE_STATS["reused"] += 1
        return RUN_CACHE[key]

    index_df = rebuild_index(base_index, market_ma_days, market_slope_days)
    saved = (
        rtb.REBALANCE_STEP_DAYS,
        rtb.ETF_MAX_POSITIONS,
        rtb.MARKET_MA_DAYS,
        rtb.MARKET_SLOPE_DAYS,
    )
    events: list[dict[str, Any]] = []
    if omit_phase and int(phase) != 0:
        raise ValueError("omit_phase는 phase 0에서만 사용합니다.")
    phase_kwargs = {} if omit_phase else {"rebalance_phase_offset": int(phase)}
    try:
        rtb.REBALANCE_STEP_DAYS = int(rebalance_step_days)
        rtb.ETF_MAX_POSITIONS = int(max_positions)
        rtb.MARKET_MA_DAYS = int(market_ma_days)
        rtb.MARKET_SLOPE_DAYS = int(market_slope_days)
        with p0d.patched_state(momentum_weight=momentum_weight_60), p0b.patched_costs(
            fee_multiplier
        ):
            curve, trades = rtb.run_etf_strategy(
                AUM,
                dates,
                index_df,
                use_market_filter=True,
                max_positions=int(max_positions),
                slippage=slippage,
                risk_off_liquidate=True,
                price_data=price,
                max_asset_pct=CAP,
                rebalance_observer=events.append,
                daily_risk_off_exit=daily_risk_off_exit,
                **phase_kwargs,
            )
    finally:
        (
            rtb.REBALANCE_STEP_DAYS,
            rtb.ETF_MAX_POSITIONS,
            rtb.MARKET_MA_DAYS,
            rtb.MARKET_SLOPE_DAYS,
        ) = saved
    p0b.assert_legacy_reconciled(curve, trades, AUM, "p1b1 scenario")

    if p0b.data_cache_digest() != run_scenario.digest_before:
        raise SystemExit("[p1b1] 실행 중 data_cache가 변경되었습니다.")

    row = p0d.metrics(
        curve,
        trades,
        dates,
        slippage=slippage,
        fee_multiplier=fee_multiplier,
        oos_start=oos_start,
    )
    warmup_days = max(120, int(market_ma_days) + int(market_slope_days))
    equity = p0b.evaluation_curve(AUM, dates, curve)
    window_start = pd.Timestamp(dates[min(COMMON_WINDOW_START_INDEX, len(dates) - 2)])
    window = p1a1.slice_stats(equity, window_start, None)
    episode = mdd_episode(equity)
    row.update(
        {
            "warmup_days": warmup_days,
            "effective_phase_vs_140": (warmup_days - NATURAL_WARMUP) % int(rebalance_step_days),
            "window_start": str(window_start.date()),
            "win_cagr": window["cagr"],
            "win_mdd": window["mdd"],
            "win_sharpe": window["sharpe"],
            "win_days": window["days"],
            "mdd_peak_date": episode["peak"],
            "mdd_trough_date": episode["trough"],
        }
    )
    decisions = [pd.Timestamp(event["decision_date"]) for event in events]
    position = {pd.Timestamp(day): index for index, day in enumerate(dates)}
    last_index = position.get(decisions[-1], -1) if decisions else -1
    row.update(
        {
            "decision_count": len(decisions),
            "first_decision": str(decisions[0].date()) if decisions else None,
            "last_decision": str(decisions[-1].date()) if decisions else None,
            "last_decision_is_final_pair": bool(
                decisions and last_index == len(dates) - 2
            ),
            "last_execution_date": (
                str(pd.Timestamp(dates[last_index + 1]).date())
                if 0 <= last_index < len(dates) - 1
                else None
            ),
            "decision_dates": [str(day.date()) for day in decisions],
        }
    )
    CURVES[key] = (curve, trades)
    RUN_CACHE[key] = row
    CACHE_STATS["runs"] += 1
    if CACHE_STATS["runs"] % 10 == 0:
        print(
            f"[p1b1] run {CACHE_STATS['runs']:3d} (reused {CACHE_STATS['reused']:3d}) "
            f"w={momentum_weight_60:.2f} r={rebalance_step_days} p={max_positions} "
            f"phase={phase} overlay={daily_risk_off_exit} cagr={row['cagr']:.3%}",
            flush=True,
        )
    return row


run_scenario.digest_before = ""  # type: ignore[attr-defined]


def equity_curve(key: tuple, dates: list[pd.Timestamp]) -> pd.DataFrame:
    curve, _ = CURVES[key]
    return p0b.evaluation_curve(AUM, dates, curve)


# --------------------------------------------------------------------------- #
# 기간 분석
# --------------------------------------------------------------------------- #
def window_stats(
    equity: pd.DataFrame,
    label: str,
    period: str,
    start: pd.Timestamp | None,
    end: pd.Timestamp | None,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """구간 통계 + 실제 관측 경계. 달력 라벨과 실제 관측일을 분리해 기록한다."""
    dates = pd.to_datetime(equity["date"])
    mask = pd.Series(True, index=equity.index)
    if start is not None:
        mask &= dates >= start
    if end is not None:
        mask &= dates <= end
    observed = dates[mask]
    stats = p1a1.slice_stats(equity, start, end)
    return {
        "config": label,
        "period": period,
        "calendar_start": None if start is None else str(pd.Timestamp(start).date()),
        "calendar_end": None if end is None else str(pd.Timestamp(end).date()),
        "observed_start": None if observed.empty else str(observed.min().date()),
        "observed_end": None if observed.empty else str(observed.max().date()),
        "days": int(stats["days"]),
        "clipped_by_data": bool(
            (start is not None and start < dates.min())
            or (end is not None and end > dates.max())
        ),
        **stats,
        **(extra or {}),
    }


def yearly_rows(equity: pd.DataFrame, label: str) -> list[dict[str, Any]]:
    rows = []
    dates = pd.to_datetime(equity["date"])
    for year in range(dates.min().year, dates.max().year + 1):
        start = pd.Timestamp(f"{year}-01-01")
        end = pd.Timestamp(f"{year}-12-31")
        if not ((dates >= start) & (dates <= end)).any():
            continue
        rows.append(window_stats(equity, label, f"year_{year}", start, end))
    return rows


def rolling_rows(
    equity: pd.DataFrame,
    label: str,
    years: int,
    *,
    step_years: int = 1,
    overlapping: bool = True,
) -> list[dict[str, Any]]:
    rows = []
    dates = pd.to_datetime(equity["date"])
    first, last = dates.min(), dates.max()
    start = first
    while start + pd.DateOffset(years=years) <= last:
        end = start + pd.DateOffset(years=years)
        rows.append(
            window_stats(
                equity,
                label,
                f"rolling_{years}y_{'overlap' if overlapping else 'non_overlap'}",
                start,
                end,
                extra={
                    "window_years": years,
                    "step_years": step_years,
                    "overlapping": overlapping,
                },
            )
        )
        start = start + pd.DateOffset(years=step_years)
    return rows


def period_summary(
    equity: pd.DataFrame, label: str, oos_start: pd.Timestamp
) -> list[dict[str, Any]]:
    rows = [
        window_stats(equity, label, "full", None, None),
        window_stats(equity, label, "ex2026", None, EXCLUDE_2026_END),
        window_stats(
            equity, label, "in_sample", None, oos_start - pd.Timedelta(days=1)
        ),
        window_stats(equity, label, "freeze_monitoring_oos", oos_start, None),
    ]
    rows.extend(yearly_rows(equity, label))
    return rows


# --------------------------------------------------------------------------- #
# 안정성 요약
# --------------------------------------------------------------------------- #
def plateau_flags(row: dict[str, Any], central: dict[str, Any]) -> dict[str, bool]:
    return {
        "cagr_ok": (row["cagr"] - central["cagr"]) >= PLATEAU["cagr_delta_min"],
        "sharpe_ok": (row["sharpe"] - central["sharpe"]) >= PLATEAU["sharpe_delta_min"],
        "mdd_ok": (row["mdd"] - central["mdd"]) >= -PLATEAU["mdd_worsen_max"],
    }


# --------------------------------------------------------------------------- #
# 실험 A: 개별 파라미터 민감도(공통 워밍업)
# --------------------------------------------------------------------------- #
A_PARAMETERS: tuple[tuple[str, tuple[Any, ...], str], ...] = (
    ("momentum_weight_60", MOMENTUM_WEIGHTS, "momentum_weight_60"),
    ("rebalance_step_days", REBALANCE_STEPS, "rebalance_step_days"),
    ("max_positions", POSITION_COUNTS, "max_positions"),
    ("market_ma_days", MA_WINDOWS, "market_ma_days"),
    ("market_slope_days", SLOPE_WINDOWS, "market_slope_days"),
)


def experiment_a(
    price: pd.DataFrame,
    base_index: pd.DataFrame,
    dates: list[pd.Timestamp],
    oos_start: pd.Timestamp,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for parameter, values, key in A_PARAMETERS:
        for value in values:
            config = {**CENTRAL, key: value}
            for cost_label, slippage, multiplier in COST_CASES:
                result = run_scenario(
                    price,
                    base_index,
                    dates,
                    momentum_weight_60=float(config["momentum_weight_60"]),
                    rebalance_step_days=int(config["rebalance_step_days"]),
                    max_positions=int(config["max_positions"]),
                    market_ma_days=int(config["market_ma_days"]),
                    market_slope_days=int(config["market_slope_days"]),
                    phase=0,
                    daily_risk_off_exit=False,
                    slippage=slippage,
                    fee_multiplier=multiplier,
                    oos_start=oos_start,
                )
                rows.append(
                    {
                        "parameter": parameter,
                        "value": value,
                        "cost_case": cost_label,
                        "comparison_window": f"from_index_{COMMON_WINDOW_START_INDEX}",
                        **result,
                    }
                )
    frame = pd.DataFrame(rows)
    for cost_label in frame["cost_case"].unique():
        mask = frame["cost_case"] == cost_label
        reference = frame[
            mask
            & (frame["parameter"] == "momentum_weight_60")
            & (frame["value"] == CENTRAL["momentum_weight_60"])
        ].iloc[0]
        frame.loc[mask, "delta_win_cagr_vs_central"] = (
            frame.loc[mask, "win_cagr"] - reference["win_cagr"]
        )
        frame.loc[mask, "delta_win_mdd_vs_central"] = (
            frame.loc[mask, "win_mdd"] - reference["win_mdd"]
        )
        frame.loc[mask, "delta_win_sharpe_vs_central"] = (
            frame.loc[mask, "win_sharpe"] - reference["win_sharpe"]
        )
        frame.loc[mask, "delta_cagr_vs_central"] = frame.loc[mask, "cagr"] - reference["cagr"]
    frame["plateau_cagr_ok"] = frame["delta_win_cagr_vs_central"] >= PLATEAU["cagr_delta_min"]
    frame["plateau_sharpe_ok"] = (
        frame["delta_win_sharpe_vs_central"] >= PLATEAU["sharpe_delta_min"]
    )
    frame["plateau_mdd_ok"] = (
        frame["delta_win_mdd_vs_central"] >= -PLATEAU["mdd_worsen_max"]
    )
    frame["plateau"] = (
        frame["plateau_cagr_ok"] & frame["plateau_sharpe_ok"] & frame["plateau_mdd_ok"]
    )
    return frame


PARAMETER_CENTRAL = {
    "momentum_weight_60": CENTRAL["momentum_weight_60"],
    "rebalance_step_days": CENTRAL["rebalance_step_days"],
    "max_positions": CENTRAL["max_positions"],
    "market_ma_days": CENTRAL["market_ma_days"],
    "market_slope_days": CENTRAL["market_slope_days"],
}


def experiment_a_summary(frame: pd.DataFrame) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "window_start": f"index_{COMMON_WINDOW_START_INDEX}",
        "note": (
            "각 변형은 자연 워밍업으로 실행하고 지표는 공통 구간에서 계산했다. "
            "MA 변형은 warmup과 risk_on 적격 집합이 함께 변하므로 '게이트만 바꾼 실험'이 아니다."
        ),
        "parameters": {},
    }
    for parameter, group in frame.groupby("parameter", sort=False):
        base = group[group["cost_case"] == "base"]
        local_central = base["value"] == PARAMETER_CENTRAL[parameter]
        central = base[local_central]
        neighbors = base[~local_central]
        summary["parameters"][parameter] = {
            "values": [float(v) if isinstance(v, (int, np.integer)) else v for v in base["value"]],
            "win_cagr": [float(v) for v in base["win_cagr"]],
            "win_mdd": [float(v) for v in base["win_mdd"]],
            "win_sharpe": [float(v) for v in base["win_sharpe"]],
            "warmup_days": [int(v) for v in base["warmup_days"]],
            "effective_phase_vs_140": [int(v) for v in base["effective_phase_vs_140"]],
            "win_cagr_spread": float(base["win_cagr"].max() - base["win_cagr"].min()),
            "win_mdd_spread": float(base["win_mdd"].max() - base["win_mdd"].min()),
            "plateau_values": [
                base.loc[index, "value"]
                for index in base.index[base["plateau"]]
            ],
            "all_plateau": bool(base["plateau"].all()),
            "neighbors_median_win_cagr": float(neighbors["win_cagr"].median())
            if len(neighbors)
            else float("nan"),
            "neighbors_worst_win_cagr": float(neighbors["win_cagr"].min())
            if len(neighbors)
            else float("nan"),
            "central_win_cagr": float(central["win_cagr"].iloc[0]) if len(central) else float("nan"),
        }
    return summary


# --------------------------------------------------------------------------- #
# 실험 B: 리밸런싱 시작 위상
# --------------------------------------------------------------------------- #
def experiment_b(
    price: pd.DataFrame,
    base_index: pd.DataFrame,
    dates: list[pd.Timestamp],
    oos_start: pd.Timestamp,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for phase in PHASES:
        for cost_label, slippage, multiplier in COST_CASES:
            result = run_scenario(
                price,
                base_index,
                dates,
                momentum_weight_60=CENTRAL["momentum_weight_60"],
                rebalance_step_days=CENTRAL["rebalance_step_days"],
                max_positions=CENTRAL["max_positions"],
                market_ma_days=CENTRAL["market_ma_days"],
                market_slope_days=CENTRAL["market_slope_days"],
                phase=phase,
                daily_risk_off_exit=False,
                    slippage=slippage,
                fee_multiplier=multiplier,
                oos_start=oos_start,
            )
            rows.append(
                {
                    "phase": phase,
                    "cost_case": cost_label,
                    **result,
                }
            )
    return pd.DataFrame(rows)


def experiment_b_summary(frame: pd.DataFrame) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for cost_label, group in frame.groupby("cost_case"):
        summary[cost_label] = {
            "cagr_values": [float(v) for v in group["cagr"]],
            "win_cagr_values": [float(v) for v in group["win_cagr"]],
            "ex2026_cagr_values": [float(v) for v in group["ex2026_cagr"]],
            "cagr_range": float(group["cagr"].max() - group["cagr"].min()),
            "win_cagr_range": float(group["win_cagr"].max() - group["win_cagr"].min()),
            "ex2026_cagr_range": float(
                group["ex2026_cagr"].max() - group["ex2026_cagr"].min()
            ),
            "cagr_std": float(group["cagr"].std(ddof=0)),
            "mdd_values": [float(v) for v in group["mdd"]],
            "mdd_range": float(group["mdd"].max() - group["mdd"].min()),
            "win_mdd_range": float(group["win_mdd"].max() - group["win_mdd"].min()),
            "sharpe_range": float(group["sharpe"].max() - group["sharpe"].min()),
            "turnover_range": float(group["turnover"].max() - group["turnover"].min()),
            "decision_count_values": [int(v) for v in group["decision_count"]],
            "trade_count_values": [int(v) for v in group["trade_count"]],
            "first_decision_values": [str(v) for v in group["first_decision"]],
            "last_decision_is_final_pair": [bool(v) for v in group["last_decision_is_final_pair"]],
            "best_phase": int(group.loc[group["cagr"].idxmax(), "phase"]),
            "worst_phase": int(group.loc[group["cagr"].idxmin(), "phase"]),
            "central_phase_rank": int(
                group["cagr"].rank(ascending=False).loc[
                    group.index[group["phase"] == 0][0]
                ]
            ),
        }
    return summary


# --------------------------------------------------------------------------- #
# 실험 C: 사전 고정 교차 조합
# --------------------------------------------------------------------------- #
def experiment_c(
    price: pd.DataFrame,
    base_index: pd.DataFrame,
    dates: list[pd.Timestamp],
    oos_start: pd.Timestamp,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for weight in CROSS_WEIGHTS:
        for rebalance in CROSS_REBALANCE:
            for positions in CROSS_POSITIONS:
                for cost_label, slippage, multiplier in COST_CASES:
                    result = run_scenario(
                        price,
                        base_index,
                        dates,
                        momentum_weight_60=weight,
                        rebalance_step_days=rebalance,
                        max_positions=positions,
                        market_ma_days=CENTRAL["market_ma_days"],
                        market_slope_days=CENTRAL["market_slope_days"],
                        phase=0,
                        daily_risk_off_exit=False,
                                    slippage=slippage,
                        fee_multiplier=multiplier,
                        oos_start=oos_start,
                    )
                    rows.append(
                        {
                            "momentum_weight_60": weight,
                            "rebalance_step_days": rebalance,
                            "max_positions": positions,
                            "cost_case": cost_label,
                            **result,
                        }
                    )
    return pd.DataFrame(rows)


def experiment_c_summary(frame: pd.DataFrame) -> dict[str, Any]:
    base = frame[frame["cost_case"] == "base"].copy()
    stress = frame[frame["cost_case"] == "stress_50bp_3x"].copy()
    window_cols = [
        "momentum_weight_60",
        "rebalance_step_days",
        "max_positions",
        "cagr",
        "mdd",
        "sharpe",
        "win_cagr",
        "win_mdd",
        "win_sharpe",
    ]
    merged = base[window_cols].merge(
        stress[window_cols],
        on=["momentum_weight_60", "rebalance_step_days", "max_positions"],
        suffixes=("_base", "_stress"),
    )
    central_mask = (
        (merged["momentum_weight_60"] == CENTRAL["momentum_weight_60"])
        & (merged["rebalance_step_days"] == CENTRAL["rebalance_step_days"])
        & (merged["max_positions"] == CENTRAL["max_positions"])
    )
    merged["delta_cagr_base"] = merged["cagr_base"] - merged.loc[central_mask, "cagr_base"].iloc[0]
    merged["delta_mdd_base"] = merged["mdd_base"] - merged.loc[central_mask, "mdd_base"].iloc[0]
    merged["rank_base"] = merged["cagr_base"].rank(ascending=False).astype(int)
    merged["rank_stress"] = merged["cagr_stress"].rank(ascending=False).astype(int)
    central = merged[central_mask].iloc[0]
    return {
        "combination_count": len(merged),
        "cost_case_spearman_cagr": spearman(merged["cagr_base"], merged["cagr_stress"]),
        "cost_case_spearman_mdd": spearman(merged["mdd_base"], merged["mdd_stress"]),
        "central": {
            "momentum_weight_60": float(central["momentum_weight_60"]),
            "rebalance_step_days": int(central["rebalance_step_days"]),
            "max_positions": int(central["max_positions"]),
            "cagr_base": float(central["cagr_base"]),
            "cagr_stress": float(central["cagr_stress"]),
            "mdd_base": float(central["mdd_base"]),
            "rank_base": int(central["rank_base"]),
            "rank_stress": int(central["rank_stress"]),
        },
        "cagr_spread_base": float(merged["cagr_base"].max() - merged["cagr_base"].min()),
        "cagr_spread_stress": float(
            merged["cagr_stress"].max() - merged["cagr_stress"].min()
        ),
        "top_by_cagr_base": merged.sort_values("cagr_base", ascending=False)
        .head(5)[
            ["momentum_weight_60", "rebalance_step_days", "max_positions", "cagr_base", "mdd_base"]
        ]
        .to_dict(orient="records"),
        "plateau_fraction": float(
            (
                (
                    merged["win_cagr_base"]
                    - merged.loc[central_mask, "win_cagr_base"].iloc[0]
                    >= PLATEAU["cagr_delta_min"]
                )
                & (
                    merged["win_sharpe_base"]
                    - merged.loc[central_mask, "win_sharpe_base"].iloc[0]
                    >= PLATEAU["sharpe_delta_min"]
                )
                & (
                    merged["win_mdd_base"]
                    - merged.loc[central_mask, "win_mdd_base"].iloc[0]
                    >= -PLATEAU["mdd_worsen_max"]
                )
            ).mean()
        ),
        "cagr_pass_fraction": float(
            (
                merged["win_cagr_base"]
                - merged.loc[central_mask, "win_cagr_base"].iloc[0]
                >= PLATEAU["cagr_delta_min"]
            ).mean()
        ),
        "grid": merged.to_dict(orient="records"),
    }


# --------------------------------------------------------------------------- #
# 실험 D: 위험 관리 후보 A 강건성
# --------------------------------------------------------------------------- #
def experiment_d(
    price: pd.DataFrame,
    base_index: pd.DataFrame,
    dates: list[pd.Timestamp],
    oos_start: pd.Timestamp,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for config_label, weight in RISK_CONFIGS:
        for phase in PHASES:
            for overlay in (False, True):
                for cost_label, slippage, multiplier in COST_CASES:
                    result = run_scenario(
                        price,
                        base_index,
                        dates,
                        momentum_weight_60=weight,
                        rebalance_step_days=CENTRAL["rebalance_step_days"],
                        max_positions=CENTRAL["max_positions"],
                        market_ma_days=CENTRAL["market_ma_days"],
                        market_slope_days=CENTRAL["market_slope_days"],
                        phase=phase,
                        daily_risk_off_exit=overlay,
                                    slippage=slippage,
                        fee_multiplier=multiplier,
                        oos_start=oos_start,
                    )
                    rows.append(
                        {
                            "config": config_label,
                            "momentum_weight_60": weight,
                            "phase": phase,
                            "daily_risk_off_exit": overlay,
                            "cost_case": cost_label,
                            **result,
                        }
                    )
    frame = pd.DataFrame(rows)
    keys = ["config", "phase", "cost_case"]
    baseline = frame[~frame["daily_risk_off_exit"]][keys + ["cagr", "mdd", "sharpe", "turnover"]]
    baseline = baseline.rename(
        columns={"cagr": "cagr_off", "mdd": "mdd_off", "sharpe": "sharpe_off", "turnover": "turnover_off"}
    )
    frame = frame.merge(baseline, on=keys, how="left")
    frame["delta_cagr"] = frame["cagr"] - frame["cagr_off"]
    frame["delta_mdd"] = frame["mdd"] - frame["mdd_off"]
    frame["delta_sharpe"] = frame["sharpe"] - frame["sharpe_off"]
    return frame


def experiment_d_summary(frame: pd.DataFrame) -> dict[str, Any]:
    overlay = frame[frame["daily_risk_off_exit"]]
    summary: dict[str, Any] = {"cells": len(overlay)}
    for cost_label, group in overlay.groupby("cost_case"):
        summary[cost_label] = {
            "mdd_improved_cells": int((group["delta_mdd"] > 0).sum()),
            "mdd_worsened_cells": int((group["delta_mdd"] < 0).sum()),
            "delta_mdd_median": float(group["delta_mdd"].median()),
            "delta_mdd_worst": float(group["delta_mdd"].min()),
            "delta_cagr_median": float(group["delta_cagr"].median()),
            "delta_cagr_worst": float(group["delta_cagr"].min()),
            "delta_sharpe_median": float(group["delta_sharpe"].median()),
            "delta_turnover_median": float(
                (group["turnover"] - group["turnover_off"]).median()
            ),
            "cagr_retention_median": float(
                (group["cagr"] / group["cagr_off"]).median()
            ),
            "mdd_trough_dates": [
                str(value) for value in group["mdd_trough_date"].unique()
            ],
        }
    by_config = (
        overlay.groupby(["config", "cost_case"])["delta_mdd"].median().unstack().to_dict()
    )
    summary["delta_mdd_median_by_config"] = {
        config: {cost: float(value) for cost, value in values.items()}
        for config, values in by_config.items()
    }
    pooled = overlay
    improve_share = float((pooled["delta_mdd"] > 0).mean())
    delta_cagr_median = float(pooled["delta_cagr"].median())
    summary["consistency_screen"] = {
        "thresholds": dict(OVERLAY_SCREEN),
        "mdd_improve_share": improve_share,
        "delta_cagr_median": delta_cagr_median,
        "pass": bool(
            improve_share >= OVERLAY_SCREEN["mdd_improve_share_min"]
            and delta_cagr_median >= OVERLAY_SCREEN["delta_cagr_median_min"]
        ),
        "note": "확인적 검정이 아니라 후보 전달 스크리닝(임계값은 과제 요구를 기계화).",
    }
    return summary


# --------------------------------------------------------------------------- #
# 실험 C 후보 평가(WFA 전달 후보, 최대 2개)
# --------------------------------------------------------------------------- #
def _combo(row: pd.Series) -> tuple[float, int, int]:
    return (
        round(float(row["momentum_weight_60"]), 4),
        int(row["rebalance_step_days"]),
        int(row["max_positions"]),
    )


def _row(frame: pd.DataFrame, combo: tuple) -> pd.Series:
    """튜플 인덱스는 .loc에서 다중 인덱서로 해석되므로 리스트로 감싼다."""
    return frame.loc[[combo]].iloc[0]


def select_candidates(
    price: pd.DataFrame,
    base_index: pd.DataFrame,
    dates: list[pd.Timestamp],
    cross_frame: pd.DataFrame,
    phase_frame: pd.DataFrame,
    oos_start: pd.Timestamp,
) -> dict[str, Any]:
    base = cross_frame[cross_frame["cost_case"] == "base"].copy()
    stress = cross_frame[cross_frame["cost_case"] == "stress_50bp_3x"].copy()
    base["combo"] = base.apply(_combo, axis=1)
    stress["combo"] = stress.apply(_combo, axis=1)
    base = base.set_index("combo")
    stress = stress.set_index("combo")
    combos = sorted(base.index)
    central_key = (
        round(float(CENTRAL["momentum_weight_60"]), 4),
        int(CENTRAL["rebalance_step_days"]),
        int(CENTRAL["max_positions"]),
    )
    central_base = _row(base, central_key)
    central_stress = _row(stress, central_key)
    central_phases = phase_frame[phase_frame["cost_case"] == "base"]
    central_phase_range = float(
        central_phases["win_cagr"].max() - central_phases["win_cagr"].min()
    )
    central_phase_median = float(central_phases["win_cagr"].median())
    phase_range_cap = max(
        central_phase_range * CANDIDATE_GATES["phase_range_max_multiple_of_central"],
        CANDIDATE_GATES["phase_range_floor"],
    )

    records: list[dict[str, Any]] = []
    for combo in combos:
        row_base = _row(base, combo)
        row_stress = _row(stress, combo)
        gates = {
            "mdd": bool(
                (row_base["win_mdd"] - central_base["win_mdd"])
                >= -CANDIDATE_GATES["mdd_worsen_max"]
                and (row_stress["win_mdd"] - central_stress["win_mdd"])
                >= -CANDIDATE_GATES["mdd_worsen_max"]
            ),
            "cagr": bool(
                (row_base["win_cagr"] - central_base["win_cagr"])
                >= CANDIDATE_GATES["cagr_delta_min"]
                and (row_stress["win_cagr"] - central_stress["win_cagr"])
                >= CANDIDATE_GATES["cagr_delta_min"]
            ),
            "sharpe": bool(
                (row_base["win_sharpe"] - central_base["win_sharpe"])
                >= CANDIDATE_GATES["sharpe_delta_min"]
            ),
        }
        stress_rank = int(stress["win_cagr"].rank(ascending=False).loc[[combo]].iloc[0])
        gates["stress_rank"] = bool(stress_rank <= max(1, len(combos) // 2))
        gates["ex2026"] = bool(
            row_base["ex2026_cagr"] >= central_base["ex2026_cagr"]
            and row_stress["ex2026_cagr"] >= central_stress["ex2026_cagr"]
        )
        neighbor_keys = [
            other
            for other in combos
            if other != combo
            and sum(first == second for first, second in zip(other, combo, strict=True)) == 2
        ]
        neighbor_cagr = (
            base.loc[neighbor_keys, "win_cagr"] if neighbor_keys else pd.Series(dtype=float)
        )
        records.append(
            {
                "combo": combo,
                "momentum_weight_60": combo[0],
                "rebalance_step_days": combo[1],
                "max_positions": combo[2],
                "is_central": combo == central_key,
                "n_neighbors": len(neighbor_keys),
                "at_grid_corner": bool(
                    combo[0] in (CROSS_WEIGHTS[0], CROSS_WEIGHTS[-1])
                    and combo[1] in (CROSS_REBALANCE[0], CROSS_REBALANCE[-1])
                ),
                "win_cagr_base": float(row_base["win_cagr"]),
                "win_cagr_stress": float(row_stress["win_cagr"]),
                "win_mdd_base": float(row_base["win_mdd"]),
                "win_mdd_stress": float(row_stress["win_mdd"]),
                "ex2026_cagr_base": float(row_base["ex2026_cagr"]),
                "ex2026_cagr_stress": float(row_stress["ex2026_cagr"]),
                "rank_base": int(base["win_cagr"].rank(ascending=False).loc[[combo]].iloc[0]),
                "rank_stress": stress_rank,
                "neighbor_win_cagr_median": float(neighbor_cagr.median())
                if len(neighbor_cagr)
                else float("nan"),
                "gate_mdd": gates["mdd"],
                "gate_cagr": gates["cagr"],
                "gate_sharpe": gates["sharpe"],
                "gate_stress_rank": gates["stress_rank"],
                "gate_ex2026": gates["ex2026"],
                "gate_phase_range": None,
                "gate_phase_level": None,
                "gate_neighborhood": None,
                "selected": False,
            }
        )

    stage1 = [
        record
        for record in records
        if all(
            record[key]
            for key in ("gate_mdd", "gate_cagr", "gate_sharpe", "gate_stress_rank", "gate_ex2026")
        )
        and not record["is_central"]
    ]

    evaluated: list[dict[str, Any]] = []
    for record in stage1:
        combo = record["combo"]
        phase_rows = []
        for phase in PHASES:
            result = run_scenario(
                price,
                base_index,
                dates,
                momentum_weight_60=combo[0],
                rebalance_step_days=combo[1],
                max_positions=combo[2],
                market_ma_days=CENTRAL["market_ma_days"],
                market_slope_days=CENTRAL["market_slope_days"],
                phase=phase,
                daily_risk_off_exit=False,
                slippage=COST_CASES[0][1],
                fee_multiplier=COST_CASES[0][2],
                oos_start=oos_start,
            )
            phase_rows.append({"phase": phase, **result})
        phase_values = pd.Series([row["win_cagr"] for row in phase_rows])
        phase_range = float(phase_values.max() - phase_values.min())
        phase_level = float(phase_values.median())
        record.update(
            {
                "phase_win_cagr_values": [float(v) for v in phase_values],
                "phase_cagr_range": phase_range,
                "phase_cagr_median": phase_level,
                "phase_cagr_min": float(phase_values.min()),
                "phase_cagr_max": float(phase_values.max()),
                "gate_phase_range": bool(phase_range <= phase_range_cap),
                "gate_phase_level": bool(
                    phase_level
                    >= central_phase_median + CANDIDATE_GATES["neighborhood_cagr_delta_min"]
                ),
                "gate_neighborhood": bool(
                    record["neighbor_win_cagr_median"]
                    >= record["win_cagr_base"] + CANDIDATE_GATES["neighborhood_cagr_delta_min"]
                ),
            }
        )
        cost_score = 1.0 - (
            (record["rank_base"] - 1) / (len(combos) - 1)
            + (record["rank_stress"] - 1) / (len(combos) - 1)
        ) / 2.0
        phase_score = float(
            (phase_values >= central_phase_median - 0.02).mean()
        )
        record["cost_score"] = float(cost_score)
        record["phase_score"] = phase_score
        record["stability_score"] = float((cost_score + phase_score) / 2.0)
        record["all_gates"] = bool(
            record["gate_mdd"]
            and record["gate_cagr"]
            and record["gate_sharpe"]
            and record["gate_stress_rank"]
            and record["gate_ex2026"]
            and record["gate_phase_range"]
            and record["gate_phase_level"]
            and record["gate_neighborhood"]
        )
        if record["all_gates"]:
            evaluated.append(record)

    evaluated.sort(key=lambda item: (-item["stability_score"], item["phase_cagr_range"]))
    selected = evaluated[:2]
    for record in selected:
        record["selected"] = True

    return {
        "criteria": {
            "pre_registered": True,
            "candidate_subset_of_plateau": True,
            "null_rule": "게이트를 모두 통과한 조합이 없으면 후보를 선정하지 않고 그대로 보고한다(사후 완화 금지).",
            "phase_range_cap": phase_range_cap,
            "central_phase_range": central_phase_range,
            "central_phase_median": central_phase_median,
            "score_note": "stability_score는 순위점수와 위상 통과율을 평균한 heuristic이며 성과 지표가 아니다.",
            **CANDIDATE_GATES,
            "combination_count": len(combos),
            "stage1_count": len(stage1),
        },
        "records": records,
        "selected": selected,
        "selected_count": len(selected),
    }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="P1-B-1 parameter stability and phase")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="A/B/C/D를 축소 실행(파이프라인 점검용, 산출물 저장 안 함)",
    )
    args = parser.parse_args()

    frozen = p0b.configure_frozen_settings(CAP)
    os.environ["ETF_MOMENTUM_WEIGHT_60"] = str(CENTRAL["momentum_weight_60"])
    rtb.START, rtb.END = p0b.DEFAULT_START, p0b.DEFAULT_END
    preflight = p0b.preflight_cache(rtb.START, rtb.END)
    digest_before = p0b.data_cache_digest()
    price, base_index, dates = p0b.load_inputs()
    p0b.verify_applied_settings()
    oos_start = pd.Timestamp(frozen["oos_start_date"])
    run_scenario.digest_before = digest_before  # type: ignore[attr-defined]

    # 지표 재계산 항등식: 동결 기간으로 재계산한 risk_on이 캐시 지표와 같아야 한다.
    rebuilt = rebuild_index(base_index, CENTRAL["market_ma_days"], CENTRAL["market_slope_days"])
    if not np.allclose(
        rebuilt["market_ma"].to_numpy(dtype=float),
        base_index["market_ma"].to_numpy(dtype=float),
        equal_nan=True,
    ):
        raise SystemExit("[p1b1] 시장 MA 재계산이 캐시 지표와 다릅니다.")
    if not (rebuilt["risk_on"].fillna(False).to_numpy() == base_index["risk_on"].fillna(False).to_numpy()).all():
        raise SystemExit("[p1b1] risk_on 재계산이 캐시 지표와 다릅니다.")

    canonical = run_scenario(
        price,
        base_index,
        dates,
        momentum_weight_60=CENTRAL["momentum_weight_60"],
        rebalance_step_days=CENTRAL["rebalance_step_days"],
        max_positions=CENTRAL["max_positions"],
        market_ma_days=CENTRAL["market_ma_days"],
        market_slope_days=CENTRAL["market_slope_days"],
        phase=0,
        daily_risk_off_exit=False,
        omit_phase=True,
        slippage=0.0005,
        fee_multiplier=1.0,
        oos_start=oos_start,
    )
    canonical_key = next(iter(RUN_CACHE))
    expected = {"cagr": 0.2454113778672673, "mdd": -0.5121244554166072, "sharpe": 1.0391357658469746}
    if any(abs(canonical[key] - value) > 1e-9 for key, value in expected.items()):
        raise SystemExit(f"[p1b1] 기준 재현 실패: { {k: canonical[k] for k in expected} }")

    # 신규 파라미터 기본값 동일성: phase 0 명시 실행과 인자 생략 실행을 별도 키로 실제 수행한다.
    explicit_params = {
        "momentum_weight_60": CENTRAL["momentum_weight_60"],
        "rebalance_step_days": CENTRAL["rebalance_step_days"],
        "max_positions": CENTRAL["max_positions"],
        "market_ma_days": CENTRAL["market_ma_days"],
        "market_slope_days": CENTRAL["market_slope_days"],
        "phase": 0,
        "daily_risk_off_exit": False,
        "omit_phase": False,
        "slippage": 0.0005,
        "fee_multiplier": 1.0,
    }
    explicit_phase_zero = run_scenario(
        price, base_index, dates, oos_start=oos_start, **explicit_params
    )
    explicit_key = _config_key(**explicit_params)
    if explicit_key == canonical_key:
        raise SystemExit("[p1b1] 기본값 동일성 검사 키가 캐시에서 분리되지 않았습니다.")
    canonical_curve, canonical_trades = CURVES[canonical_key]
    explicit_curve, explicit_trades = CURVES[explicit_key]
    identity_ok = bool(
        canonical["decision_dates"] == explicit_phase_zero["decision_dates"]
        and canonical_trades.equals(explicit_trades)
        and np.allclose(
            canonical_curve["equity"].to_numpy(dtype=float),
            explicit_curve["equity"].to_numpy(dtype=float),
            rtol=0.0,
            atol=1e-9,
        )
        and abs(canonical["final"] - explicit_phase_zero["final"]) < 1e-6
        and abs(canonical["cagr"] - explicit_phase_zero["cagr"]) < 1e-12
    )
    if not identity_ok:
        raise SystemExit("[p1b1] phase 0 명시가 기존 동작과 다릅니다.")

    if args.smoke:
        print("[p1b1] smoke: 기준 재현·항등식 검증 통과")
        return 0

    print("[p1b1] 실험 A 시작", flush=True)
    sensitivity = experiment_a(price, base_index, dates, oos_start)
    sensitivity_summary = experiment_a_summary(sensitivity)

    print("[p1b1] 실험 B 시작", flush=True)
    phase_frame = experiment_b(price, base_index, dates, oos_start)
    phase_summary = experiment_b_summary(phase_frame)

    print("[p1b1] 실험 C 시작", flush=True)
    cross_frame = experiment_c(price, base_index, dates, oos_start)
    cross_summary = experiment_c_summary(cross_frame)

    print("[p1b1] 실험 D 시작", flush=True)
    overlay_frame = experiment_d(price, base_index, dates, oos_start)
    overlay_summary = experiment_d_summary(overlay_frame)

    print("[p1b1] 후보 평가 시작", flush=True)
    candidates = select_candidates(
        price, base_index, dates, cross_frame, phase_frame, oos_start
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    def tidy(frame: pd.DataFrame) -> pd.DataFrame:
        return frame.drop(columns=["decision_dates"], errors="ignore")

    tidy(sensitivity).to_csv(
        OUTPUT_DIR / "parameter_sensitivity.csv", index=False, encoding="utf-8-sig"
    )
    tidy(phase_frame).to_csv(
        OUTPUT_DIR / "phase_sensitivity.csv", index=False, encoding="utf-8-sig"
    )
    tidy(cross_frame).to_csv(
        OUTPUT_DIR / "cross_validation.csv", index=False, encoding="utf-8-sig"
    )
    tidy(overlay_frame).to_csv(
        OUTPUT_DIR / "risk_overlay_robustness.csv", index=False, encoding="utf-8-sig"
    )

    decision_rows: list[dict[str, Any]] = []
    for phase in PHASES:
        result = run_scenario(
            price,
            base_index,
            dates,
            momentum_weight_60=CENTRAL["momentum_weight_60"],
            rebalance_step_days=CENTRAL["rebalance_step_days"],
            max_positions=CENTRAL["max_positions"],
            market_ma_days=CENTRAL["market_ma_days"],
            market_slope_days=CENTRAL["market_slope_days"],
            phase=phase,
            daily_risk_off_exit=False,
            slippage=0.0005,
            fee_multiplier=1.0,
            oos_start=oos_start,
        )
        position = {pd.Timestamp(day): index for index, day in enumerate(dates)}
        for decision in result["decision_dates"]:
            index = position[pd.Timestamp(decision)]
            decision_rows.append(
                {
                    "phase": phase,
                    "decision_date": decision,
                    "decision_index": index,
                    "execution_date": str(pd.Timestamp(dates[index + 1]).date()),
                    "execution_index": index + 1,
                    "is_final_pair": index == len(dates) - 2,
                }
            )
    pd.DataFrame(decision_rows).to_csv(
        OUTPUT_DIR / "decision_log.csv", index=False, encoding="utf-8-sig"
    )

    period_rows: list[dict[str, Any]] = []
    rolling_detail: list[dict[str, Any]] = []
    def add_window_analyses(equity: pd.DataFrame, label: str) -> None:
        period_rows.extend(period_summary(equity, label, oos_start))
        for years in (3, 5):
            rolling_detail.extend(rolling_rows(equity, label, years))
            rolling_detail.extend(
                rolling_rows(equity, label, years, step_years=years, overlapping=False)
            )

    canonical_equity = equity_curve(canonical_key, dates)
    add_window_analyses(canonical_equity, "canonical_baseline")

    for config_label, weight in RISK_CONFIGS:
        result = run_scenario(
            price,
            base_index,
            dates,
            momentum_weight_60=weight,
            rebalance_step_days=CENTRAL["rebalance_step_days"],
            max_positions=CENTRAL["max_positions"],
            market_ma_days=CENTRAL["market_ma_days"],
            market_slope_days=CENTRAL["market_slope_days"],
            phase=0,
            daily_risk_off_exit=False,
            slippage=0.0005,
            fee_multiplier=1.0,
            oos_start=oos_start,
        )
        del result
        label = f"risk_config_{config_label}"
        equity = equity_curve(_config_key(
            momentum_weight_60=weight,
            rebalance_step_days=CENTRAL["rebalance_step_days"],
            max_positions=CENTRAL["max_positions"],
            market_ma_days=CENTRAL["market_ma_days"],
            market_slope_days=CENTRAL["market_slope_days"],
            phase=0,
            daily_risk_off_exit=False,
            slippage=0.0005,
            fee_multiplier=1.0,
        ), dates)
        add_window_analyses(equity, label)

    for record in candidates["selected"]:
        combo = tuple(record["combo"])
        equity = equity_curve(
            _config_key(
                momentum_weight_60=combo[0],
                rebalance_step_days=combo[1],
                max_positions=combo[2],
                market_ma_days=CENTRAL["market_ma_days"],
                market_slope_days=CENTRAL["market_slope_days"],
                phase=0,
                daily_risk_off_exit=False,
                slippage=0.0005,
                fee_multiplier=1.0,
            ),
            dates,
        )
        label = f"candidate_w{combo[0]}_r{combo[1]}_p{combo[2]}"
        add_window_analyses(equity, label)

    pd.DataFrame(period_rows).to_csv(
        OUTPUT_DIR / "period_stats.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(rolling_detail).to_csv(
        OUTPUT_DIR / "rolling_windows.csv", index=False, encoding="utf-8-sig"
    )

    if p0b.data_cache_digest() != digest_before:
        raise SystemExit("[p1b1] 실행 중 data_cache가 변경되었습니다.")

    grid_candidates = [
        {
            "type": "parameter_combo",
            "momentum_weight_60": record["momentum_weight_60"],
            "rebalance_step_days": record["rebalance_step_days"],
            "max_positions": record["max_positions"],
            "stability_score": record["stability_score"],
        }
        for record in candidates["selected"]
    ]
    overlay_candidate = None
    if overlay_summary["consistency_screen"]["pass"]:
        overlay_candidate = {
            "type": "risk_overlay_A",
            "config": "daily_risk_off_exit=True",
            "mdd_improve_share": overlay_summary["consistency_screen"]["mdd_improve_share"],
            "delta_cagr_median": overlay_summary["consistency_screen"]["delta_cagr_median"],
        }
    wfa_candidates = grid_candidates + ([overlay_candidate] if overlay_candidate else [])
    wfa_candidates = wfa_candidates[:2]
    span_years = (pd.Timestamp(dates[-1]) - pd.Timestamp(dates[0])).days / 365.25

    summary = {
        "research_phase": "P1-B-1",
        "baseline": {
            "cagr": canonical["cagr"],
            "mdd": canonical["mdd"],
            "sharpe": canonical["sharpe"],
            "calmar": canonical["calmar"],
            "expected_match": True,
        },
        "identity_checks": {
            "phase_zero_explicit_matches_default": identity_ok,
            "index_recompute_matches_cache": True,
            "natural_warmup_days": NATURAL_WARMUP,
            "common_window_start_index": COMMON_WINDOW_START_INDEX,
        },
        "parameter_sensitivity": sensitivity_summary,
        "phase_sensitivity": {
            **phase_summary,
            "note": "20거래일 주기 유지, 시작 위상만 변경. 위험 관리 주기는 위상과 독립.",
        },
        "cross_validation": cross_summary,
        "risk_overlay": overlay_summary,
        "candidates": candidates,
        "wfa_candidates": {
            "max_allowed": 2,
            "from_cross_grid": grid_candidates,
            "risk_overlay_candidate_a": overlay_candidate,
            "final": wfa_candidates,
            "note": (
                "cross-grid 후보가 0개이고 위험 관리 후보 A만 일관성 스크리닝을 통과했다. "
                "후보 성과는 in-sample 선택 조건부이며 WFA·독립 OOS 검증이 아니다."
            ),
        },
        "cache": dict(CACHE_STATS),
        "pre_registered": {
            "plateau": PLATEAU,
            "candidate_gates": CANDIDATE_GATES,
            "momentum_weights": list(MOMENTUM_WEIGHTS),
            "rebalance_steps": list(REBALANCE_STEPS),
            "position_counts": list(POSITION_COUNTS),
            "ma_windows": list(MA_WINDOWS),
            "slope_windows": list(SLOPE_WINDOWS),
            "phases": list(PHASES),
            "cross_grid": [list(CROSS_WEIGHTS), list(CROSS_REBALANCE), list(CROSS_POSITIONS)],
            "risk_configs": [list(item) for item in RISK_CONFIGS],
            "cost_cases": [list(item) for item in COST_CASES],
        },
        "rolling": {
            "windows": [3, 5],
            "step_years": 1,
            "overlapping": True,
            "non_overlapping_rows": True,
            "span_years": round(span_years, 2),
            "effective_independent_windows": {
                f"{years}y": round(span_years / years, 2) for years in (3, 5)
            },
            "note": "중첩 창에 평균·표준편차·백분위를 계산하지 않는다(유효 독립 창 수 수준).",
        },
        "limits": {
            "rolling_overlap": True,
            "freeze_monitoring_days": int(
                next(
                    row["days"]
                    for row in period_rows
                    if row["config"] == "canonical_baseline"
                    and row["period"] == "freeze_monitoring_oos"
                )
            ),
            "freeze_monitoring_not_independent": True,
            "oos_annualized_metrics_not_used": True,
            "single_universe": True,
            "in_sample_centered": True,
            "spearman_note": "n=18·비용조건은 동일 경로의 중첩이라 탐색적 지표로만 사용",
            "multiple_comparisons": "FWER 미보정, 사전 등록 논리곱 스크리닝(위음성 우세)",
        },
    }
    (OUTPUT_DIR / "p1b1_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    # provenance/run_metadata는 이 블록 이후에 기록되므로 자기 자신의 해시는 담지 않는다.
    self_outputs = {"provenance.json", "run_metadata.json"}
    artifacts = {
        path.name: file_sha256(path)
        for path in sorted(OUTPUT_DIR.glob("*"))
        if path.is_file() and path.name not in self_outputs
    }
    provenance = {
        "phase": "P1-B-1",
        "run_mode": "full",
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
        "central": CENTRAL,
        "common_window_start_index": COMMON_WINDOW_START_INDEX,
        "decision_log_rows": len(decision_rows),
        "unique_runs": CACHE_STATS["runs"],
        "reused_runs": CACHE_STATS["reused"],
        "artifacts": artifacts,
    }
    (OUTPUT_DIR / "run_metadata.json").write_text(
        json.dumps(
            {
                "phase": "P1-B-1",
                "runs": dict(CACHE_STATS),
                "period_rows": len(period_rows),
                "rolling_rows": len(rolling_detail),
                "candidates": [
                    {
                        "momentum_weight_60": record["momentum_weight_60"],
                        "rebalance_step_days": record["rebalance_step_days"],
                        "max_positions": record["max_positions"],
                        "stability_score": record["stability_score"],
                    }
                    for record in candidates["selected"]
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    provenance["run_metadata_sha256"] = file_sha256(OUTPUT_DIR / "run_metadata.json")
    (OUTPUT_DIR / "provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(
        f"[p1b1] 완료: {OUTPUT_DIR} (unique={CACHE_STATS['runs']}, "
        f"reused={CACHE_STATS['reused']}, candidates={candidates['selected_count']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
