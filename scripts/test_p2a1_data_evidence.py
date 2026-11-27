#!/usr/bin/env python3
"""P2-A-1: evidence 스크립트의 순수 함수·산출물 schema fixture.

네트워크 호출 없이 검증한다.
- 정규화 SHA256의 순서 독립성·값 민감성
- 파일 SHA256
- env presence가 boolean만 노출하는지(시크릿 미노출)
- 파일럿 티커가 국내/해외/원자재/분배 카테고리를 포함하는지
- 산출물이 존재하면 필수 파일·열·provenance 키를 만족하는지

실행::

    uv run python scripts/test_p2a1_data_evidence.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import p2a1_data_evidence as evidence


def check_frame_hash() -> None:
    frame = pd.DataFrame(
        {
            "ticker": ["069500", "360750", "069500"],
            "date": ["2023-01-03", "2023-01-03", "2023-01-02"],
            "close": [1.0, 2.0, 3.0],
        }
    )
    first = evidence.frame_sha256(frame, ["date", "ticker"])
    shuffled = evidence.frame_sha256(frame.sample(frac=1.0, random_state=1), ["date", "ticker"])
    if first != shuffled:
        raise AssertionError("정규화 SHA256이 행 순서에 의존합니다")
    changed = evidence.frame_sha256(frame.assign(close=[1.0, 2.0, 3.1]), ["date", "ticker"])
    if first == changed:
        raise AssertionError("값이 바뀌었는데 SHA256이 같습니다")


def check_file_hash(tmp_path: Path) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("p2a1", encoding="utf-8")
    expected = hashlib.sha256(b"p2a1").hexdigest()
    if evidence.file_sha256(path) != expected:
        raise AssertionError("파일 SHA256이 다릅니다")
    if evidence.file_sha256(tmp_path / "missing.txt") != "missing":
        raise AssertionError("누락 파일 표기가 다릅니다")


def check_env_presence() -> None:
    presence = evidence.env_presence()
    if not presence or not all(isinstance(value, bool) for value in presence.values()):
        raise AssertionError("env presence가 boolean map이 아닙니다")
    for key in presence:
        if not key.isupper():
            raise AssertionError(f"환경변수 이름이 비정상입니다: {key}")


def check_price_status() -> None:
    meta_ok = [{"status": "ok"}, {"status": "ok"}]
    comparison_ok = pd.DataFrame(
        {
            "ticker": ["069500", "360750"],
            "status": ["compared", "compared"],
            "expected_rows": [20, 20],
            "overlap_rows": [20, 20],
            "max_abs_open_diff": [0.0, 0.0],
            "max_abs_high_diff": [0.0, 0.0],
            "max_abs_low_diff": [0.0, 0.0],
            "max_abs_close_diff": [0.0, 0.0],
            "max_abs_volume_diff": [0.0, 0.0],
            "max_abs_value_diff": [0.0, 0.0],
            "date_set_match": [True, True],
            "official_calendar_verified": [False, False],
            "validation_errors": ["", ""],
        }
    )
    if evidence.classify_price_path(meta_ok, comparison_ok) != "verified_same_origin":
        raise AssertionError("정상 파일럿이 verified로 분류되지 않았습니다")
    meta_fail = [{"status": "error"}, {"status": "empty"}]
    if evidence.classify_price_path(meta_fail, comparison_ok) != "failed":
        raise AssertionError("전부 실패한 파일럿이 failed로 분류되지 않았습니다")
    meta_partial = [{"status": "ok"}, {"status": "empty"}]
    if evidence.classify_price_path(meta_partial, comparison_ok) != "partial":
        raise AssertionError("일부 실패가 partial로 분류되지 않았습니다")
    comparison_diff = comparison_ok.assign(max_abs_close_diff=[0.0, 1.0])
    if evidence.classify_price_path(meta_ok, comparison_diff) != "partial":
        raise AssertionError("가격 불일치가 partial로 분류되지 않았습니다")
    missing_cache = comparison_ok.assign(status=["compared", "cache_missing"])
    if evidence.classify_price_path(meta_ok, missing_cache) != "partial":
        raise AssertionError("캐시 누락이 partial로 분류되지 않았습니다")
    full = pd.DataFrame(
        {
            "ticker": ["069500"],
            "status": ["compared"],
            "expected_rows": [20],
            "overlap_rows": [20],
            "max_abs_open_diff": [0.0],
            "max_abs_high_diff": [0.0],
            "max_abs_low_diff": [0.0],
            "max_abs_close_diff": [0.0],
            "max_abs_volume_diff": [0.0],
            "max_abs_value_diff": [0.0],
            "date_set_match": [True],
            "official_calendar_verified": [False],
            "validation_errors": [""],
        }
    )
    if evidence.classify_price_path([{"status": "ok"}], full) != "verified_same_origin":
        raise AssertionError("완전 일치가 verified가 아닙니다")
    if evidence.classify_price_path([{"status": "ok"}], full.assign(overlap_rows=1)) != "partial":
        raise AssertionError("부분 겹침이 partial이 아닙니다")
    if evidence.classify_price_path([{"status": "ok"}], full.assign(max_abs_volume_diff=1.0)) != "partial":
        raise AssertionError("거래량 불일치가 partial이 아닙니다")
    if evidence.classify_price_path([{"status": "ok"}], full.assign(max_abs_close_diff=[None])) != "partial":
        raise AssertionError("결측 diff가 partial이 아닙니다")
    if evidence.classify_price_path([{"status": "ok"}], full.assign(date_set_match=[False])) != "partial":
        raise AssertionError("날짜 집합 불일치가 partial이 아닙니다")
    if (
        evidence.classify_price_path(
            [{"status": "ok"}], full, expected_tickers={"069500", "360750"}
        )
        != "partial"
    ):
        raise AssertionError("파일럿 티커 누락이 partial이 아닙니다")


def check_independent_source() -> None:
    none_usable = pd.DataFrame(
        {
            "dataset": ["가격", "분배금"],
            "access_status": ["verified", "blocked"],
            "independence": ["동일 원천(독립 아님)", "KRX 공식(독립 아님)"],
        }
    )
    if evidence.classify_independent_source(none_usable) != "unavailable":
        raise AssertionError("비독립 원천이 available로 분류되었습니다")
    usable = pd.DataFrame(
        {
            "dataset": ["분배금"],
            "access_status": ["verified"],
            "independence": ["예탁결제원(가격 원천과 독립)"],
        }
    )
    if evidence.classify_independent_source(usable) != "available":
        raise AssertionError("독립 원천이 available로 분류되지 않았습니다")


def check_secret_redaction() -> None:
    os.environ["DART_API_KEY"] = "SECRET123"
    text = "HTTPError: https://x/api?crtfc_key=SECRET123&page=1 SECRET123"
    redacted = evidence.redact_secrets(text)
    if "SECRET123" in redacted or "crtfc_key=***" not in redacted:
        raise AssertionError(f"시크릿이 제거되지 않았습니다: {redacted}")
    del os.environ["DART_API_KEY"]


def check_price_frames_nan() -> None:
    dates = pd.to_datetime(["2023-01-02", "2023-01-03"])
    krx = pd.DataFrame(
        {
            "date": dates,
            "open": [1.0, 2.0],
            "high": [1.1, 2.1],
            "low": [0.9, 1.9],
            "close": [1.0, 2.0],
            "volume": [10, 20],
            "trading_value": [100, 200],
        }
    )
    cache = krx.copy()
    cache.loc[1, "volume"] = np.nan
    result = evidence.compare_price_frames(krx, cache)
    if result["status"] != "nonfinite_values" or result["diffs"]["volume"] is not None:
        raise AssertionError(f"캐시 NaN이 검출되지 않았습니다: {result}")
    missing = evidence.compare_price_frames(krx.drop(columns=["close"]), krx)
    if missing["status"] != "missing_fields":
        raise AssertionError("필드 누락이 검출되지 않았습니다")
    cache_missing = evidence.compare_price_frames(krx, cache.drop(columns=["volume"]))
    if cache_missing["status"] != "missing_fields":
        raise AssertionError("캐시 필드 누락이 검출되지 않았습니다")

    clean_cache = krx.copy()
    no_overlap = evidence.compare_price_frames(
        krx, clean_cache.assign(date=pd.to_datetime(["2023-02-01", "2023-02-02"]))
    )
    if no_overlap["status"] != "no_overlap" or no_overlap["diffs"]["close"] is not None:
        raise AssertionError(f"no_overlap 처리 오류: {no_overlap}")
    duplicate = evidence.compare_price_frames(
        pd.concat([krx, krx.iloc[[0]]], ignore_index=True), cache
    )
    if duplicate["status"] != "invalid_dates":
        raise AssertionError("중복 날짜가 검출되지 않았습니다")
    nat = evidence.compare_price_frames(
        krx.assign(date=[pd.Timestamp("NaT"), krx["date"].iloc[1]]), cache
    )
    if nat["status"] != "invalid_dates":
        raise AssertionError("NaT 날짜가 검출되지 않았습니다")
    inf = evidence.compare_price_frames(krx.assign(close=[np.inf, 2.0]), clean_cache)
    if inf["status"] != "nonfinite_values":
        raise AssertionError("Inf 값이 검출되지 않았습니다")
    mismatch = evidence.compare_price_frames(krx, clean_cache.iloc[:1])
    if mismatch["status"] != "date_set_mismatch":
        raise AssertionError("날짜 집합 불일치가 검출되지 않았습니다")

    missing_date = evidence.compare_price_frames(krx.drop(columns=["date"]), clean_cache)
    if missing_date["status"] != "invalid_dates":
        raise AssertionError("날짜 열 누락이 상태로 반환되지 않았습니다")
    bad_date = evidence.compare_price_frames(
        krx.assign(date=["not-a-date", "2023-01-03"]), clean_cache
    )
    if bad_date["status"] != "invalid_dates":
        raise AssertionError("잘못된 날짜 문자열이 상태로 반환되지 않았습니다")
    bad_number = evidence.compare_price_frames(
        krx.assign(close=["x", "2"]), clean_cache
    )
    if bad_number["status"] != "nonfinite_values":
        raise AssertionError("비수치 가격이 상태로 반환되지 않았습니다")

    membership = evidence.compare_membership(["A", "B"], ["B", "C"])
    if membership["ticker_set_match"] or membership["intersection"] != 1:
        raise AssertionError("membership 비교 오류")
    if not evidence.compare_membership(["A"], ["A"])["ticker_set_match"]:
        raise AssertionError("membership 일치 오류")
    ok = evidence.compare_price_frames(krx, krx)
    if ok["status"] != "compared" or any(value != 0.0 for value in ok["diffs"].values()):
        raise AssertionError(f"정상 비교가 실패했습니다: {ok}")


def check_membership() -> None:
    matched = evidence.compare_membership(["069500", "360750"], ["360750", "069500"])
    if not matched["ticker_set_match"] or matched["krx_only"] or matched["snapshot_only"]:
        raise AssertionError(f"membership 일치 오류: {matched}")
    mismatched = evidence.compare_membership(["069500", "360750"], ["069500", "411060"])
    if mismatched["ticker_set_match"] or mismatched["intersection"] != 1:
        raise AssertionError(f"membership 불일치 오류: {mismatched}")


def check_empty_comparison() -> None:
    frame = evidence.empty_comparison_frame()
    if list(frame.columns) != evidence.COMPARISON_COLUMNS:
        raise AssertionError("빈 비교 프레임 열이 다릅니다")
    import tempfile
    from pathlib import Path

    path = Path(tempfile.mkdtemp()) / "empty.csv"
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    reloaded = pd.read_csv(path)
    if list(reloaded.columns) != evidence.COMPARISON_COLUMNS or len(reloaded) != 0:
        raise AssertionError("빈 비교 CSV schema가 유지되지 않았습니다")


def check_snapshot_status() -> None:
    if evidence.classify_snapshot([{"ohlcv_count": 220}, {"ohlcv_count": 1150}]) != "verified":
        raise AssertionError("정상 snapshot이 verified가 아닙니다")
    if evidence.classify_snapshot([{"ohlcv_count": 220}, {"ohlcv_count": None}]) != "partial":
        raise AssertionError("부분 실패 snapshot이 partial이 아닙니다")
    if evidence.classify_snapshot([{"ohlcv_count": None}]) != "failed":
        raise AssertionError("전부 실패 snapshot이 failed가 아닙니다")
    if evidence.classify_krx_access("verified", "verified_same_origin") != "verified":
        raise AssertionError("KRX access verified 계산 오류")
    if evidence.classify_krx_access("failed", "failed") != "failed":
        raise AssertionError("KRX access failed 계산 오류")


def check_distribution_and_preflight() -> None:
    if evidence.classify_distribution({"total_return_approvable": True, "coverage_verified": False}) != "blocked":
        raise AssertionError("coverage 미검증 분배금이 승인되었습니다")
    if evidence.classify_distribution({"total_return_approvable": True, "coverage_verified": True}) != "verified":
        raise AssertionError("승인 가능 분배금이 verified가 아닙니다")
    if evidence.classify_distribution({"total_return_approvable": False}) != "blocked":
        raise AssertionError("승인 불가 분배금이 blocked가 아닙니다")
    gated = evidence.gate_preflight_report(
        {"status": "BLOCKED", "approval_valid": False, "event_count": 0, "blockers": [{"code": "X"}]},
        written=False,
    )
    if gated["status"] != "no_report" or gated["blockers"] or gated["event_count"] is not None:
        raise AssertionError("stale preflight 필드가 노출되었습니다")
    fresh = evidence.gate_preflight_report({"status": "BLOCKED", "blockers": [{"code": "X"}]}, True)
    if fresh["status"] != "BLOCKED" or len(fresh["blockers"]) != 1:
        raise AssertionError("fresh preflight 필드가 누락되었습니다")
    if evidence.classify_dart_access({"status": "missing_key"}) != "missing_key":
        raise AssertionError("DART missing key 계산 오류")
    if (
        evidence.classify_dart_access({"corp_code_ok": True, "list_status": "000"})
        != "api_verified_no_etf_distribution_dataset"
    ):
        raise AssertionError("DART 정상 상태 계산 오류")


def check_json_sanitize() -> None:
    payload = {
        "nan": np.nan,
        "inf": np.inf,
        "values": [1.0, np.float64("nan")],
        "nested": {"n": np.int64(3)},
    }
    text = evidence.dump_json(payload)
    parsed = json.loads(text)
    if parsed["nan"] is not None or parsed["inf"] is not None:
        raise AssertionError("NaN/Inf가 null로 변환되지 않았습니다")
    if parsed["values"][1] is not None or parsed["nested"]["n"] != 3:
        raise AssertionError("리스트/정수 변환이 잘못되었습니다")
    if "NaN" in text or "Infinity" in text:
        raise AssertionError("표준 JSON이 아닌 토큰이 남아 있습니다")


def check_pilot_coverage() -> None:
    tickers = {ticker for ticker, _ in evidence.PILOT_TICKERS}
    required = {"069500", "360750", "411060", "161510"}
    if not required.issubset(tickers):
        raise AssertionError(f"파일럿 카테고리 누락: {required - tickers}")
    if len(set(evidence.PILOT_WINDOWS)) != len(evidence.PILOT_WINDOWS):
        raise AssertionError("파일럿 window 키가 중복됩니다")
    for ticker in tickers:
        if ticker not in evidence.PILOT_WINDOWS:
            raise AssertionError(f"{ticker} window 누락")


def check_artifacts_if_present() -> None:
    output_dir = ROOT / "outputs_p2a1"
    if not output_dir.exists():
        print("  (산출물 없음: schema 검사 생략)")
        return
    required_files = {
        "implementation_inventory.csv": {"component", "status", "blocker"},
        "approval_blockers.csv": {"blocker_id", "area", "status"},
        "data_source_inventory.csv": {"source", "access_status", "independence"},
        "coverage_matrix.csv": {"dataset", "available", "approval_impact"},
        "cross_source_comparison.csv": {"ticker", "status"},
        "pilot_validation.json": None,
        "provenance.json": None,
    }
    for name, columns in required_files.items():
        path = output_dir / name
        if not path.exists():
            raise AssertionError(f"산출물 누락: {name}")
        if columns is None:
            continue
        frame = pd.read_csv(path)
        if not columns.issubset(frame.columns):
            raise AssertionError(f"{name} 열 누락: {columns - set(frame.columns)}")
    provenance = json.loads((output_dir / "provenance.json").read_text(encoding="utf-8"))
    for key in ("code_digest", "freeze_sha256", "artifacts", "pykrx_version", "created_at"):
        if key not in provenance:
            raise AssertionError(f"provenance 키 누락: {key}")
    artifact_keys = set(provenance["artifacts"])
    on_disk = {
        str(path.relative_to(output_dir))
        for path in output_dir.rglob("*")
        if path.is_file() and path.name != "provenance.json"
    }
    if artifact_keys != on_disk:
        raise AssertionError(f"provenance artifact 목록 불일치: {artifact_keys ^ on_disk}")
    if "artifacts" in provenance["code_digest_scope"]:
        raise AssertionError("code_digest_scope가 잘못되었습니다")
    if provenance.get("raw_response_provenance") != "not_stored(small probes only; no bulk raw responses)":
        raise AssertionError("raw response provenance 표기가 다릅니다")
    pilot = json.loads((output_dir / "pilot_validation.json").read_text(encoding="utf-8"))
    for key in ("paths", "krx_snapshot_probe", "sources", "blockers"):
        if key not in pilot:
            raise AssertionError(f"pilot_validation 키 누락: {key}")
    if pilot["paths"]["corporate_action_to_preflight"]["status"] not in {"blocked", "verified"}:
        raise AssertionError("preflight 상태 표기가 비정상입니다")


def main() -> int:
    import tempfile

    checks = [
        ("frame hash", check_frame_hash),
        ("file hash", lambda: check_file_hash(Path(tempfile.mkdtemp()))),
        ("env presence", check_env_presence),
        ("secret redaction", check_secret_redaction),
        ("empty comparison", check_empty_comparison),
        ("price frames nan", check_price_frames_nan),
        ("membership compare", check_membership),
        ("price status", check_price_status),
        ("json sanitize", check_json_sanitize),
        ("snapshot status", check_snapshot_status),
        ("distribution and preflight", check_distribution_and_preflight),
        ("independent source", check_independent_source),
        ("pilot coverage", check_pilot_coverage),
        ("artifacts schema", check_artifacts_if_present),
    ]
    failures = 0
    for label, check in checks:
        try:
            check()
            print(f"PASS {label}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {label}: {exc}")
    if failures:
        print(f"\n{failures} checks failed")
        return 1
    print("\np2a1 evidence checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
