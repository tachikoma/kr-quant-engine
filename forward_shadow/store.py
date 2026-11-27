"""Independent runtime root: lock, append-only event ledger, atomic state.

The ledger is the source of truth.  Every business event carries a
``state_patch``; a final ``COMMIT`` event carries the digest of the state that
results from replaying all patches up to that point.  A crash between the
COMMIT append and the state write is therefore recoverable by replay, and any
uncommitted tail events are explicitly marked ``ABORT`` without being applied.
The GENESIS event binds the manifest and initial state to the chain.
"""

from __future__ import annotations

import fcntl
import hashlib
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
WARMUP_NAME = "warmup.json"
LOCK_NAME = ".shadow.lock"
SCHEMA_VERSION = 2
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
    def warmup(self) -> Path:
        return self.root / WARMUP_NAME

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
    return hashlib.sha256(_canonical(state)).hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
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


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(
        path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse the ledger, ignoring a torn (newline-less) final write.

    An event is durable only once its terminating newline is on disk, so a torn
    final line is by definition uncommitted.  Any other parse error fails closed.
    """
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    complete = text[: text.rfind("\n") + 1]
    try:
        return [json.loads(line) for line in complete.split("\n") if line.strip()]
    except ValueError as exc:
        raise IntegrityError(f"ledger line unreadable: {exc}") from exc


def _event_hash(event: dict[str, Any]) -> str:
    body = {key: value for key, value in event.items() if key != "hash"}
    return hashlib.sha256(_canonical(body)).hexdigest()


def initial_state(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "last_processed_date": None,
        "processed_dates": [],
        "observations": {},
        "snapshot_hashes": {},
        "extra_holidays": [],
        "cash": float(manifest["initial_capital"]),
        "initial_equity": float(manifest["initial_capital"]),
        "holdings": {},
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
        self.events: list[dict[str, Any]] = []

    # ── lifecycle ──────────────────────────────────────────────────────────

    def initialize(self, manifest: dict[str, Any]) -> dict[str, Any]:
        self.paths.ensure()
        if self.paths.manifest.exists() or self.paths.ledger.exists():
            raise FileExistsError(f"Runtime already initialized: {self.paths.root}")
        atomic_write_json(self.paths.manifest, manifest)
        state = initial_state(manifest)
        atomic_write_json(self.paths.genesis, state)
        atomic_write_json(self.paths.state, state)
        self.events = []
        self.append_event(
            date="",
            event_type="GENESIS",
            payload={
                "initial_state_digest": state_digest(state),
                "manifest_sha256": manifest["manifest_sha256"],
            },
        )
        self.commit(date="", state=state, status="INITIALIZED")
        return state

    def load(self, *, repair: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
        """Replay and reconcile; raises IntegrityError on any inconsistency.

        With ``repair=False`` nothing on disk is modified.
        """
        if not self.paths.manifest.exists():
            raise FileNotFoundError(f"Shadow runtime not initialized: {self.paths.manifest}")
        try:
            manifest = read_json(self.paths.manifest)
            genesis = read_json(self.paths.genesis)
        except (OSError, ValueError) as exc:
            raise IntegrityError(f"manifest/genesis unreadable: {exc}") from exc
        events = read_jsonl(self.paths.ledger)
        self.events = events
        integrity = self.verify_chain(events)
        if not integrity["ok"]:
            raise IntegrityError(f"ledger hash chain broken: {integrity}")
        if not events or events[0]["type"] != "GENESIS":
            raise IntegrityError("ledger does not start with GENESIS")
        bound = events[0]["payload"]
        if bound.get("manifest_sha256") != manifest.get("manifest_sha256"):
            raise IntegrityError("manifest is not the one bound at genesis")
        if bound.get("initial_state_digest") != state_digest(genesis):
            raise IntegrityError("genesis state differs from the one bound at genesis")

        commit_events = [e for e in events if e["type"] == "COMMIT"]
        if not commit_events:
            raise IntegrityError("ledger has no COMMIT event")
        last_commit = commit_events[-1]
        commit_seq = int(last_commit["seq"])

        aborted = self.aborted_seqs(events)
        replayed = json.loads(json.dumps(genesis))
        for event in events[: commit_seq + 1]:
            if int(event["seq"]) in aborted:
                continue
            replayed.update(event.get("state_patch", {}))
        replayed["last_event_seq"] = commit_seq

        expected_digest = last_commit.get("payload", {}).get("state_digest")
        if state_digest(replayed) != expected_digest:
            raise IntegrityError(
                "state replay digest mismatch: "
                f"expected={expected_digest}, replayed={state_digest(replayed)}"
            )

        try:
            on_disk = read_json(self.paths.state)
        except (OSError, ValueError):
            on_disk = None
        state_matches = on_disk == replayed
        orphans = [
            int(e["seq"])
            for e in events[commit_seq + 1 :]
            if e["type"] != "ABORT" and int(e["seq"]) not in aborted
        ]
        torn = self._has_torn_tail()
        if repair:
            if torn:
                self._truncate_torn_tail()
            if not state_matches:
                # Crash after COMMIT but before the atomic state write: rewriting
                # the replayed state is the only safe recovery.
                atomic_write_json(self.paths.state, replayed)
            if orphans:
                self.abort(
                    min(orphans),
                    "uncommitted tail events discarded by recovery",
                    date=str(last_commit.get("date", "")),
                )
        return replayed, {
            **integrity,
            "commit_seq": commit_seq,
            "last_commit_seq": commit_seq,
            "orphan_count": len(orphans),
            "torn_tail": torn,
            "state_file_matches": state_matches,
            "state_digest": expected_digest,
        }

    # ── ledger ─────────────────────────────────────────────────────────────

    @staticmethod
    def aborted_seqs(events: list[dict[str, Any]]) -> set[int]:
        result: set[int] = set()
        for event in events:
            if event["type"] == "ABORT":
                payload = event["payload"]
                result.update(
                    range(
                        int(payload["orphan_from_seq"]),
                        int(payload["orphan_through_seq"]) + 1,
                    )
                )
        return result

    def committed_events(self) -> list[dict[str, Any]]:
        commits = [int(e["seq"]) for e in self.events if e["type"] == "COMMIT"]
        if not commits:
            return []
        aborted = self.aborted_seqs(self.events)
        return [e for e in self.events[: commits[-1] + 1] if int(e["seq"]) not in aborted]

    def _has_torn_tail(self) -> bool:
        data = self.paths.ledger.read_bytes()
        return bool(data) and not data.endswith(b"\n")

    def _truncate_torn_tail(self) -> None:
        data = self.paths.ledger.read_bytes()
        os.truncate(self.paths.ledger, data.rfind(b"\n") + 1)

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
        seq = len(self.events)
        prev_hash = self.events[-1]["hash"] if self.events else ZERO_HASH
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
        # Detach from caller-owned objects so later mutation cannot alter history,
        # with the same key order a reload from disk will produce.
        event = json.loads(
            json.dumps(event, ensure_ascii=False, allow_nan=False, sort_keys=True)
        )
        event["hash"] = _event_hash(event)
        line = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        data = line.encode("utf-8")
        fd = os.open(self.paths.ledger, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            while data:
                data = data[os.write(fd, data) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        self.events.append(event)
        return event

    def abort(self, from_seq: int, reason: str, *, date: str = "") -> None:
        """Mark every event from ``from_seq`` to the tail as never applied."""
        through = len(self.events) - 1
        self.append_event(
            date=date,
            event_type="ABORT",
            payload={
                "reason": reason,
                "orphan_from_seq": from_seq,
                "orphan_through_seq": through,
                "orphan_count": through - from_seq + 1,
            },
        )

    def commit(self, *, date: str, state: dict[str, Any], status: str) -> dict[str, Any]:
        next_seq = len(self.events)
        patch = {"last_event_seq": next_seq, "status": status}
        state = {**state, **patch}
        digest = state_digest(json.loads(json.dumps(state)))
        event = self.append_event(
            date=date,
            event_type="COMMIT",
            payload={"state_digest": digest, "status": status},
            state_patch=patch,
        )
        atomic_write_json(self.paths.state, state)
        return event
