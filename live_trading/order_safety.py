"""Fail-closed single-run and write-ahead intent safeguards for live ETF orders.

This is deliberately conservative: an ambiguous submission, cancellation, or crash
requires broker-side reconciliation before the marker may be removed.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile


class OrderSafetyBlocked(RuntimeError):
    """A live order or recovery is unsafe without manual reconciliation."""


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def exclusive_runner_lock(path: Path):
    """Serialize runs sharing runtime_state, including the pre-market wait."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OrderSafetyBlocked("이미 다른 데일리 러너가 실행 중입니다.") from exc
        yield
    finally:
        os.close(fd)


class OrderIntentJournal:
    """Durable pre-submit marker; no network calls and no automatic recovery."""

    def __init__(self, path: Path):
        self.path = path
        self.records: list[dict] = []

    def assert_clear(self) -> None:
        if self.path.exists():
            raise OrderSafetyBlocked(
                f"미해결 실주문 intent: {self.path}. "
                "브로커 주문/체결/취소 내역을 대조한 뒤에만 수동 해소하세요."
            )

    def _persist(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".order-intent-", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "records": self.records}, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_name, self.path)
            _fsync_dir(self.path.parent)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def before_submit(self, *, side: str, ticker: str, qty: int, attempt: int) -> int:
        self.records.append({
            "side": side, "ticker": ticker, "qty": qty, "attempt": attempt,
            "state": "UNKNOWN", "order_id": "",
            "time_utc": datetime.now(timezone.utc).isoformat(),
        })
        self._persist()  # durability must precede broker transmission
        return len(self.records) - 1

    def mark_accepted(self, index: int, order_id: str) -> None:
        if not str(order_id).strip():
            raise OrderSafetyBlocked("주문 API가 접수번호를 반환하지 않았습니다.")
        self.records[index]["state"] = "ACKNOWLEDGED"
        self.records[index]["order_id"] = str(order_id).strip()
        self._persist()

    def mark_unknown(self, index: int) -> None:
        self.records[index]["state"] = "UNKNOWN"
        self._persist()

    def clear_if_verified(self, results: list[dict]) -> bool:
        """Clear only after persisted state + broker-confirmed terminal outcomes.

        A cancellation submission or missing order is NOT a terminal outcome.
        """
        if not self.path.exists():
            return True
        if not self.records:
            return False
        used: set[int] = set()
        for record in self.records:
            if record["state"] != "ACKNOWLEDGED" or not record["order_id"]:
                return False
            match = next(
                ((i, row) for i, row in enumerate(results)
                 if i not in used
                 and str(row.get("order_id", "")).strip() == record["order_id"]
                 and str(row.get("side", "")) == record["side"]
                 and str(row.get("ticker", "")) == record["ticker"]
                 and int(row.get("attempt", 1)) == record["attempt"]),
                None,
            )
            if match is None:
                return False
            i, row = match
            used.add(i)
            requested = int(row.get("requested_qty", row.get("qty", 0)))
            filled = int(row.get("filled_qty", 0))
            fully_filled = (\n                requested > 0 and filled >= requested\n                and bool(row.get("is_filled")) and bool(row.get("status_verified"))\n            )
            cancelled = (
                bool(row.get("cancel_confirmed"))
                and bool(row.get("status_verified"))
                and int(row.get("remaining_qty", -1)) == 0
            )
            if not (fully_filled or cancelled):
                return False
        self.path.unlink()
        _fsync_dir(self.path.parent)
        return True
