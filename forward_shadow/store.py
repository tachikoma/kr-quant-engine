"""Independent runtime root: lock, append-only event ledger, atomic state.

The ledger is the source of truth.  Every business event carries a
``state_patch``; a final ``COMMIT`` event carries the digest of the state that
results from replaying all patches up to that point.  A crash between the
COMMIT append and the state write is therefore recoverable by replay, and any
uncommitted tail events are explicitly marked ``ABORT`` without being applied.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MANIFEST_NAME = "manifest.json"
STATE_NAME = "state.json"
LEDGER_NAME = "events.jsonl"
GENESIS_NAME = "genesis.json"
LOCK_NAME = ".shadow.lock"
SCHEMA_VERSION = 1
ZERO_HASH = "0" * 64


class IntegrityError(RuntimeError):
    """Raised when ledger/state consistency checks fail (fail-closed)."""


@dataclass(frozen=True)
class RuntimePaths:
    root: Path

    @property
    def manifest(self) -> Path:
        return self.root / MANIFEST_NAME

    @property
    def state(self) -> Path:
        return self.root / STATE_NAME

    @property
    def ledger(self) -> Path:
        return self.root / LEDGER_NAME

    @property
    def genesis(self) -> Path:
        return self.root / GENESIS_NAME

    @property
    def lock(self) -> Path:
        return self.root / LOCK_NAME

    @property
    def snapshots(self) -> Path:
        return self.root / "snapshots"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    def ensure(self) -> None:
        for path in (self.root, self.snapshots, self.reports, self.logs, self.cache):
            path.mkdir(parents=True, exist_ok=True)


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Non-blocking exclusive OS file lock (same pattern as the live runner)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Forward Shadow already active: concurrent execution blocked") from exc
        yield
    finally:
        os.close(fd)


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def state_digest(state: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(_canonical(state)).hexdigest()


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def _event_hash(event: dict[str, Any]) -> str:
    import hashlib

    body = {key: value for key, value in event.items() if key != "hash"}
    return hashlib.sha256(_canonical(body)).hexdigest()


def initial_state(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "last_processed_date": None,
        "processed_dates": [],
        "forward_day_index": -1,
        "cash": float(manifest["initial_capital"]),
        "initial_equity": float(manifest["initial_capital"]),
        "holdings": {str(k): int(v) for k, v in manifest["initial_holdings"].items()},
        "cost_basis": {},
        "peak_equity": float(manifest["initial_capital"]),
        "pending_signal": None,
        "carry": [],
        "equity_curve": [],
        "last_valid_prices": {},
        "last_event_seq": 0,
        "status": "INITIALIZED",
    }


class ShadowStore:
    """Append-only ledger plus atomically written, replayable state."""

    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths
        self._events: list[dict[str, Any]] = []

    # ── lifecycle ──────────────────────────────────────────────────────────

    def initialize(self, manifest: dict[str, Any]) -> dict[str, Any]:
        self.paths.ensure()
        if self.paths.manifest.exists():
            raise FileExistsError(f"Manifest already exists: {self.paths.manifest}")
        atomic_write_json(self.paths.manifest, manifest)
        state = initial_state(manifest)
        atomic_write_json(self.paths.genesis, state)
        atomic_write_json(self.paths.state, state)
        self._events = []
        self.append_event(
            date="",
            event_type="GENESIS",
            payload={"initial_state_digest": state_digest(state)},
            state_patch={},
        )
        self.commit(date="", state=state, status="INITIALIZED")
        return state

    def load(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Load and reconcile state; raises IntegrityError on any inconsistency."""
        if not self.paths.manifest.exists():
            raise FileNotFoundError(f"Shadow runtime not initialized: {self.paths.manifest}")
        read_json(self.paths.manifest)  # existence/JSON validity check
        genesis = read_json(self.paths.genesis)
        events = read_jsonl(self.paths.ledger)
        self._events = events
        integrity = self.verify_chain(events)
        if not integrity["ok"]:
            raise IntegrityError(f"ledger hash chain broken: {integrity}")

        commit_events = [e for e in events if e["type"] == "COMMIT"]
        if not commit_events:
            raise IntegrityError("ledger has no COMMIT event")
        last_commit = commit_events[-1]
        commit_seq = int(last_commit["seq"])

        aborted_ranges = [
            (
                int(e.get("payload", {}).get("orphan_from_seq", e["seq"])),
                int(e.get("payload", {}).get("orphan_through_seq", e["seq"])),
            )
            for e in events
            if e["type"] == "ABORT"
        ]

        replayed = json.loads(json.dumps(genesis))
        for event in events:
            seq = int(event["seq"])
            if seq > commit_seq:
                continue
            if any(lo <= seq <= hi for lo, hi in aborted_ranges):
                continue
            replayed.update(event.get("state_patch", {}))
        replayed["last_event_seq"] = commit_seq

        expected_digest = last_commit.get("payload", {}).get("state_digest")
        if state_digest(replayed) != expected_digest:
            raise IntegrityError(
                "state replay digest mismatch: "
                f"expected={expected_digest}, replayed={state_digest(replayed)}"
            )

        on_disk = read_json(self.paths.state)
        if on_disk != replayed:
            # Crash after COMMIT but before the atomic state write: repair by
            # rewriting the replayed state.  This is the only safe recovery.
            atomic_write_json(self.paths.state, replayed)

        orphans = [e for e in events if int(e["seq"]) > commit_seq]
        if orphans:
            max_orphan = max(int(e["seq"]) for e in orphans)
            already_aborted = any(
                e["type"] == "ABORT"
                and int(e["seq"]) > commit_seq
                and int(e.get("payload", {}).get("orphan_through_seq", -1)) >= max_orphan
                for e in events
            )
            if not already_aborted:
                self.append_event(
                    date=str(last_commit.get("date", "")),
                    event_type="ABORT",
                    payload={
                        "reason": "uncommitted tail events discarded by recovery",
                        "orphan_from_seq": min(int(e["seq"]) for e in orphans),
                        "orphan_through_seq": max_orphan,
                        "orphan_count": len(orphans),
                    },
                    state_patch={},
                )
        return replayed, {
            **integrity,
            "commit_seq": commit_seq,
            "last_commit_seq": commit_seq,
            "orphan_count": len(orphans),
            "state_digest": expected_digest,
        }

    # ── ledger ─────────────────────────────────────────────────────────────

    @staticmethod
    def verify_chain(events: list[dict[str, Any]]) -> dict[str, Any]:
        prev = ZERO_HASH
        for index, event in enumerate(events):
            if int(event.get("seq", -1)) != index:
                return {"ok": False, "reason": f"seq gap at index {index}"}
            if event.get("prev_hash") != prev:
                return {"ok": False, "reason": f"prev_hash mismatch at seq {index}"}
            if _event_hash(event) != event.get("hash"):
                return {"ok": False, "reason": f"event hash mismatch at seq {index}"}
            prev = event["hash"]
        return {"ok": True, "events": len(events), "last_hash": prev}

    def append_event(
        self,
        *,
        date: str,
        event_type: str,
        payload: dict[str, Any],
        state_patch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        seq = len(self._events)
        prev_hash = self._events[-1]["hash"] if self._events else ZERO_HASH
        event: dict[str, Any] = {
            "seq": seq,
            "event_id": f"{date}:{event_type}:{seq}",
            "date": str(date),
            "type": event_type,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
            "state_patch": state_patch or {},
            "prev_hash": prev_hash,
        }
        event["hash"] = _event_hash(event)
        line = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        fd = os.open(self.paths.ledger, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        self._events.append(event)
        return event

    def commit(self, *, date: str, state: dict[str, Any], status: str) -> dict[str, Any]:
        next_seq = len(self._events)
        state = dict(state)
        state["last_event_seq"] = next_seq
        state["last_processed_date"] = date
        state["status"] = status
        digest = state_digest(state)
        event = self.append_event(
            date=date,
            event_type="COMMIT",
            payload={"state_digest": digest, "status": status},
            state_patch={
                "last_event_seq": next_seq,
                "last_processed_date": date,
                "status": status,
            },
        )
        atomic_write_json(self.paths.state, state)
        return event

    def is_processed(self, date: str) -> bool:
        state = read_json(self.paths.state)
        return date in state.get("processed_dates", [])

    def verify_integrity(self) -> dict[str, Any]:
        events = read_jsonl(self.paths.ledger)
        chain = self.verify_chain(events)
        state = read_json(self.paths.state)
        commits = [e for e in events if e["type"] == "COMMIT"]
        last_seq = commits[-1]["seq"] if commits else 0
        return {
            **chain,
            "state_seq": state.get("last_event_seq"),
            "last_commit_seq": last_seq,
            "consistent": bool(chain["ok"] and state.get("last_event_seq") == last_seq),
        }
