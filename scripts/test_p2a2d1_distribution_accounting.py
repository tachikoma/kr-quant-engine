"""P2-A-2d-1 회계 검증 fixture(네트워크 없음)."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from types import SimpleNamespace

import p2a2d1_distribution_accounting as acc

from etf_corporate_actions import (
    ApprovalBlocker,
    CorporateAction,
    CorporateActionBlocked,
    DistributionReceivable,
    EventType,
    HoldingState,
    LifecycleState,
)
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


def _empty_price() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime([]),
            "ticker": pd.Series([], dtype=str),
            "close": pd.Series([], dtype=float),
        }
    )


def _fake_ledger(blockers=(), events=(), sha: str = "a" * 64) -> SimpleNamespace:
    return SimpleNamespace(
        events=tuple(events),
        ledger_sha256=sha,
        approval_report=lambda: SimpleNamespace(
            approval_valid=not blockers, blockers=tuple(blockers)
        ),
    )


def _event() -> CorporateAction:
    return CorporateAction(
        event_id="P2A2B-091170-2026-08-31",
        ticker="091170",
        event_type=EventType.CASH_DISTRIBUTION,
        event_date=date(2026, 8, 28),
        record_date=date(2026, 8, 31),
        ex_date=date(2026, 8, 28),
        payment_date=date(2026, 9, 2),
        cash_amount=59,
        currency="KRW",
        source_document_id="kodex_api_091170",
        source_url="https://example.test",
        source_sha256="b" * 64,
    )


def check_partial_seam_whitelist() -> None:
    # coverage 부족만 허용: 연구 실행은 통과하고 최종 보고서는 BLOCKED.
    coverage_only = _fake_ledger(
        blockers=(ApprovalBlocker("INCOMPLETE_COVERAGE", "partial"),)
    )
    result = run_etf_strategy(
        0.0,
        [],
        pd.DataFrame(),
        price_data=_empty_price(),
        universe_tickers=[],
        approval_strict=True,
        corporate_action_ledger=coverage_only,
        allow_partial_ledger=True,
        return_final_state=True,
    )
    report = result[-1]["approval_report"]
    assert report is not None and report.status == "BLOCKED"

    # 구조적/lifecycle 실패는 seam에서도 차단한다.
    structural = _fake_ledger(
        blockers=(ApprovalBlocker("MISSING_EX_DATE", "bad event", event_id="E1"),)
    )
    try:
        run_etf_strategy(
            0.0,
            [],
            pd.DataFrame(),
            price_data=_empty_price(),
            universe_tickers=[],
            approval_strict=True,
            corporate_action_ledger=structural,
            allow_partial_ledger=True,
        )
    except CorporateActionBlocked as exc:
        assert exc.blocker.code == "MISSING_EX_DATE"
    else:
        raise AssertionError("구조적 blocker가 연구 seam에서 차단되지 않았습니다")


def check_research_state_gate() -> None:
    state = {
        "cash": 0.0,
        "holdings": {},
        "holding_cost_basis": {},
        "holding_peak_closes": {},
        "portfolio_peak_equity": None,
        "last_valid_closes": {},
        "research_only": True,
        "corporate_action_ledger_sha256": "a" * 64,
    }
    ledger = _fake_ledger()
    # 연구 상태를 비연구 실행으로 복원할 수 없다.
    try:
        run_etf_strategy(
            0.0,
            [],
            pd.DataFrame(),
            price_data=_empty_price(),
            universe_tickers=[],
            approval_strict=True,
            corporate_action_ledger=ledger,
            initial_state=state,
        )
    except ValueError as exc:
        assert "research_only" in str(exc)
    else:
        raise AssertionError("연구 상태가 비연구 실행에 유입되었습니다")

    # 비승인 경로에 strict 상태를 전달할 수 없다.
    try:
        run_etf_strategy(
            0.0,
            [],
            pd.DataFrame(),
            price_data=_empty_price(),
            universe_tickers=[],
            approval_strict=False,
            initial_state={"cash": 0.0, "pending_receivables": ()},
        )
    except ValueError as exc:
        assert "비승인" in str(exc)
    else:
        raise AssertionError("strict 상태가 비승인 경로에 유입되었습니다")


def check_restore_validation() -> None:
    event = _event()
    ledger = _fake_ledger(events=[event], sha="a" * 64)
    base = {
        "cash": 0.0,
        "holdings": {"091170": 1000},
        "pending_receivables": (
            DistributionReceivable("P2A2B-091170-2026-08-31", "091170", 1000, 59000, date(2026, 9, 2), False),
        ),
        "processed_action_ids": ("P2A2B-091170-2026-08-31",),
        "corporate_action_states": {
            "091170": HoldingState("091170", 1000, __import__("decimal").Decimal("16000000"))
        },
        "strict_action_blockers": (),
        "research_only": True,
        "corporate_action_ledger_sha256": "a" * 64,
    }
    acc._validate_research_state(base, ledger, base["holdings"])

    for broken, needle in [
        ({**base, "corporate_action_ledger_sha256": "b" * 64}, "다른 ledger"),
        ({**base, "processed_action_ids": ("UNKNOWN",)}, "없는 처리"),
        (
            {
                **base,
                "pending_receivables": (
                    DistributionReceivable("UNKNOWN", "091170", 1000, 59000, date(2026, 9, 2), False),
                ),
            },
            "없는 미수금",
        ),
        (
            {
                **base,
                "pending_receivables": (
                    DistributionReceivable("P2A2B-091170-2026-08-31", "091170", 1000, 999999, date(2026, 9, 2), False),
                ),
            },
            "금액",
        ),
        (
            {
                **base,
                "pending_receivables": (
                    DistributionReceivable("P2A2B-091170-2026-08-31", "091170", 1000, 59000, date(2026, 9, 1), False),
                ),
            },
            "지급일",
        ),
        (
            {
                **base,
                "pending_receivables": (
                    base["pending_receivables"][0],
                    base["pending_receivables"][0],
                ),
            },
            "중복",
        ),
        ({**base, "processed_action_ids": ()}, "처리 완료"),
        ({**base, "holdings": {"091170": 900}}, "보유수량"),
        ({**base, "corporate_action_ledger_sha256": None}, "식별"),
    ]:
        try:
            acc._validate_research_state(broken, ledger, broken.get("holdings", {}))
        except ValueError as exc:
            assert needle in str(exc), (needle, exc)
        else:
            raise AssertionError(f"잘못된 상태가 통과했습니다: {needle}")


def check_required_state_fields() -> None:
    event = _event()
    ledger = _fake_ledger(events=[event], sha="a" * 64)
    base = {
        "cash": 0.0,
        "holdings": {"091170": 1000},
        "pending_receivables": (
            DistributionReceivable("P2A2B-091170-2026-08-31", "091170", 1000, 59000, date(2026, 9, 2), False),
        ),
        "processed_action_ids": ("P2A2B-091170-2026-08-31",),
        "corporate_action_states": {
            "091170": HoldingState("091170", 1000, __import__("decimal").Decimal("16000000"))
        },
        "strict_action_blockers": (),
        "research_only": True,
        "corporate_action_ledger_sha256": "a" * 64,
    }
    acc._validate_research_state(base, ledger, base["holdings"])

    for missing in (
        "pending_receivables",
        "processed_action_ids",
        "corporate_action_states",
        "strict_action_blockers",
        "research_only",
        "corporate_action_ledger_sha256",
    ):
        broken = dict(base)
        broken.pop(missing)
        try:
            acc._validate_research_state(broken, ledger, broken["holdings"])
        except ValueError as exc:
            assert "필수 필드 누락" in str(exc), (missing, exc)
        else:
            raise AssertionError(f"필수 필드 누락이 통과했습니다: {missing}")

    # 최초 실행 상태(cash/holdings만)는 허용한다.
    acc._validate_research_state(
        {"cash": 0.0, "holdings": {"091170": 1000}}, ledger, {"091170": 1000}
    )

    # 타입 위반
    for broken, needle in [
        ({**base, "research_only": "false"}, "boolean"),
        (
            {
                **base,
                "pending_receivables": (
                    DistributionReceivable("P2A2B-091170-2026-08-31", "091170", 1000, 59000, date(2026, 9, 2), "false"),
                ),
            },
            "paid",
        ),
        (
            {
                **base,
                "pending_receivables": (
                    DistributionReceivable("P2A2B-091170-2026-08-31", "091170", 1000.0, 59000, date(2026, 9, 2), False),
                ),
            },
            "정수",
        ),
        ({**base, "holdings": {"091170": 1000.0}}, "정수"),
        ({**base, "holdings": {"091170": True}}, "정수"),
    ]:
        try:
            acc._validate_research_state(broken, ledger, broken.get("holdings", {}))
        except (TypeError, ValueError) as exc:
            assert needle in str(exc), (needle, exc)
        else:
            raise AssertionError(f"타입 위반이 통과했습니다: {needle}")


def check_lifecycle_consistency() -> None:
    event = CorporateAction(
        event_id="SYNTH-SUSP-1",
        ticker="091170",
        event_type=EventType.SUSPENSION_START,
        event_date=date(2026, 8, 24),
        source_document_id="doc",
        source_url="https://example.test",
        source_sha256="b" * 64,
    )
    ledger = _fake_ledger(events=[event], sha="a" * 64)
    base = {
        "cash": 0.0,
        "holdings": {"091170": 1000},
        "pending_receivables": (),
        "processed_action_ids": ("SYNTH-SUSP-1",),
        "corporate_action_states": {
            "091170": HoldingState(
                "091170", 1000, __import__("decimal").Decimal("16000000"),
                LifecycleState.ACTIVE, None,
            )
        },
        "strict_action_blockers": (),
        "research_only": True,
        "corporate_action_ledger_sha256": "a" * 64,
    }
    try:
        acc._validate_research_state(base, ledger, base["holdings"])
    except ValueError as exc:
        assert "lifecycle" in str(exc)
    else:
        raise AssertionError("lifecycle 불일치가 차단되지 않았습니다")

    suspended = {
        **base,
        "corporate_action_states": {
            "091170": HoldingState(
                "091170", 1000, __import__("decimal").Decimal("16000000"),
                LifecycleState.SUSPENDED, None,
            )
        },
    }
    acc._validate_research_state(suspended, ledger, suspended["holdings"])

    missing = {**base, "corporate_action_states": {}}
    try:
        acc._validate_research_state(missing, ledger, missing["holdings"])
    except ValueError as exc:
        assert "상태가 없" in str(exc)
    else:
        raise AssertionError("lifecycle 상태 누락이 차단되지 않았습니다")


def check_serialization_roundtrip() -> None:
    event = _event()
    ledger = _fake_ledger(events=[event], sha="a" * 64)
    state = {
        "cash": 100.0,
        "holdings": {"091170": 1000},
        "holding_cost_basis": {"091170": 16000.0},
        "holding_peak_closes": {"091170": 16500.0},
        "portfolio_peak_equity": 200.0,
        "last_valid_closes": {"091170": 16500.0},
        "rebalance_phase_offset": 0,
        "exit_phase_offset": 0,
        "pending_daily_risk_off_exits": {},
        "pending_receivables": (
            DistributionReceivable("P2A2B-091170-2026-08-31", "091170", 1000, 59000, date(2026, 9, 2), False),
        ),
        "processed_action_ids": ("P2A2B-091170-2026-08-31",),
        "corporate_action_states": {
            "091170": HoldingState("091170", 1000, __import__("decimal").Decimal("16000000"))
        },
        "strict_action_blockers": (ApprovalBlocker("MISSING_PAYMENT_DATE", "blocked", "E1"),),
        "research_only": True,
        "corporate_action_ledger_sha256": "a" * 64,
        "final_equity": 300.0,
    }
    payload = acc.serialize_state(state)
    restored = acc.deserialize_state(json.loads(json.dumps(payload)), ledger)
    assert acc.state_signature(restored) == acc.state_signature(state)
    assert restored["strict_action_blockers"][0].code == "MISSING_PAYMENT_DATE"

    duplicated = dict(payload)
    duplicated["corporate_action_states"] = payload["corporate_action_states"] * 2
    try:
        acc.deserialize_state(json.loads(json.dumps(duplicated)), ledger)
    except ValueError as exc:
        assert "중복" in str(exc)
    else:
        raise AssertionError("중복 상태 키가 통과했습니다")

    duplicate_receivables = dict(payload)
    duplicate_receivables["pending_receivables"] = payload["pending_receivables"] * 2
    try:
        acc.deserialize_state(json.loads(json.dumps(duplicate_receivables)), ledger)
    except ValueError as exc:
        assert "중복" in str(exc)
    else:
        raise AssertionError("중복 미수금이 통과했습니다")

    bad_bool = dict(payload)
    bad_bool["research_only"] = "false"
    try:
        acc.deserialize_state(json.loads(json.dumps(bad_bool)), ledger)
    except TypeError as exc:
        assert "boolean" in str(exc)
    else:
        raise AssertionError("문자열 research_only가 통과했습니다")

    for missing_key in ("pending_receivables", "strict_action_blockers"):
        broken_payload = dict(payload)
        broken_payload.pop(missing_key)
        try:
            acc.deserialize_state(json.loads(json.dumps(broken_payload)), ledger)
        except ValueError as exc:
            assert "필수 필드" in str(exc), (missing_key, exc)
        else:
            raise AssertionError(f"필수 필드 누락이 통과했습니다: {missing_key}")

    for bad_quantity in (1000.0, True):
        bad_holdings = dict(payload)
        bad_holdings["holdings"] = {"091170": bad_quantity}
        try:
            acc.deserialize_state(json.loads(json.dumps(bad_holdings)), ledger)
        except TypeError as exc:
            assert "정수" in str(exc)
        else:
            raise AssertionError(f"보유수량 타입 위반이 통과했습니다: {bad_quantity}")

    other = _fake_ledger(events=[event], sha="c" * 64)
    try:
        acc.deserialize_state(payload, other)
    except ValueError as exc:
        assert "다른 ledger" in str(exc)
    else:
        raise AssertionError("다른 ledger 상태가 복원되었습니다")


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
        ("partial seam whitelist", check_partial_seam_whitelist),
        ("research state gate", check_research_state_gate),
        ("restore validation", check_restore_validation),
        ("required state fields", check_required_state_fields),
        ("lifecycle consistency", check_lifecycle_consistency),
        ("serialization roundtrip", check_serialization_roundtrip),
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
