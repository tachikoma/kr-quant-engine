"""P2-A-2b: 동결 ETF 기업행위 실데이터 확보와 ledger/manifest 검증.

범위:
- 공식 원천 접근성 조사(KIND/운용사/예탁결제원/KRX/DART/증권사).
- P2-A-2a에서 확보한 분배금 이벤트를 재사용해 부분 ledger/manifest를 구성.
- 비분배 이벤트(분할·거래정지·상장폐지·정산)는 공식 근거가 확인된 것만 기록하고,
  확인되지 않은 absence를 "이벤트 없음"으로 승격하지 않는다.
- 실데이터 E2E는 확보된 이벤트 유형(분배금)에만 수행하고, 합성 fixture와 분리한다.

운영 경로·캐시·동결 파일은 변경하지 않는다. 산출물은 `outputs_p2a2b/`에만 쓴다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import p2a2a_etf_distributions as dist

from etf_corporate_actions import (
    EventType,
    canonical_ledger_sha256,
    create_distribution_receivable,
    load_corporate_action_ledger,
    process_pending_receivables,
)

OUTPUT_DIR = ROOT / "outputs_p2a2b"
BACKTEST_START = date(2016, 1, 5)
BACKTEST_END = date(2026, 9, 28)
LEDGER_EVENT_TYPES = [
    "SPLIT",
    "REVERSE_SPLIT",
    "SUSPENSION_START",
    "SUSPENSION_END",
    "DELISTING",
    "CASH_SETTLEMENT",
    "REDEMPTION",
    "CASH_DISTRIBUTION",
]

SOURCE_SURVEY: list[dict[str, Any]] = [
    {
        "source_id": "kind",
        "name": "KRX KIND 전자공시",
        "method": "GET 공시 문서/검색",
        "result": "http_403",
        "structured": "html_table",
        "covers": "분할·합병·거래정지·상장폐지·명칭변경",
        "note": "Akamai Access Denied. 공시 존재 여부 자체를 이 환경에서 확인할 수 없음",
        "usable": "manual_only",
    },
    {
        "source_id": "issuer_kodex",
        "name": "삼성자산운용 KODEX",
        "method": "GET 상품/공지(공개)",
        "result": "api_200_html_429",
        "structured": "json/html",
        "covers": "분배금, 상품 공지",
        "note": "분배금 API는 200. 공지 HTML은 반복 호출 후 429(Cloudflare)",
        "usable": "yes_for_distributions",
    },
    {
        "source_id": "issuer_tiger",
        "name": "미래에셋 TIGER",
        "method": "GET",
        "result": "http_403",
        "structured": "unknown",
        "covers": "공지·상품 문서",
        "note": "tigeretf.com 및 investments.miraeasset.com 모두 403",
        "usable": "no",
    },
    {
        "source_id": "issuer_ace",
        "name": "한국투자 ACE",
        "method": "GET",
        "result": "page_200_api_blocked",
        "structured": "no",
        "covers": "공지·상품 문서",
        "note": "상품 페이지는 200이나 데이터 API 호스트 타임아웃",
        "usable": "no",
    },
    {
        "source_id": "issuer_plus",
        "name": "한화자산운용 PLUS",
        "method": "GET",
        "result": "connection_failed",
        "structured": "unknown",
        "covers": "공지·상품 문서",
        "note": "TCP 연결 실패. 명칭 변경 공지 확인 불가",
        "usable": "no",
    },
    {
        "source_id": "issuer_rise",
        "name": "KB자산운용 RISE",
        "method": "GET",
        "result": "connection_failed",
        "structured": "unknown",
        "covers": "공지·상품 문서",
        "note": "TCP 연결 실패. 명칭 변경 공지 확인 불가",
        "usable": "no",
    },
    {
        "source_id": "seibro",
        "name": "한국예탁결제원 SEIBro",
        "method": "GET",
        "result": "js_shell_only",
        "structured": "no",
        "covers": "권리일정·기업행위",
        "note": "WebSquare 셸만 반환, 구조화 계약 미확인",
        "usable": "no",
    },
    {
        "source_id": "dart",
        "name": "DART OpenAPI(펀드공시 G)",
        "method": "API list.json",
        "result": "accessible",
        "structured": "metadata",
        "covers": "펀드 명칭·설명서·신고서",
        "note": (
            "5개 운용사 G공시 12,312~32,072건. 2016-2026 전체 나열은 범위 밖이며, "
            "좁은 구간 표본에서 명칭 변경 공지 자체는 확인되지 않음"
        ),
        "usable": "partial",
    },
    {
        "source_id": "krx_mdc",
        "name": "KRX 정보데이터시스템",
        "method": "pykrx/JSON(로그인)",
        "result": "accessible",
        "structured": "yes",
        "covers": "종목 기본정보·상장일·ISIN·현재 상장상태",
        "note": "이벤트 이력 API는 없음. 현재 스냅샷만 제공",
        "usable": "metadata_only",
    },
    {
        "source_id": "kis_quotes",
        "name": "KIS 조회 전용 시세",
        "method": "API(주문 없음)",
        "result": "accessible",
        "structured": "yes",
        "covers": "가격 연속성(보조)",
        "note": "기업행위 공시가 아니므로 확정 근거로 사용하지 않음",
        "usable": "supporting_only",
    },
]

# 공식 공시가 아니라 DART 펀드공시에서 관측한 명칭 상태다. ledger 이벤트로 승격하지 않는다.
NAME_OBSERVATIONS: list[dict[str, Any]] = [
    {
        "ticker": "161510",
        "event_type": "NAME_CHANGE",
        "observed_name": "한화PLUS고배당주증권상장지수투자신탁(주식)",
        "observed_at": "2023-01-12",
        "receipt_no": "20230112000376",
        "source": "DART",
        "status": "UNVERIFIED",
        "ledger_mappable": False,
        "note": "PLUS 브랜드 사용은 확인되나 명칭 변경 공지·효력일은 확인되지 않음",
    },
    {
        "ticker": "367760",
        "event_type": "NAME_CHANGE",
        "observed_name": "KBRISE네트워크인프라증권상장지수투자신탁(주식)",
        "observed_at": "2024-01-17",
        "receipt_no": "20240117000280",
        "source": "DART",
        "status": "UNVERIFIED",
        "ledger_mappable": False,
        "note": "RISE 브랜드 사용은 확인되나 명칭 변경 공지·효력일은 확인되지 않음",
    },
]

ANOMALY_DATES = ["2026-03-05", "2026-07-31", "2026-08-03"]


def load_events(p2a2a_dir: Path) -> pd.DataFrame:
    path = p2a2a_dir / "distribution_events.csv"
    if not path.exists():
        raise FileNotFoundError(f"P2-A-2a 이벤트가 없습니다: {path}")
    events = pd.read_csv(path, dtype={"ticker": str})
    required = {
        "ticker",
        "isin",
        "name",
        "record_date",
        "payment_date",
        "amount_per_share",
        "ex_date",
        "source_url",
        "source_sha256",
        "retrieved_at",
    }
    missing = required - set(events.columns)
    if missing:
        raise ValueError(f"이벤트 컬럼 누락: {sorted(missing)}")
    return events[events["in_backtest_window"].astype(bool)].reset_index(drop=True)


def ledger_rows(events: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    for row in events.itertuples():
        if not row.ex_date or not row.payment_date:
            continue
        rows.append(
            {
                "event_id": f"P2A2B-{row.ticker}-{row.record_date}",
                "ticker": row.ticker,
                "event_type": EventType.CASH_DISTRIBUTION.value,
                "event_date": row.ex_date,
                "record_date": row.record_date,
                "ex_date": row.ex_date,
                "payment_date": row.payment_date,
                "settlement_date": "",
                "ratio_num": "",
                "ratio_den": "",
                "cash_amount": f"{float(row.amount_per_share):.4f}".rstrip("0").rstrip("."),
                "currency": "KRW",
                "source_document_id": f"kodex_api_{row.ticker}",
                "source_document": f"kodex_api_{row.ticker}",
                "source_url": row.source_url,
                "source_sha256": row.source_sha256,
                "notes": (
                    "P2-A-2a 분배금 이벤트. ex_date는 기준일 직전 거래일 파생값"
                    f"(공식 확정 아님). 최초수집 {row.retrieved_at}"
                ),
            }
        )
    return rows


def write_ledger(events: pd.DataFrame) -> dict[str, Any]:
    ledger_path = OUTPUT_DIR / "corporate_action_ledger.csv"
    manifest_path = OUTPUT_DIR / "corporate_action_manifest.json"
    rows = ledger_rows(events)
    columns = [
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
    ]
    pd.DataFrame(rows, columns=columns).to_csv(ledger_path, index=False)
    documents = [
        {
            "document_id": f"kodex_api_{ticker}",
            "source_url": url,
            "sha256": sha,
        }
        for ticker, url, sha in sorted(
            {(row["ticker"], row["source_url"], row["source_sha256"]) for row in rows}
        )
    ]
    starts = [date.fromisoformat(row["ex_date"]) for row in rows]
    ends = [date.fromisoformat(row["payment_date"]) for row in rows]
    manifest = {
        "manifest_version": 1,
        "ledger_file": ledger_path.name,
        "ledger_sha256": "0" * 64,
        "verification_period": {
            "start": min(starts).isoformat(),
            "end": max(ends).isoformat(),
        },
        "verification_tickers": sorted({row["ticker"] for row in rows}),
        "source_documents": documents,
        "coverage_status": "PARTIAL",
        "notes": (
            "KODEX 공식 API에서 확보한 분배금 이벤트만 포함. 동결 16종 전체/전 기간 "
            "coverage 아님. ex_date는 기준일 직전 거래일 파생값이며 공식 확정값이 아님"
        ),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    provisional = load_corporate_action_ledger(ledger_path, manifest_path)
    manifest["ledger_sha256"] = canonical_ledger_sha256(provisional.events)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    ledger = load_corporate_action_ledger(ledger_path, manifest_path)
    report = ledger.approval_report()
    summary = {
        "ledger_path": str(ledger_path.relative_to(ROOT)),
        "manifest_path": str(manifest_path.relative_to(ROOT)),
        "ledger_sha256": manifest["ledger_sha256"],
        "ledger_file_sha256": dist.sha256_file(ledger_path),
        "event_count": len(rows),
        "tickers": manifest["verification_tickers"],
        "status": report.status,
        "approval_valid": report.approval_valid,
        "blockers": [{"code": b.code, "message": b.message} for b in report.blockers],
    }
    return summary, ledger


def price_continuity(cache_dir: Path, tickers: list[str]) -> list[dict[str, Any]]:
    """분할/병합 후보를 가격 단절로 탐지한다(확정 근거 아님)."""
    rows = []
    for ticker in tickers:
        path = cache_dir / f"{ticker}.parquet"
        if not path.exists():
            rows.append({"ticker": ticker, "status": "cache_missing"})
            continue
        frame = pd.read_parquet(path)
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame.sort_values("date")
        returns = frame["close"].astype(float).pct_change()
        biggest = float(returns.abs().max())
        index = returns.abs().idxmax()
        rows.append(
            {
                "ticker": ticker,
                "status": "ok",
                "first_date": frame["date"].iloc[0].date().isoformat(),
                "last_date": frame["date"].iloc[-1].date().isoformat(),
                "max_abs_daily_return_pct": round(biggest * 100, 4),
                "max_move_date": frame.loc[index, "date"].date().isoformat(),
                "split_like_move": bool(biggest > 0.25),
                "known_market_anomaly": frame.loc[index, "date"].date().isoformat()
                in ANOMALY_DATES,
            }
        )
    return rows


def metadata_rows(tickers: list[str]) -> tuple[dict[str, dict[str, Any]], str]:
    try:
        return dist.fetch_krx_etf_metadata(tickers)
    except (RuntimeError, OSError, ValueError) as exc:
        return {}, str(exc)[:160]


def coverage_rows(
    tickers: list[str],
    metadata: dict[str, dict[str, Any]],
    event_counts: dict[str, int],
    continuity: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for ticker in tickers:
        listed = metadata.get(ticker, {}).get("listed_date", "")
        for event_type in LEDGER_EVENT_TYPES:
            if event_type == "CASH_DISTRIBUTION":
                count = event_counts.get(ticker, 0)
                status = "PARTIAL" if count else "UNAVAILABLE"
                reason = (
                    "KODEX 공식 API 2022년 이후 이벤트"
                    if count
                    else "공식 원천 접근 실패(이 환경) — 이벤트 없음이 아니라 미확보"
                )
            elif event_type in {"SPLIT", "REVERSE_SPLIT"}:
                info = continuity.get(ticker, {})
                status = "UNVERIFIED"
                reason = (
                    "가격 단절 없음(보조 근거)이나 공식 부재 확인 문서 없음"
                    if info.get("split_like_move") is False
                    else "가격 단절 탐지 — 공식 공시 미확인"
                )
            elif event_type == "DELISTING":
                status = "PARTIAL"
                reason = "KRX 현재 상장 상태만 확인(전 기간 공식 부재 문서 없음)"
            elif event_type in {"SUSPENSION_START", "SUSPENSION_END"}:
                status = "UNAVAILABLE"
                reason = "KIND 403으로 거래정지 공시 확인 불가"
            else:
                status = "UNVERIFIED"
                reason = "공식 정산·해지 문서 미확보"
            rows.append(
                {
                    "ticker": ticker,
                    "name": metadata.get(ticker, {}).get("name", ""),
                    "listed_date": listed,
                    "event_type": event_type,
                    "period_start": max(BACKTEST_START, date.fromisoformat(listed)) .isoformat()
                    if listed
                    else BACKTEST_START.isoformat(),
                    "period_end": BACKTEST_END.isoformat(),
                    "status": status,
                    "event_count": event_counts.get(ticker, 0) if event_type == "CASH_DISTRIBUTION" else 0,
                    "reason": reason,
                }
            )
    return rows


def real_e2e(ledger: Any) -> dict[str, Any]:
    """생성된 ledger 이벤트로 지급·적격성·중복 처리를 검증한다.

    P2-A-2a 원본 이벤트를 다시 쓰지 않고, ledger에 기록된 금액·날짜를 그대로 사용한다.
    기대값과 다르면 status=blocked로 실패한다.
    """
    results: dict[str, Any] = {}
    tickers = sorted({event.ticker for event in ledger.events})
    for ticker in tickers:
        actions = [
            event
            for event in ledger.events
            if event.ticker == ticker and event.event_type == EventType.CASH_DISTRIBUTION
        ]
        if not actions:
            continue
        event = max(actions, key=lambda item: item.event_date)
        quantity = 100
        expected = float(event.cash_amount) * quantity
        receivable = create_distribution_receivable(
            event, quantity, held_on_record_date=True, held_on_ex_date=True
        )
        not_entitled = create_distribution_receivable(
            event, quantity, held_on_record_date=True, held_on_ex_date=False
        )
        before = process_pending_receivables(
            [receivable], event.payment_date - timedelta(days=1)
        )
        after = process_pending_receivables([receivable], event.payment_date)
        paid_amount = float(after[1])
        checks = {
            "receivable_matches_ledger": bool(
                receivable is not None and abs(float(receivable.amount) - expected) < 1e-6
            ),
            "not_entitled_is_none": not_entitled is None,
            "paid_before_payment_date_zero": float(before[1]) == 0.0,
            "paid_on_payment_date_matches": abs(paid_amount - expected) < 1e-6,
            "post_sale_still_payable": paid_amount > 0,
        }
        results[ticker] = {
            "status": "ok" if all(checks.values()) else "blocked",
            "event_id": event.event_id,
            "event_date": event.event_date.isoformat(),
            "record_date": event.record_date.isoformat() if event.record_date else None,
            "payment_date": event.payment_date.isoformat() if event.payment_date else None,
            "cash_amount_per_share": float(event.cash_amount),
            "quantity": quantity,
            "expected_amount": round(expected, 4),
            "paid_amount": round(paid_amount, 4),
            "payment_lag_days": (
                event.payment_date - event.ex_date
            ).days if event.ex_date else None,
            "checks": checks,
        }
    return results


def run_preflight() -> dict[str, Any]:
    import subprocess

    output_dir = OUTPUT_DIR / "approval_preflight"
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(ROOT / "run_etf_backtest.py"),
        "--approval-strict",
        "--mode",
        "single",
        "--corporate-actions-ledger",
        str(OUTPUT_DIR / "corporate_action_ledger.csv"),
        "--corporate-actions-manifest",
        str(OUTPUT_DIR / "corporate_action_manifest.json"),
        "--approval-output-dir",
        str(output_dir),
    ]
    completed = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, timeout=900, check=False
    )
    report_path = output_dir / "approval_report.json"
    report = {}
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    return {
        "exit_code": completed.returncode,
        "status": report.get("status"),
        "approval_valid": report.get("approval_valid"),
        "blockers": [
            {"code": item.get("code"), "message": item.get("message")}
            for item in report.get("blockers", [])
        ],
        "stdout_tail": completed.stdout.strip().splitlines()[-3:],
        "stderr_tail": completed.stderr.strip().splitlines()[-3:],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "data_cache")
    parser.add_argument(
        "--p2a2a-dir",
        type=Path,
        default=Path(
            os.environ.get(
                "P2A2A_OUTPUT_DIR",
                "/home/ubuntu/investment/worktrees/p2a2a-dist/outputs_p2a2a",
            )
        ),
    )
    parser.add_argument("--skip-preflight", action="store_true")
    args = parser.parse_args()

    dist.load_env_file(Path(os.environ.get("ETF_ENV_FILE", ROOT / ".env")))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    run_id = dist.utc_now()

    tickers = dist.freeze_universe()
    events = load_events(args.p2a2a_dir)
    event_counts = events.groupby("ticker").size().to_dict()
    metadata, metadata_status = metadata_rows(tickers)
    continuity_rows = price_continuity(args.cache_dir, tickers)
    continuity = {row["ticker"]: row for row in continuity_rows}

    ledger_summary, ledger = write_ledger(events)
    coverage = coverage_rows(tickers, metadata, event_counts, continuity)
    e2e = real_e2e(ledger)
    preflight = {"status": "skipped"} if args.skip_preflight else run_preflight()

    pd.DataFrame(SOURCE_SURVEY).to_csv(OUTPUT_DIR / "corporate_action_source_inventory.csv", index=False)
    pd.DataFrame(NAME_OBSERVATIONS).to_csv(
        OUTPUT_DIR / "corporate_action_events.csv", index=False
    )
    pd.DataFrame(coverage).to_csv(OUTPUT_DIR / "corporate_action_coverage.csv", index=False)
    pd.DataFrame(continuity_rows).to_csv(
        OUTPUT_DIR / "corporate_action_crosscheck.csv", index=False
    )

    result = {
        "generated_at": run_id,
        "universe": tickers,
        "distribution_events": len(events),
        "distribution_tickers": sorted(event_counts),
        "metadata_status": metadata_status,
        "metadata_matched": sorted(metadata),
        "ledger": ledger_summary,
        "coverage_counts": pd.DataFrame(coverage)["status"].value_counts().to_dict(),
        "coverage_by_type": pd.DataFrame(coverage)
        .groupby(["event_type", "status"])
        .size()
        .reset_index(name="count")
        .to_dict(orient="records"),
        "e2e": e2e,
        "preflight": preflight,
        "name_observations": NAME_OBSERVATIONS,
        "price_continuity": continuity_rows,
        "anomaly_dates": ANOMALY_DATES,
        "anomaly_note": (
            "2026-03-05/07-31/08-03 기업행위 공시는 KIND 403으로 확인 불가. "
            "가격 이상 판정은 변경하지 않음"
        ),
        "total_return_approvable": False,
        "blockers": [
            {
                "code": "CA-COVERAGE",
                "status": "open",
                "message": (
                    "비분배 이벤트 공식 공시 미확보. 이벤트 없음이 아니라 확인 불가. "
                    "분배금만 4종/2022년 이후 PARTIAL"
                ),
            },
            {
                "code": "CA-NAME-CHANGE",
                "status": "open",
                "message": "브랜드 변경 관측은 있으나 공식 명칭변경 공지·효력일 미확인, 스키마 미지원",
            },
            {
                "code": "CA-SUSPENSION",
                "status": "open",
                "message": "거래정지 공시 원천(KIND) 403",
            },
        ],
        "limits": [
            "분할·병합 부재는 가격 연속성과 제도 맥락에 근거한 추정이며 공식 부재 확인이 아님",
            "KIND/운용사 공지 접근 실패는 환경 의존",
            "실데이터 E2E는 분배금 유형에만 적용(합성 fixture와 분리)",
            "DART 펀드공시 전체 나열은 범위 밖",
        ],
    }
    (OUTPUT_DIR / "pilot_validation.json").write_text(
        json.dumps(dist.sanitize_json(result), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )

    provenance = {
        "run_id": run_id,
        "git_commit": dist.git_commit(),
        "code_sha256": dist.sha256_file(Path(__file__)),
        "freeze_sha256": dist.sha256_file(ROOT / "strategy_freeze.json"),
        "source_events": {
            "path": str(args.p2a2a_dir / "distribution_events.csv"),
            "sha256": dist.sha256_file(args.p2a2a_dir / "distribution_events.csv"),
        },
        "artifacts": {},
        "env_presence": dist.env_presence(),
    }
    for name in [
        "corporate_action_source_inventory.csv",
        "corporate_action_events.csv",
        "corporate_action_coverage.csv",
        "corporate_action_crosscheck.csv",
        "corporate_action_ledger.csv",
        "corporate_action_manifest.json",
        "pilot_validation.json",
    ]:
        path = OUTPUT_DIR / name
        if path.exists():
            provenance["artifacts"][name] = {
                "sha256": dist.sha256_file(path),
                "bytes": path.stat().st_size,
            }
    (OUTPUT_DIR / "provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"[p2a2b] 완료: {OUTPUT_DIR} "
        f"(events={len(events)}, ledger={ledger_summary['status']}, "
        f"coverage={result['coverage_counts']}, preflight={preflight.get('status')})"
    )


if __name__ == "__main__":
    main()
