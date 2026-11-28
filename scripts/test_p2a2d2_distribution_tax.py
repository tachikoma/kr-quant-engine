"""P2-A-2d-2 과세표준 회계 fixture(네트워크 없음)."""

from __future__ import annotations

import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import p2a2d1_distribution_accounting as d1
import p2a2d2_distribution_tax as tax

from etf_corporate_actions import (
    LEGACY_CSV_COLUMNS,
    CorporateAction,
    CorporateActionBlocked,
    CorporateActionValidationError,
    EventType,
    TaxPolicy,
    _event_payload,
    canonical_ledger_sha256,
    create_distribution_receivable,
    process_distribution_payment,
)
from run_etf_backtest import _validate_research_state


def _action(base: Decimal | None = Decimal(2)) -> CorporateAction:
    return CorporateAction(
        event_id="P2A2D2-498400-2026-09-15",
        ticker="498400",
        event_type=EventType.CASH_DISTRIBUTION,
        event_date=date(2026, 9, 14),
        record_date=date(2026, 9, 15),
        ex_date=date(2026, 9, 14),
        payment_date=date(2026, 9, 17),
        cash_amount=Decimal(300),
        currency="KRW",
        source_document_id="kodex_api_498400",
        source_url="https://example.test",
        source_sha256="a" * 64,
        taxable_base_per_share=base,
        tax_policy_id=tax.POLICY_ID if base is not None else None,
    )


def _ledger(events=(), sha: str = "a" * 64) -> SimpleNamespace:
    return SimpleNamespace(
        events=tuple(events),
        ledger_sha256=sha,
        approval_report=lambda: SimpleNamespace(approval_valid=True, blockers=()),
    )


def check_tax_policy_validation() -> None:
    policy = TaxPolicy("general", Decimal("0.154"))
    assert policy.rate == Decimal("0.154")
    for bad in [Decimal("1.1"), Decimal("-0.01"), Decimal("NaN")]:
        try:
            TaxPolicy("general", bad)
        except CorporateActionValidationError:
            pass
        else:
            raise AssertionError(f"잘못된 세율이 통과했습니다: {bad}")
    try:
        TaxPolicy(" ", Decimal("0.154"))
    except CorporateActionValidationError:
        pass
    else:
        raise AssertionError("빈 정책 ID가 통과했습니다")
    try:
        TaxPolicy("general", Decimal("0.154"), basis="gross")
    except CorporateActionValidationError:
        pass
    else:
        raise AssertionError("지원하지 않는 과세기준이 통과했습니다")


def check_receivable_tax() -> None:
    action = _action()
    receivable = create_distribution_receivable(
        action, 100, held_on_record_date=True, held_on_ex_date=True, tax_policy=tax.POLICY
    )
    assert receivable is not None
    assert receivable.amount == Decimal(30000)
    assert receivable.tax_amount == Decimal("30.8")
    assert receivable.tax_policy_id == tax.POLICY_ID
    paid, cash = process_distribution_payment(receivable, date(2026, 9, 17))
    assert paid.paid is True and cash == Decimal("29969.2")
    again, cash_again = process_distribution_payment(paid, date(2026, 9, 18))
    assert cash_again == 0 and again.paid is True

    # 과세표준 누락은 fail-closed (정책 ID는 일치시켜 누락 검증에 도달)
    missing_base = _action(None)
    object.__setattr__(missing_base, "tax_policy_id", tax.POLICY_ID)
    try:
        create_distribution_receivable(
            missing_base,
            100,
            held_on_record_date=True,
            held_on_ex_date=True,
            tax_policy=tax.POLICY,
        )
    except CorporateActionBlocked as exc:
        assert exc.blocker.code == "MISSING_TAXABLE_BASE"
    else:
        raise AssertionError("과세표준 누락이 차단되지 않았습니다")

    # 세전 경로는 세금 0
    pre_tax = create_distribution_receivable(
        action, 100, held_on_record_date=True, held_on_ex_date=True
    )
    assert pre_tax is not None and pre_tax.tax_amount == 0 and pre_tax.tax_policy_id is None
    _, gross_cash = process_distribution_payment(pre_tax, date(2026, 9, 17))
    assert gross_cash == Decimal(30000)


def check_zero_vs_missing() -> None:
    zero = _action(Decimal(0))
    missing = _action(None)
    assert zero.taxable_base_per_share == Decimal(0)
    assert missing.taxable_base_per_share is None
    zero_event = create_distribution_receivable(
        zero, 100, held_on_record_date=True, held_on_ex_date=True, tax_policy=tax.POLICY
    )
    assert zero_event is not None and zero_event.tax_amount == 0
    try:
        create_distribution_receivable(
            missing, 100, held_on_record_date=True, held_on_ex_date=True, tax_policy=tax.POLICY
        )
    except CorporateActionBlocked:
        pass
    else:
        raise AssertionError("누락이 0으로 대체되었습니다")


