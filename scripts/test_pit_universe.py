#!/usr/bin/env python3
"""Point-in-time universe 순수 로직 회귀 검사."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pit_universe import (
    build_membership_events,
    build_snapshot_dates,
    membership_sha256,
    normalize_krx_etf_history,
    normalize_krx_etf_snapshot,
    validate_snapshot_panel,
)
from run_etf_backtest import ensure_no_current_prefix_columns


def main() -> None:
    dates = pd.Series(pd.bdate_range("2020-01-01", periods=10))
    schedule = build_snapshot_dates(
        dates,
        start=dates.iloc[0],
        end=dates.iloc[-1],
        warmup_days=2,
        step_days=3,
    )
    assert len(schedule) == 4
    assert schedule["snapshot_date"].is_monotonic_increasing
    assert not schedule["snapshot_date"].duplicated().any()
    assert bool(schedule.iloc[-1]["is_rebalance_snapshot"]) is False

    raw_first = pd.DataFrame(
        [
            {"ISU_SRT_CD": "000001", "ISU_ABBRV": "A", "TDD_CLSPRC": "1,000"},
            {"ISU_SRT_CD": "000002", "ISU_ABBRV": "B", "TDD_CLSPRC": "-"},
        ]
    )
    raw_second = pd.DataFrame(
        [
            {"ISU_SRT_CD": "000001", "ISU_ABBRV": "A", "TDD_CLSPRC": "1,100"},
            {"ISU_SRT_CD": "000003", "ISU_ABBRV": "C", "TDD_CLSPRC": "900"},
        ]
    )
    first = normalize_krx_etf_snapshot(raw_first, "20200103")
    second = normalize_krx_etf_snapshot(raw_second, "20200106")
    panel = pd.concat([first, second], ignore_index=True)

    assert first.loc[first["ticker"] == "000001", "close"].iloc[0] == 1000
    assert pd.isna(first.loc[first["ticker"] == "000002", "close"].iloc[0])
    coverage = validate_snapshot_panel(panel)
    assert coverage["snapshot_count"] == 2
    assert coverage["unique_ticker_count"] == 3
    assert coverage["observed_then_absent_count"] == 1
    assert coverage["entered_after_first_count"] == 1

    events = build_membership_events(panel)
    later = events[~events["is_initial_snapshot"]]
    assert set(zip(later["ticker"], later["event"], strict=False)) == {
        ("000002", "EXIT"),
        ("000003", "ENTER"),
    }
    assert membership_sha256(panel) == membership_sha256(panel.sample(frac=1, random_state=7))

    raw_history = pd.DataFrame(
        [
            {
                "TRD_DD": "2020/01/03",
                "TDD_OPNPRC": "1,000",
                "TDD_CLSPRC": "1,100",
                "ACC_TRDVOL": "2,000",
            }
        ]
    )
    history = normalize_krx_etf_history(raw_history, "000001")
    assert history.iloc[0]["ticker"] == "000001"
    assert history.iloc[0]["close"] == 1100
    assert history.iloc[0]["volume"] == 2000

    ensure_no_current_prefix_columns(
        pd.DataFrame({"ticker": ["000001"], "close": [1100]}),
        context="test",
    )
    try:
        ensure_no_current_prefix_columns(
            pd.DataFrame({"ticker": ["000001"], "current_list_dd": ["20200101"]}),
            context="test",
        )
    except ValueError as exc:
        assert "current_" in str(exc)
    else:
        raise AssertionError("current_ 컬럼 방어가 작동하지 않았습니다.")

    print("point-in-time universe regression checks passed")


if __name__ == "__main__":
    main()
