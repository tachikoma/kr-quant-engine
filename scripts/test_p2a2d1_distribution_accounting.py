"""P2-A-2d-1 회계 검증 fixture(네트워크 없음)."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import p2a2d1_distribution_accounting as acc

from etf_corporate_actions import DistributionReceivable, EventType, HoldingState, LifecycleState
from run_etf_backtest import run_etf_strategy


class _Event:
    def __init__(self, ticker: str, ex: str, pay: str, amount: float):
        self.ticker = ticker
        self.ex_date = date.fromisoformat(ex)
        self.payment_date = date.fromisoformat(pay)
        self.cash_amount = amount
        self.event_type = EventType.CASH_DISTRIBUTION


def check_holdings_timeline() -> None:
    dates = [pd.Timestamp("2026-08-24"), pd.Timestamp("2026-08-25"), pd.Timestamp("2026-08-26")]
    trades = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-08-25", "2026-08-26"]),
            "ticker": ["091170", "091170"],
            "side": ["BUY", "SELL"],
            "qty": [100, 50],
        }
    )
    timeline = acc.holdings_timeline(dates, {"091170": 1000}, trades)
    assert timeline[dates[0]]["091170"] == 1000
    assert timeline[dates[1]]["091170"] == 1100
    assert timeline[dates[2]]["091170"] == 1050


def check_independent_receivables() -> None:
    dates = [pd.Timestamp("2026-08-28"), pd.Timestamp("2026-08-31"), pd.Timestamp("2026-09-02")]
    event = _Event("091170", "2026-08-28", "2026-09-02", 59.0)

    # 배당락일 신규 매수는 제외: 초기 보유 0, 당일 매수만 있음.
    bought = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-08-28"]),
            "ticker": ["091170"],
            "side": ["BUY"],
            "qty": [100],
        }
    )
    timeline = acc.holdings_timeline(dates, {}, bought)
    receivables = acc.independent_receivables([event], timeline, {})
    assert receivables[dates[0]] == 0.0

    # 배당락일 매도는 포함: 직전 보유 1000 기준.
    sold = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-08-31"]),
            "ticker": ["091170"],
            "side": ["SELL"],
            "qty": [1000],
        }
    )
    timeline = acc.holdings_timeline(dates, {"091170": 1000}, sold)
    receivables = acc.independent_receivables([event], timeline, {"091170": 1000})
    assert receivables[dates[0]] == 59_000.0
    assert receivables[dates[1]] == 59_000.0  # 매도 후에도 지급 전까지 유지
    assert receivables[dates[2]] == 0.0  # 지급일 제거


def check_state_signature() -> None:
    receivable = DistributionReceivable(
        "E1", "091170", 1000, 59000, date(2026, 9, 2), False
    )
    holding = HoldingState(
        "091170", 1000, __import__("decimal").Decimal("16000000"),
        LifecycleState.SUSPENDED, None,
    )
    state = {
        "cash": 0.0,
        "holdings": {"091170": 1000},
        "holding_cost_basis": {"091170": 16000.0},
        "pending_receivables": (receivable,),
        "processed_action_ids": ("E1",),
        "corporate_action_states": {"091170": holding},
        "final_equity": 100.0,
    }
    signature = acc.state_signature(state)
    assert signature["receivables"] == [
        ("E1", "091170", 1000, 59000.0, "2026-09-02", False)
    ]
    assert signature["processed_action_ids"] == ["E1"]
    assert signature["lifecycles"] == {"091170": "SUSPENDED"}


def check_engine_seam_guard() -> None:
    try:
        run_etf_strategy(0.0, [], pd.DataFrame(), allow_partial_ledger=True)
    except ValueError as exc:
        assert "allow_partial_ledger" in str(exc)
    else:
        raise AssertionError("allow_partial_ledger 단독 사용이 차단되지 않았습니다")


def check_empty_calendar_research_blocked() -> None:
    from etf_corporate_actions import ApprovalBlocker, CorporateActionLedger, CoverageManifest

    manifest = CoverageManifest(
        manifest_version=1,
        ledger_file="synthetic.csv",
        ledger_sha256="0" * 64,
        verification_start=None,
        verification_end=None,
        verification_tickers=(),
        source_documents=(),
        coverage_status="PARTIAL",
        notes="fixture",
    )
    ledger = CorporateActionLedger(
        events=(),
        manifest=manifest,
        ledger_sha256="0" * 64,
        blockers=(ApprovalBlocker("INCOMPLETE_COVERAGE", "fixture"),),
    )
    empty_price = pd.DataFrame(
        {
            "date": pd.to_datetime([]),
            "ticker": pd.Series([], dtype=str),
            "close": pd.Series([], dtype=float),
        }
    )
    result = run_etf_strategy(
        0.0,
        [],
        pd.DataFrame(),
        price_data=empty_price,
        universe_tickers=[],
        approval_strict=True,
        corporate_action_ledger=ledger,
        allow_partial_ledger=True,
        return_final_state=True,
    )
    final_state = result[-1]
    report = final_state.get("approval_report")
    assert report is not None and report.status == "BLOCKED"
    codes = {blocker.code for blocker in report.blockers}
    assert {"EMPTY_STRATEGY_CALENDAR", "RESEARCH_PARTIAL_LEDGER"} <= codes
    assert final_state.get("research_only") is True


def check_frozen_expectations() -> None:
    assert abs(acc.EXPECTED_FROZEN["cagr"] - 0.245411377867) < 1e-15
    assert abs(acc.EXPECTED_FROZEN["mdd"] + 0.5121244554) < 1e-15


def main() -> None:
    checks = [
        ("holdings timeline", check_holdings_timeline),
        ("independent receivables", check_independent_receivables),
        ("state signature", check_state_signature),
        ("engine seam guard", check_engine_seam_guard),
        ("empty calendar research blocked", check_empty_calendar_research_blocked),
        ("frozen expectations", check_frozen_expectations),
    ]
    for name, check in checks:
        try:
            check()
        except AssertionError as exc:
            print(f"FAIL {name}: {exc}")
            raise
        print(f"PASS {name}")
    print("\np2a2d1 accounting checks passed")


if __name__ == "__main__":
    main()