def check_canonical_hash_compatibility() -> None:
    legacy = _action(None)
    legacy_payload = _event_payload(legacy)
    assert set(legacy_payload) == set(LEGACY_CSV_COLUMNS)
    legacy_hash = canonical_ledger_sha256([legacy])
    assert legacy_hash == canonical_ledger_sha256([_action(None)])

    taxed = _action()
    taxed_payload = _event_payload(taxed)
    assert "taxable_base_per_share" in taxed_payload
    assert "tax_policy_id" in taxed_payload
    assert canonical_ledger_sha256([taxed]) != legacy_hash


def check_policy_restore_guard() -> None:
    action = _action()
    receivable = create_distribution_receivable(
        action, 100, held_on_record_date=True, held_on_ex_date=True, tax_policy=tax.POLICY
    )
    assert receivable is not None
    state = {
        "cash": 0.0,
        "holdings": {"498400": 1000},
        "pending_receivables": (receivable,),
        "processed_action_ids": (action.event_id,),
        "corporate_action_states": {},
        "strict_action_blockers": (),
        "research_only": True,
        "tax_policy_id": tax.POLICY_ID,
        "tax_policy_signature": f"{tax.POLICY_ID}|{tax.POLICY_RATE}|taxable_base",
        "corporate_action_ledger_sha256": "a" * 64,
    }
    ledger = _ledger([action])
    _validate_research_state(state, ledger, state["holdings"], tax.POLICY)
    for bad_policy in [None, TaxPolicy("other", tax.POLICY_RATE)]:
        try:
            _validate_research_state(state, ledger, state["holdings"], bad_policy)
        except ValueError as exc:
            assert "정책" in str(exc)
        else:
            raise AssertionError("다른 세금 정책 상태가 통과했습니다")
    # 같은 정책 ID라도 세율이 바뀌면 복원 차단
    state["tax_policy_signature"] = f"{tax.POLICY_ID}|{tax.POLICY_RATE}|taxable_base"
    try:
        _validate_research_state(
            state, ledger, state["holdings"], TaxPolicy(tax.POLICY_ID, Decimal("0.5"))
        )
    except ValueError as exc:
        assert "세율" in str(exc)
    else:
        raise AssertionError("같은 ID·다른 세율 상태가 통과했습니다")


def check_serialization_roundtrip() -> None:
    action = _action()
    receivable = create_distribution_receivable(
        action, 100, held_on_record_date=True, held_on_ex_date=True, tax_policy=tax.POLICY
    )
    assert receivable is not None
    state = {
        "cash": 0.0,
        "holdings": {"498400": 1000},
        "holding_cost_basis": {"498400": 16000.0},
        "holding_peak_closes": {},
        "portfolio_peak_equity": None,
        "last_valid_closes": {},
        "pending_receivables": (receivable,),
        "processed_action_ids": (action.event_id,),
        "corporate_action_states": {},
        "strict_action_blockers": (),
        "research_only": True,
        "tax_policy_id": tax.POLICY_ID,
        "tax_policy_signature": f"{tax.POLICY_ID}|{tax.POLICY_RATE}|taxable_base",
        "corporate_action_ledger_sha256": "a" * 64,
        "final_equity": 0.0,
    }
    payload = json.loads(json.dumps(d1.serialize_state(state)))
    restored = d1.deserialize_state(payload, _ledger([action]), tax.POLICY)
    item = restored["pending_receivables"][0]
    assert item.tax_amount == Decimal("30.8")
    assert item.tax_policy_id == tax.POLICY_ID
    assert restored["tax_policy_id"] == tax.POLICY_ID


def check_taxable_base_bounds() -> None:
    # 과세표준이 분배금을 초과하면 ledger 로더가 거부한다.
    import csv
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.csv"
        manifest = Path(tmp) / "manifest.json"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=tax.LEDGER_COLUMNS)
            writer.writeheader()
            row = {column: "" for column in tax.LEDGER_COLUMNS}
            row.update(
                {
                    "event_id": "E1",
                    "ticker": "498400",
                    "event_type": "CASH_DISTRIBUTION",
                    "event_date": "2026-09-14",
                    "record_date": "2026-09-15",
                    "ex_date": "2026-09-14",
                    "payment_date": "2026-09-17",
                    "cash_amount": "300",
                    "currency": "KRW",
                    "source_document_id": "doc",
                    "source_document": "doc",
                    "source_url": "https://example.test",
                    "source_sha256": "a" * 64,
                    "taxable_base_per_share": "301",
                    "tax_policy_id": tax.POLICY_ID,
                }
            )
            writer.writerow(row)
        manifest.write_text(
            json.dumps(
                {
                    "manifest_version": 1,
                    "ledger_file": path.name,
                    "ledger_sha256": "0" * 64,
                    "verification_period": {"start": "2026-09-14", "end": "2026-09-17"},
                    "verification_tickers": ["498400"],
                    "source_documents": [
                        {
                            "document_id": "doc",
                            "source_url": "https://example.test",
                            "sha256": "a" * 64,
                        }
                    ],
                    "coverage_status": "PARTIAL",
                    "notes": "fixture",
                }
            ),
            encoding="utf-8",
        )
        from etf_corporate_actions import load_corporate_action_ledger

        try:
            load_corporate_action_ledger(path, manifest)
        except CorporateActionValidationError as exc:
            assert "taxable_base" in str(exc)
        else:
            raise AssertionError("과세표준 초과가 차단되지 않았습니다")


