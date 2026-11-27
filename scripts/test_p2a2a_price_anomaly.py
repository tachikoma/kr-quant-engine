"""P2-A-2a 가격 이상 판정 로직 fixture(네트워크 없음)."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import p2a2a_price_anomaly as anomaly


def _rows(count: int = 3, *, diff: float = 0.0) -> list[dict]:
    return [
        {
            "ticker": f"00000{index}",
            "cache_return_pct": 10.0,
            "kis_flag1_raw_return_pct": 10.0 + diff,
        }
        for index in range(count)
    ]


def check_confirmation_requires_all() -> None:
    ok = anomaly.evaluate_confirmation(_rows())
    assert ok["kis_confirmed"] is True and ok["kis_checked"] == 3

    missing = _rows()
    missing[1]["kis_flag1_raw_return_pct"] = None
    result = anomaly.evaluate_confirmation(missing)
    assert result["kis_confirmed"] is False
    assert result["kis_missing_tickers"] == ["000001"]

    nan_row = _rows()
    nan_row[2]["kis_flag1_raw_return_pct"] = float("nan")
    result = anomaly.evaluate_confirmation(nan_row)
    assert result["kis_confirmed"] is False
    assert result["kis_missing_tickers"] == ["000002"]

    mismatch = _rows(diff=0.2)
    result = anomaly.evaluate_confirmation(mismatch)
    assert result["kis_confirmed"] is False
    assert len(result["kis_mismatch_tickers"]) == 3

    empty = anomaly.evaluate_confirmation([])
    assert empty["kis_confirmed"] is False

    # 기대 종목 중 행 자체가 없으면 미확인이다.
    result = anomaly.evaluate_confirmation(_rows(2), ["000000", "000001", "000099"])
    assert result["kis_confirmed"] is False
    assert "000099" in result["kis_missing_tickers"]


def check_classify_order() -> None:
    base = {
        "kis_confirmed": True,
        "evidence_complete": True,
        "breadth": {"share_gt_10pct": 0.3, "median_return_pct": 2.0},
        "index": {"KOSPI": {"return_pct": 10.0}},
    }
    assert anomaly.classify({**base, "source_mismatch": True}) == "source_mismatch"
    assert (
        anomaly.classify({**base, "kis_confirmed": False}) == "unconfirmed"
    )
    assert anomaly.classify({**base, "evidence_complete": False}) == "unconfirmed"
    assert (
        anomaly.classify(
            {
                **base,
                "breadth": {"share_gt_10pct": None, "median_return_pct": None},
                "index": {},
            }
        )
        == "unconfirmed"
    )
    assert (
        anomaly.classify({**base, "breadth": {"share_gt_10pct": 0.0}, "index": {}})
        == "unconfirmed"
    )
    assert (
        anomaly.classify(base)
        == "market_move_confirmed_by_independent_feed"
    )


def _frame(**overrides) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-07-30", "2026-07-31"]),
            "open": [1.0, 2.0],
            "high": [1.0, 2.0],
            "low": [1.0, 2.0],
            "close": [1.0, 2.0],
            "volume": [10, 20],
            "trading_value": [100, 200],
            "nav": [1.0, 2.0],
        }
    )
    for key, value in overrides.items():
        frame[key] = value
    return frame


def check_strict_krx_comparison() -> None:
    identical = anomaly.strict_krx_comparison(_frame(), _frame())
    assert identical["identical"] is True, identical

    missing_date = anomaly.strict_krx_comparison(_frame(), _frame().iloc[:1])
    assert missing_date["identical"] is False
    assert missing_date["date_set_match"] is False

    field_diff = _frame()
    field_diff.loc[1, "volume"] = 21
    result = anomaly.strict_krx_comparison(_frame(), field_diff)
    assert result["identical"] is False
    assert result["max_abs_diff"]["volume"] == 1

    nan_value = _frame()
    nan_value.loc[0, "close"] = float("nan")
    result = anomaly.strict_krx_comparison(_frame(), nan_value)
    assert result["identical"] is False
    assert result["validation_errors"]

    duplicate = pd.concat([_frame(), _frame().iloc[[0]]], ignore_index=True)
    result = anomaly.strict_krx_comparison(_frame(), duplicate)
    assert result["identical"] is False

    # 비교 범위 밖 추가 날짜도 전체 날짜 집합 불일치로 처리한다.
    extra_row = _frame().iloc[[0]].copy()
    extra_row["date"] = pd.Timestamp("2026-08-01")
    extra_row[["close", "nav"]] = 3.0
    extra = pd.concat([_frame(), extra_row], ignore_index=True)
    result = anomaly.strict_krx_comparison(_frame(), extra)
    assert result["identical"] is False
    assert result["full_date_set_match"] is False

    # NAV 열이 없으면 통과하지 않는다.
    result = anomaly.strict_krx_comparison(_frame().drop(columns=["nav"]), _frame())
    assert result["identical"] is False
    assert result["nav_ok"] is False


def check_missing_snapshot_row() -> None:
    row = anomaly.missing_snapshot_row("000000")
    for key in [
        "ticker",
        "cache_return_pct",
        "kis_flag1_raw_return_pct",
        "cache_vs_krx_day",
        "list_shares_changed",
        "list_shares_ratio",
        "split_like",
    ]:
        assert key in row, key
    # 집계 코드가 참조하는 키가 모두 있어야 한다.
    assert row["list_shares_changed"] is None and row["split_like"] is None
    result = anomaly.evaluate_confirmation([row], ["000000"])
    assert result["kis_confirmed"] is False
    assert result["kis_missing_tickers"] == ["000000"]


def check_finite_helper() -> None:
    assert anomaly._finite("1.5") == 1.5
    assert anomaly._finite(None) is None
    assert anomaly._finite(float("nan")) is None
    assert anomaly._finite("bad") is None


def main() -> None:
    checks = [
        ("confirmation requires all", check_confirmation_requires_all),
        ("classify order", check_classify_order),
        ("strict krx comparison", check_strict_krx_comparison),
        ("missing snapshot row", check_missing_snapshot_row),
        ("finite helper", check_finite_helper),
    ]
    for name, check in checks:
        try:
            check()
        except AssertionError as exc:
            print(f"FAIL {name}: {exc}")
            raise
        print(f"PASS {name}")
    print("\np2a2a anomaly checks passed")


if __name__ == "__main__":
    main()
