"""전략 동결 스냅샷의 생성·검증 유틸리티."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


FREEZE_PATH = Path(__file__).resolve().parent / "strategy_freeze.json"


def canonical_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """메타데이터와 해시를 제외한 비교 대상 전략 설정을 반환한다."""
    return {
        key: value
        for key, value in payload.items()
        if key not in {"freeze_date", "oos_start_date", "note", "sha256"}
    }


def payload_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        canonical_payload(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_frozen_strategy(path: Path = FREEZE_PATH) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = payload.get("sha256")
    actual = payload_sha256(payload)
    if expected != actual:
        raise ValueError(f"전략 동결 파일 해시 불일치: expected={expected}, actual={actual}")
    return payload


def diff_payloads(frozen: Any, current: Any, prefix: str = "") -> list[str]:
    """중첩된 설정 두 개의 차이를 사람이 읽을 수 있는 문자열로 반환한다."""
    if isinstance(frozen, dict) and isinstance(current, dict):
        diffs: list[str] = []
        for key in sorted(set(frozen) | set(current)):
            path = f"{prefix}.{key}" if prefix else key
            if key not in frozen:
                diffs.append(f"{path}: <missing> -> {current[key]!r}")
            elif key not in current:
                diffs.append(f"{path}: {frozen[key]!r} -> <missing>")
            else:
                diffs.extend(diff_payloads(frozen[key], current[key], path))
        return diffs
    if frozen != current:
        return [f"{prefix}: {frozen!r} -> {current!r}"]
    return []
