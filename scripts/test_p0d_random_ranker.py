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


def main() -> int:
    checks = [
        ("determinism", check_determinism),
        ("seed variation", check_seed_variation),
        ("row order independence", check_row_order_independence),
        ("eligible set preserved", check_eligible_set_preserved),
        ("filters respected", check_filters_respected),
        ("empty snapshot", check_empty_snapshot),
        ("date variation", check_date_variation),
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
