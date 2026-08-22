#!/usr/bin/env python3
"""Offline deterministic checks for the Phase 2A corporate-action ledger."""

from __future__ import annotations

import csv
import json
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from etf_corporate_actions import (
    CSV_COLUMNS,
    CorporateAction,
    CorporateActionBlocked,
    CorporateActionValidationError,
    EventType,
    HoldingState,
    LifecycleState,
    SourceDocument,
    _event_payload,
    apply_lifecycle_event,
    canonical_ledger_sha256,
    create_distribution_receivable,
    final_approval_report,
    load_corporate_action_ledger,
    process_distribution_payment,
    process_settlement,
    replay_lifecycle,
    transform_split_holding,
)

SOURCE_SHA = "a" * 64
SOURCE = SourceDocument("kind-doc-1", "https://example.test/kind-doc-1", SOURCE_SHA)


def _action(
    event_id: str,
    ticker: str,
    event_type: EventType,
    event_date: date,
    source_document_id: str | None = SOURCE.document_id,
    source_document: str | None = None,
    source_url: str | None = SOURCE.source_url,
    source_sha256: str | None = SOURCE.sha256,
    **kwargs: Any,
) -> CorporateAction:
    return CorporateAction(
        event_id,
        ticker,
        event_type,
        event_date,
        source_document_id=source_document_id,
        source_document=source_document or source_document_id,
        source_url=source_url,
        source_sha256=source_sha256,
        **kwargs,
    )


def _row(action: CorporateAction) -> dict[str, str]:
    row = {}
    for column in CSV_COLUMNS:
        value = _event_payload(action)[column]
        row[column] = "" if value is None else value
    return row


