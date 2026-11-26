#!/usr/bin/env python3
"""P1-B-3: block bootstrap·경로 지표 합성 fixture.

검증:
- 평가액과 일별 수익률의 복리 항등식
- circular block 인덱스(블록 길이·wrap·seed 재현성·paired 동일 인덱스)
- MDD·회수 기간·TUW·미회복·원금 손실 구분
- 2026 제외 범위, 입력 검증(NaN/Inf/중복/정렬)
- 비용 중복 차감 없음(순수익률만 사용)
- 비율 표본오차·분위수 안정성 함수

실행::

    uv run python scripts/test_p1b3_bootstrap.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import p1b3_monte_carlo as mc


def check_compounding_identity() -> None:
    rng = np.random.default_rng(0)
    returns = rng.normal(0.0004, 0.01, size=500)
    indices = mc.circular_block_indices(len(returns), 20, 252, 50, [1, 2, 3])
    equity = mc.equity_paths(returns, indices, 1_000_000.0)
    expected = 1_000_000.0 * np.prod(1.0 + returns[indices], axis=1)
    if not np.allclose(equity[:, -1], expected, rtol=0.0, atol=1e-6):
        raise AssertionError("복리 항등식이 성립하지 않습니다")
    if not np.all(equity[:, 0] == 1_000_000.0):
        raise AssertionError("초기 자본 열이 없습니다")


def check_circular_blocks() -> None:
    n, block, horizon, paths = 12, 4, 8, 20
    seed = mc.block_seed_key(0, 0, block, horizon, paths)
    indices = mc.circular_block_indices(n, block, horizon, paths, seed)
    if indices.shape != (paths, horizon):
        raise AssertionError(f"인덱스 shape 오류: {indices.shape}")
    if indices.min() < 0 or indices.max() >= n:
        raise AssertionError("인덱스 범위 오류")
    first_block = indices[0, :block]
    if len({(first_block[i + 1] - first_block[i]) % n for i in range(block - 1)}) != 1:
        raise AssertionError("첫 블록이 연속 구간이 아닙니다")
    wrap_starts = mc.circular_block_indices(n, n, n, 5, mc.block_seed_key(9))
    for row in wrap_starts:
        if sorted(row.tolist()) != list(range(n)):
            raise AssertionError("circular wrap이 순열이 아닙니다")
        if len({(int(row[i + 1]) - int(row[i])) % n for i in range(n - 1)}) != 1:
            raise AssertionError("circular wrap이 연속 회전이 아닙니다")
    again = mc.circular_block_indices(n, block, horizon, paths, seed)
    if not np.array_equal(indices, again):
        raise AssertionError("동일 seed 재현성이 없습니다")
    other = mc.circular_block_indices(n, block, horizon, paths, mc.block_seed_key(0, 0, block, horizon, paths + 1))
    if np.array_equal(indices, other):
        raise AssertionError("seed가 달라도 인덱스가 같습니다")


def check_paired_indices() -> None:
    rng = np.random.default_rng(1)
    returns = rng.normal(0.0003, 0.01, size=300)
    key = mc.block_seed_key(3, 1, 20, 756, 2000)
    first = mc.bootstrap_chunked(
        returns, block=20, horizon=756, n_paths=2000, seed_key=key, chunk=500
    )
    second = mc.bootstrap_chunked(
        returns, block=20, horizon=756, n_paths=2000, seed_key=key, chunk=500
    )
    if not np.array_equal(first["cagr"], second["cagr"]):
        raise AssertionError("동일 seed·동일 chunk의 paired 결과가 다릅니다")
    lower = mc.bootstrap_chunked(
        returns * 0.9, block=20, horizon=756, n_paths=2000, seed_key=key, chunk=500
    )
    if np.array_equal(first["cagr"], lower["cagr"]):
        raise AssertionError("수익률이 다른데 경로 결과가 같습니다")
    other_seed = mc.bootstrap_chunked(
        returns,
        block=20,
        horizon=756,
        n_paths=2000,
        seed_key=mc.block_seed_key(3, 1, 20, 756, 2001),
        chunk=500,
    )
    if np.array_equal(first["cagr"], other_seed["cagr"]):
        raise AssertionError("seed가 다른데 경로 결과가 같습니다")


def check_paired_inputs() -> None:
    dates = pd.Series(pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]))
    returns = np.array([0.01, -0.005, 0.002])
    mc.validate_paired_inputs(dates, returns, dates.copy(), returns.copy(), "valid")
    cases = (
        ("length", dates, returns, dates.iloc[:2], returns[:2]),
        (
            "different dates",
            dates,
            returns,
            pd.Series(pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-05"])),
            returns,
        ),
        (
            "missing date",
            dates,
            returns,
            pd.Series(pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-03"])),
            returns,
        ),
    )
    for label, base_dates, base_returns, overlay_dates, overlay_returns in cases:
        try:
            mc.validate_paired_inputs(
                base_dates, base_returns, overlay_dates, overlay_returns, label
            )
        except ValueError:
            continue
        raise AssertionError(f"paired 입력 검증({label})이 거부되지 않았습니다")


def check_iid_bootstrap() -> None:
    n, horizon, paths = 50, 100, 100
    key = mc.block_seed_key(7)
    indices = mc.iid_bootstrap_indices(n, horizon, paths, key)
    if indices.shape != (paths, horizon):
        raise AssertionError("iid bootstrap shape 오류")
    if indices.min() < 0 or indices.max() >= n:
        raise AssertionError("iid bootstrap 인덱스 범위 오류")
    if len(np.unique(indices)) >= n * horizon:
        raise AssertionError("복원추출이 아닙니다(중복 인덱스 없음)")
    again = mc.iid_bootstrap_indices(n, horizon, paths, key)
    if not np.array_equal(indices, again):
        raise AssertionError("iid bootstrap seed 재현성이 없습니다")


def check_path_metrics() -> None:
    paths = np.array(
        [
            [1000.0, 1200.0, 900.0, 800.0, 1300.0],
            [1000.0, 1200.0, 900.0, 950.0, 990.0],
            [1000.0, 900.0, 800.0, 850.0, 860.0],
            [1000.0, 1000.0, 1000.0, 1000.0, 1000.0],
        ]
    )
    metrics = mc.path_metrics(paths)
    if abs(metrics["mdd"][0] + 1 / 3) > 1e-9:
        raise AssertionError(f"MDD 계산 오류: {metrics['mdd'][0]}")
    if int(metrics["tuw"][0]) != 2:
        raise AssertionError(f"TUW 계산 오류: {metrics['tuw'][0]}")
    if not bool(metrics["recovered"][0]) or int(metrics["recovery_days"][0]) != 1:
        raise AssertionError(f"회수 기간 오류: {metrics['recovery_days'][0]}")
    if bool(metrics["recovered"][1]) or not np.isnan(metrics["recovery_days"][1]):
        raise AssertionError("미회복 경로가 회복으로 처리되었습니다")
    # 최대 낙폭 구간은 회복했지만 종료 시점에는 고점 미달인 경로
    late_high = np.array([[1000.0, 1200.0, 900.0, 1300.0, 1250.0]])
    late = mc.path_metrics(late_high)
    if not bool(late["recovered"][0]):
        raise AssertionError("최대 낙폭 구간 회복이 False로 계산되었습니다")
    if not bool(late["terminal_underwater"][0]):
        raise AssertionError("종료 시 고점 미달이 감지되지 않았습니다")
    if bool(metrics["terminal_underwater"][0]):
        raise AssertionError("고점 회복 후 종료 경로가 고점 미달로 계산되었습니다")
    if not bool(metrics["terminal_loss"][2]) or abs(metrics["loss_from_initial"][2] + 0.2) > 1e-12:
        raise AssertionError("원금 손실/중간 손실 구분 오류")
    if abs(metrics["mdd"][3]) > 1e-12 or bool(metrics["terminal_loss"][3]):
        raise AssertionError("무변동 경로 계산 오류")
    if bool(metrics["has_drawdown"][3]):
        raise AssertionError("MDD=0 경로가 낙폭 발생으로 분류되었습니다")
    if not np.isnan(metrics["recovery_days"][3]):
        raise AssertionError("MDD=0 경로의 회복 기간이 계산되었습니다")


def check_input_validation() -> None:
    dates = pd.Series(pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]))
    cases = [
        (dates, np.array([0.01, np.nan, 0.0])),
        (dates, np.array([0.01, np.inf, 0.0])),
        (pd.Series(pd.to_datetime(["2024-01-03", "2024-01-02", "2024-01-04"])), np.zeros(3)),
        (pd.Series(pd.to_datetime(["2024-01-02", "2024-01-02", "2024-01-04"])), np.zeros(3)),
        (dates, np.array([0.01, -1.0, 0.0])),
    ]
    for index, (case_dates, case_returns) in enumerate(cases):
        try:
            mc.validate_returns(case_dates, case_returns, f"case{index}")
        except ValueError:
            continue
        raise AssertionError(f"입력 검증 실패 케이스 {index}가 통과했습니다")
    mc.validate_returns(dates, np.array([0.01, -0.02, 0.0]), "valid")


def check_exclude_2026() -> None:
    dates = pd.Series(pd.to_datetime(["2025-12-29", "2025-12-30", "2026-01-02", "2026-03-02"]))
    returns = np.array([0.01, 0.02, 0.03, 0.04])
    filtered_dates, filtered_returns = mc.exclude_2026(dates, returns)
    if filtered_dates.max() > mc.EXCLUDE_2026_END:
        raise AssertionError("2026 데이터가 제외되지 않았습니다")
    if len(filtered_returns) != 2 or not np.allclose(filtered_returns, [0.01, 0.02]):
        raise AssertionError("2026 제외 수익률이 잘못되었습니다")


def check_no_double_cost() -> None:
    returns = np.full(100, -0.0005)
    indices = mc.circular_block_indices(100, 20, 252, 10, mc.block_seed_key(0))
    equity = mc.equity_paths(returns, indices, 1_000_000.0)
    expected = 1_000_000.0 * (1 - 0.0005) ** 252
    if not np.allclose(equity[:, -1], expected, rtol=0.0, atol=1e-6):
        raise AssertionError("순수익률 외 비용이 추가로 차감되었습니다")


def check_conditional_unrecovered() -> None:
    mixed = np.array(
        [
            [1000.0, 900.0, 800.0, 950.0],
            [1000.0, 1000.0, 1000.0, 1000.0],
            [1000.0, 1200.0, 900.0, 1300.0],
        ]
    )
    metrics = mc.path_metrics(mixed)
    rate = mc.conditional_unrecovered_rate(metrics)
    if abs(rate - 0.5) > 1e-12:
        raise AssertionError(f"조건부 미회복 비율 오류: {rate}")
    row = mc.summarize(metrics, n_paths=3, horizon=3)
    if abs(row["mdd_unrecovered_rate"] - rate) > 1e-12:
        raise AssertionError("summary와 조건부 미회복 비율이 다릅니다")
    if mc._conditional_delta(metrics, metrics) != 0.0:
        raise AssertionError("동일 지표의 paired delta가 0이 아닙니다")
    flat_only = mc.path_metrics(np.array([[1000.0, 1000.0, 1000.0, 1000.0]]))
    if mc.conditional_unrecovered_rate(flat_only) is not None:
        raise AssertionError("무낙폭 경로만 있을 때 조건부 비율이 None이 아닙니다")


def check_summary_and_stability() -> None:
    rng = np.random.default_rng(2)
    returns = rng.normal(0.0003, 0.01, size=800)
    indices = mc.circular_block_indices(len(returns), 20, 252, 2000, mc.block_seed_key(11))
    metrics = mc.bootstrap_metrics(returns, indices=indices)
    row = mc.summarize(metrics, n_paths=2000, horizon=252)
    p = row["terminal_loss_rate"]
    expected_se = float(np.sqrt(p * (1 - p) / 2000))
    if abs(row["terminal_loss_se"] - expected_se) > 1e-12:
        raise AssertionError("표본오차 계산이 다릅니다")
    for key in (
        "cagr_p05",
        "cagr_p50",
        "cagr_p95",
        "mdd_p05",
        "mdd_p01",
        "mdd_unrecovered_rate",
        "mdd_unrecovered_se",
        "terminal_underwater_rate",
        "terminal_underwater_se",
        "mdd_le_50_se",
    ):
        if key not in row:
            raise AssertionError(f"{key} 누락")
    expected_unrecovered_se = mc.proportion_se(row["mdd_unrecovered_rate"], row["drawdown_paths"])
    if abs(row["mdd_unrecovered_se"] - expected_unrecovered_se) > 1e-12:
        raise AssertionError("미회복 비율 SE 계산이 다릅니다")
    if row["mdd_unrecovered_denominator"] != "drawdown_paths":
        raise AssertionError("미회복 비율 분모 표기가 다릅니다")
    if not (0.0 <= row["no_drawdown_rate"] <= 1.0):
        raise AssertionError("무낙폭 비율 범위 오류")

    # MDD=0 경로는 회복/미회복 통계에서 분리된다.
    mixed = np.array(
        [
            [1000.0, 900.0, 800.0, 950.0],
            [1000.0, 1000.0, 1000.0, 1000.0],
            [1000.0, 1200.0, 900.0, 1300.0],
        ]
    )
    mixed_metrics = mc.path_metrics(mixed)
    mixed_row = mc.summarize(mixed_metrics, n_paths=3, horizon=3)
    if mixed_row["drawdown_paths"] != 2:
        raise AssertionError(f"낙폭 경로 수 오류: {mixed_row['drawdown_paths']}")
    if abs(mixed_row["no_drawdown_rate"] - 1 / 3) > 1e-12:
        raise AssertionError("무낙폭 비율 오류")
    if abs(mixed_row["mdd_unrecovered_rate"] - 0.5) > 1e-12:
        raise AssertionError(f"분모 적용 오류: {mixed_row['mdd_unrecovered_rate']}")
    if abs(mixed_row["recovered_drawdown_rate"] - 0.5) > 1e-12:
        raise AssertionError("낙폭 경로 회복 비율 오류")
    if not (row["cagr_p05"] <= row["cagr_p50"] <= row["cagr_p95"]):
        raise AssertionError("CAGR 분위수 순서 오류")

    wider = mc.bootstrap_metrics(
        returns,
        indices=mc.circular_block_indices(len(returns), 20, 252, 10000, mc.block_seed_key(11)),
    )
    second = mc.summarize(wider, n_paths=10000, horizon=252)
    diffs = mc.quantile_stability(row, second)
    if diffs["max_abs_quantile_diff"] > 0.02:
        raise AssertionError(f"분위수 안정성 차이가 큽니다: {diffs['max_abs_quantile_diff']}")


def check_phase_summary_positions() -> None:
    frame = pd.DataFrame(
        {
            "phase": list(range(20)),
            "policy": "baseline",
            "cost_case": "base",
            "subset": "full",
            "method": "block",
            "block_length": 20,
            "horizon": 1260,
            "stage": "extended",
            "n_paths": 10000,
            "cagr_p50": np.linspace(0.10, 0.20, 20),
            "cagr_p05": np.linspace(0.05, 0.15, 20),
            "mdd_p05": np.linspace(-0.40, -0.30, 20),
            "terminal_loss_rate": np.linspace(0.02, 0.12, 20),
            "mdd_unrecovered_rate": np.linspace(0.0, 0.1, 20),
            "terminal_underwater_rate": np.linspace(0.5, 0.9, 20),
        }
    )
    summary = mc.phase_summary(frame, subset="full", block=20, stage="extended")
    phase0 = summary[summary["metric"] == "cagr_p50"].iloc[0]
    if int(phase0["phase0_rank"]) != 20 or abs(float(phase0["phase0_percentile"]) - 0.0) > 1e-12:
        raise AssertionError(f"phase 0 위치 계산 오류: {phase0.to_dict()}")
    loss = summary[summary["metric"] == "terminal_loss_rate"].iloc[0]
    if int(loss["phase0_rank"]) != 1:
        raise AssertionError("원금 손실 순위 계산 오류")


def main() -> int:
    checks = [
        ("compounding identity", check_compounding_identity),
        ("circular blocks", check_circular_blocks),
        ("paired indices", check_paired_indices),
        ("paired inputs", check_paired_inputs),
        ("iid bootstrap", check_iid_bootstrap),
        ("path metrics", check_path_metrics),
        ("input validation", check_input_validation),
        ("exclude 2026", check_exclude_2026),
        ("no double cost", check_no_double_cost),
        ("conditional unrecovered", check_conditional_unrecovered),
        ("summary and stability", check_summary_and_stability),
        ("phase summary positions", check_phase_summary_positions),
    ]
    failures = 0
    for label, check in checks:
        try:
            check()
            print(f"PASS {label}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {label}: {exc}")
    if failures:
        print(f"\n{failures} checks failed")
        return 1
    print("\np1b3 bootstrap checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