def check_research_seam_required() -> None:
    from run_etf_backtest import run_etf_strategy

    try:
        run_etf_strategy(
            0.0,
            [pd.Timestamp("2026-09-14")],
            pd.DataFrame({"date": [pd.Timestamp("2026-09-14")]}),
            distribution_tax_policy=tax.POLICY,
            approval_strict=False,
        )
    except ValueError as exc:
        assert "연구 seam" in str(exc)
    else:
        raise AssertionError("세금 정책이 승인 경로로 유출되었습니다")


def check_policy_id_match() -> None:
    # 이벤트 정책 ID가 실행 정책과 다르면 차단
    wrong = _action()
    object.__setattr__(wrong, "tax_policy_id", "other_policy")
    try:
        create_distribution_receivable(
            wrong, 100, held_on_record_date=True, held_on_ex_date=True, tax_policy=tax.POLICY
        )
    except CorporateActionBlocked as exc:
        assert exc.blocker.code == "TAX_POLICY_MISMATCH"
    else:
        raise AssertionError("정책 ID 불일치가 차단되지 않았습니다")

    # 이벤트에 정책 ID가 없어도 차단
    missing = _action()
    object.__setattr__(missing, "tax_policy_id", None)
    try:
        create_distribution_receivable(
            missing, 100, held_on_record_date=True, held_on_ex_date=True, tax_policy=tax.POLICY
        )
    except CorporateActionBlocked as exc:
        assert exc.blocker.code == "TAX_POLICY_MISMATCH"
    else:
        raise AssertionError("정책 ID 누락이 차단되지 않았습니다")


def check_direct_call_taxable_base() -> None:
    # loader를 거치지 않은 직접 호출에서 비정상 과세표준을 차단
    for bad in [
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal(-1),
        Decimal(301),
    ]:
        action = _action(bad)
        try:
            create_distribution_receivable(
                action, 100, held_on_record_date=True, held_on_ex_date=True, tax_policy=tax.POLICY
            )
        except CorporateActionBlocked as exc:
            assert exc.blocker.code == "INVALID_TAXABLE_BASE"
        else:
            raise AssertionError(f"직접 호출 비정상 과세표준이 통과했습니다: {bad}")


def check_input_validation() -> None:
    # 정상 74건 형태는 통과
    good = pd.DataFrame(
        {
            "ticker": ["498400", "091170"],
            "record_date": ["2026-09-15", "2026-10-30"],
            "amount_per_share": [300.0, 53.0],
            "taxable_base_per_share": [2.0, 0.0],
        }
    )
    counts = tax.validate_taxable_base_input(good)
    assert counts["total"] == 2 and counts["excess"] == 0

    # 누락/NaN/Inf/음수/초과는 fail-closed
    for bad in [None, float("nan"), float("inf"), -1.0, 301.0]:
        frame = good.copy()
        frame.loc[0, "taxable_base_per_share"] = bad
        try:
            tax.validate_taxable_base_input(frame)
        except ValueError:
            pass
        else:
            raise AssertionError(f"비정상 입력이 통과했습니다: {bad}")

    # 음수 분배금도 차단
    negative_amount = good.copy()
    negative_amount.loc[0, "amount_per_share"] = -300.0
    try:
        tax.validate_taxable_base_input(negative_amount)
    except ValueError:
        pass
    else:
        raise AssertionError("음수 분배금이 통과했습니다")

    # 비정상값이 partial로 분류되지 않음
    row = SimpleNamespace(ticker="498400", record_date="2026-09-15")
    for bad in [float("nan"), float("inf"), -1.0, 301.0]:
        try:
            tax.classify_base_type(bad, 300.0, row)
        except ValueError:
            pass
        else:
            raise AssertionError(f"비정상값이 partial로 분류되었습니다: {bad}")
    assert tax.classify_base_type(2.0, 300.0, row) == "partial"
    assert tax.classify_base_type(0.0, 53.0, row) == "zero"
    assert tax.classify_base_type(300.0, 300.0, row) == "equal"


def main() -> None:
    checks = [
        ("tax policy validation", check_tax_policy_validation),
        ("receivable tax", check_receivable_tax),
        ("zero vs missing", check_zero_vs_missing),
        ("canonical hash compatibility", check_canonical_hash_compatibility),
        ("policy restore guard", check_policy_restore_guard),
        ("serialization roundtrip", check_serialization_roundtrip),
        ("taxable base bounds", check_taxable_base_bounds),
        ("research seam required", check_research_seam_required),
        ("event policy id match", check_policy_id_match),
        ("direct-call taxable base", check_direct_call_taxable_base),
        ("input validation", check_input_validation),
    ]
    for name, check in checks:
        try:
            check()
        except AssertionError as exc:
            print(f"FAIL {name}: {exc}")
            raise
        print(f"PASS {name}")
    print("\np2a2d2 tax checks passed")


if __name__ == "__main__":
    main()
