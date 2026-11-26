#!/usr/bin/env python3
"""P1-B-3: Block Bootstrap Monte Carlo 검증(연구용).

관측된 일별 순수익률(거래비용 포함)을 circular moving block bootstrap으로 재표본화해
장기 CAGR·MDD·원금 손실·회복 기간의 불확실성을 정량화한다.

- Baseline(동결) / A(daily_risk_off_exit) 동일 조건·동일 블록 인덱스로 paired 비교
- 위상 0~19 전수, 기본/고비용
- 블록 길이 5/10/20/60, 투자 기간 1/3/5년, 경로 2,000 → 기준 블록 10,000
- 2026 제외 subset, iid shuffle 참고군

실행::

    uv run python scripts/p1b3_monte_carlo.py

산출물: ``outputs_p1b3/``
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
import p1b2_frozen_wfa as p1b2

import run_etf_backtest as rtb

OUTPUT_DIR = ROOT / "outputs_p1b3"
AUM = float(rtb.INITIAL_CASH)
EXCLUDE_2026_END = pd.Timestamp("2025-12-31")

PHASES: tuple[int, ...] = tuple(range(20))
COST_CASES: tuple[tuple[str, float, float], ...] = (
    ("base", 0.0005, 1.0),
    ("stress_50bp_3x", 0.005, 3.0),
)
POLICIES: tuple[tuple[str, bool], ...] = (("baseline", False), ("A", True))
BLOCK_LENGTHS: tuple[int, ...] = (5, 10, 20, 60)
BASE_BLOCK = 20
HORIZONS: tuple[int, ...] = (252, 756, 1260)
N_PATHS = 2_000
N_PATHS_EXTENDED = 10_000
SEED = 20261126
MDD_THRESHOLDS: tuple[float, ...] = (-0.20, -0.30, -0.40, -0.50)
LOSS_THRESHOLDS: tuple[float, ...] = (-0.30, -0.50)
CHUNK = 2_000

CODE_FILES = (
    "scripts/p1b3_monte_carlo.py",
    "scripts/test_p1b3_bootstrap.py",
    "scripts/p1b2_frozen_wfa.py",
    "scripts/p0b_execution_stress.py",
    "run_etf_backtest.py",
    "etf_shared.py",
)

RESULT_COLUMNS = (
    "phase",
    "policy",
    "cost_case",
    "subset",
    "method",
    "block_length",
    "horizon",
    "stage",
    "n_paths",
)


# --------------------------------------------------------------------------- #
# 공용 유틸
# --------------------------------------------------------------------------- #
def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"


def code_digest() -> str:
    payload = [f"{name}:{file_sha256(ROOT / name)}" for name in CODE_FILES]
    return hashlib.sha256("|".join(payload).encode("utf-8")).hexdigest()


def validate_returns(dates: pd.Series, returns: np.ndarray, label: str) -> None:
    parsed = pd.to_datetime(dates)
    if parsed.duplicated().any():
        raise ValueError(f"{label}: 중복 날짜가 있습니다")
    if not parsed.is_monotonic_increasing:
        raise ValueError(f"{label}: 날짜가 정렬되어 있지 않습니다")
    if len(returns) != len(parsed):
        raise ValueError(f"{label}: 날짜와 수익률 길이가 다릅니다")
    if not np.isfinite(returns).all():
        raise ValueError(f"{label}: 수익률에 NaN/Inf가 있습니다")
    if (returns <= -1.0).any():
        raise ValueError(f"{label}: -100% 이하 수익률이 있습니다")


def exclude_2026(dates: pd.Series, returns: np.ndarray) -> tuple[pd.Series, np.ndarray]:
    parsed = pd.to_datetime(dates)
    mask = parsed <= EXCLUDE_2026_END
    if not mask.any():
        raise ValueError("2026 제외 구간이 비어 있습니다")
    return parsed[mask].reset_index(drop=True), returns[mask.to_numpy()]


def validate_paired_inputs(
    base_dates: pd.Series,
    base_returns: np.ndarray,
    overlay_dates: pd.Series,
    overlay_returns: np.ndarray,
    label: str,
) -> None:
    """재표본화 전 Baseline/A의 날짜·길이 동일성을 검증한다."""
    base_parsed = pd.to_datetime(base_dates)
    overlay_parsed = pd.to_datetime(overlay_dates)
    if len(base_parsed) != len(overlay_parsed):
        raise ValueError(f"{label}: Baseline/A 길이 불일치 {len(base_parsed)} != {len(overlay_parsed)}")
    if not np.array_equal(base_parsed.to_numpy(), overlay_parsed.to_numpy()):
        raise ValueError(f"{label}: Baseline/A 날짜 배열 불일치")
    validate_returns(base_parsed, base_returns, f"{label}:baseline")
    validate_returns(overlay_parsed, overlay_returns, f"{label}:A")


def block_seed_key(*parts: int) -> list[int]:
    return [int(SEED), *[int(part) for part in parts]]


def circular_block_indices(
    n: int,
    block: int,
    horizon: int,
    n_paths: int,
    seed_key: list[int],
) -> np.ndarray:
    """Circular moving block bootstrap 인덱스(끝에서 시작으로 wrap)."""
    if n <= 0 or block <= 0 or horizon <= 0 or n_paths <= 0:
        raise ValueError("block bootstrap 파라미터는 양수여야 합니다")
    rng = np.random.default_rng(seed_key)
    blocks = int(np.ceil(horizon / block))
    starts = rng.integers(0, n, size=(n_paths, blocks), dtype=np.int64)
    offsets = np.arange(block, dtype=np.int64)
    indices = (starts[:, :, None] + offsets[None, None, :]) % n
    return indices.reshape(n_paths, blocks * block)[:, :horizon]


def iid_bootstrap_indices(
    n: int, horizon: int, n_paths: int, seed_key: list[int]
) -> np.ndarray:
    """일별 IID bootstrap(복원추출) 참고군."""
    rng = np.random.default_rng(seed_key)
    return rng.integers(0, n, size=(n_paths, horizon), dtype=np.int64)


def equity_paths(returns: np.ndarray, indices: np.ndarray, initial: float) -> np.ndarray:
    """초기 자본 열을 포함한 평가액 경로(shape: paths, horizon+1)."""
    sampled = returns[indices]
    compounded = np.cumprod(1.0 + sampled, axis=1) * initial
    anchor = np.full((compounded.shape[0], 1), initial, dtype=float)
    return np.concatenate([anchor, compounded], axis=1)


def longest_underwater_run(equity: np.ndarray) -> np.ndarray:
    """각 경로의 최장 고점 미회복 연속 구간(거래일)."""
    running = np.maximum.accumulate(equity, axis=1)
    under = equity < running
    paths, length = under.shape
    longest = np.zeros(paths, dtype=np.int64)
    current = np.zeros(paths, dtype=np.int64)
    for position in range(length):
        current = np.where(under[:, position], current + 1, 0)
        longest = np.maximum(longest, current)
    return longest


def path_metrics(equity: np.ndarray) -> dict[str, np.ndarray]:
    """경로별 CAGR·MDD·손실·회복 지표. equity는 초기 자본 열을 포함한다."""
    initial = equity[:, 0]
    final = equity[:, -1]
    horizon = equity.shape[1] - 1
    total_return = final / initial - 1.0
    cagr = np.power(final / initial, 252.0 / horizon) - 1.0
    running = np.maximum.accumulate(equity, axis=1)
    drawdown = equity / running - 1.0
    mdd = drawdown.min(axis=1)
    trough = drawdown.argmin(axis=1)
    peak_value = running[np.arange(len(equity)), trough]
    after_trough = np.arange(equity.shape[1])[None, :] > trough[:, None]
    recovered_mask = (equity >= peak_value[:, None]) & after_trough
    recovered = recovered_mask.any(axis=1)
    recovery_index = np.where(recovered, recovered_mask.argmax(axis=1), -1)
    has_drawdown = mdd < 0
    recovery_days = np.where(recovered & has_drawdown, recovery_index - trough, np.nan)
    tuw = longest_underwater_run(equity)
    min_equity = equity.min(axis=1)
    running_final = running[:, -1]
    return {
        "cagr": cagr,
        "total_return": total_return,
        "final": final,
        "mdd": mdd,
        "tuw": tuw,
        "recovery_days": recovery_days,
        "recovered": recovered,
        "has_drawdown": has_drawdown,
        "terminal_underwater": final < running_final,
        "terminal_loss": final < initial,
        "loss_from_initial": min_equity / initial - 1.0,
    }


def proportion_se(rate: float, n_paths: int) -> float:
    return float(np.sqrt(max(rate * (1 - rate), 0.0) / n_paths))


def conditional_unrecovered_rate(metrics: dict[str, np.ndarray]) -> float | None:
    """낙폭 발생 경로 기준 미회복 비율(무낙폭 경로 제외)."""
    drawdown = metrics["has_drawdown"]
    if not drawdown.any():
        return None
    return float(np.mean(~metrics["recovered"][drawdown]))


def summarize(
    metrics: dict[str, np.ndarray],
    *,
    n_paths: int,
    horizon: int,
) -> dict[str, Any]:
    cagr = metrics["cagr"]
    mdd = metrics["mdd"]
    row: dict[str, Any] = {
        "n_paths": n_paths,
        "cagr_p05": float(np.quantile(cagr, 0.05)),
        "cagr_p25": float(np.quantile(cagr, 0.25)),
        "cagr_p50": float(np.quantile(cagr, 0.50)),
        "cagr_p75": float(np.quantile(cagr, 0.75)),
        "cagr_p95": float(np.quantile(cagr, 0.95)),
        "cagr_mean": float(np.mean(cagr)),
        "total_return_p05": float(np.quantile(metrics["total_return"], 0.05)),
        "total_return_p50": float(np.quantile(metrics["total_return"], 0.50)),
        "final_p05": float(np.quantile(metrics["final"], 0.05)),
        "final_p50": float(np.quantile(metrics["final"], 0.50)),
        "final_p95": float(np.quantile(metrics["final"], 0.95)),
        "mdd_median": float(np.median(mdd)),
        "mdd_p05": float(np.quantile(mdd, 0.05)),
        "mdd_p01": float(np.quantile(mdd, 0.01)),
        "terminal_loss_rate": float(np.mean(metrics["terminal_loss"])),
        "terminal_loss_se": proportion_se(float(np.mean(metrics["terminal_loss"])), n_paths),
        "tuw_median": float(np.median(metrics["tuw"])),
        "tuw_p95": float(np.quantile(metrics["tuw"], 0.95)),
        "tuw_gt_1y_rate": float(np.mean(metrics["tuw"] > 252)),
        "tuw_gt_1y_se": proportion_se(float(np.mean(metrics["tuw"] > 252)), n_paths),
        "no_drawdown_rate": float(np.mean(~metrics["has_drawdown"])),
        "terminal_underwater_rate": float(np.mean(metrics["terminal_underwater"])),
        "terminal_underwater_se": proportion_se(
            float(np.mean(metrics["terminal_underwater"])), n_paths
        ),
        "recovery_days_median": float(np.nanmedian(metrics["recovery_days"]))
        if np.isfinite(metrics["recovery_days"]).any()
        else None,
        "recovery_days_p95": float(np.nanquantile(metrics["recovery_days"], 0.95))
        if np.isfinite(metrics["recovery_days"]).any()
        else None,
        "recovery_days_scope": "drawdown_paths_recovered_only",
        "loss_from_initial_p05": float(np.quantile(metrics["loss_from_initial"], 0.05)),
        "horizon_days": horizon,
    }
    for threshold in MDD_THRESHOLDS:
        rate = float(np.mean(mdd <= threshold))
        row[f"mdd_le_{abs(int(threshold * 100))}"] = rate
        row[f"mdd_le_{abs(int(threshold * 100))}_se"] = proportion_se(rate, n_paths)
    for threshold in LOSS_THRESHOLDS:
        rate = float(np.mean(metrics["loss_from_initial"] <= threshold))
        row[f"loss_from_initial_le_{abs(int(threshold * 100))}"] = rate
        row[f"loss_from_initial_le_{abs(int(threshold * 100))}_se"] = proportion_se(rate, n_paths)
    drawdown_mask = metrics["has_drawdown"]
    drawdown_paths = int(drawdown_mask.sum())
    row["drawdown_paths"] = drawdown_paths
    if drawdown_paths:
        unrecovered_rate = conditional_unrecovered_rate(metrics)
        recovered_rate = float(np.mean(metrics["recovered"][drawdown_mask]))
        row["mdd_unrecovered_rate"] = unrecovered_rate
        row["mdd_unrecovered_se"] = proportion_se(unrecovered_rate, drawdown_paths)
        row["recovered_drawdown_rate"] = recovered_rate
        row["mdd_unrecovered_denominator"] = "drawdown_paths"
    else:
        row["mdd_unrecovered_rate"] = None
        row["mdd_unrecovered_se"] = None
        row["recovered_drawdown_rate"] = None
        row["mdd_unrecovered_denominator"] = "drawdown_paths(0)"
    return row


def quantile_stability(
    first: dict[str, Any], second: dict[str, Any]
) -> dict[str, float]:
    keys = ("cagr_p05", "cagr_p50", "cagr_p95", "mdd_p05", "mdd_unrecovered_rate")
    diffs = {f"{key}_diff": float(second[key] - first[key]) for key in keys}
    diffs["max_abs_quantile_diff"] = max(
        abs(second[key] - first[key]) for key in keys if "rate" not in key
    )
    return diffs


def bootstrap_metrics(
    returns: np.ndarray,
    *,
    indices: np.ndarray,
) -> dict[str, np.ndarray]:
    equity = equity_paths(returns, indices, AUM)
    return path_metrics(equity)


def bootstrap_chunked(
    returns: np.ndarray,
    *,
    block: int,
    horizon: int,
    n_paths: int,
    seed_key: list[int],
    chunk: int = CHUNK,
) -> dict[str, np.ndarray]:
    """메모리 절약을 위해 경로를 chunk 단위로 생성한다.

    Baseline/A가 동일 seed_key·동일 chunk 경계를 쓰므로 paired 인덱스가 보장된다.
    """
    parts: dict[str, list[np.ndarray]] = {}
    for start in range(0, n_paths, chunk):
        size = min(chunk, n_paths - start)
        indices = circular_block_indices(
            len(returns), block, horizon, size, [*seed_key, start]
        )
        metrics = bootstrap_metrics(returns, indices=indices)
        for key, values in metrics.items():
            parts.setdefault(key, []).append(values)
        del indices
    return {key: np.concatenate(values) for key, values in parts.items()}


def iid_bootstrap_chunked(
    returns: np.ndarray,
    *,
    horizon: int,
    n_paths: int,
    seed_key: list[int],
    chunk: int = CHUNK,
) -> dict[str, np.ndarray]:
    parts: dict[str, list[np.ndarray]] = {}
    for start in range(0, n_paths, chunk):
        size = min(chunk, n_paths - start)
        indices = iid_bootstrap_indices(len(returns), horizon, size, [*seed_key, start])
        metrics = bootstrap_metrics(returns, indices=indices)
        for key, values in metrics.items():
            parts.setdefault(key, []).append(values)
        del indices
    return {key: np.concatenate(values) for key, values in parts.items()}


# --------------------------------------------------------------------------- #
# 원본 일별 수익률 생성
# --------------------------------------------------------------------------- #
def build_source_returns(
    price: pd.DataFrame,
    index_df: pd.DataFrame,
    dates: list[pd.Timestamp],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    rows: list[pd.DataFrame] = []
    summary: dict[str, Any] = {}
    for cost_label, slippage, multiplier in COST_CASES:
        for policy_label, overlay in POLICIES:
            for phase in PHASES:
                curve, trades, _ = p1b2.run_policy(
                    price,
                    index_df,
                    dates,
                    overlay=overlay,
                    slippage=slippage,
                    fee_multiplier=multiplier,
                    phase=phase,
                )
                equity = p0b.evaluation_curve(AUM, dates, curve)
                equity["date"] = pd.to_datetime(equity["date"])
                returns = equity["equity"].pct_change().fillna(0.0).to_numpy(dtype=float)
                validate_returns(equity["date"], returns, f"{policy_label}:{cost_label}:p{phase}")
                compounded = AUM * float(np.prod(1.0 + returns))
                if abs(compounded - float(equity["equity"].iloc[-1])) > 1e-6:
                    raise SystemExit(
                        f"[p1b3] 복리 검산 실패 {policy_label}:{cost_label}:p{phase} "
                        f"{compounded} != {equity['equity'].iloc[-1]}"
                    )
                frame = pd.DataFrame(
                    {
                        "date": equity["date"].to_numpy(),
                        "phase": phase,
                        "policy": policy_label,
                        "cost_case": cost_label,
                        "equity": equity["equity"].to_numpy(dtype=float),
                        "daily_return": returns,
                    }
                )
                rows.append(frame)
                if phase == 0:
                    stats = p1b2.metrics(
                        curve,
                        trades,
                        dates,
                        slippage=slippage,
                        fee_multiplier=multiplier,
                        oos_start=pd.Timestamp("2026-07-22"),
                    )
                    summary[f"{policy_label}:{cost_label}:p0"] = {
                        key: stats[key] for key in ("cagr", "mdd", "sharpe", "calmar", "turnover", "total_cost")
                    }
    return pd.concat(rows, ignore_index=True), summary


# --------------------------------------------------------------------------- #
# Monte Carlo 실행
# --------------------------------------------------------------------------- #
def _conditional_delta(
    overlay_metrics: dict[str, np.ndarray], base_metrics: dict[str, np.ndarray]
) -> float | None:
    overlay_rate = conditional_unrecovered_rate(overlay_metrics)
    base_rate = conditional_unrecovered_rate(base_metrics)
    if overlay_rate is None or base_rate is None:
        return None
    return float(overlay_rate - base_rate)


def run_grid(
    returns_by_condition: dict[tuple[str, int, str], tuple[pd.Series, np.ndarray]],
    *,
    blocks: tuple[int, ...],
    horizons: tuple[int, ...],
    n_paths: int,
    stage: str,
    subsets: tuple[str, ...] = ("full",),
) -> tuple[pd.DataFrame, pd.DataFrame]:
    summary_rows: list[dict[str, Any]] = []
    paired_rows: list[dict[str, Any]] = []
    policy_names = [name for name, _ in POLICIES]
    for subset in subsets:
        for cost_index, (cost_label, _, _) in enumerate(COST_CASES):
            for phase in PHASES:
                for block in blocks:
                    for horizon in horizons:
                        key = block_seed_key(phase, cost_index, block, horizon, n_paths)
                        payload: dict[str, tuple[pd.Series, np.ndarray]] = {}
                        for policy_label in policy_names:
                            condition_dates, returns_p = returns_by_condition[
                                (policy_label, phase, cost_label)
                            ]
                            if subset == "ex2026":
                                condition_dates, returns_p = exclude_2026(
                                    condition_dates, returns_p
                                )
                            payload[policy_label] = (condition_dates, returns_p)
                        validate_paired_inputs(
                            *payload["baseline"],
                            *payload["A"],
                            f"{subset}:p{phase}:{cost_label}",
                        )
                        if len(payload["baseline"][1]) < block:
                            continue
                        metrics_by_policy: dict[str, dict[str, np.ndarray]] = {}
                        for policy_label in policy_names:
                            metrics_by_policy[policy_label] = bootstrap_chunked(
                                payload[policy_label][1],
                                block=block,
                                horizon=horizon,
                                n_paths=n_paths,
                                seed_key=key,
                            )
                            summary_rows.append(
                                {
                                    "phase": phase,
                                    "policy": policy_label,
                                    "cost_case": cost_label,
                                    "subset": subset,
                                    "method": "block",
                                    "block_length": block,
                                    "horizon": horizon,
                                    "stage": stage,
                                    **summarize(
                                        metrics_by_policy[policy_label],
                                        n_paths=n_paths,
                                        horizon=horizon,
                                    ),
                                }
                            )
                        base_metrics = metrics_by_policy["baseline"]
                        overlay_metrics = metrics_by_policy["A"]
                        delta_cagr = overlay_metrics["cagr"] - base_metrics["cagr"]
                        delta_mdd = overlay_metrics["mdd"] - base_metrics["mdd"]
                        delta_final = overlay_metrics["final"] - base_metrics["final"]
                        delta_tuw = overlay_metrics["tuw"].astype(float) - base_metrics["tuw"].astype(float)
                        paired_rows.append(
                            {
                                "phase": phase,
                                "cost_case": cost_label,
                                "subset": subset,
                                "block_length": block,
                                "horizon": horizon,
                                "stage": stage,
                                "n_paths": n_paths,
                                "delta_cagr_median": float(np.median(delta_cagr)),
                                "delta_cagr_p05": float(np.quantile(delta_cagr, 0.05)),
                                "delta_mdd_median": float(np.median(delta_mdd)),
                                "delta_mdd_p05": float(np.quantile(delta_mdd, 0.05)),
                                "delta_final_median": float(np.median(delta_final)),
                                "delta_tuw_median": float(np.median(delta_tuw)),
                                "delta_terminal_loss_rate": float(
                                    np.mean(overlay_metrics["terminal_loss"])
                                    - np.mean(base_metrics["terminal_loss"])
                                ),
                                "delta_mdd_unrecovered_rate": _conditional_delta(
                                    overlay_metrics, base_metrics
                                ),
                                "mdd_improve_5pp_share": float(np.mean(delta_mdd >= 0.05)),
                                "cagr_worse_1pp_share": float(np.mean(delta_cagr <= -0.01)),
                                "joint_mdd_up_cagr_down_share": float(
                                    np.mean((delta_mdd >= 0.05) & (delta_cagr <= -0.01))
                                ),
                            }
                        )
    return pd.DataFrame(summary_rows), pd.DataFrame(paired_rows)


def run_shuffle_reference(
    returns_by_condition: dict[tuple[str, int, str], tuple[pd.Series, np.ndarray]],
    *,
    n_paths: int,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    cost_label = COST_CASES[0][0]
    for policy_label, _ in POLICIES:
        for horizon in HORIZONS:
            _, returns_p = returns_by_condition[(policy_label, 0, cost_label)]
            key = block_seed_key(0, 0, 0, horizon, n_paths)
            metrics = iid_bootstrap_chunked(
                returns_p, horizon=horizon, n_paths=n_paths, seed_key=key
            )
            rows.append(
                {
                    "phase": 0,
                    "policy": policy_label,
                    "cost_case": cost_label,
                    "subset": "full",
                    "method": "iid_bootstrap",
                    "block_length": 0,
                    "horizon": horizon,
                    "stage": "reference",
                    **summarize(metrics, n_paths=n_paths, horizon=horizon),
                }
            )
    return pd.DataFrame(rows)


def phase_summary(frame: pd.DataFrame, *, subset: str, block: int, stage: str) -> pd.DataFrame:
    focus = frame[
        (frame["subset"] == subset)
        & (frame["block_length"] == block)
        & (frame["stage"] == stage)
    ]
    rows: list[dict[str, Any]] = []
    for (cost_label, policy_label, horizon), group in focus.groupby(
        ["cost_case", "policy", "horizon"]
    ):
        group = group.sort_values("phase")
        phase0 = group[group["phase"] == 0].iloc[0]
        for metric in (
            "cagr_p50",
            "cagr_p05",
            "mdd_p05",
            "terminal_loss_rate",
            "mdd_unrecovered_rate",
            "terminal_underwater_rate",
        ):
            values = group[metric].astype(float)
            phase0_value = float(phase0[metric])
            if metric in {"mdd_p05"}:
                rank = int(values.rank(ascending=False).loc[phase0.name])
                percentile = float((values < phase0_value).mean())
            elif metric in {"terminal_loss_rate", "mdd_unrecovered_rate", "terminal_underwater_rate"}:
                rank = int(values.rank(ascending=True).loc[phase0.name])
                percentile = float((values > phase0_value).mean())
            else:
                rank = int(values.rank(ascending=False).loc[phase0.name])
                percentile = float((values < phase0_value).mean())
            rows.append(
                {
                    "cost_case": cost_label,
                    "policy": policy_label,
                    "horizon": horizon,
                    "subset": subset,
                    "block_length": block,
                    "stage": stage,
                    "metric": metric,
                    "phase0_value": phase0_value,
                    "phase0_rank": rank,
                    "phase0_percentile": percentile,
                    "phase_median": float(values.median()),
                    "phase_min": float(values.min()),
                    "phase_max": float(values.max()),
                    "phase_range": float(values.max() - values.min()),
                }
            )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="P1-B-3 block bootstrap Monte Carlo")
    parser.add_argument("--smoke", action="store_true", help="기준 재현·경로 수 축소 점검")
    args = parser.parse_args()

    frozen = p0b.configure_frozen_settings(0.85)
    os.environ["ETF_MOMENTUM_WEIGHT_60"] = "0.55"
    rtb.START, rtb.END = p0b.DEFAULT_START, p0b.DEFAULT_END
    preflight = p0b.preflight_cache(rtb.START, rtb.END)
    digest_before = p0b.data_cache_digest()
    price, index_df, dates = p0b.load_inputs()
    p0b.verify_applied_settings()
    full_dates = [pd.Timestamp(day) for day in dates]

    # 기준 재현(phase 0, base)
    base_curve, base_trades, _ = p1b2.run_policy(
        price, index_df, full_dates, overlay=False, slippage=0.0005, fee_multiplier=1.0, phase=0
    )
    baseline = p1b2.metrics(
        base_curve,
        base_trades,
        full_dates,
        slippage=0.0005,
        fee_multiplier=1.0,
        oos_start=pd.Timestamp(frozen["oos_start_date"]),
    )
    expected = {"cagr": 0.2454113778672673, "mdd": -0.5121244554166072, "sharpe": 1.0391357658469746}
    if any(abs(baseline[key] - value) > 1e-9 for key, value in expected.items()):
        raise SystemExit(f"[p1b3] 기준 재현 실패: { {k: baseline[k] for k in expected} }")

    print("[p1b3] 원본 일별 수익률 생성(80조건)", flush=True)
    source, p0_stats = build_source_returns(price, index_df, full_dates)
    returns_by_condition: dict[tuple[str, int, str], tuple[pd.Series, np.ndarray]] = {}
    for (phase, policy_label, cost_label), group in source.groupby(["phase", "policy", "cost_case"]):
        ordered = group.sort_values("date")
        returns_by_condition[(policy_label, int(phase), cost_label)] = (
            ordered["date"].reset_index(drop=True),
            ordered["daily_return"].to_numpy(dtype=float),
        )

    n_paths = 200 if args.smoke else N_PATHS
    blocks = (BASE_BLOCK,) if args.smoke else BLOCK_LENGTHS
    horizons = HORIZONS

    print(f"[p1b3] stage1 block bootstrap (paths={n_paths})", flush=True)
    summary_frame, paired_frame = run_grid(
        returns_by_condition,
        blocks=blocks,
        horizons=horizons,
        n_paths=n_paths,
        stage="base",
        subsets=("full", "ex2026") if not args.smoke else ("full",),
    )
    shuffle_frame = run_shuffle_reference(returns_by_condition, n_paths=n_paths)

    extended_frame = pd.DataFrame()
    stability_records: list[dict[str, Any]] = []
    if not args.smoke:
        print("[p1b3] stage2 확장(기준 블록 10,000경로)", flush=True)
        extended_frame, extended_paired = run_grid(
            returns_by_condition,
            blocks=(BASE_BLOCK,),
            horizons=HORIZONS,
            n_paths=N_PATHS_EXTENDED,
            stage="extended",
            subsets=("full", "ex2026"),
        )
        base_focus = summary_frame[
            (summary_frame["subset"] == "full")
            & (summary_frame["block_length"] == BASE_BLOCK)
            & (summary_frame["stage"] == "base")
        ]
        for row in extended_frame[extended_frame["subset"] == "full"].itertuples():
            match = base_focus[
                (base_focus["phase"] == row.phase)
                & (base_focus["policy"] == row.policy)
                & (base_focus["cost_case"] == row.cost_case)
                & (base_focus["horizon"] == row.horizon)
            ]
            if match.empty:
                continue
            first = match.iloc[0].to_dict()
            second = row._asdict()
            stability_records.append(
                {
                    "phase": int(row.phase),
                    "policy": row.policy,
                    "cost_case": row.cost_case,
                    "horizon": int(row.horizon),
                    **quantile_stability(first, second),
                }
            )
        paired_frame = pd.concat([paired_frame, extended_paired], ignore_index=True)
        summary_frame = pd.concat([summary_frame, extended_frame], ignore_index=True)

    phase_summary_frame = phase_summary(
        summary_frame,
        subset="full",
        block=BASE_BLOCK,
        stage="extended" if not args.smoke else "base",
    )

    if p0b.data_cache_digest() != digest_before:
        raise SystemExit("[p1b3] 실행 중 data_cache가 변경되었습니다.")

    if args.smoke:
        print(
            "[p1b3] smoke: 기준 재현·부트스트랩 점검 통과 "
            f"(conditions={len(returns_by_condition)}, rows={len(summary_frame)})"
        )
        return 0

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    source.to_csv(OUTPUT_DIR / "source_returns.csv", index=False, encoding="utf-8-sig")
    summary_frame.to_csv(OUTPUT_DIR / "mc_summary.csv", index=False, encoding="utf-8-sig")
    phase_summary_frame.to_csv(
        OUTPUT_DIR / "mc_phase_summary.csv", index=False, encoding="utf-8-sig"
    )
    paired_frame.to_csv(
        OUTPUT_DIR / "mc_paired_comparison.csv", index=False, encoding="utf-8-sig"
    )
    shuffle_frame.to_csv(OUTPUT_DIR / "mc_iid_reference.csv", index=False, encoding="utf-8-sig")
    block_frame = summary_frame[
        (summary_frame["method"] == "block") & (summary_frame["subset"].isin(["full", "ex2026"]))
    ]
    block_frame.to_csv(
        OUTPUT_DIR / "mc_block_sensitivity.csv", index=False, encoding="utf-8-sig"
    )
    recovery_columns = [
        "phase",
        "policy",
        "cost_case",
        "subset",
        "block_length",
        "horizon",
        "stage",
        "n_paths",
        "tuw_median",
        "tuw_p95",
        "tuw_gt_1y_rate",
        "drawdown_paths",
        "no_drawdown_rate",
        "recovered_drawdown_rate",
        "mdd_unrecovered_rate",
        "mdd_unrecovered_se",
        "mdd_unrecovered_denominator",
        "terminal_underwater_rate",
        "recovery_days_median",
        "recovery_days_scope",
        "recovery_days_p95",
        "mdd_p05",
    ]
    summary_frame[recovery_columns].to_csv(
        OUTPUT_DIR / "mc_recovery_summary.csv", index=False, encoding="utf-8-sig"
    )

    stability_frame = pd.DataFrame(stability_records)
    if not stability_frame.empty:
        stability_frame.to_csv(
            OUTPUT_DIR / "mc_quantile_stability.csv", index=False, encoding="utf-8-sig"
        )

    def pull(frame: pd.DataFrame, **conditions: Any) -> pd.DataFrame:
        focus = frame
        for key, value in conditions.items():
            focus = focus[focus[key] == value]
        return focus

    def headline(policy_label: str, cost_label: str, horizon: int, phase: int = 0) -> dict[str, Any]:
        focus = pull(
            summary_frame,
            policy=policy_label,
            cost_case=cost_label,
            phase=phase,
            horizon=horizon,
            subset="full",
            block_length=BASE_BLOCK,
            stage="extended",
        )
        if focus.empty:
            return {}
        row = focus.iloc[0]
        return {
            key: float(row[key])
            for key in (
                "cagr_p05",
                "cagr_p50",
                "cagr_p95",
                "mdd_median",
                "mdd_p05",
                "terminal_loss_rate",
                "mdd_unrecovered_rate",
                "terminal_underwater_rate",
                "tuw_p95",
            )
        }

    paired_extended = paired_frame[
        (paired_frame["stage"] == "extended")
        & (paired_frame["subset"] == "full")
        & (paired_frame["block_length"] == BASE_BLOCK)
    ]
    paired_5y = paired_extended[paired_extended["horizon"] == 1260]

    summary = {
        "phase": "P1-B-3",
        "baseline": {
            key: baseline[key] for key in ("cagr", "mdd", "sharpe", "calmar", "turnover", "total_cost")
        },
        "source": {
            "conditions": len(returns_by_condition),
            "phase0_metrics": p0_stats,
            "rows": len(source),
            "date_start": str(pd.Timestamp(source["date"].min()).date()),
            "date_end": str(pd.Timestamp(source["date"].max()).date()),
            "mean_return_base_p0": float(
                source[
                    (source["phase"] == 0)
                    & (source["policy"] == "baseline")
                    & (source["cost_case"] == "base")
                ]["daily_return"].mean()
            ),
            "compounding_identity_ok": True,
        },
        "monte_carlo": {
            "method": "circular_moving_block_bootstrap",
            "seed": SEED,
            "n_paths": N_PATHS,
            "n_paths_extended": N_PATHS_EXTENDED,
            "block_lengths": list(BLOCK_LENGTHS),
            "base_block": BASE_BLOCK,
            "horizons": list(HORIZONS),
            "conditions": len(returns_by_condition),
            "grid_rows": len(summary_frame),
            "paths_evaluated": int(summary_frame["n_paths"].sum()),
            "cost_on_returns_only": True,
            "paired_same_indices": True,
        },
        "headline": {
            f"{policy}:{cost}:{horizon}": headline(policy, cost, horizon)
            for policy, _ in POLICIES
            for cost in [label for label, _, _ in COST_CASES]
            for horizon in HORIZONS
        },
        "phase_dispersion": phase_summary_frame.to_dict(orient="records"),
        "paired_5y": paired_5y.to_dict(orient="records"),
        "paired_summary_5y": {
            "delta_mdd_median": float(paired_5y["delta_mdd_median"].median()),
            "delta_cagr_median": float(paired_5y["delta_cagr_median"].median()),
            "mdd_improve_5pp_share_median": float(paired_5y["mdd_improve_5pp_share"].median()),
            "cagr_worse_1pp_share_median": float(paired_5y["cagr_worse_1pp_share"].median()),
            "joint_share_median": float(paired_5y["joint_mdd_up_cagr_down_share"].median()),
        }
        if not paired_5y.empty
        else {},
        "shuffle_reference": shuffle_frame.to_dict(orient="records"),
        "quantile_stability": {
            "rows": len(stability_frame),
            "max_abs_quantile_diff": float(stability_frame["max_abs_quantile_diff"].max())
            if not stability_frame.empty
            else None,
            "median_abs_quantile_diff": float(stability_frame["max_abs_quantile_diff"].median())
            if not stability_frame.empty
            else None,
        },
        "cost_case_headline": {
            cost_label: {
                policy_label: headline(policy_label, cost_label, 1260)
                for policy_label, _ in POLICIES
            }
            for cost_label, _, _ in COST_CASES
        },
        "ex2026_headline": {
            policy_label: {
                key: float(row[key])
                for key in ("cagr_p50", "mdd_p05", "terminal_loss_rate")
            }
            for policy_label, _ in POLICIES
            for row in [
                pull(
                    summary_frame,
                    policy=policy_label,
                    cost_case="base",
                    phase=0,
                    horizon=1260,
                    subset="ex2026",
                    block_length=BASE_BLOCK,
                    stage="extended",
                ).iloc[0]
            ]
        },
        "limits": {
            "resampling_of_observed_returns": True,
            "not_new_price_paths": True,
            "not_independent_oos": True,
            "phases_not_independent": True,
            "phase_mixture_not_strategy": True,
            "model_assumptions": True,
            "pit_blockers": True,
        },
        "pre_registered": {
            "phases": list(PHASES),
            "policies": [list(item) for item in POLICIES],
            "cost_cases": [list(item) for item in COST_CASES],
            "block_lengths": list(BLOCK_LENGTHS),
            "horizons": list(HORIZONS),
            "seed": SEED,
        },
        "warning": (
            "재표본 기반 추정치이며 미래 손실 확률을 보장하지 않는다. "
            "신규 가격 경로에서 매매 결정을 재실행한 시뮬레이션이 아니다."
        ),
    }
    (OUTPUT_DIR / "p1b3_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    self_outputs = {"provenance.json"}
    artifacts = {
        path.name: file_sha256(path)
        for path in sorted(OUTPUT_DIR.glob("*"))
        if path.is_file() and path.name not in self_outputs
    }
    provenance = {
        "phase": "P1-B-3",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": p0b.git_commit(),
        "code_digest": code_digest(),
        "code_files": list(CODE_FILES),
        "data_cache_digest": digest_before,
        "freeze_sha256": frozen["freeze_sha256"],
        "freeze_date": frozen.get("freeze_date"),
        "period": {"start": p0b.DEFAULT_START, "end": p0b.DEFAULT_END},
        "oos_start_date": frozen["oos_start_date"],
        "cost_cases": {label: [slippage, multiplier] for label, slippage, multiplier in COST_CASES},
        "phases": list(PHASES),
        "seed": SEED,
        "n_paths": N_PATHS,
        "n_paths_extended": N_PATHS_EXTENDED,
        "block_lengths": list(BLOCK_LENGTHS),
        "horizons": list(HORIZONS),
        "conditions": len(returns_by_condition),
        "preflight": preflight,
        "simulated_paths": int(summary_frame["n_paths"].sum()),
        "artifacts": artifacts,
    }
    (OUTPUT_DIR / "provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(
        f"[p1b3] 완료: {OUTPUT_DIR} (conditions={len(returns_by_condition)}, "
        f"rows={len(summary_frame)}, paths={int(summary_frame['n_paths'].sum())})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
