"""Strict, source-backed ETF corporate-action ledger primitives.

This module intentionally does not fetch data or infer events from prices.  The
CSV is a manually normalized ledger backed by KIND or issuer documents.  The
ledger can therefore be validated and exercised before strategy integration.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path

DEFAULT_LEDGER_PATH = Path("data/etf_corporate_actions.csv")
DEFAULT_MANIFEST_PATH = Path("data/etf_corporate_actions_manifest.json")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
TICKER_RE = re.compile(r"^[0-9A-Z]{6}$")

CSV_COLUMNS = (
    "event_id",
    "ticker",
    "event_type",
    "event_date",
    "record_date",
    "ex_date",
    "payment_date",
    "settlement_date",
    "ratio_num",
    "ratio_den",
    "cash_amount",
    "currency",
    "source_document_id",
    "source_document",
    "source_url",
    "source_sha256",
    "notes",
)
COMMON_COLUMNS = {"event_id", "ticker", "event_type", "event_date"}


class EventType(str, Enum):
    CASH_DISTRIBUTION = "CASH_DISTRIBUTION"
    SPLIT = "SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    SUSPENSION_START = "SUSPENSION_START"
    SUSPENSION_END = "SUSPENSION_END"
    DELISTING = "DELISTING"
    CASH_SETTLEMENT = "CASH_SETTLEMENT"
    REDEMPTION = "REDEMPTION"


class LifecycleState(str, Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DELISTED_UNSETTLED = "DELISTED_UNSETTLED"
    SETTLED = "SETTLED"


class CorporateActionValidationError(ValueError):
    """Raised for malformed or unsupported normalized ledger input."""


class CorporateActionBlocked(RuntimeError):
    """Raised when a pure state operation cannot produce an approval-safe result."""

    def __init__(self, blocker: ApprovalBlocker) -> None:
        self.blocker = blocker
        super().__init__(f"[{blocker.code}] {blocker.message}")


@dataclass(frozen=True)
class ApprovalBlocker:
    code: str
    message: str
    event_id: str | None = None
    ticker: str | None = None
    event_date: date | None = None


@dataclass(frozen=True)
class ApprovalReport:
    status: str
    blockers: tuple[ApprovalBlocker, ...]
    event_count: int
    ledger_sha256: str

    @property
    def approval_valid(self) -> bool:
        return self.status == "APPROVED"


@dataclass(frozen=True)
class SourceDocument:
    document_id: str
    source_url: str
    sha256: str


@dataclass(frozen=True)
class CoverageManifest:
    manifest_version: int
    ledger_file: str
    ledger_sha256: str
    verification_start: date | None
    verification_end: date | None
    verification_tickers: tuple[str, ...]
    source_documents: tuple[SourceDocument, ...]
    coverage_status: str
    notes: str


@dataclass(frozen=True)
class CorporateAction:
    event_id: str
    ticker: str
    event_type: EventType
    event_date: date
    record_date: date | None = None
    ex_date: date | None = None
    payment_date: date | None = None
    settlement_date: date | None = None
    ratio_num: int | None = None
    ratio_den: int | None = None
    cash_amount: Decimal | None = None
    currency: str | None = None
    source_document_id: str | None = None
    source_document: str | None = None
    source_url: str | None = None
    source_sha256: str | None = None
    notes: str = ""


@dataclass(frozen=True)
class HoldingState:
    ticker: str
    quantity: int
    total_cost_basis: Decimal
    lifecycle: LifecycleState = LifecycleState.ACTIVE
    last_event_date: date | None = None


@dataclass(frozen=True)
class DistributionReceivable:
    event_id: str
    ticker: str
    quantity: int
    amount: Decimal
    payment_date: date
    paid: bool = False


@dataclass(frozen=True)
class SettlementResult:
    holding: HoldingState
    cash_paid: Decimal


@dataclass(frozen=True)
class LifecycleReplay:
    ticker: str
    opening_state: LifecycleState
    final_state: HoldingState
    blockers: tuple[ApprovalBlocker, ...]


@dataclass(frozen=True)
class CorporateActionLedger:
    events: tuple[CorporateAction, ...]
    manifest: CoverageManifest
    ledger_sha256: str
    blockers: tuple[ApprovalBlocker, ...]

    def events_for_ticker(self, ticker: str) -> tuple[CorporateAction, ...]:
        ticker = _ticker(ticker)
        return tuple(event for event in self.events if event.ticker == ticker)

    def events_on_date(self, event_date: str | date | datetime) -> tuple[CorporateAction, ...]:
        target = _date(event_date, "lookup date")
        return tuple(event for event in self.events if event.event_date == target)

    def event_by_id(self, event_id: str) -> CorporateAction:
        for event in self.events:
            if event.event_id == event_id:
                return event
        raise KeyError(f"corporate action event_id not found: {event_id}")

    def approval_report(
        self,
        final_holdings: Mapping[str, HoldingState] | None = None,
        *,
        stale_tickers: Iterable[str] = (),
    ) -> ApprovalReport:
        blockers = list(self.blockers)
        for ticker in sorted({event.ticker for event in self.events}):
            replay = replay_lifecycle(ticker, self.events_for_ticker(ticker))
            blockers.extend(replay.blockers)
        stale = {_ticker(ticker) for ticker in stale_tickers}
        for ticker, holding in (final_holdings or {}).items():
            normalized = _ticker(ticker)
            if holding.lifecycle in {
                LifecycleState.SUSPENDED,
                LifecycleState.DELISTED_UNSETTLED,
            }:
                blockers.append(
                    ApprovalBlocker(
                        "FINAL_LIFECYCLE_UNSETTLED",
                        f"final holding has lifecycle {holding.lifecycle.value}",
                        ticker=normalized,
                    )
                )
            if normalized in stale:
                blockers.append(
                    ApprovalBlocker(
                        "FINAL_POSITION_STALE",
                        "final holding has no current usable value; no price-derived value is allowed",
                        ticker=normalized,
                    )
                )
            if holding.lifecycle == LifecycleState.SETTLED and holding.quantity > 0:
                blockers.append(
                    ApprovalBlocker(
                        "SETTLED_HOLDING_POSITIVE",
                        "SETTLED holding must have zero quantity",
                        ticker=normalized,
                    )
                )
        return ApprovalReport(
            "APPROVED" if not blockers else "BLOCKED",
            tuple(_unique_blockers(blockers)),
            len(self.events),
            self.ledger_sha256,
        )


def _unique_blockers(blockers: Iterable[ApprovalBlocker]) -> list[ApprovalBlocker]:
    seen: set[tuple[object, ...]] = set()
    result = []
    for blocker in blockers:
        key = (
            blocker.code,
            blocker.event_id,
            blocker.ticker,
            blocker.event_date,
            blocker.message,
        )
        if key not in seen:
            seen.add(key)
            result.append(blocker)
    return result


def _date(value: str | date | datetime, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise CorporateActionValidationError(f"invalid {field}: {value!r}") from exc


def _optional_date(value: object, field: str) -> date | None:
    if value is None or str(value).strip() == "":
        return None
    return _date(str(value), field)


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _ticker(value: object) -> str:
    ticker = str(value)
    if not TICKER_RE.fullmatch(ticker):
        raise CorporateActionValidationError(
            f"ticker must be exactly six ASCII uppercase alphanumeric characters, got {ticker!r}"
        )
    return ticker


def _decimal(value: object, field: str) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise CorporateActionValidationError(f"invalid {field}: {value!r}") from exc
    if not parsed.is_finite():
        raise CorporateActionValidationError(f"{field} must be finite")
    return parsed


def _integer(value: object, field: str) -> int | None:
    parsed = _decimal(value, field)
    if parsed is None:
        return None
    if parsed != parsed.to_integral_value():
        raise CorporateActionValidationError(f"{field} must be an integer")
    return int(parsed)


def _source_sha(value: object) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    sha = str(value).strip().lower()
    if not SHA256_RE.fullmatch(sha):
        return None
    return sha


def _blocker(code: str, message: str, event: CorporateAction | None = None) -> ApprovalBlocker:
    return ApprovalBlocker(
        code,
        message,
        None if event is None else event.event_id,
        None if event is None else event.ticker,
        None if event is None else event.event_date,
    )


def _event_from_row(row: Mapping[str, str]) -> tuple[CorporateAction, list[ApprovalBlocker]]:
    event_id = str(row.get("event_id", "")).strip()
    if not event_id:
        raise CorporateActionValidationError("event_id is required")
    ticker = _ticker(row.get("ticker", ""))
    raw_type = str(row.get("event_type", "")).strip()
    try:
        event_type = EventType(raw_type)
    except ValueError as exc:
        raise CorporateActionValidationError(f"unknown event_type: {raw_type!r}") from exc
    event_date = _date(row.get("event_date", ""), "event_date")
    ratio_num = _integer(row.get("ratio_num"), "ratio_num")
    ratio_den = _integer(row.get("ratio_den"), "ratio_den")
    cash_amount = _decimal(row.get("cash_amount"), "cash_amount")
    currency = _text(row.get("currency"))
    if currency not in {None, "KRW"}:
        raise CorporateActionValidationError("currency must be KRW")
    if cash_amount is not None and cash_amount <= 0:
        raise CorporateActionValidationError("cash_amount must be positive")
    if ratio_num is not None and ratio_num <= 0:
        raise CorporateActionValidationError("ratio_num must be positive")
    if ratio_den is not None and ratio_den <= 0:
        raise CorporateActionValidationError("ratio_den must be positive")
    event = CorporateAction(
        event_id=event_id,
        ticker=ticker,
        event_type=event_type,
        event_date=event_date,
        record_date=_optional_date(row.get("record_date"), "record_date"),
        ex_date=_optional_date(row.get("ex_date"), "ex_date"),
        payment_date=_optional_date(row.get("payment_date"), "payment_date"),
        settlement_date=_optional_date(row.get("settlement_date"), "settlement_date"),
        ratio_num=ratio_num,
        ratio_den=ratio_den,
        cash_amount=cash_amount,
        currency=currency,
        source_document_id=_text(row.get("source_document_id")),
        source_document=_text(row.get("source_document")),
        source_url=_text(row.get("source_url")),
        source_sha256=_source_sha(row.get("source_sha256")),
        notes=_text(row.get("notes")) or "",
    )
    blockers: list[ApprovalBlocker] = []
    if event.source_sha256 is None:
        blockers.append(_blocker("MISSING_SOURCE_PROVENANCE", "source SHA-256 is required", event))
    if not event.source_url:
        blockers.append(
            _blocker("MISSING_SOURCE_PROVENANCE", "source URL is required", event)
        )
    if not _text(row.get("source_document_id")):
        blockers.append(
            _blocker("MISSING_SOURCE_PROVENANCE", "source_document_id is required", event)
        )
    if event.source_document and event.source_document != event.source_document_id:
        raise CorporateActionValidationError(
            "source_document must exactly equal source_document_id"
        )
    if event.event_type == EventType.CASH_DISTRIBUTION:
        if event.record_date is None:
            blockers.append(_blocker("MISSING_RECORD_DATE", "record_date is required", event))
        if event.ex_date is None:
            blockers.append(_blocker("MISSING_EX_DATE", "ex_date is required", event))
        if event.payment_date is None:
            blockers.append(_blocker("MISSING_PAYMENT_DATE", "payment_date is required", event))
        if event.cash_amount is None:
            blockers.append(_blocker("MISSING_CASH_AMOUNT", "cash_amount is required", event))
        if event.currency != "KRW":
            blockers.append(_blocker("MISSING_CURRENCY", "currency must be KRW", event))
        if event.ex_date and event.record_date and event.ex_date > event.record_date:
            raise CorporateActionValidationError("ex_date must not be after record_date")
        if event.record_date and event.payment_date and event.record_date > event.payment_date:
            raise CorporateActionValidationError("payment_date must not precede record_date")
    if event.event_type in {EventType.SPLIT, EventType.REVERSE_SPLIT} and (
        event.ratio_num is None or event.ratio_den is None
    ):
        blockers.append(_blocker("MISSING_RATIO", "ratio_num and ratio_den are required", event))
    if (
        event.event_type == EventType.SPLIT
        and event.ratio_num
        and event.ratio_den
        and event.ratio_num <= event.ratio_den
    ):
        raise CorporateActionValidationError("SPLIT ratio must be greater than 1")
    if (
        event.event_type == EventType.REVERSE_SPLIT
        and event.ratio_num
        and event.ratio_den
        and event.ratio_num >= event.ratio_den
    ):
        raise CorporateActionValidationError("REVERSE_SPLIT ratio must be less than 1")
    if event.event_type in {EventType.CASH_SETTLEMENT, EventType.REDEMPTION}:
        if event.settlement_date is None:
            blockers.append(_blocker("MISSING_SETTLEMENT_DATE", "settlement_date is required", event))
        if event.cash_amount is None:
            blockers.append(_blocker("MISSING_SETTLEMENT", "cash settlement amount is required", event))
        if event.currency != "KRW":
            blockers.append(_blocker("MISSING_CURRENCY", "currency must be KRW", event))
        if event.settlement_date and event.event_date > event.settlement_date:
            raise CorporateActionValidationError("settlement_date must not precede event_date")
    return event, blockers


def _canonical_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, Enum):
        return value.value
    return str(value)


def _event_payload(event: CorporateAction) -> dict[str, str | None]:
    return {
        column: _canonical_value(getattr(event, column))
        for column in CSV_COLUMNS
    }


def canonical_ledger_sha256(events: Iterable[CorporateAction]) -> str:
    payload = [_event_payload(event) for event in sorted(events, key=lambda item: item.event_id)]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(path: Path) -> CoverageManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorporateActionValidationError(f"invalid corporate-action manifest: {path}") from exc
    required = {
        "manifest_version",
        "ledger_file",
        "ledger_sha256",
        "verification_period",
        "verification_tickers",
        "source_documents",
        "coverage_status",
    }
    missing = required - set(raw)
    if missing:
        raise CorporateActionValidationError(f"manifest fields missing: {sorted(missing)}")
    period = raw["verification_period"]
    if not isinstance(period, dict):
        raise CorporateActionValidationError("manifest verification_period must be an object")
    start = _optional_date(period.get("start"), "verification start")
    end = _optional_date(period.get("end"), "verification end")
    if start and end and start > end:
        raise CorporateActionValidationError("manifest verification period is reversed")
    tickers = tuple(sorted({_ticker(item) for item in raw["verification_tickers"]}))
    documents = []
    document_ids: set[str] = set()
    for item in raw["source_documents"]:
        if not isinstance(item, dict):
            raise CorporateActionValidationError("manifest source_documents entries must be objects")
        document_id = str(item.get("document_id", "")).strip()
        if document_id in document_ids:
            raise CorporateActionValidationError(f"duplicate manifest document_id: {document_id}")
        document_ids.add(document_id)
        source_url = str(item.get("source_url", "")).strip()
        sha = _source_sha(item.get("sha256"))
        if not document_id or not source_url or sha is None:
            raise CorporateActionValidationError("manifest source documents need id, URL, and SHA-256")
        documents.append(SourceDocument(document_id, source_url, sha))
    ledger_sha = str(raw["ledger_sha256"]).strip().lower()
    if not SHA256_RE.fullmatch(ledger_sha):
        raise CorporateActionValidationError("manifest ledger_sha256 must be a SHA-256 hex string")
    return CoverageManifest(
        int(raw["manifest_version"]),
        str(raw["ledger_file"]),
        ledger_sha,
        start,
        end,
        tickers,
        tuple(documents),
        str(raw["coverage_status"]).strip().upper(),
        str(raw.get("notes", "")).strip(),
    )


def load_corporate_action_ledger(
    path: Path | str = DEFAULT_LEDGER_PATH,
    manifest_path: Path | str = DEFAULT_MANIFEST_PATH,
) -> CorporateActionLedger:
    """Load and strictly normalize a manually sourced corporate-action ledger.

    Malformed rows raise.  Approval-data gaps return in ``blockers`` so callers
    cannot mistake an empty or incomplete template for verified absence of events.
    """
    ledger_path = Path(path)
    manifest = _manifest(Path(manifest_path))
    if not ledger_path.exists():
        raise FileNotFoundError(f"corporate-action ledger not found: {ledger_path}")
    with ledger_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        missing = COMMON_COLUMNS - fields
        unknown = fields - set(CSV_COLUMNS)
        if missing:
            raise CorporateActionValidationError(f"ledger fields missing: {sorted(missing)}")
        if unknown:
            raise CorporateActionValidationError(f"unknown ledger fields: {sorted(unknown)}")
        events = []
        blockers: list[ApprovalBlocker] = []
        seen_ids: set[str] = set()
        for row in reader:
            if not any(str(value or "").strip() for value in row.values()):
                continue
            event, row_blockers = _event_from_row(row)
            if event.event_id in seen_ids:
                raise CorporateActionValidationError(f"duplicate event_id: {event.event_id}")
            seen_ids.add(event.event_id)
            events.append(event)
            blockers.extend(row_blockers)
    normalized_events = tuple(sorted(events, key=lambda item: (item.event_date, item.event_id)))
    ledger_sha = canonical_ledger_sha256(normalized_events)
    if Path(manifest.ledger_file).name != ledger_path.name:
        blockers.append(
            _blocker(
                "MANIFEST_LEDGER_PATH_MISMATCH",
                f"manifest ledger_file={manifest.ledger_file!r} does not name {ledger_path.name!r}",
            )
        )
    if ledger_sha != manifest.ledger_sha256:
        blockers.append(
            _blocker(
                "LEDGER_HASH_MISMATCH",
                f"manifest ledger_sha256={manifest.ledger_sha256} does not match {ledger_sha}",
            )
        )
    if not normalized_events:
        blockers.append(
            _blocker("EMPTY_LEDGER", "ledger is header-only; absence of events is not verified")
        )
    if manifest.coverage_status != "VERIFIED":
        blockers.append(
            _blocker(
                "INCOMPLETE_COVERAGE",
                "manifest coverage_status is not VERIFIED; update period, tickers, and sources",
            )
        )
    if manifest.verification_start is None or manifest.verification_end is None:
        blockers.append(_blocker("INCOMPLETE_COVERAGE", "verification period is incomplete"))
    if not manifest.verification_tickers:
        blockers.append(_blocker("INCOMPLETE_COVERAGE", "verification_tickers is empty"))
    if not manifest.source_documents:
        blockers.append(_blocker("INCOMPLETE_COVERAGE", "source_documents is empty"))
    covered = set(manifest.verification_tickers)
    document_map = {item.document_id: item.sha256 for item in manifest.source_documents}
    document_details = {item.document_id: item for item in manifest.source_documents}
    for event in normalized_events:
        if event.ticker not in covered:
            blockers.append(_blocker("TICKER_OUTSIDE_COVERAGE", "ticker is not in manifest coverage", event))
        if manifest.verification_start and event.event_date < manifest.verification_start:
            blockers.append(_blocker("EVENT_OUTSIDE_COVERAGE", "event precedes manifest coverage", event))
        if manifest.verification_end and event.event_date > manifest.verification_end:
            blockers.append(_blocker("EVENT_OUTSIDE_COVERAGE", "event exceeds manifest coverage", event))
        if event.source_document_id and event.source_document_id not in document_map:
            raise CorporateActionValidationError(
                f"unknown source document ID: {event.source_document_id}"
            )
        if event.source_document_id and event.source_document_id in document_details:
            document = document_details[event.source_document_id]
            if event.source_url != document.source_url:
                raise CorporateActionValidationError(
                    f"source URL mismatch for document ID: {event.source_document_id}"
                )
            if event.source_sha256 != document.sha256:
                raise CorporateActionValidationError(
                    f"source SHA mismatch for document ID: {event.source_document_id}"
                )
    for event in normalized_events:
        if event.event_type == EventType.DELISTING:
            settled = any(
                candidate.ticker == event.ticker
                and candidate.event_type in {EventType.CASH_SETTLEMENT, EventType.REDEMPTION}
                and candidate.event_date >= event.event_date
                for candidate in normalized_events
            )
            if not settled:
                blockers.append(
                    _blocker(
                        "MISSING_SETTLEMENT",
                        "delisting has no authoritative cash settlement or redemption event",
                        event,
                    )
                )
    return CorporateActionLedger(
        normalized_events,
        manifest,
        ledger_sha,
        tuple(_unique_blockers(blockers)),
    )


def create_distribution_receivable(
    action: CorporateAction,
    quantity: int,
    *,
    held_on_record_date: bool,
    held_on_ex_date: bool = True,
) -> DistributionReceivable | None:
    """Create a receivable only for documented record/ex-date entitlement."""
    if action.event_type != EventType.CASH_DISTRIBUTION:
        raise CorporateActionValidationError("action is not CASH_DISTRIBUTION")
    if quantity <= 0:
        raise CorporateActionValidationError("distribution quantity must be positive")
    if not held_on_record_date or not held_on_ex_date:
        return None
    if action.payment_date is None or action.cash_amount is None:
        raise CorporateActionBlocked(
            _blocker("MISSING_PAYMENT_DATE", "distribution payment date and amount are required", action)
        )
    return DistributionReceivable(
        action.event_id,
        action.ticker,
        quantity,
        action.cash_amount * quantity,
        action.payment_date,
    )


def process_distribution_payment(
    receivable: DistributionReceivable,
    payment_date: str | date | datetime,
) -> tuple[DistributionReceivable, Decimal]:
    """Pay on or after the documented date; the caller supplies any holiday mapping."""
    if receivable.paid:
        return receivable, Decimal(0)
    if _date(payment_date, "payment date") < receivable.payment_date:
        return receivable, Decimal(0)
    return replace(receivable, paid=True), receivable.amount


def process_pending_receivables(
    receivables: Sequence[DistributionReceivable],
    payment_date: str | date | datetime,
) -> tuple[tuple[DistributionReceivable, ...], Decimal]:
    updated = []
    total = Decimal(0)
    for receivable in receivables:
        paid, cash = process_distribution_payment(receivable, payment_date)
        updated.append(paid)
        total += cash
    return tuple(updated), total


def transform_split_holding(
    holding: HoldingState, action: CorporateAction
) -> HoldingState:
    """Apply explicit split arithmetic while preserving total cost basis."""
    if action.event_type not in {EventType.SPLIT, EventType.REVERSE_SPLIT}:
        raise CorporateActionValidationError("action is not a split or reverse split")
    if holding.ticker != action.ticker:
        raise CorporateActionValidationError("holding and corporate action ticker differ")
    if holding.last_event_date and action.event_date < holding.last_event_date:
        raise CorporateActionBlocked(
            _blocker("EVENT_OUT_OF_ORDER", "corporate action is earlier than holding state", action)
        )
    if holding.lifecycle == LifecycleState.SETTLED:
        raise CorporateActionBlocked(
            _blocker("INVALID_LIFECYCLE", "split cannot be applied after settlement", action)
        )
    if holding.lifecycle == LifecycleState.DELISTED_UNSETTLED:
        raise CorporateActionBlocked(
            _blocker("INVALID_LIFECYCLE", "split cannot be applied after delisting", action)
        )
    if action.ratio_num is None or action.ratio_den is None:
        raise CorporateActionBlocked(_blocker("MISSING_RATIO", "split ratio is unresolved", action))
    if action.ratio_num <= 0 or action.ratio_den <= 0:
        raise CorporateActionValidationError("split ratio components must be positive")
    if action.event_type == EventType.SPLIT and action.ratio_num <= action.ratio_den:
        raise CorporateActionValidationError("SPLIT ratio must be greater than 1")
    if action.event_type == EventType.REVERSE_SPLIT and action.ratio_num >= action.ratio_den:
        raise CorporateActionValidationError("REVERSE_SPLIT ratio must be less than 1")
    numerator = holding.quantity * action.ratio_num
    if numerator % action.ratio_den:
        raise CorporateActionBlocked(
            _blocker(
                "FRACTIONAL_SHARE_UNRESOLVED",
                "split would create fractional shares without authoritative cash-in-lieu",
                action,
            )
        )
    return replace(
        holding,
        quantity=numerator // action.ratio_den,
        last_event_date=action.event_date,
    )


def process_settlement(
    holding: HoldingState,
    action: CorporateAction,
    processing_date: str | date | datetime,
) -> SettlementResult:
    """Process authoritative settlement without using a market price."""
    if action.event_type not in {EventType.CASH_SETTLEMENT, EventType.REDEMPTION}:
        raise CorporateActionValidationError("action is not a settlement or redemption")
    if holding.ticker != action.ticker:
        raise CorporateActionValidationError("holding and corporate action ticker differ")
    if holding.last_event_date and action.event_date < holding.last_event_date:
        raise CorporateActionBlocked(
            _blocker("EVENT_OUT_OF_ORDER", "corporate action is earlier than holding state", action)
        )
    if holding.lifecycle == LifecycleState.SETTLED:
        if holding.quantity > 0:
            raise CorporateActionBlocked(
                _blocker("SETTLED_HOLDING_POSITIVE", "SETTLED holding must have zero quantity", action)
            )
        return SettlementResult(holding, Decimal(0))
    if holding.lifecycle != LifecycleState.DELISTED_UNSETTLED:
        raise CorporateActionBlocked(
            _blocker("INVALID_LIFECYCLE", "settlement requires DELISTED_UNSETTLED", action)
        )
    if action.settlement_date is None or action.cash_amount is None:
        raise CorporateActionBlocked(_blocker("MISSING_SETTLEMENT", "settlement is incomplete", action))
    target_date = _date(processing_date, "settlement processing date")
    if target_date < action.settlement_date:
        return SettlementResult(holding, Decimal(0))
    cash_paid = Decimal(holding.quantity) * action.cash_amount
    settled = replace(
        holding,
        quantity=0,
        total_cost_basis=Decimal(0),
        lifecycle=LifecycleState.SETTLED,
        last_event_date=action.event_date,
    )
    return SettlementResult(settled, cash_paid)


def apply_lifecycle_event(holding: HoldingState, action: CorporateAction) -> HoldingState:
    """Apply one explicit suspension, delisting, or settlement transition."""
    if holding.ticker != action.ticker:
        raise CorporateActionValidationError("holding and corporate action ticker differ")
    if holding.last_event_date and action.event_date < holding.last_event_date:
        raise CorporateActionBlocked(_blocker("EVENT_OUT_OF_ORDER", "lifecycle event is out of order", action))
    state = holding.lifecycle
    next_state = state
    if action.event_type == EventType.SUSPENSION_START:
        if state != LifecycleState.ACTIVE:
            raise CorporateActionBlocked(_blocker("INVALID_LIFECYCLE", "suspension start requires ACTIVE", action))
        next_state = LifecycleState.SUSPENDED
    elif action.event_type == EventType.SUSPENSION_END:
        if state != LifecycleState.SUSPENDED:
            raise CorporateActionBlocked(_blocker("INVALID_LIFECYCLE", "suspension end requires SUSPENDED", action))
        next_state = LifecycleState.ACTIVE
    elif action.event_type == EventType.DELISTING:
        if state not in {LifecycleState.ACTIVE, LifecycleState.SUSPENDED}:
            raise CorporateActionBlocked(_blocker("INVALID_LIFECYCLE", "delisting requires unsettled holding", action))
        next_state = LifecycleState.DELISTED_UNSETTLED
    elif action.event_type in {EventType.CASH_SETTLEMENT, EventType.REDEMPTION}:
        return process_settlement(holding, action, action.settlement_date or action.event_date).holding
    return replace(holding, lifecycle=next_state, last_event_date=action.event_date)


def _group_events_by_date(
    events: Sequence[CorporateAction],
) -> tuple[tuple[date, tuple[CorporateAction, ...]], ...]:
    groups: list[tuple[date, tuple[CorporateAction, ...]]] = []
    for event in events:
        if groups and groups[-1][0] == event.event_date:
            groups[-1] = (groups[-1][0], groups[-1][1] + (event,))
        else:
            groups.append((event.event_date, (event,)))
    return tuple(groups)


def replay_lifecycle(
    ticker: str,
    events: Sequence[CorporateAction],
) -> LifecycleReplay:
    """Replay one ticker's ordered lifecycle before it can be approval-valid.

    No caller-created opening state is accepted. A sequence may begin with a new
    suspension or delisting, but an event requiring prior state is blocked.
    """
    ticker = _ticker(ticker)
    state = HoldingState(ticker, 0, Decimal(0))
    blockers: list[ApprovalBlocker] = []
    prior_lifecycle = False
    needs_opening = {
        EventType.SUSPENSION_END,
        EventType.CASH_SETTLEMENT,
        EventType.REDEMPTION,
    }
    ordered = sorted(events, key=lambda item: (item.event_date, item.event_id))
    lifecycle_types = {
        EventType.SUSPENSION_START,
        EventType.SUSPENSION_END,
        EventType.DELISTING,
        EventType.CASH_SETTLEMENT,
        EventType.REDEMPTION,
    }
    for event_date, same_day in _group_events_by_date(ordered):
        transitions = [event for event in same_day if event.event_type in lifecycle_types]
        if len(transitions) > 1:
            blockers.append(
                _blocker(
                    "AMBIGUOUS_SAME_DATE_LIFECYCLE",
                    f"{len(transitions)} lifecycle transitions share {event_date.isoformat()}",
                    transitions[0],
                )
            )
            break
        for event in same_day:
            if event.ticker != ticker:
                raise CorporateActionValidationError("replay contains a different ticker")
            if event.event_type in needs_opening and not prior_lifecycle:
                blockers.append(
                    _blocker(
                        "OPENING_LIFECYCLE_UNVERIFIED",
                        "event requires a preceding in-ledger lifecycle event",
                        event,
                    )
                )
                break
            try:
                if event.event_type in {EventType.SPLIT, EventType.REVERSE_SPLIT}:
                    state = transform_split_holding(state, event)
                elif event.event_type in lifecycle_types:
                    state = apply_lifecycle_event(state, event)
                    prior_lifecycle = True
            except CorporateActionBlocked as exc:
                blockers.append(exc.blocker)
                break
        if blockers:
            break
    return LifecycleReplay(ticker, state.lifecycle, state, tuple(_unique_blockers(blockers)))


def final_approval_report(
    ledger: CorporateActionLedger,
    final_holdings: Mapping[str, HoldingState] | None = None,
    *,
    stale_tickers: Iterable[str] = (),
) -> ApprovalReport:
    """Return the final approval/blocker structure without valuing any position."""
    return ledger.approval_report(
        final_holdings,
        stale_tickers=stale_tickers,
    )
