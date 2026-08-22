#!/usr/bin/env python3
"""Point-in-time universe 순수 로직 회귀 검사."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pit_universe import (
    add_pit_membership_flag,
    build_membership_events,
    build_snapshot_dates,
    latest_snapshot_as_of,
    membership_as_of,
    membership_sha256,
    normalize_krx_etf_history,
    normalize_krx_etf_snapshot,
    validate_pit_preflight,
    validate_snapshot_panel,
)

_import_env = {key: os.environ.get(key) for key in ("KRX_ID", "KRX_PW")}
_import_modules = {key: sys.modules.get(key) for key in ("pykrx", "pykrx.stock")}
try:
    os.environ["KRX_ID"] = ""
    os.environ["KRX_PW"] = ""
    _stub_pykrx = types.ModuleType("pykrx")
    _stub_stock = types.ModuleType("pykrx.stock")
    setattr(_stub_pykrx, "stock", _stub_stock)  # noqa: B010 - import-only test stub
    sys.modules["pykrx"] = _stub_pykrx
    sys.modules["pykrx.stock"] = _stub_stock
    from run_etf_backtest import ensure_no_current_prefix_columns
finally:
    for _key, _value in _import_env.items():
        if _value is None:
            os.environ.pop(_key, None)
        else:
            os.environ[_key] = _value
    for _key, _value in _import_modules.items():
        if _value is None:
            sys.modules.pop(_key, None)
        else:
            sys.modules[_key] = _value


def _sparse_membership_panel() -> pd.DataFrame:
    rows = []
    for date, tickers in [
        ("2020-01-02", ["A", "B"]),
        ("2020-01-06", ["A"]),
        ("2020-01-10", ["A", "B"]),
    ]:
        rows.extend({"snapshot_date": date, "ticker": ticker} for ticker in tickers)
    return pd.DataFrame(rows)


def _expect_preflight_failure(
    panel: pd.DataFrame,
    price: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    groups: dict[str, str],
    message: str,
    *,
    historical_verified: bool = True,
    decision_dates: pd.DatetimeIndex | None = None,
) -> None:
    dates = calendar if decision_dates is None else decision_dates
    try:
        validate_pit_preflight(
            panel,
            price,
            calendar,
            groups,
            decision_dates=dates,
            historical_classification_tax_verified=historical_verified,
        )
    except ValueError as exc:
        if message not in str(exc):
            raise AssertionError(f"preflight error lacked {message!r}: {exc}") from exc
    else:
        raise AssertionError(f"preflight unexpectedly passed without {message!r}")


def check_latest_as_of_membership() -> None:
    panel = _sparse_membership_panel()
    assert membership_as_of(panel, "2020-01-01") == set()
    assert membership_as_of(panel, "2020-01-02") == {"A", "B"}
    assert membership_as_of(panel, "2020-01-08") == {"A"}
    assert membership_as_of(panel, "2020-01-11") == {"A", "B"}
    assert set(latest_snapshot_as_of(panel, "2020-01-08")["ticker"]) == {"A"}

    price = pd.DataFrame(
        {
            "date": pd.to_datetime(["2020-01-01", "2020-01-08", "2020-01-08", "2020-01-10"]),
            "ticker": ["B", "A", "B", "B"],
            "close": [1.0, 2.0, 999.0, 3.0],
        }
    )
    flagged = add_pit_membership_flag(price, panel)
    flags = dict(zip(zip(flagged["date"], flagged["ticker"], strict=False), flagged["pit_membership_ok"], strict=False))
    assert flags[(pd.Timestamp("2020-01-01"), "B")] is False
    assert flags[(pd.Timestamp("2020-01-08"), "B")] is False
    assert flags[(pd.Timestamp("2020-01-10"), "B")] is True


def check_snapshot_age_preflight() -> None:
    calendar = pd.DatetimeIndex(pd.bdate_range("2020-01-02", periods=27))
    panel = pd.DataFrame({"snapshot_date": [calendar[0]], "ticker": ["A"]})
    price = pd.DataFrame({"date": calendar, "ticker": ["A"] * len(calendar), "close": 100.0})
    groups = {"A": "domestic_equity"}
    same_day = validate_pit_preflight(
        panel,
        price,
        calendar,
        groups,
        decision_dates=[calendar[0]],
        historical_classification_tax_verified=True,
    )
    assert same_day["status"] == "passed"
    passed = validate_pit_preflight(
        panel,
        price,
        calendar,
        groups,
        decision_dates=[calendar[25]],
        historical_classification_tax_verified=True,
    )
    assert passed["status"] == "passed"
    _expect_preflight_failure(
        panel,
        price,
        calendar,
        groups,
        "snapshot age exceeds limit",
        decision_dates=pd.DatetimeIndex([calendar[26]]),
    )

    _expect_preflight_failure(
        panel,
        price,
        calendar,
        groups,
        "decision date missing from trading calendar",
        decision_dates=pd.DatetimeIndex([pd.Timestamp("2020-02-14")]),
    )

    between_calendar = pd.DatetimeIndex(pd.to_datetime(["2020-01-02", "2020-01-06"]))
    between_panel = pd.DataFrame(
        {"snapshot_date": [pd.Timestamp("2020-01-03")], "ticker": ["A"]}
    )
    between_price = pd.DataFrame(
        {"date": [between_calendar[1]], "ticker": ["A"], "close": [100.0]}
    )
    _expect_preflight_failure(
        between_panel,
        between_price,
        between_calendar,
        groups,
        "snapshot date missing from trading calendar",
        decision_dates=between_calendar[1:],
    )

    before_panel = pd.DataFrame(
        {"snapshot_date": [pd.Timestamp("2020-01-01")], "ticker": ["A"]}
    )
    before_price = pd.DataFrame(
        {"date": [between_calendar[0]], "ticker": ["A"], "close": [100.0]}
    )
    _expect_preflight_failure(
        before_panel,
        before_price,
        between_calendar,
        groups,
        "snapshot date missing from trading calendar",
        decision_dates=between_calendar[:1],
    )


def check_coverage_and_historical_preflight() -> None:
    calendar = pd.DatetimeIndex(pd.to_datetime(["2020-01-02"]))
    panel = pd.DataFrame({"snapshot_date": [calendar[0], calendar[0]], "ticker": ["A", "B"]})
    price_a = pd.DataFrame({"date": [calendar[0]], "ticker": ["A"], "close": [100.0]})
    _expect_preflight_failure(
        panel,
        price_a,
        calendar,
        {"A": "domestic_equity", "B": "domestic_equity"},
        "missing usable price coverage",
    )

    _expect_preflight_failure(
        pd.DataFrame(columns=["snapshot_date", "ticker"]),
        price_a,
        calendar,
        {"A": "domestic_equity"},
        "missing snapshot",
    )

    price_ab = pd.DataFrame(
        {"date": [calendar[0], calendar[0]], "ticker": ["A", "B"], "close": [100.0, 100.0]}
    )
    _expect_preflight_failure(
        panel,
        price_ab,
        calendar,
        {"A": "domestic_equity", "B": "unknown"},
        "unknown group coverage",
    )
    _expect_preflight_failure(
        panel,
        price_ab,
        calendar,
        {"A": "domestic_equity"},
        "missing group coverage",
    )
    _expect_preflight_failure(
        panel,
        price_ab,
        calendar,
        {"A": "domestic_equity", "B": "domestic_equity"},
        "historical classification/tax coverage unverified",
        historical_verified=False,
    )


def main() -> None:
    check_latest_as_of_membership()
    check_snapshot_age_preflight()
    check_coverage_and_historical_preflight()

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