def _write_case(
    directory: Path,
    actions: list[CorporateAction],
    *,
    status: str = "VERIFIED",
    source_documents: list[SourceDocument] | None = None,
):
    ledger_path = directory / "actions.csv"
    manifest_path = directory / "manifest.json"
    with ledger_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(_row(action) for action in actions)
    source_documents = source_documents or [SOURCE]
    manifest = {
        "manifest_version": 1,
        "ledger_file": ledger_path.name,
        "ledger_sha256": canonical_ledger_sha256(actions),
        "verification_period": {"start": "2020-01-01", "end": "2020-12-31"},
        "verification_tickers": sorted({action.ticker for action in actions}),
        "source_documents": [
            {
                "document_id": document.document_id,
                "source_url": document.source_url,
                "sha256": document.sha256,
            }
            for document in source_documents
        ],
        "coverage_status": status,
        "notes": "synthetic fixture",
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return ledger_path, manifest_path


def _assert_blocked(report, code: str) -> None:
    assert not report.approval_valid
    assert code in {blocker.code for blocker in report.blockers}


def check_deterministic_load_and_blockers() -> None:
    with tempfile.TemporaryDirectory() as raw_directory:
        directory = Path(raw_directory)
        actions = [
            _action("e2", "000002", EventType.SUSPENSION_START, date(2020, 2, 1)),
            _action("e1", "000001", EventType.SPLIT, date(2020, 1, 2), ratio_num=2, ratio_den=1),
        ]
        ledger_path, manifest_path = _write_case(directory, list(reversed(actions)))
        loaded = load_corporate_action_ledger(ledger_path, manifest_path)
        assert [event.event_id for event in loaded.events] == ["e1", "e2"]
        assert loaded.approval_report().approval_valid

    template = load_corporate_action_ledger(
        ROOT / "data/etf_corporate_actions.csv",
        ROOT / "data/etf_corporate_actions_manifest.json",
    )
    _assert_blocked(template.approval_report(), "EMPTY_LEDGER")
    _assert_blocked(template.approval_report(), "INCOMPLETE_COVERAGE")


def check_provenance_and_rejection() -> None:
    with tempfile.TemporaryDirectory() as raw_directory:
        directory = Path(raw_directory)
        action = _action("missing-source", "000001", EventType.DELISTING, date(2020, 1, 2))
        action = CorporateAction(
            action.event_id,
            action.ticker,
            action.event_type,
            action.event_date,
        )
        ledger_path, manifest_path = _write_case(directory, [action])
        loaded = load_corporate_action_ledger(ledger_path, manifest_path)
        _assert_blocked(loaded.approval_report(), "MISSING_SOURCE_PROVENANCE")

        unknown = _action(
            "unknown",
            "000001",
            EventType.DELISTING,
            date(2020, 1, 2),
            source_document_id="other-doc",
            source_url="https://example.test/other-doc",
            source_sha256="b" * 64,
        )
        unknown_path, unknown_manifest = _write_case(directory, [unknown])
        try:
            load_corporate_action_ledger(unknown_path, unknown_manifest)
        except CorporateActionValidationError as exc:
            assert "unknown source document ID" in str(exc)
        else:
            raise AssertionError("unknown source document was accepted")

        mismatch = _action(
            "mismatch",
            "000001",
            EventType.DELISTING,
            date(2020, 1, 2),
            source_url="https://example.test/wrong-url",
        )
        mismatch_path, mismatch_manifest = _write_case(directory, [mismatch])
        try:
            load_corporate_action_ledger(mismatch_path, mismatch_manifest)
        except CorporateActionValidationError as exc:
            assert "source URL mismatch" in str(exc)
        else:
            raise AssertionError("source URL mismatch was accepted")

        blank_url = _action(
            "blank-url",
            "000001",
            EventType.DELISTING,
            date(2020, 1, 2),
            source_url=None,
        )
        blank_path, blank_manifest = _write_case(directory, [blank_url])
        try:
            load_corporate_action_ledger(blank_path, blank_manifest)
        except CorporateActionValidationError as exc:
            assert "source URL mismatch" in str(exc)
        else:
            raise AssertionError("blank source URL was accepted")

        unrelated = _action(
            "unrelated-source",
            "000001",
            EventType.DELISTING,
            date(2020, 1, 2),
            source_document="unrelated-file.pdf",
        )
        unrelated_path, unrelated_manifest = _write_case(directory, [unrelated])
        try:
            load_corporate_action_ledger(unrelated_path, unrelated_manifest)
        except CorporateActionValidationError as exc:
            assert "source_document must exactly equal" in str(exc)
        else:
            raise AssertionError("unrelated source document was accepted")

        sha_mismatch = _action(
            "sha-mismatch",
            "000001",
            EventType.DELISTING,
            date(2020, 1, 2),
            source_sha256="b" * 64,
        )
        sha_path, sha_manifest = _write_case(directory, [sha_mismatch])
        try:
            load_corporate_action_ledger(sha_path, sha_manifest)
        except CorporateActionValidationError as exc:
            assert "source SHA mismatch" in str(exc)
        else:
            raise AssertionError("source SHA mismatch was accepted")

        duplicate_path, duplicate_manifest = _write_case(directory, [unknown], source_documents=[SOURCE])
        manifest = json.loads(duplicate_manifest.read_text(encoding="utf-8"))
        manifest["source_documents"].append(manifest["source_documents"][0])
        duplicate_manifest.write_text(json.dumps(manifest), encoding="utf-8")
        try:
            load_corporate_action_ledger(duplicate_path, duplicate_manifest)
        except CorporateActionValidationError as exc:
            assert "duplicate manifest document_id" in str(exc)
        else:
            raise AssertionError("duplicate manifest document ID was accepted")

        invalid_event = _action("invalid", "000001", EventType.DELISTING, date(2020, 1, 2))
        invalid_path, invalid_manifest = _write_case(directory, [invalid_event])
        text = invalid_path.read_text(encoding="utf-8").replace("DELISTING", "UNKNOWN_EVENT")
        invalid_path.write_text(text, encoding="utf-8")
        try:
            load_corporate_action_ledger(invalid_path, invalid_manifest)
        except CorporateActionValidationError as exc:
            assert "unknown event_type" in str(exc)
        else:
            raise AssertionError("unknown event type was accepted")


def check_distribution_processing() -> None:
    action = _action(
        "dist-1",
        "000001",
        EventType.CASH_DISTRIBUTION,
        date(2020, 1, 2),
        record_date=date(2020, 1, 3),
        ex_date=date(2020, 1, 2),
        payment_date=date(2020, 1, 6),
        cash_amount=Decimal("1.25"),
        currency="KRW",
    )
    assert create_distribution_receivable(
        action, 10, held_on_record_date=False
    ) is None
    receivable = create_distribution_receivable(
        action, 10, held_on_record_date=True, held_on_ex_date=True
    )
    assert receivable is not None
    assert receivable.amount == Decimal("12.50")
    pending, cash = process_distribution_payment(receivable, date(2020, 1, 5))
    assert cash == Decimal(0) and not pending.paid
    paid, cash = process_distribution_payment(pending, date(2020, 1, 7))
    assert cash == Decimal("12.50") and paid.paid


def check_split_arithmetic() -> None:
    holding = HoldingState("000001", 10, Decimal(1000))
    split = _action(
        "split-1", "000001", EventType.SPLIT, date(2020, 1, 2), ratio_num=2, ratio_den=1
    )
    split_holding = transform_split_holding(holding, split)
    assert split_holding.quantity == 20
    assert split_holding.total_cost_basis == Decimal(1000)
    reverse = _action(
        "reverse-1",
        "000001",
        EventType.REVERSE_SPLIT,
        date(2020, 1, 3),
        ratio_num=1,
        ratio_den=2,
    )
    assert transform_split_holding(split_holding, reverse).quantity == 10
    odd = _action(
        "reverse-odd",
        "000001",
        EventType.REVERSE_SPLIT,
        date(2020, 1, 4),
        ratio_num=1,
        ratio_den=3,
    )
    try:
        transform_split_holding(HoldingState("000001", 10, Decimal(1000)), odd)
    except CorporateActionBlocked as exc:
        assert exc.blocker.code == "FRACTIONAL_SHARE_UNRESOLVED"
    else:
        raise AssertionError("odd reverse split did not block")

    invalid_split = _action(
        "split-invalid",
        "000001",
        EventType.SPLIT,
        date(2020, 1, 5),
        ratio_num=1,
        ratio_den=1,
    )
    try:
        transform_split_holding(holding, invalid_split)
    except CorporateActionValidationError as exc:
        assert "SPLIT ratio" in str(exc)
    else:
        raise AssertionError("one-for-one split was accepted")
    invalid_reverse = _action(
        "reverse-invalid",
        "000001",
        EventType.REVERSE_SPLIT,
        date(2020, 1, 6),
        ratio_num=2,
        ratio_den=1,
    )
    try:
        transform_split_holding(holding, invalid_reverse)
    except CorporateActionValidationError as exc:
        assert "REVERSE_SPLIT ratio" in str(exc)
    else:
        raise AssertionError("reverse ratio greater than one was accepted")
    settled = HoldingState("000001", 0, Decimal(0), LifecycleState.SETTLED)
    try:
        transform_split_holding(settled, split)
    except CorporateActionBlocked as exc:
        assert exc.blocker.code == "INVALID_LIFECYCLE"
    else:
        raise AssertionError("split after settlement was accepted")
    out_of_order_holding = HoldingState(
        "000001", 10, Decimal(1000), last_event_date=date(2020, 1, 5)
    )
    try:
        transform_split_holding(out_of_order_holding, split)
    except CorporateActionBlocked as exc:
        assert exc.blocker.code == "EVENT_OUT_OF_ORDER"
    else:
        raise AssertionError("out-of-order split was accepted")


def check_lifecycle_and_final_blockers() -> None:
    start = _action("s1", "000001", EventType.SUSPENSION_START, date(2020, 1, 2))
    end = _action("s2", "000001", EventType.SUSPENSION_END, date(2020, 1, 3))
    holding = HoldingState("000001", 10, Decimal(1000))

    holding = apply_lifecycle_event(holding, start)
    assert holding.lifecycle == LifecycleState.SUSPENDED
    holding = apply_lifecycle_event(holding, end)
    assert holding.lifecycle == LifecycleState.ACTIVE

    delisting = _action("d1", "000001", EventType.DELISTING, date(2020, 1, 4))
    settlement = _action(
        "d2",
        "000001",
        EventType.CASH_SETTLEMENT,
        date(2020, 1, 5),
        settlement_date=date(2020, 1, 6),
        cash_amount=Decimal(100),
        currency="KRW",
    )
    with tempfile.TemporaryDirectory() as raw_directory:
        directory = Path(raw_directory)
        standalone_end = _action(
            "standalone-end", "000001", EventType.SUSPENSION_END, date(2020, 1, 2)
        )
        assert "OPENING_LIFECYCLE_UNVERIFIED" in {
            blocker.code for blocker in replay_lifecycle("000001", [standalone_end]).blockers
        }
        path, manifest = _write_case(directory, [standalone_end])
        standalone_ledger = load_corporate_action_ledger(path, manifest)
        _assert_blocked(
            standalone_ledger.approval_report(), "OPENING_LIFECYCLE_UNVERIFIED"
        )
        _assert_blocked(standalone_ledger.approval_report(), "OPENING_LIFECYCLE_UNVERIFIED")

        standalone_settlement = _action(
            "standalone-settlement",
            "000001",
            EventType.CASH_SETTLEMENT,
            date(2020, 1, 2),
            settlement_date=date(2020, 1, 3),
            cash_amount=Decimal(100),
            currency="KRW",
        )
        path, manifest = _write_case(directory, [standalone_settlement])
        _assert_blocked(
            load_corporate_action_ledger(path, manifest).approval_report(),
            "OPENING_LIFECYCLE_UNVERIFIED",
        )

        path, manifest = _write_case(directory, [delisting])
        _assert_blocked(load_corporate_action_ledger(path, manifest).approval_report(), "MISSING_SETTLEMENT")
        path, manifest = _write_case(directory, [delisting, settlement])
        loaded = load_corporate_action_ledger(path, manifest)
        assert loaded.approval_report().approval_valid
        delisted = apply_lifecycle_event(holding, delisting)
        before = process_settlement(delisted, settlement, date(2020, 1, 5))
        assert before.cash_paid == Decimal(0)
        assert before.holding.lifecycle == LifecycleState.DELISTED_UNSETTLED
        assert before.holding.quantity == 10
        settled = process_settlement(before.holding, settlement, date(2020, 1, 6))
        assert settled.cash_paid == Decimal(1000)
        assert settled.holding.lifecycle == LifecycleState.SETTLED
        assert settled.holding.quantity == 0
        assert apply_lifecycle_event(delisted, settlement).lifecycle == LifecycleState.SETTLED

        early_settlement = _action(
            "early-settlement",
            "000001",
            EventType.CASH_SETTLEMENT,
            date(2020, 1, 3),
            settlement_date=date(2020, 1, 6),
            cash_amount=Decimal(100),
            currency="KRW",
        )
        try:
            process_settlement(
                HoldingState(
                    "000001",
                    10,
                    Decimal(1000),
                    LifecycleState.DELISTED_UNSETTLED,
                    last_event_date=date(2020, 1, 5),
                ),
                early_settlement,
                date(2020, 1, 6),
            )
        except CorporateActionBlocked as exc:
            assert exc.blocker.code == "EVENT_OUT_OF_ORDER"
        else:
            raise AssertionError("out-of-order settlement was accepted")

        same_day_start = _action(
            "same-day-start", "000001", EventType.SUSPENSION_START, date(2020, 1, 7)
        )
        same_day_end = _action(
            "same-day-end", "000001", EventType.SUSPENSION_END, date(2020, 1, 7)
        )
        assert "AMBIGUOUS_SAME_DATE_LIFECYCLE" in {
            blocker.code
            for blocker in replay_lifecycle("000001", [same_day_start, same_day_end]).blockers
        }

        try:
            apply_lifecycle_event(holding, settlement)
        except CorporateActionBlocked as exc:
            assert exc.blocker.code == "INVALID_LIFECYCLE"
        else:
            raise AssertionError("settlement without delisting was accepted")

        positive_settled = HoldingState("000001", 1, Decimal(100), LifecycleState.SETTLED)
        _assert_blocked(
            final_approval_report(loaded, {"000001": positive_settled}),
            "SETTLED_HOLDING_POSITIVE",
        )

        suspended = apply_lifecycle_event(HoldingState("000001", 10, Decimal(1000)), start)
        report = final_approval_report(loaded, {"000001": suspended}, stale_tickers=["000001"])
        _assert_blocked(report, "FINAL_LIFECYCLE_UNSETTLED")
        _assert_blocked(report, "FINAL_POSITION_STALE")


def main() -> None:
    check_deterministic_load_and_blockers()
    check_provenance_and_rejection()
    check_distribution_processing()
    check_split_arithmetic()
    check_lifecycle_and_final_blockers()
    print("corporate-action ledger checks passed")


if __name__ == "__main__":
    main()
