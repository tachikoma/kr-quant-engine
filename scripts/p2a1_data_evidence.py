#!/usr/bin/env python3
"""P2-A-1: 역사 데이터 신뢰성·승인 blocker evidence 수집(읽기 전용).

기존 구현의 상태를 분류하고, 실제 KRX 데이터로 PIT/가격 경로를 소량 검증하며,
기업행위·분배금 원천의 확보 가능성을 실제 조회로 확인한다. 엔진·동결·운영 경로는
변경하지 않으며, 실패/미확인은 그대로 기록한다.

실행::

    uv run python scripts/p2a1_data_evidence.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import etf_corporate_actions as eca
import etf_distributions as ed

OUTPUT_DIR = ROOT / "outputs_p2a1"
PILOT_DIR = OUTPUT_DIR / "pilot"
EXCLUDE_2026_END = pd.Timestamp("2025-12-31")

PILOT_TICKERS: tuple[tuple[str, str], ...] = (
    ("069500", "국내 주식형 대표(KODEX 200), 분배금 지급 이력"),
    ("360750", "해외 주식형(TIGER 미국S&P500)"),
    ("411060", "원자재/금현물(ACE KRX금현물)"),
    ("161510", "배당주(PLUS 고배당주), 분배금 지급"),
    ("498400", "커버드콜(KODEX 200타겟위클리커버드콜), 고빈도 분배"),
)
PILOT_WINDOWS: dict[str, tuple[str, str]] = {
    "069500": ("20230102", "20230131"),
    "360750": ("20230102", "20230131"),
    "411060": ("20230102", "20230131"),
    "161510": ("20230102", "20230131"),
    "498400": ("20250102", "20250131"),
}
SNAPSHOT_DATES: tuple[str, ...] = ("20160801", "20200102", "20240102", "20260721")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
HTTP_TIMEOUT = 12

CODE_FILES = (
    "scripts/p2a1_data_evidence.py",
    "etf_corporate_actions.py",
    "etf_distributions.py",
    "pit_universe.py",
    "run_etf_backtest.py",
    "etf_shared.py",
    "config_utils.py",
    "pykrx_utils.py",
)


# --------------------------------------------------------------------------- #
# 공용
# --------------------------------------------------------------------------- #
def load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def redact_secrets(text: str) -> str:
    """오류 문자열에 포함될 수 있는 인증 파라미터를 제거한다."""
    import re

    redacted = re.sub(r"(crtfc_key=)[^&\s]+", r"\1***", str(text))
    for value in (os.environ.get("DART_API_KEY"), os.environ.get("KRX_PW"), os.environ.get("KRX_ID")):
        if value:
            redacted = redacted.replace(str(value), "***")
    return redacted


def sanitize_json(value: Any) -> Any:
    """NaN/Inf를 null로 바꿔 표준 JSON 직렬화가 가능하게 한다."""
    if isinstance(value, dict):
        return {str(key): sanitize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json(item) for item in value]
    if isinstance(value, (np.floating, float)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def dump_json(payload: Any) -> str:
    return json.dumps(
        sanitize_json(payload), ensure_ascii=False, indent=2, allow_nan=False, default=str
    )


def classify_price_path(
    pilot_meta: list[dict[str, Any]],
    comparison: pd.DataFrame,
    expected_tickers: set[str] | None = None,
) -> str:
    """파일럿 조회·캐시 대조 결과로 상태를 계산한다(상수 성공 금지)."""
    if not pilot_meta or all(item.get("status") != "ok" for item in pilot_meta):
        return "failed"
    if any(item.get("status") != "ok" for item in pilot_meta):
        return "partial"
    diff_columns = [
        "max_abs_open_diff",
        "max_abs_high_diff",
        "max_abs_low_diff",
        "max_abs_close_diff",
        "max_abs_volume_diff",
        "max_abs_value_diff",
    ]
    required = {
        "ticker",
        "status",
        "expected_rows",
        "overlap_rows",
        "date_set_match",
        "official_calendar_verified",
        *diff_columns,
    }
    if comparison.empty or not required.issubset(comparison.columns):
        return "partial"
    if expected_tickers is not None and set(comparison["ticker"].astype(str)) != set(
        expected_tickers
    ):
        return "partial"
    if (comparison["status"] != "compared").any():
        return "partial"
    if (comparison["overlap_rows"] != comparison["expected_rows"]).any():
        return "partial"
    if not comparison["date_set_match"].fillna(False).all():
        return "partial"
    if comparison[diff_columns].isna().any().any():
        return "partial"
    if comparison[diff_columns].abs().gt(0).any().any():
        return "partial"
    return "verified_same_origin"


def compare_membership(
    krx_tickers: Iterable[str], snapshot_tickers: Iterable[str]
) -> dict[str, Any]:
    """KRX 조회 ticker 집합과 스냅샷 ticker 집합을 비교한다."""
    krx = {str(ticker) for ticker in krx_tickers}
    snapshot = {str(ticker) for ticker in snapshot_tickers}
    return {
        "krx_count": len(krx),
        "snapshot_count": len(snapshot),
        "intersection": len(krx & snapshot),
        "krx_only": sorted(krx - snapshot),
        "snapshot_only": sorted(snapshot - krx),
        "ticker_set_match": krx == snapshot,
    }


def classify_snapshot(probe: list[dict[str, Any]]) -> str:
    if not probe:
        return "failed"
    if all(item.get("ohlcv_count") for item in probe):
        return "verified"
    if any(item.get("ohlcv_count") for item in probe):
        return "partial"
    return "failed"


def classify_krx_access(snapshot_status: str, price_status: str) -> str:
    if snapshot_status == "verified" and price_status.startswith("verified"):
        return "verified"
    if snapshot_status == "partial" or price_status == "partial":
        return "partial"
    return "failed"


def classify_dart_access(dart: dict[str, Any]) -> str:
    if dart.get("status") == "missing_key":
        return "missing_key"
    if dart.get("corp_code_ok") and dart.get("list_status") == "000":
        return "api_verified_no_etf_distribution_dataset"
    return "api_error"


def classify_independent_source(sources: pd.DataFrame) -> str:
    """가격/분배금/기업행위 데이터를 실제 제공하는 독립 원천이 있는지 판정."""
    if sources.empty:
        return "unavailable"
    relevant = sources[
        sources["dataset"].str.contains("가격|시세|분배|기업행위|공시", na=False)
        & sources["access_status"].isin({"verified", "verified_page_only"})
        & sources["independence"].str.contains("독립", na=False)
        & ~sources["independence"].str.contains("동일|아님|단일", na=False)
    ]
    return "available" if not relevant.empty else "unavailable"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"


def frame_sha256(frame: pd.DataFrame, sort_by: list[str]) -> str:
    canonical = frame.sort_values(sort_by).reset_index(drop=True)
    payload = canonical.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def code_digest() -> str:
    payload = [f"{name}:{file_sha256(ROOT / name)}" for name in CODE_FILES]
    return hashlib.sha256("|".join(payload).encode("utf-8")).hexdigest()


def env_presence() -> dict[str, bool]:
    keys = ("KRX_ID", "KRX_PW", "DART_API_KEY", "KIWOOM_APPKEY", "KIS_APP_KEY", "NHPLUG_APP_KEY")
    return {key: bool(os.environ.get(key)) for key in keys}


def http_probe(url: str) -> dict[str, Any]:
    observed = datetime.now(timezone.utc).isoformat()
    try:
        response = requests.get(url, headers=UA, timeout=HTTP_TIMEOUT, allow_redirects=True)
        text = response.text
        return {
            "url": url,
            "final_url": response.url,
            "redirect_history": [item.status_code for item in response.history],
            "observed_at": observed,
            "status": response.status_code,
            "bytes": len(text),
            "mentions_etf": ("ETF" in text) or ("etf" in text),
            "blocked_hint": response.status_code in {401, 403, 429},
        }
    except Exception as exc:  # noqa: BLE001 - 조회 실패도 evidence
        return {
            "url": url,
            "observed_at": observed,
            "status": None,
            "error": type(exc).__name__,
            "message": str(exc)[:200],
        }


# --------------------------------------------------------------------------- #
# KRX 조회(PIT/가격)
# --------------------------------------------------------------------------- #
def krx_snapshot_probe() -> dict[str, Any]:
    from pykrx import stock

    results: list[dict[str, Any]] = []
    for date in SNAPSHOT_DATES:
        try:
            tickers = stock.get_etf_ticker_list(date)
            list_count = len(tickers)
            list_sha = hashlib.sha256(
                ",".join(sorted(map(str, tickers))).encode("utf-8")
            ).hexdigest()
        except Exception as exc:  # noqa: BLE001
            list_count = None
            tickers = []
            results.append({"date": date, "error": f"ticker_list:{type(exc).__name__}:{exc}"})
            continue
        try:
            ohlcv = stock.get_etf_ohlcv_by_ticker(date)
            ohlcv_count = len(ohlcv)
            columns = [str(item) for item in ohlcv.columns]
        except Exception as exc:  # noqa: BLE001
            ohlcv_count = None
            columns = []
            results.append(
                {
                    "date": date,
                    "ticker_list_count": list_count,
                    "error": f"ohlcv:{type(exc).__name__}:{exc}",
                }
            )
            continue
        ohlcv_tickers = sorted({str(item) for item in ohlcv.reset_index().iloc[:, 0]})
        results.append(
            {
                "date": date,
                "ticker_list_count": list_count,
                "ticker_list_sha256": list_sha,
                "ohlcv_count": ohlcv_count,
                "ohlcv_ticker_set_sha256": hashlib.sha256(
                    ",".join(ohlcv_tickers).encode("utf-8")
                ).hexdigest(),
                "tickers": list(map(str, tickers)),
                "ohlcv_tickers": ohlcv_tickers,
                "ohlcv_columns": columns,
                "has_nav": "NAV" in columns,
                "has_value": "거래대금" in columns,
            }
        )
    return {"snapshot_probe": results, "observed_at": datetime.now(timezone.utc).isoformat()}


def krx_pilot_prices() -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    from pykrx import stock

    frames = []
    meta: list[dict[str, Any]] = []
    for ticker, reason in PILOT_TICKERS:
        start, end = PILOT_WINDOWS[ticker]
        observed = datetime.now(timezone.utc).isoformat()
        try:
            raw = stock.get_etf_ohlcv_by_date(start, end, ticker)
        except Exception as exc:  # noqa: BLE001
            meta.append(
                {
                    "ticker": ticker,
                    "reason": reason,
                    "window": f"{start}-{end}",
                    "observed_at": observed,
                    "status": "error",
                    "error": f"{type(exc).__name__}:{exc}",
                }
            )
            continue
        if raw.empty:
            meta.append(
                {
                    "ticker": ticker,
                    "reason": reason,
                    "window": f"{start}-{end}",
                    "observed_at": observed,
                    "status": "empty",
                }
            )
            continue
        frame = raw.reset_index()
        date_column = "날짜" if "날짜" in frame.columns else frame.columns[0]
        frame = frame.rename(
            columns={
                date_column: "date",
                "시가": "open",
                "고가": "high",
                "저가": "low",
                "종가": "close",
                "거래량": "volume",
                "거래대금": "trading_value",
                "NAV": "nav",
                "기초지수": "base_index",
            }
        )
        frame["ticker"] = ticker
        frame["date"] = pd.to_datetime(frame["date"])
        keep = [
            column
            for column in (
                "ticker",
                "date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "trading_value",
                "nav",
                "base_index",
            )
            if column in frame.columns
        ]
        frame = frame[keep].sort_values("date").reset_index(drop=True)
        name = stock.get_etf_ticker_name(ticker)
        isin = stock.get_etf_isin(ticker)
        meta.append(
            {
                "ticker": ticker,
                "name": name,
                "isin": isin,
                "reason": reason,
                "window": f"{start}-{end}",
                "observed_at": observed,
                "status": "ok",
                "rows": len(frame),
                "sha256": frame_sha256(frame, ["date"]),
                "first_date": str(frame["date"].min().date()),
                "last_date": str(frame["date"].max().date()),
            }
        )
        frames.append(frame)
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return combined, meta


COMPARISON_COLUMNS = [
    "ticker",
    "status",
    "expected_rows",
    "overlap_rows",
    "max_abs_open_diff",
    "max_abs_high_diff",
    "max_abs_low_diff",
    "max_abs_close_diff",
    "max_abs_volume_diff",
    "max_abs_value_diff",
    "date_set_match",
    "official_calendar_verified",
    "validation_errors",
    "cache_sha256",
    "source_independence",
]


def empty_comparison_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=COMPARISON_COLUMNS)


COMPARISON_FIELDS = ("open", "high", "low", "close", "volume", "trading_value")


def validate_price_frame(frame: pd.DataFrame, label: str) -> list[str]:
    """날짜 중복/NaT, 수치 Inf/NaN, 필수 열 존재를 검증한다."""
    errors: list[str] = []
    if "date" not in frame.columns:
        return [f"{label}:date_missing"]
    dates = pd.to_datetime(frame["date"], errors="coerce")
    if dates.isna().any():
        errors.append(f"{label}:nat_date")
    if dates.dropna().duplicated().any():
        errors.append(f"{label}:duplicate_date")
    for column in COMPARISON_FIELDS:
        if column not in frame.columns:
            continue
        values = pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=float)
        if not np.isfinite(values).all():
            errors.append(f"{label}:nonfinite_{column}")
    return errors


def compare_price_frames(krx: pd.DataFrame, cache: pd.DataFrame) -> dict[str, Any]:
    """KRX 추출과 캐시 프레임을 6개 필드·전 기간·날짜 집합 기준으로 비교한다."""
    errors = validate_price_frame(krx, "krx") + validate_price_frame(cache, "cache")
    if errors:
        status = (
            "invalid_dates"
            if any("date" in error or "nat" in error for error in errors)
            else "nonfinite_values"
        )
        return {
            "status": status,
            "overlap_rows": 0,
            "missing_fields": [],
            "nan_fields": [],
            "validation_errors": errors,
            "date_set_match": False,
            "official_calendar_verified": False,
            "diffs": {column: None for column in COMPARISON_FIELDS},
        }
    missing_inputs = [
        column
        for column in COMPARISON_FIELDS
        if column not in krx.columns or column not in cache.columns
    ]
    select_fields = [column for column in COMPARISON_FIELDS if column not in missing_inputs]
    krx_dates = pd.to_datetime(krx["date"], errors="coerce").dropna()
    cache_dates = pd.to_datetime(cache["date"], errors="coerce").dropna()
    window_start = krx_dates.min() if not krx_dates.empty else None
    window_end = krx_dates.max() if not krx_dates.empty else None
    if window_start is not None:
        cache_dates = cache_dates[
            (cache_dates >= window_start) & (cache_dates <= window_end)
        ]
    date_set_match = bool(
        window_start is not None
        and set(krx_dates) == set(cache_dates)
    )
    merged = krx[["date", *select_fields]].merge(
        cache[["date", *select_fields]],
        on="date",
        suffixes=("_krx", "_cache"),
        how="inner",
    )
    missing_fields = missing_inputs + [
        column
        for column in COMPARISON_FIELDS
        if column not in missing_inputs
        and (
            f"{column}_krx" not in merged.columns
            or f"{column}_cache" not in merged.columns
        )
    ]
    nan_fields = [
        column
        for column in COMPARISON_FIELDS
        if column not in missing_fields
        and (
            merged[f"{column}_krx"].isna().any()
            or merged[f"{column}_cache"].isna().any()
        )
    ]
    if missing_fields:
        status = "missing_fields"
    elif merged.empty:
        status = "no_overlap"
    elif not date_set_match:
        status = "date_set_mismatch"
    elif nan_fields:
        status = "nan_in_source"
    else:
        status = "compared"
    diffs: dict[str, float | None] = {}
    for column in COMPARISON_FIELDS:
        if column in missing_fields or column in nan_fields or merged.empty:
            diffs[column] = None
        else:
            diffs[column] = float(
                np.max(np.abs(merged[f"{column}_krx"] - merged[f"{column}_cache"]))
            )
    return {
        "status": status,
        "overlap_rows": len(merged),
        "missing_fields": missing_fields,
        "nan_fields": nan_fields,
        "validation_errors": errors,
        "date_set_match": date_set_match,
        "official_calendar_verified": False,
        "diffs": diffs,
    }


def pilot_completeness(
    pilot_meta: list[dict[str, Any]],
    comparison: pd.DataFrame,
    expected_tickers: set[str] | list[str],
) -> dict[str, Any]:
    """파일럿 조회·비교 완료 여부를 집합·중복 기준으로 판정한다."""
    expected = sorted(str(ticker) for ticker in expected_tickers)
    statuses = [(str(item.get("ticker")), item.get("status")) for item in pilot_meta]
    queried_tickers = [ticker for ticker, _ in statuses]
    queried_ok = sorted(ticker for ticker, status in statuses if status == "ok")
    compared = (
        sorted(str(ticker) for ticker in comparison["ticker"].astype(str).tolist())
        if not comparison.empty and "ticker" in comparison.columns
        else []
    )
    reasons: list[str] = []
    if sorted(set(queried_tickers)) != expected:
        reasons.append("queried ticker set differs from expected")
    if len(queried_tickers) != len(expected):
        reasons.append("duplicate or missing pilot queries")
    if any(status != "ok" for _, status in statuses):
        reasons.append("some pilot queries failed")
    if compared != expected:
        reasons.append("compared ticker set differs from expected")
    if len(compared) != len(expected):
        reasons.append("duplicate comparison rows")
    return {
        "expected_tickers": expected,
        "queried_ok": queried_ok,
        "compared_tickers": compared,
        "all_expected_queried_and_compared": not reasons,
        "reasons": reasons,
    }


def pit_membership_probe(snapshot_probe: list[dict[str, Any]]) -> dict[str, Any]:
    """스냅샷 캐시가 있으면 ticker 집합을 비교하고, 없으면 부분 검증으로 기록한다."""
    snapshot_path = ROOT / "data_cache" / "pit_universe" / "pit_universe_snapshots.parquet"
    if not snapshot_path.exists():
        return {
            "status": "partial_no_snapshot_cache",
            "snapshot_path": str(snapshot_path.relative_to(ROOT)),
            "ticker_set_comparison": False,
            "note": (
                "스냅샷 캐시가 없어 ticker 집합 비교를 수행하지 않았다. "
                "OHLCV 행 수 일치는 membership 재현성의 부분 근거일 뿐이다."
            ),
            "probe_dates": [item.get("date") for item in snapshot_probe],
        }
    snapshots = pd.read_parquet(snapshot_path)
    date_column = "snapshot_date" if "snapshot_date" in snapshots.columns else "date"
    ticker_column = "ticker" if "ticker" in snapshots.columns else "isu_cd"
    snapshots[date_column] = pd.to_datetime(snapshots[date_column]).dt.normalize()
    results = []
    for item in snapshot_probe:
        target = pd.Timestamp(item["date"])
        available = snapshots[snapshots[date_column] <= target]
        if available.empty:
            results.append({"date": item["date"], "status": "no_snapshot_asof"})
            continue
        asof = available[date_column].max()
        snapshot_tickers = set(
            snapshots.loc[snapshots[date_column] == asof, ticker_column].astype(str)
        )
        krx_tickers = set(item.get("ohlcv_tickers") or [])
        comparison = compare_membership(krx_tickers, snapshot_tickers)
        results.append(
            {
                "date": item["date"],
                "asof_snapshot": str(asof.date()),
                "status": "compared" if comparison["ticker_set_match"] else "mismatch",
                **{key: value for key, value in comparison.items() if key != "krx_only"},
                "krx_only_count": len(comparison["krx_only"]),
                "snapshot_only_count": len(comparison["snapshot_only"]),
            }
        )
    statuses = [item.get("status") for item in results]
    return {
        "status": (
            "verified" if statuses and all(item == "compared" for item in statuses) else "partial"
        ),
        "snapshot_path": str(snapshot_path.relative_to(ROOT)),
        "ticker_set_comparison": True,
        "results": [
            {key: value for key, value in item.items() if key not in {"krx_only", "snapshot_only"}}
            for item in results
        ],
        "note": "스냅샷 as-of ticker 집합과 KRX OHLCV ticker 집합을 비교했다.",
    }


def cross_check_cache(prices: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for ticker, group in prices.groupby("ticker"):
        cache_path = ROOT / "data_cache" / f"{ticker}.parquet"
        if not cache_path.exists():
            rows.append({"ticker": ticker, "status": "cache_missing"})
            continue
        cache = pd.read_parquet(cache_path)
        if "date" in cache.columns:
            # 변환 예외가 비교 이전에 터지지 않도록 coerce 후 검증에 맡긴다.
            cache["date"] = pd.to_datetime(cache["date"], errors="coerce")
        comparison = compare_price_frames(group, cache)
        diffs = comparison["diffs"]
        rows.append(
            {
                "ticker": ticker,
                "status": comparison["status"],
                "expected_rows": len(group),
                "overlap_rows": comparison["overlap_rows"],
                "max_abs_open_diff": diffs["open"],
                "max_abs_high_diff": diffs["high"],
                "max_abs_low_diff": diffs["low"],
                "max_abs_close_diff": diffs["close"],
                "max_abs_volume_diff": diffs["volume"],
                "max_abs_value_diff": diffs["trading_value"],
                "date_set_match": comparison["date_set_match"],
                "official_calendar_verified": comparison["official_calendar_verified"],
                "validation_errors": ";".join(comparison["validation_errors"]) or "none",
                "cache_sha256": file_sha256(cache_path),
                "source_independence": "same_origin(KRX via pykrx vs KRX-built cache)",
            }
        )
    return pd.DataFrame(rows, columns=COMPARISON_COLUMNS)


# --------------------------------------------------------------------------- #
# 기존 preflight/ledger/distribution
# --------------------------------------------------------------------------- #
def gate_preflight_report(report: dict[str, Any], written: bool) -> dict[str, Any]:
    """이번 실행에서 보고서가 생성되지 않았으면 이전 파일 내용을 노출하지 않는다."""
    if not written:
        return {
            "status": "no_report",
            "approval_valid": None,
            "event_count": None,
            "ledger_sha256": None,
            "blockers": [],
        }
    return {
        "status": report.get("status"),
        "approval_valid": report.get("approval_valid"),
        "event_count": report.get("event_count"),
        "ledger_sha256": report.get("ledger_sha256"),
        "blockers": [
            {"code": item.get("code"), "message": item.get("message")}
            for item in report.get("blockers", [])
        ],
    }


def run_approval_preflight() -> dict[str, Any]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = OUTPUT_DIR / "approval_preflight" / f"run_{run_id}"
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(ROOT / "run_etf_backtest.py"),
        "--approval-strict",
        "--mode",
        "single",
        "--approval-output-dir",
        str(output_dir),
    ]
    started = datetime.now(timezone.utc).isoformat()
    completed = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, timeout=300, check=False
    )
    report_path = output_dir / "approval_report.json"
    report_exists = report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_exists else {}
    report_written = (
        report_exists
        and datetime.fromtimestamp(report_path.stat().st_mtime, tz=timezone.utc).isoformat()
        >= started
    )
    gated = gate_preflight_report(report, report_written)
    return {
        "command": " ".join(command),
        "started_at": started,
        "returncode": completed.returncode,
        "stdout_tail": completed.stdout.strip().splitlines()[-3:],
        "stderr_tail": completed.stderr.strip().splitlines()[-3:],
        "run_id": run_id,
        "report_path": str(report_path.relative_to(ROOT)),
        "report_sha256": file_sha256(report_path) if report_written else None,
        "report_written_this_run": bool(report_written),
        **gated,
    }


def ledger_probe() -> dict[str, Any]:
    ledger = eca.load_corporate_action_ledger(
        ROOT / "data" / "etf_corporate_actions.csv",
        ROOT / "data" / "etf_corporate_actions_manifest.json",
    )
    report = ledger.approval_report()
    manifest = json.loads(
        (ROOT / "data" / "etf_corporate_actions_manifest.json").read_text(encoding="utf-8")
    )
    return {
        "events": len(ledger.events),
        "approval_valid": report.approval_valid,
        "blocker_codes": sorted({blocker.code for blocker in report.blockers}),
        "ledger_sha256": file_sha256(ROOT / "data" / "etf_corporate_actions.csv"),
        "manifest_sha256": file_sha256(ROOT / "data" / "etf_corporate_actions_manifest.json"),
        "manifest_coverage_status": manifest.get("coverage_status"),
        "manifest_verification_period": manifest.get("verification_period"),
        "manifest_tickers": manifest.get("verification_tickers"),
        "manifest_source_documents": len(manifest.get("source_documents", [])),
    }


def distribution_probe() -> dict[str, Any]:
    path = ROOT / "data" / "etf_distributions.csv"
    frame = ed.load_distributions(path)
    required_fields = {"ticker", "ex_date", "amount_per_share", "payment_date", "source"}
    populated = frame.dropna() if not frame.empty else frame
    fields_present = bool(
        len(frame) > 0
        and required_fields.issubset(frame.columns)
        and len(populated) == len(frame)
        and (frame["source"].astype(str).str.len() > 0).all()
    )
    coverage_verified = False  # 기간·티커 coverage와 공식 출처 검증은 이번 범위에서 미확인
    approvable = bool(fields_present and coverage_verified)
    return {
        "path": str(path.relative_to(ROOT)),
        "rows": len(frame),
        "columns": list(frame.columns),
        "sha256": file_sha256(path),
        "required_fields": sorted(required_fields),
        "fields_present": fields_present,
        "coverage_verified": coverage_verified,
        "total_return_approvable": approvable,
        "approval_block_reason": (
            None
            if approvable
            else "기간·티커 coverage와 공식 출처 검증 미확인 — total_return 승인 근거로 사용 불가"
        ),
        "note": "빈 template. 실제 분배금 원천 확보 전에는 total_return 성과를 승인 근거로 쓰지 않는다.",
    }


def classify_distribution(probe: dict[str, Any]) -> str:
    return (
        "verified"
        if probe.get("total_return_approvable") and probe.get("coverage_verified")
        else "blocked"
    )


# --------------------------------------------------------------------------- #
# 원천 조사
# --------------------------------------------------------------------------- #
def dart_probe() -> dict[str, Any]:
    key = os.environ.get("DART_API_KEY")
    if not key:
        return {"status": "missing_key"}
    observed = datetime.now(timezone.utc).isoformat()
    result: dict[str, Any] = {"observed_at": observed}
    try:
        code_response = requests.get(
            "https://opendart.fss.or.kr/api/corpCode.xml", params={"crtfc_key": key}, timeout=30
        )
        result["corp_code_http"] = code_response.status_code
        result["corp_code_bytes"] = len(code_response.content)
        result["corp_code_ok"] = code_response.content[:2] == b"PK"
    except Exception as exc:  # noqa: BLE001
        result["corp_code_error"] = redact_secrets(f"{type(exc).__name__}:{exc}")
    try:
        total = 0
        hits = 0
        pages = 0
        last_status = None
        for page in range(1, 6):
            listing = requests.get(
                "https://opendart.fss.or.kr/api/list.json",
                params={
                    "crtfc_key": key,
                    "bgn_de": "20260701",
                    "end_de": "20260930",
                    "page_no": page,
                    "page_count": 100,
                },
                timeout=30,
            ).json()
            last_status = listing.get("status")
            items = listing.get("list", [])
            if not items:
                break
            pages += 1
            total += len(items)
            hits += int(sum("분배" in str(item.get("report_nm", "")) for item in items))
        result.update(
            {
                "list_status": last_status,
                "list_pages": pages,
                "list_items_total": total,
                "distribution_report_hits": hits,
            }
        )
    except Exception as exc:  # noqa: BLE001
        result["list_error"] = redact_secrets(f"{type(exc).__name__}:{exc}")
    result["usability_for_etf_distributions"] = "unverified_no_structured_dataset"
    return result


def source_inventory(
    dart: dict[str, Any] | None = None,
    *,
    snapshot_status: str = "failed",
    price_status: str = "failed",
    pilot_meta: list[dict[str, Any]] | None = None,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []

    krx = http_probe(
        "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201020101"
    )
    rows.append(
        {
            "source": "KRX 정보데이터시스템 (pykrx 경유)",
            "dataset": "ETF 전종목 시세/개별종목 시세/종목 기본정보",
            "access_status": classify_krx_access(snapshot_status, price_status),
            "evidence": (
                f"snapshot={snapshot_status}, price_pilot={price_status}, "
                f"pilot_rows={sum(int(item.get('rows', 0)) for item in (pilot_meta or []))}"
            ),
            "period": "2016-01-05~2026-09-28 (캐시 기준)",
            "fields": "NAV,시가,고가,저가,종가,거래량,거래대금,기초지수",
            "auth_cost": "KRX_ID/KRX_PW (.env)",
            "automation": "pykrx 호출(일별 전종목)",
            "independence": "KRX 단일 원천(pykrx와 캐시는 동일 원천)",
            "crosscheck": "동일 원천 내부 일치성만 확인 가능",
            "observed_at": krx.get("observed_at"),
            "http_status": krx.get("status"),
        }
    )
    kind = http_probe("http://kind.krx.co.kr/disclosure/searchtotalinfo.do?method=searchTotalInfoMain")
    rows.append(
        {
            "source": "KRX KIND 공시",
            "dataset": "ETF 기업행위(분할/합병/상장폐지/정산) 공식 공시",
            "access_status": "blocked",
            "evidence": f"HTTP {kind.get('status')} (Access Denied)" if kind.get("status") else kind.get("error"),
            "period": "미확인",
            "fields": "공시 문서(PDF/HTML)",
            "auth_cost": "없음(공개)",
            "automation": "비공식 스크래핑 필요, 현재 차단",
            "independence": "KRX 공식(독립 아님)",
            "crosscheck": "문서 단위 수동 대조만 가능",
            "observed_at": kind.get("observed_at"),
            "http_status": kind.get("status"),
        }
    )
    seibro = http_probe(
        "https://seibro.or.kr/websquare/control.jsp?w2xPath=/IPORTAL/user/company/BIP_CNTS01001V.xml"
    )
    rows.append(
        {
            "source": "한국예탁결제원 SEIBro",
            "dataset": "ETF 분배금/권리 일정",
            "access_status": "unverified_js_only",
            "evidence": f"HTTP {seibro.get('status')}, {seibro.get('bytes')} bytes (JS shell)",
            "period": "미확인",
            "fields": "미확인",
            "auth_cost": "공개 웹, 세션/JS 필요",
            "automation": "구조화 API 미확인",
            "independence": "예탁결제원(가격 원천과 독립 가능성)",
            "crosscheck": "API 확보 시 가능",
            "observed_at": seibro.get("observed_at"),
            "http_status": seibro.get("status"),
        }
    )
    dart = dart if dart is not None else dart_probe()
    dart_status = classify_dart_access(dart)
    rows.append(
        {
            "source": "금융감독원 DART OpenAPI",
            "dataset": "자산운용사 공시(분배금/펀드 변경)",
            "access_status": dart_status,
            "evidence": (
                f"corpCode {dart.get('corp_code_http')}({dart.get('corp_code_bytes')}B), "
                f"list {dart.get('list_status')}, {dart.get('list_pages')}p/"
                f"{dart.get('list_items_total')}건, 분배 키워드 "
                f"{dart.get('distribution_report_hits')}건"
            ),
            "period": "공시 원문 기준(가변)",
            "fields": "공시 제목/접수일/원문 문서(정형 필드 없음)",
            "auth_cost": "DART_API_KEY(무료, 호출 제한)",
            "automation": "공시 검색은 가능, 분배금 정형 추출은 수동/파싱 필요",
            "independence": "금감원(가격 원천과 독립)",
            "crosscheck": "문서 단위 수동 대조",
            "observed_at": dart.get("observed_at"),
        }
    )
    issuers = {
        "HANARO ETF": "https://www.hanaroetf.com/",
        "KODEX": "https://www.samsungfund.com/",
        "TIGER ETF": "https://www.tigeretf.com/",
        "KBSTAR ETF": "https://www.kbstaretf.com/",
        "KOSEF ETF": "https://www.kosefetf.com/",
        "ARIRANG ETF": "https://www.arirangetf.com/",
    }
    for name, url in issuers.items():
        probe = http_probe(url)
        rows.append(
            {
                "source": f"운용사 {name}",
                "dataset": "ETF 분배금 지급 내역/공지",
                "access_status": "verified_page_only" if probe.get("status") == 200 else "blocked_or_unreachable",
                "evidence": (
                    f"HTTP {'→'.join(map(str, [*probe.get('redirect_history', []), probe.get('status')]))}"
                    f", {probe.get('bytes')} bytes"
                    if probe.get("status")
                    else probe.get("error")
                ),
                "period": "미확인",
                "fields": "운용사별 상이",
                "auth_cost": "공개",
                "automation": "사이트별 HTML/API 상이, 구조화 API 미확인",
                "independence": "운용사 공식",
                "crosscheck": "문서 단위",
                "observed_at": probe.get("observed_at"),
                "http_status": probe.get("status"),
            }
        )
    rows.append(
        {
            "source": "증권사 API (KIS/Kiwoom/NH)",
            "dataset": "시세/잔고(과거 분배금·기업행위 이력 데이터 없음)",
            "access_status": "implemented_not_probed",
            "evidence": "live_trading/adapter 코드 존재, 이번 조사에서는 조회 호출 미실행",
            "period": "API별 상이",
            "fields": "현재가/호가/체결/잔고",
            "auth_cost": "앱키/시크릿(.env 존재)",
            "automation": "가능(코드 존재)",
            "independence": "한국거래소 시세 기반(가격 원천 독립 아님)",
            "crosscheck": "가격 동일 원천, 기업행위/분배금 미제공",
        }
    )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    load_env()
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PILOT_DIR.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).isoformat()

    freeze = json.loads((ROOT / "strategy_freeze.json").read_text(encoding="utf-8"))
    inventory = pd.DataFrame(
        [
            {
                "component": "PIT membership 스냅샷/빌더",
                "implementation": "scripts/build_point_in_time_universe.py, pit_universe.py",
                "status": "구현 완료, 실제 데이터 캐시 미포함",
                "evidence": "docs/point-in-time-universe.md: 124 snapshots/74,129 rows/hash 기록. 캐시는 gitignored",
                "blocker": "캐시 재생성 필요(원천 재조회), snapshot은 결정일 격자",
            },
            {
                "component": "PIT membership 백테스트 연결",
                "implementation": "pit_universe.add_pit_membership_flag, build_pit_ticker_groups, run_etf_strategy",
                "status": "구현 및 fixture 검증 완료",
                "evidence": "tests/test_pit_universe.py, P1 계열 회귀 PASS",
                "blocker": "없음(가격/멤버십 경로)",
            },
            {
                "component": "PIT historical classification/tax",
                "implementation": "scripts/restore_pit_classification.py(추정), strict preflight",
                "status": "부분 구현(추정 복원만)",
                "evidence": "docs: 227종목 키워드 추정, 96.1%/91.6% 정확도, effective-dated 원천 없음",
                "blocker": "effective_start/end·group·taxable·source 원천 미확보 → strict fail-closed",
            },
            {
                "component": "기업행위 ledger/manifest",
                "implementation": "etf_corporate_actions.py, data/etf_corporate_actions*.{csv,json}",
                "status": "구현 완료, 실제 데이터 미확보",
                "evidence": "template 0 events, coverage_status=INCOMPLETE, preflight BLOCKED",
                "blocker": "공식 KIND/운용사 문서·기간·티커·SHA 미확보",
            },
            {
                "component": "strict approval preflight",
                "implementation": "run_etf_backtest.py --approval-strict",
                "status": "구현 및 실행 검증 완료(차단 동작)",
                "evidence": "outputs_p2a1/approval_preflight/approval_report.json status=BLOCKED",
                "blocker": "ledger/manifest coverage 입력 필요",
            },
            {
                "component": "분배금 CSV/Total Return",
                "implementation": "etf_distributions.py, data/etf_distributions.csv",
                "status": "구현 완료, 실제 데이터 미확보",
                "evidence": "CSV header-only, loader rows=0",
                "blocker": "기준일/배당락/지급일/주당/수정·취소/세금 원천 미확보",
            },
            {
                "component": "캐시 digest·provenance",
                "implementation": "p0b.data_cache_digest, strategy_freeze, outputs_* provenance",
                "status": "부분 구현",
                "evidence": "가격 캐시 digest·freeze hash는 검증됨. PIT/기업행위/분배금 원천 manifest는 미완",
                "blocker": "원천별 document/hash/coverage 미확보",
            },
        ]
    )
    inventory.to_csv(OUTPUT_DIR / "implementation_inventory.csv", index=False, encoding="utf-8-sig")

    ledger_info = ledger_probe()
    distribution_info = distribution_probe()
    preflight = run_approval_preflight()

    snapshots = krx_snapshot_probe()
    prices, pilot_meta = krx_pilot_prices()
    if not prices.empty:
        prices.to_csv(PILOT_DIR / "pilot_prices_normalized.csv", index=False, encoding="utf-8-sig")
        for ticker, group in prices.groupby("ticker"):
            group.to_csv(
                PILOT_DIR / f"{ticker}_krx_prices.csv", index=False, encoding="utf-8-sig"
            )
    comparison = (
        cross_check_cache(prices) if not prices.empty else empty_comparison_frame()
    )
    comparison.to_csv(
        OUTPUT_DIR / "cross_source_comparison.csv", index=False, encoding="utf-8-sig"
    )
    expected_tickers = {ticker for ticker, _ in PILOT_TICKERS}
    price_status = classify_price_path(pilot_meta, comparison, expected_tickers)
    snapshot_status = classify_snapshot(snapshots["snapshot_probe"])
    membership = pit_membership_probe(snapshots["snapshot_probe"])
    snapshot_output = {
        **snapshots,
        "snapshot_probe": [
            {key: value for key, value in item.items() if key not in {"tickers", "ohlcv_tickers"}}
            for item in snapshots["snapshot_probe"]
        ],
    }

    dart = dart_probe()
    sources = source_inventory(
        dart,
        snapshot_status=snapshot_status,
        price_status=price_status,
        pilot_meta=pilot_meta,
    )
    sources.to_csv(OUTPUT_DIR / "data_source_inventory.csv", index=False, encoding="utf-8-sig")

    coverage = pd.DataFrame(
        [
            {
                "dataset": "거래일별 ETF 목록",
                "available": (
                    "verified_ticker_set"
                    if membership.get("status") == "verified"
                    else ("partial_row_count_only" if membership.get("status") == "partial_no_snapshot_cache" else "partial_snapshot_mismatch")
                ),
                "basis": "derived:sample_probe",
                "coverage": "2016-08-01 220종목(OHLCV rows), 2026-07-21 전종목",
                "fields": "ticker, name, OHLCV, NAV, 거래대금",
                "limitation": "일별 전종목 조회는 KRX 호출 필요, snapshot 격자는 결정일 기준",
                "approval_impact": "가격/멤버십 승인 경로 사용 가능",
            },
            {
                "dataset": "상장일/상장폐지일",
                "available": "partial",
                "coverage": "캐시 첫/마지막 관측일로만 추정",
                "fields": "first_date, last_date(관측)",
                "limitation": "공식 상장/폐지일·사유·정산 정보 없음",
                "approval_impact": "폐지/정산 승인 blocker 유지",
            },
            {
                "dataset": "과거 시점별 자산군/시장 분류",
                "available": "not_available",
                "coverage": "현재 분류 + 키워드 추정만 존재",
                "fields": "-",
                "limitation": "effective-dated 공식 분류 없음",
                "approval_impact": "PIT strict 승인 blocker",
            },
            {
                "dataset": "과거 시점별 과세 유형",
                "available": "not_available",
                "coverage": "-",
                "fields": "-",
                "limitation": "effective-dated taxable source 없음",
                "approval_impact": "PIT strict 승인 blocker",
            },
            {
                "dataset": "거래정지/재개 이력",
                "available": "not_available",
                "coverage": "-",
                "fields": "-",
                "limitation": "pykrx ETF API 미제공, KIND 차단",
                "approval_impact": "기업행위 승인 blocker",
            },
            {
                "dataset": "가격·거래량·거래대금",
                "available": snapshot_status if price_status.startswith("verified") else price_status,
                "basis": "derived:sample_probe",
                "coverage": "동결 16종 2016~2026 캐시",
                "fields": "OHLCV, trading_value, NAV",
                "limitation": "단일 원천, 독립 교차검증 불가",
                "approval_impact": "가격 경로 승인 가능(독립성 한계 명시)",
            },
            {
                "dataset": "기업행위(분할/합병/폐지/정산)",
                "available": "not_available",
                "coverage": "ledger 0 events",
                "fields": "ledger schema만 존재",
                "limitation": "공식 문서·기간·SHA 미확보",
                "approval_impact": "승인 blocker",
            },
            {
                "dataset": "분배금(기준일/배당락/지급일/주당/세금)",
                "available": "not_available",
                "coverage": "CSV 0 rows",
                "fields": "schema만 존재",
                "limitation": "구조화 API/공식 문서 미확보",
                "approval_impact": "total_return 승인 blocker",
            },
        ]
    )
    coverage.to_csv(OUTPUT_DIR / "coverage_matrix.csv", index=False, encoding="utf-8-sig")

    blockers = pd.DataFrame(
        [
            {
                "blocker_id": "BLOCKER-PIT-CLASS-TAX",
                "area": "PIT",
                "description": "historical classification/tax effective-dated 원천 없음",
                "evidence": "docs/point-in-time-universe.md, strict preflight fail-closed",
                "status": "open",
                "unblocks_with": "공식 effective-dated 분류/과세 데이터 + document hash",
            },
            {
                "blocker_id": "BLOCKER-CA-COVERAGE",
                "area": "Corporate actions",
                "description": "공식 기업행위 coverage/manifest 없음(ledger 0 events, INCOMPLETE)",
                "evidence": "; ".join(sorted({b["code"] for b in preflight.get("blockers", [])}))
                or "preflight BLOCKED",
                "status": "open",
                "unblocks_with": "KIND/운용사 공식 문서, 기간·티커·SHA, coverage_status=VERIFIED",
            },
            {
                "blocker_id": "BLOCKER-DISTRIBUTION",
                "area": "Distributions/Total return",
                "description": "분배금 이력 원천 없음(CSV 0 rows)",
                "evidence": f"distributions rows={distribution_info['rows']}",
                "status": "open",
                "unblocks_with": "기준일/배당락/지급일/주당/수정·취소/세금 원천",
            },
            {
                "blocker_id": "BLOCKER-INDEPENDENT-SOURCE",
                "area": "Cross-source",
                "description": "독립 가격/분배금 원천 확보 실패(KIND 403, SEIBro JS, 운용사 차단/미확인)",
                "evidence": "outputs_p2a1/data_source_inventory.csv",
                "status": "open",
                "unblocks_with": "SEIBro/운용사 구조화 API 또는 계약 데이터",
            },
            {
                "blocker_id": "BLOCKER-OOS",
                "area": "Validation scope",
                "description": "46거래일 freeze 모니터링은 독립 OOS가 아님",
                "evidence": "docs/backtest-integrity.md",
                "status": "open",
                "unblocks_with": "실행 기간 연장 및 별도 OOS 설계",
            },
        ]
    )
    blockers.to_csv(OUTPUT_DIR / "approval_blockers.csv", index=False, encoding="utf-8-sig")

    independent_status = classify_independent_source(sources)
    corporate_status = (
        "verified"
        if ledger_info["approval_valid"] and preflight.get("status") == "VERIFIED"
        else "blocked"
    )
    distribution_status = classify_distribution(distribution_info)

    pilot_validation = {
        "phase": "P2-A-1",
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "pilot_tickers": [{"ticker": t, "reason": r} for t, r in PILOT_TICKERS],
        "paths": {
            "price_krx_to_compare": {
                "status": price_status,
                "steps": [
                    "KRX(pykrx) 개별종목 시세 조회",
                    "정규화(date/ticker/OHLCV/NAV)",
                    "provenance(SHA256, 조회시점, pykrx 버전)",
                    "기존 data_cache 가격과 표본 대조",
                ],
                "pilot_meta": pilot_meta,
                "cross_check": comparison.to_dict(orient="records") if not comparison.empty else [],
            },
            "corporate_action_to_preflight": {
                "status": corporate_status,
                "ledger": ledger_info,
                "preflight": preflight,
                "note": "공식 이벤트 원천 미확보. 변환/검증 경로는 차단 동작으로만 확인.",
            },
            "distribution_to_total_return": {
                "status": distribution_status,
                "distribution": distribution_info,
                "note": "분배금 원천 미확보. total return 승인 근거 사용 불가.",
            },
            "independent_cross_check": {
                "status": independent_status,
                "note": "KRX 외 구조화 원천 미확보. pykrx와 캐시는 동일 원천.",
            },
        },
        "krx_snapshot_probe": snapshot_output,
        "krx_snapshot_status": snapshot_status,
        "pit_membership": membership,
        "pilot_completeness": pilot_completeness(pilot_meta, comparison, expected_tickers),
        "dart_probe": dart,
        "sources": sources.to_dict(orient="records"),
        "blockers": blockers.to_dict(orient="records"),
        "freeze": {
            "freeze_sha256": freeze.get("sha256"),
            "freeze_date": freeze.get("freeze_date"),
            "universe": freeze.get("universe"),
        },
        "pykrx_version": _pykrx_version(),
        "env_presence": env_presence(),
        "limits": {
            "small_sample_only": True,
            "no_bulk_collection": True,
            "no_independent_source": True,
            "blockers_not_bypassed": True,
            "no_fabricated_data": True,
        },
    }
    (OUTPUT_DIR / "pilot_validation.json").write_text(
        dump_json(pilot_validation), encoding="utf-8"
    )

    artifacts = {
        str(path.relative_to(OUTPUT_DIR)): file_sha256(path)
        for path in sorted(OUTPUT_DIR.rglob("*"))
        if path.is_file() and path.name != "provenance.json"
    }
    provenance = {
        "phase": "P2-A-1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "code_digest": code_digest(),
        "code_files": list(CODE_FILES),
        "freeze_sha256": freeze.get("sha256"),
        "freeze_date": freeze.get("freeze_date"),
        "universe": freeze.get("universe"),
        "data_cache_digest": _data_cache_digest(),
        "ledger_sha256": ledger_info["ledger_sha256"],
        "manifest_sha256": ledger_info["manifest_sha256"],
        "distributions_sha256": distribution_info["sha256"],
        "pykrx_version": _pykrx_version(),
        "query_dates": list(SNAPSHOT_DATES),
        "pilot_windows": PILOT_WINDOWS,
        "sources_probed": sources["source"].tolist(),
        "artifacts": artifacts,
        "package_versions": {
            "pandas": pd.__version__,
            "numpy": np.__version__,
            "requests": requests.__version__,
            "pykrx": _pykrx_version(),
        },
        "code_digest_scope": list(CODE_FILES),
        "raw_response_provenance": "not_stored(small probes only; no bulk raw responses)",
        "raw_data_committed": False,
    }
    (OUTPUT_DIR / "provenance.json").write_text(dump_json(provenance), encoding="utf-8")
    print(
        f"[p2a1] 완료: {OUTPUT_DIR} "
        f"(pilot={len(pilot_meta)}, sources={len(sources)}, blockers={len(blockers)}, "
        f"preflight={preflight.get('status')})"
    )
    return 0


def _pykrx_version() -> str:
    import pykrx

    return str(getattr(pykrx, "__version__", "unknown"))


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=10
        ).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _data_cache_digest() -> str:
    try:
        import p0b_execution_stress as p0b

        return p0b.data_cache_digest()
    except Exception:  # noqa: BLE001
        return "unavailable"


if __name__ == "__main__":
    sys.exit(main())
