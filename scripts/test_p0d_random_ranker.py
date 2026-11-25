"""P0-D: 무작위 ranker 합성 fixture 검증.

- 동일 seed·결정일 → 동일 순서, 다른 seed → 다른 순서
- 입력 행 순서에 독립
- 적격 집합(필터 통과 종목) 불변
- 필터(trend/liquidity/listing/deviation) 유지
- 기존 ranker 결과 불변(직접 호출)

실행::

    uv run python scripts/test_p0d_random_ranker.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import p0d_factor_random_baseline as p0d

import etf_shared


def build_snapshot(date: str = "2024-01-02", rows: int = 6) -> pd.DataFrame:
    tickers = [f"90000{i}" for i in range(rows)]
    return pd.DataFrame(
        {
            "date": pd.Timestamp(date),
            "ticker": tickers,
            "ret_60": [0.10 - 0.01 * i for i in range(rows)],
            "ret_120": [0.20 - 0.02 * i for i in range(rows)],
            "trend_ok": [True] * rows,
            "liquidity_ok": [True] * rows,
            "listing_ok": [True] * rows,
            "deviation_ok": [True] * rows,
        }
    )


def order_of(frame: pd.DataFrame) -> list[str]:
    return frame["ticker"].astype(str).tolist()


def check_determinism() -> None:
    snapshot = build_snapshot()
    ranker = p0d.make_random_ranker(7, etf_shared.rank_etfs)
    first = order_of(ranker(snapshot))
    second = order_of(ranker(snapshot))
    if first != second:
        raise AssertionError("같은 seed·날짜에서 순서가 달라졌습니다")


def check_seed_variation() -> None:
    snapshot = build_snapshot()
    orders = {
        seed: order_of(p0d.make_random_ranker(seed, etf_shared.rank_etfs)(snapshot))
        for seed in range(5)
    }
    if len({tuple(order) for order in orders.values()}) < 2:
        raise AssertionError("서로 다른 seed가 같은 순서만 생성했습니다")


def check_row_order_independence() -> None:
    snapshot = build_snapshot()
    ranker = p0d.make_random_ranker(11, etf_shared.rank_etfs)
    expected = order_of(ranker(snapshot))
    shuffled = snapshot.sample(frac=1.0, random_state=3).reset_index(drop=True)
    if order_of(ranker(shuffled)) != expected:
        raise AssertionError("입력 행 순서에 따라 무작위 순서가 달라졌습니다")


def check_eligible_set_preserved() -> None:
    snapshot = build_snapshot()
    original = etf_shared.rank_etfs(snapshot)
    for seed in range(5):
        random_order = p0d.make_random_ranker(seed, etf_shared.rank_etfs)(snapshot)
        if set(order_of(random_order)) != set(order_of(original)):
            raise AssertionError(f"seed {seed}: 적격 집합이 달라졌습니다")
        if len(random_order) != len(original):
            raise AssertionError(f"seed {seed}: 행 수가 달라졌습니다")


def check_filters_respected() -> None:
    snapshot = build_snapshot()
    snapshot.loc[snapshot["ticker"] == "900000", "trend_ok"] = False
    snapshot.loc[snapshot["ticker"] == "900001", "liquidity_ok"] = False
    snapshot.loc[snapshot["ticker"] == "900002", "listing_ok"] = False
    snapshot.loc[snapshot["ticker"] == "900003", "deviation_ok"] = False
    for seed in range(5):
        order = order_of(p0d.make_random_ranker(seed, etf_shared.rank_etfs)(snapshot))
        if set(order) != {"900004", "900005"}:
            raise AssertionError(f"seed {seed}: 필터가 무시되었습니다 {order}")


def check_empty_snapshot() -> None:
    snapshot = build_snapshot()
    snapshot["trend_ok"] = False
    result = p0d.make_random_ranker(1, etf_shared.rank_etfs)(snapshot)
    if not result.empty:
        raise AssertionError("적격 종목이 없는데 결과가 비어있지 않습니다")


def check_date_variation() -> None:
    same_seed = p0d.make_random_ranker(3, etf_shared.rank_etfs)
    orders = {
        tuple(order_of(same_seed(build_snapshot(date))))
        for date in ("2024-01-02", "2024-01-03", "2024-01-04")
    }
    if len(orders) < 2:
        raise AssertionError("결정일이 달라도 순서가 동일합니다(seed·날짜 키 미적용 의심)")


def _reuse_fixture():
    frame = pd.DataFrame(
        {
            "seed": [0, 0, 1, 1],
            "cost_case": ["base", "stress_50bp_3x", "base", "stress_50bp_3x"],
            "cagr": [0.1, 0.05, 0.2, 0.1],
            "mdd": [-0.2, -0.3, -0.1, -0.2],
            "sharpe": [1.0, 0.5, 1.5, 0.8],
            "calmar": [0.5, 0.2, 0.8, 0.4],
            "final": [2e6, 1.5e6, 3e6, 2e6],
            "total_cost": [1e5, 3e5, 1.1e5, 3.1e5],
            "turnover": [50.0, 49.0, 51.0, 50.0],
            "avg_cash_weight": [0.3, 0.31, 0.29, 0.3],
            "ex2026_cagr": [0.08, 0.04, 0.15, 0.08],
            "ex2026_mdd": [-0.1, -0.15, -0.05, -0.1],
            "is_cagr": [0.09, 0.05, 0.17, 0.09],
            "oos_cagr": [0.01, -0.01, 0.03, 0.0],
            "trade_count": [100, 100, 110, 110],
        }
    )
    current = {
        "code_digest": "code",
        "data_cache_digest": "data",
        "freeze_sha256": "freeze",
        "period": {"start": "20160105", "end": "20260928"},
        "cost_cases": {"base": [0.0005, 1.0], "stress_50bp_3x": [0.005, 3.0]},
        "momentum_weight_60": 0.55,
        "max_asset_pct": 0.85,
        "spread_pct": 0.0002,
        "oos_start_date": "2026-07-22",
        "seeds": {"count": 2, "range": [0, 1]},
    }
    stored = {
        **current,
        "run_mode": "full",
        "created_at": "2026-11-25T00:00:00+00:00",
        "random_runs_sha256": "hash",
        "failures": 0,
        "experiment_complete": True,
    }
    expected = {(0, "base"), (0, "stress_50bp_3x"), (1, "base"), (1, "stress_50bp_3x")}
    return frame, stored, current, expected


def check_reuse_validation() -> None:
    frame, stored, current, expected = _reuse_fixture()
    reasons = p0d.validate_reuse(frame, stored, current, expected, "hash")
    if reasons:
        raise AssertionError(f"정상 입력이 거부되었습니다: {reasons}")

    cases = {
        "duplicate": (
            pd.concat([frame, frame.iloc[[0]]], ignore_index=True),
            stored,
            current,
            "hash",
        ),
        "missing": (frame.iloc[:-1].copy(), stored, current, "hash"),
        "fractional_seed": (frame.assign(seed=[0.9, 0.0, 1.0, 1.0]), stored, current, "hash"),
        "missing_metric": (frame.drop(columns=["ex2026_mdd"]), stored, current, "hash"),
        "failures_str": (frame, {**stored, "failures": "0"}, current, "hash"),
        "failures_negative": (frame, {**stored, "failures": -1}, current, "hash"),
        "complete_str": (frame, {**stored, "experiment_complete": "true"}, current, "hash"),
        "nan_metric": (frame.assign(cagr=[0.1, None, 0.2, 0.1]), stored, current, "hash"),
        "inf_metric": (frame.assign(final=[2e6, float("inf"), 3e6, 2e6]), stored, current, "hash"),
        "digest_mismatch": (frame, {**stored, "data_cache_digest": "other"}, current, "hash"),
        "code_mismatch": (frame, {**stored, "code_digest": "other"}, current, "hash"),
        "sha_mismatch": (frame, stored, current, "other"),
        "incomplete": (frame, {**stored, "failures": 1}, current, "hash"),
        "no_provenance": (frame, None, current, "hash"),
    }
    for label, (f, st, cu, sha) in cases.items():
        if not p0d.validate_reuse(f, st, cu, expected, sha):
            raise AssertionError(f"{label}: 거부되지 않았습니다")


def check_source_provenance_chain() -> None:
    full_a = {"run_mode": "full", "random_runs_sha256": "A", "created_at": "a"}
    reused_a = {"run_mode": "reused", "random_runs_sha256": "A", "source": {"reuse_count": 1}}
    full_b = {"run_mode": "full", "random_runs_sha256": "B", "created_at": "b"}

    # 같은 산출물의 최초 full provenance는 유지되고 reuse_count만 증가
    original, count, write = p0d.resolve_source_provenance(full_a, reused_a, "A")
    if (original, count, write) != (full_a, 2, False):
        raise AssertionError(f"동일 산출물 계보 유지 실패: {original}, {count}, {write}")
    original, count, write = p0d.resolve_source_provenance(None, full_a, "A")
    if (original, count, write) != (full_a, 1, True):
        raise AssertionError(f"최초 full 보존 실패: {original}, {count}, {write}")
    # 새 full 실행(B) 뒤 재사용이면 이전 실험(A) 잔여 파일을 계보로 쓰지 않는다
    original, count, write = p0d.resolve_source_provenance(full_a, full_b, "B")
    if (original, count, write) != (full_b, 1, True):
        raise AssertionError(f"잔여 provenance 교체 실패: {original}, {count}, {write}")


def check_clopper_pearson() -> None:
    upper = p0d.upper_one_sided_cp(0, 100)
    if abs(upper - 0.0295) > 5e-4:
        raise AssertionError(f"0/100 단측 상한이 2.95%가 아닙니다: {upper}")
    if p0d.upper_one_sided_cp(100, 100) != 1.0:
        raise AssertionError("100/100 상한이 1이 아닙니다")
    mid = p0d.upper_one_sided_cp(48, 100)
    if not 0.38 <= mid <= 0.60:
        raise AssertionError(f"48/100 상한이 비정상입니다: {mid}")


def main() -> int:
    checks = [
        ("determinism", check_determinism),
        ("seed variation", check_seed_variation),
        ("row order independence", check_row_order_independence),
        ("eligible set preserved", check_eligible_set_preserved),
        ("filters respected", check_filters_respected),
        ("empty snapshot", check_empty_snapshot),
        ("date variation", check_date_variation),
        ("reuse validation", check_reuse_validation),
        ("source provenance chain", check_source_provenance_chain),
        ("clopper-pearson bound", check_clopper_pearson),
    ]
    failures = 0
    for label, check in checks:
        try:
            check()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {label}: {exc}")
        else:
            print(f"PASS {label}")
    if failures:
        print(f"\nrandom ranker checks FAILED ({failures}건)")
        return 1
    print("\nrandom ranker checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
