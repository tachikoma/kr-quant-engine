"""P2-A-2d-2: 실제 과세표준액으로 분배금 세후 회계를 검증한다.

- P2-A-2a의 74건 이벤트/과세표준액과 P2-A-2b ledger를 재사용한다.
- 세금 정책은 일반 과세계좌의 **명시적 연구 세율** 시나리오에 한정한다.
- PR #20의 연구 seam·상태 복원 계약을 그대로 사용한다.

산출물은 `outputs_p2a2d2/`에만 쓴다. 운영 경로·동결 전략·Track B는 변경하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import p2a2b_corporate_actions as ca
import p2a2d1_distribution_accounting as d1

from etf_corporate_actions import (
    TaxPolicy,
    canonical_ledger_sha256,
    load_corporate_action_ledger,
)
from run_etf_backtest import load_etf_price, run_etf_strategy

OUTPUT_DIR = ROOT / "outputs_p2a2d2"
TAX_COLUMNS = ["taxable_base_per_share", "tax_policy_id"]
LEDGER_COLUMNS = list(d1.LEDGER_COLUMNS) + TAX_COLUMNS
POLICY_ID = "general_account_research_15_4pct"
POLICY_RATE = Decimal("0.154")
POLICY = TaxPolicy(POLICY_ID, POLICY_RATE)

POLICY_DOC = {
    "policy_id": POLICY_ID,
    "account_type": "general_taxable_account",
    "rate": "0.154",
    "rate_components": {"income_tax": "0.14", "local_income_tax": "0.014"},
    "basis": "taxable_base_per_share",
    "checked_at": "2026-11-28",
    "official_sources": [
        {
            "name": "국가법령정보센터 소득세법 제17조 제1항 제5호",
            "url": "https://www.law.go.kr/LSW/lsLawLinkInfo.do?chrClsCd=010202&lsId=001565&lsJoLnkSeq=1000819918&print=print",
            "finding": "집합투자기구로부터의 이익은 배당소득에 포함",
        },
        {
            "name": "국가법령정보센터 소득세법 제129조 제1항 제2호 나목",
            "url": "https://www.law.go.kr/LSW//lsLawLinkInfo.do?chrClsCd=010202&lsId=001565&lsJoLnkSeq=1000224917&print=print",
            "finding": "배당소득 원천징수세율 14% (지방소득세 1.4% 별도)",
        },
        {
            "name": "국가법령정보센터 소득세법 시행규칙 제13조",
            "url": "https://www.law.go.kr/LSW//lsLawLinkInfo.do?chrClsCd=010202&lsId=007507&lsJoLnkSeq=1000316689&print=print",
            "finding": "집합투자기구 좌당 배당소득금액은 과세 대상 손익을 제외해 계산",
        },
        {
            "name": "삼성자산운용 KODEX 중개형 ISA 투자 가이드(2026-01-13)",
            "url": "https://www.samsungfund.com/upload/kodex/pdf/ISA_GUIDE_2026.pdf",
            "finding": "일반계좌 분배금은 MIN(분배금, 과표증분) × 15.4%",
        },
    ],
    "account_differences": {
        "ISA": "순이익 200만원 비과세 후 9.9% 분리과세(조특법 제91조의18). 이번 구현 범위 밖",
        "pension_IRP": "계좌 내 과세이연, 연금 수령 시 3.3~5.5%. 이번 구현 범위 밖",
    },
    "assumptions": [
        "15.4%는 일반 과세계좌의 연구용 명시 세율이며 모든 상품·계좌·역사 기간의 확정 세율이 아니다",
        "원 단위 절사·반올림 규칙은 공식 근거를 확인하지 못해 적용하지 않고 Decimal 정확값을 유지한다",
        "금융소득종합과세(연 2,000만원)와 매매차익 과세는 이번 범위 밖",
    ],
    "unverified": ["원천징수 원 단위 처리", "계좌별 실제 세액", "역사 기간별 세율 변동"],
}


def validate_taxable_base_input(events: pd.DataFrame) -> dict[str, int]:
    """과세표준액의 누락·NaN·Inf·음수·분배금 초과를 fail-closed로 검증한다."""
    counts = {
        "total": len(events),
        "missing": 0,
        "nan": 0,
        "inf": 0,
        "negative": 0,
        "excess": 0,
    }
    issues: list[str] = []
    for row in events.itertuples():
        raw = getattr(row, "taxable_base_per_share", None)
        label = f"{row.ticker}:{row.record_date}"
        if raw is None or str(raw).strip() == "":
            counts["missing"] += 1
            issues.append(f"{label}:missing")
            continue
        try:
            base = Decimal(str(raw).strip())
        except (InvalidOperation, ValueError):
            counts["missing"] += 1
            issues.append(f"{label}:non_numeric")
            continue
        if not base.is_finite():
            if base.is_nan():
                counts["nan"] += 1
                issues.append(f"{label}:NaN")
            else:
                counts["inf"] += 1
                issues.append(f"{label}:Inf")
            continue
        if base < 0:
            counts["negative"] += 1
            issues.append(f"{label}:negative")
        amount = Decimal(str(row.amount_per_share))
        if not amount.is_finite():
            issues.append(f"{label}:invalid_amount")
        elif amount < 0:
            issues.append(f"{label}:negative_amount")
        elif base > amount:
            counts["excess"] += 1
            issues.append(f"{label}:excess")
    if issues:
        raise ValueError(f"과세표준액 입력 검증 실패: {issues[:10]}")
    return counts


def load_events(p2a2a_dir: Path) -> pd.DataFrame:
    path = p2a2a_dir / "distribution_events.csv"
    provenance_path = p2a2a_dir / "provenance.json"
    if not path.exists() or not provenance_path.exists():
        raise FileNotFoundError(f"P2-A-2a 입력이 없습니다: {p2a2a_dir}")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    expected = (
        provenance.get("artifacts", {}).get("distribution_events.csv", {}).get("sha256")
    )
    actual = ca.dist.sha256_file(path)
    if not expected or expected != actual:
        raise ValueError(f"분배금 입력 SHA256 불일치: expected={expected} actual={actual}")
    events = pd.read_csv(path, dtype={"ticker": str})
    issues = ca.validate_events(events, ca.dist.freeze_universe())
    if issues:
        raise ValueError(f"분배금 입력 검증 실패: {issues}")
    return events


def build_tax_ledger(
    events: pd.DataFrame, name: str, *, drop_taxable: bool = False
) -> Any:
    """실제 과세표준액을 담은 부분 ledger를 만든다."""
    ledger_dir = OUTPUT_DIR / "scenario_ledgers"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = ledger_dir / f"{name}.csv"
    manifest_path = ledger_dir / f"{name}.json"
    rows = []
    for row in events.itertuples():
        base = "" if drop_taxable else row.taxable_base_per_share
        rows.append(
            {
                "event_id": f"P2A2D2-{row.ticker}-{row.record_date}",
                "ticker": row.ticker,
                "event_type": "CASH_DISTRIBUTION",
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
                "notes": f"P2-A-2a 실데이터. 최초수집 {row.retrieved_at}",
                "taxable_base_per_share": base,
                "tax_policy_id": POLICY_ID,
            }
        )
    pd.DataFrame(rows, columns=LEDGER_COLUMNS).to_csv(ledger_path, index=False)
    documents = [
        {
            "document_id": document_id,
            "source_url": url,
            "sha256": sha,
        }
        for document_id, url, sha in sorted(
            {(row["source_document_id"], row["source_url"], row["source_sha256"]) for row in rows}
        )
    ]
    manifest = {
        "manifest_version": 1,
        "ledger_file": ledger_path.name,
        "ledger_sha256": "0" * 64,
        "verification_period": {
            "start": min(row["event_date"] for row in rows),
            "end": max(row["payment_date"] for row in rows),
        },
        "verification_tickers": sorted({row["ticker"] for row in rows}),
        "source_documents": documents,
        "coverage_status": "PARTIAL",
        "tax_schema": "taxable_base_v1",
        "notes": "P2-A-2d-2 연구용 과세표준 ledger",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    provisional = load_corporate_action_ledger(ledger_path, manifest_path)
    manifest["ledger_sha256"] = canonical_ledger_sha256(provisional.events)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_corporate_action_ledger(ledger_path, manifest_path)


def engine_run(
    *,
    window: tuple[str, str],
    holdings: dict[str, int],
    cash: float,
    universe: list[str],
    price: pd.DataFrame,
    index_df: pd.DataFrame,
    ledger: Any,
    policy: TaxPolicy | None,
    market_filter: bool = False,
) -> dict[str, Any]:
    start, end = (pd.Timestamp(value) for value in window)
    dates = [d for d in index_df["date"] if start <= d <= end]
    init = {
        "cash": float(cash),
        "holdings": dict(holdings),
        "holding_cost_basis": {ticker: 16000.0 for ticker in holdings},
        "holding_peak_closes": {},
        "portfolio_peak_equity": None,
        "last_valid_closes": {},
    }
    result = run_etf_strategy(
        0.0,
        dates,
        index_df[index_df["date"].isin(dates)].reset_index(drop=True),
        use_market_filter=market_filter,
        price_data=price[price["ticker"].isin(universe)],
        universe_tickers=universe,
        initial_state=init,
        approval_strict=True,
        corporate_action_ledger=ledger,
        allow_partial_ledger=True,
        distribution_tax_policy=policy,
        return_final_state=True,
    )
    curve = result[0].reset_index(drop=True)
    trades = result[1].reset_index(drop=True)
    if trades.empty:
        trades = pd.DataFrame(columns=["date", "ticker", "side", "qty", "price", "cash_flow"])
    return {"curve": curve, "trades": trades, "final": result[-1], "dates": dates}


def expected_tax(amount: float, base: float, quantity: int, rate: Decimal) -> dict[str, Decimal]:
    gross = Decimal(str(amount)) * quantity
    taxable = Decimal(str(base)) * quantity
    withholding = taxable * rate
    return {
        "gross": gross,
        "taxable": taxable,
        "withholding": withholding,
        "net": gross - withholding,
    }


def classify_base_type(base: float, amount: float, row: Any) -> str:
    """비정상 과세표준이 정상 partial로 분류되지 않도록 fail-closed."""
    if (
        not math.isfinite(base)
        or not math.isfinite(amount)
        or base < 0
        or base > amount
    ):
        raise ValueError(
            f"비정상 과세표준액의 partial 분류 차단: {row.ticker} {row.record_date} "
            f"base={base} amount={amount}"
        )
    if base == amount:
        return "equal"
    if base == 0:
        return "zero"
    return "partial"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--p2a2a-dir",
        type=Path,
        default=Path("/home/ubuntu/investment/worktrees/p2a2a-dist/outputs_p2a2a"),
    )
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "data_cache")
    args = parser.parse_args()

    ca.dist.load_env_file(Path(os.environ.get("ETF_ENV_FILE", ROOT / ".env")))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    run_id = ca.dist.utc_now()
    events = load_events(args.p2a2a_dir)
    base_validation = validate_taxable_base_input(events)
    flags = events["in_backtest_window"].map(ca._parse_bool)
    if flags.isna().any():
        raise ValueError("비정상 in_backtest_window 값이 있습니다")
    in_window = events[flags.astype(bool)].reset_index(drop=True)
    price = load_etf_price(["069500", "091160", "091170", "498400"])
    index_df = pd.read_parquet(args.cache_dir / "index.parquet")
    index_df["date"] = pd.to_datetime(index_df["date"])

    # 이벤트 유형별 건수와 100좌 기준 세금 비교
    comparison_rows = []
    for row in in_window.itertuples():
        values = expected_tax(
            float(row.amount_per_share), float(row.taxable_base_per_share), 100, POLICY_RATE
        )
        gross_basis_tax = values["gross"] * POLICY_RATE
        comparison_rows.append(
            {
                "ticker": row.ticker,
                "record_date": row.record_date,
                "ex_date": row.ex_date,
                "payment_date": row.payment_date,
                "amount_per_share": float(row.amount_per_share),
                "taxable_base_per_share": float(row.taxable_base_per_share),
                "base_type": classify_base_type(
                    float(row.taxable_base_per_share), float(row.amount_per_share), row
                ),
                "gross_100": float(values["gross"]),
                "taxable_100": float(values["taxable"]),
                "withholding_base_100": float(values["withholding"]),
                "withholding_gross_100": float(gross_basis_tax),
                "withholding_diff_100": float(gross_basis_tax - values["withholding"]),
                "net_base_100": float(values["net"]),
                "net_gross_100": float(values["gross"] - gross_basis_tax),
            }
        )
    comparison = pd.DataFrame(comparison_rows)
    comparison.to_csv(OUTPUT_DIR / "distribution_tax_comparison.csv", index=False)
    comparison.to_csv(OUTPUT_DIR / "distribution_tax_events.csv", index=False)

    # 시나리오: 실제 이벤트를 한 건씩 담은 부분 ledger
    def find_event(ticker: str, record_date: str) -> pd.DataFrame:
        selected = in_window[
            (in_window["ticker"] == ticker) & (in_window["record_date"] == record_date)
        ]
        if selected.empty:
            raise ValueError(f"이벤트 없음: {ticker} {record_date}")
        return selected

    scenarios = {
        "taxable_equal_gross": ("069500", "2026-04-30", ("2026-04-27", "2026-05-08"), {"069500": 1000}, 0.0, False, True),
        "taxable_less_than_gross": ("498400", "2026-09-15", ("2026-09-11", "2026-09-22"), {"498400": 1000}, 0.0, False, True),
        "taxable_zero": ("498400", "2025-02-14", ("2025-02-11", "2025-02-20"), {"498400": 1000}, 0.0, False, True),
        "pre_tax_path": ("498400", "2026-09-15", ("2026-09-11", "2026-09-22"), {"498400": 1000}, 0.0, False, False),
        "sell_before_payment": ("091170", "2026-08-31", ("2026-08-28", "2026-09-04"), {"091170": 1000}, 0.0, True, True),
        "buy_on_ex": ("091170", "2026-08-31", ("2026-08-27", "2026-09-04"), {}, 1_000_000.0, True, True),
    }
    daily_rows: list[dict[str, Any]] = []
    scenario_rows: list[dict[str, Any]] = []
    runs: dict[str, dict[str, Any]] = {}
    for name, (ticker, record_date, window, holdings, cash, market_filter, use_policy) in scenarios.items():
        ledger = build_tax_ledger(find_event(ticker, record_date), name)
        run = engine_run(
            window=window,
            holdings=holdings,
            cash=cash,
            universe=[ticker],
            price=price,
            index_df=index_df,
            ledger=ledger,
            policy=POLICY if use_policy else None,
            market_filter=market_filter,
        )
        runs[name] = run
        event = ledger.events[0]
        expected = expected_tax(
            float(event.cash_amount),
            float(event.taxable_base_per_share),
            int(next(iter(holdings.values()), 0) or 0),
            POLICY_RATE if use_policy else Decimal(0),
        )
        curve = run["curve"].set_index("date")
        curve_rows = list(run["curve"].itertuples())
        ex_day = pd.Timestamp(event.ex_date)
        pay_day = pd.Timestamp(event.payment_date)
        receivable_on_ex = float(curve.loc[ex_day, "receivables"]) if ex_day in curve.index else None
        cash_before = float(curve.loc[pay_day - pd.Timedelta(days=1), "cash"]) if (pay_day - pd.Timedelta(days=1)) in curve.index else None
        cash_on_pay = float(curve.loc[pay_day, "cash"]) if pay_day in curve.index else None
        pay_idx = next(
            (i for i, row in enumerate(curve_rows) if pd.Timestamp(row.date) == pay_day), None
        )
        cash_delta_on_pay = (
            float(curve_rows[pay_idx].cash) - float(curve_rows[pay_idx - 1].cash)
            if pay_idx is not None and pay_idx > 0
            else None
        )
        trades_df = run["trades"]
        trade_count = len(trades_df)
        # engine_net_match는 무체결 시나리오 전용이다. 체결이 있는 매도/매수는
        # 아래 sell_before_payment_* / buy_on_ex_no_entitlement로 별도 검증한다.
        sell_rows = trades_df[trades_df["side"] == "SELL"] if trade_count else trades_df
        buy_rows = trades_df[trades_df["side"] == "BUY"] if trade_count else trades_df
        sell_qty = int(sell_rows["qty"].sum()) if len(sell_rows) else 0
        buy_qty = int(buy_rows["qty"].sum()) if len(buy_rows) else 0
        sell_dates = (
            [pd.Timestamp(value).date().isoformat() for value in sell_rows["date"]]
            if len(sell_rows)
            else []
        )
        buy_dates = (
            [pd.Timestamp(value).date().isoformat() for value in buy_rows["date"]]
            if len(buy_rows)
            else []
        )
        receivable_rows = curve[curve["receivables"] > 0]
        first_receivable = (
            float(receivable_rows.iloc[0]["receivables"]) if len(receivable_rows) else 0.0
        )
        net_match = (
            trade_count == 0
            and receivable_on_ex is not None
            and cash_delta_on_pay is not None
            and abs(receivable_on_ex - float(expected["net"])) < 1e-6
            and abs(cash_delta_on_pay - float(expected["net"])) < 1e-6
        )
        for row in run["curve"].itertuples():
            daily_rows.append(
                {
                    "scenario": name,
                    "date": pd.Timestamp(row.date).date().isoformat(),
                    "cash": float(row.cash),
                    "market_value": float(row.market_value),
                    "receivables": float(getattr(row, "receivables", 0.0) or 0.0),
                    "equity": float(row.equity),
                    "holdings": str(row.holdings),
                }
            )
        scenario_rows.append(
            {
                "scenario": name,
                "ticker": ticker,
                "policy": POLICY_ID if use_policy else "pre_tax",
                "event_id": event.event_id,
                "expected_gross": float(expected["gross"]),
                "expected_taxable": float(expected["taxable"]),
                "expected_withholding": float(expected["withholding"]),
                "expected_net": float(expected["net"]),
                "receivable_on_ex": receivable_on_ex,
                "cash_before_payment": cash_before,
                "cash_on_payment": cash_on_pay,
                "cash_delta_on_payment": cash_delta_on_pay,
                "first_receivable": first_receivable,
                "sell_qty": sell_qty,
                "sell_dates": sell_dates,
                "buy_qty": buy_qty,
                "buy_dates": buy_dates,
                "engine_net_match": net_match,
                "trades": trade_count,
            }
        )

    # 실패 시나리오: 과세표준 누락 / 다른 정책 상태 복원
    missing_ledger = build_tax_ledger(find_event("498400", "2026-09-15"), "missing_taxable", drop_taxable=True)
    missing_run = engine_run(
        window=("2026-09-11", "2026-09-22"),
        holdings={"498400": 1000},
        cash=0.0,
        universe=["498400"],
        price=price,
        index_df=index_df,
        ledger=missing_ledger,
        policy=POLICY,
    )
    missing_report = missing_run["final"].get("approval_report")
    missing_blocked = bool(
        missing_report is not None
        and any(blocker.code == "MISSING_TAXABLE_BASE" for blocker in missing_report.blockers)
    )

    policy_mismatch_blocked = False
    try:
        payload = json.loads(json.dumps(d1.serialize_state(runs["taxable_less_than_gross"]["final"])))
        payload["tax_policy_id"] = None
        state = d1.deserialize_state(
            payload,
            build_tax_ledger(find_event("498400", "2026-09-15"), "policy_mismatch"),
            POLICY,
        )
        from etf_corporate_actions import TaxPolicy as _TP

        run_etf_strategy(
            0.0,
            [pd.Timestamp("2026-09-16"), pd.Timestamp("2026-09-17")],
            index_df[index_df["date"].isin([pd.Timestamp("2026-09-16"), pd.Timestamp("2026-09-17")])].reset_index(drop=True),
            use_market_filter=False,
            price_data=price[price["ticker"] == "498400"],
            universe_tickers=["498400"],
            initial_state=state,
            approval_strict=True,
            corporate_action_ledger=build_tax_ledger(find_event("498400", "2026-09-15"), "policy_mismatch"),
            allow_partial_ledger=True,
            distribution_tax_policy=_TP("other_policy", POLICY_RATE),
            return_final_state=True,
        )
    except ValueError as exc:
        policy_mismatch_blocked = "정책" in str(exc)

    # 세후 상태 연속성: 지급 전/후 분할 실행
    single_run = engine_run(
        window=("2026-09-11", "2026-09-22"),
        holdings={"498400": 1000},
        cash=0.0,
        universe=["498400"],
        price=price,
        index_df=index_df,
        ledger=build_tax_ledger(find_event("498400", "2026-09-15"), "continuity_single"),
        policy=POLICY,
    )
    continuity_rows: list[dict[str, Any]] = []
    for label, split_date, resume_date in [
        ("before_payment", "2026-09-16", "2026-09-17"),
        ("after_payment", "2026-09-17", "2026-09-18"),
    ]:
        ledger_a = build_tax_ledger(find_event("498400", "2026-09-15"), f"continuity_{label}_a")
        ledger_b = build_tax_ledger(find_event("498400", "2026-09-15"), f"continuity_{label}_b")
        part_a = engine_run(
            window=("2026-09-11", split_date),
            holdings={"498400": 1000},
            cash=0.0,
            universe=["498400"],
            price=price,
            index_df=index_df,
            ledger=ledger_a,
            policy=POLICY,
        )
        payload = json.loads(json.dumps(d1.serialize_state(part_a["final"])))
        state = d1.deserialize_state(payload, ledger_b, POLICY)
        resumed_dates = [
            d
            for d in index_df["date"]
            if pd.Timestamp(resume_date) <= d <= pd.Timestamp("2026-09-22")
        ]
        resumed = run_etf_strategy(
            0.0,
            resumed_dates,
            index_df[index_df["date"].isin(resumed_dates)].reset_index(drop=True),
            use_market_filter=False,
            price_data=price[price["ticker"] == "498400"],
            universe_tickers=["498400"],
            initial_state=state,
            approval_strict=True,
            corporate_action_ledger=ledger_b,
            allow_partial_ledger=True,
            distribution_tax_policy=POLICY,
            return_final_state=True,
        )
        final = resumed[-1]
        single_final = single_run["final"]
        continuity_rows.append(
            {
                "scenario": label,
                "split_at": split_date,
                "single_cash": float(single_final["cash"]),
                "split_cash": float(final["cash"]),
                "cash_match": abs(float(single_final["cash"]) - float(final["cash"])) < 1e-9,
                "single_receivables": float(
                    sum(r.amount - r.tax_amount for r in single_final["pending_receivables"] if not r.paid)
                ),
                "split_receivables": float(
                    sum(r.amount - r.tax_amount for r in final["pending_receivables"] if not r.paid)
                ),
                "receivables_match": abs(
                    float(
                        sum(r.amount - r.tax_amount for r in single_final["pending_receivables"] if not r.paid)
                    )
                    - float(
                        sum(r.amount - r.tax_amount for r in final["pending_receivables"] if not r.paid)
                    )
                )
                < 1e-9,
                "tax_policy_match": final.get("tax_policy_id") == POLICY_ID,
                "single_equity": float(single_final["final_equity"] or 0.0),
                "split_equity": float(final["final_equity"] or 0.0),
                "equity_match": abs(
                    float(single_final["final_equity"] or 0.0)
                    - float(final["final_equity"] or 0.0)
                )
                < 1e-6,
            }
        )
    pd.DataFrame(continuity_rows).to_csv(OUTPUT_DIR / "state_continuity_comparison.csv", index=False)

    pd.DataFrame(daily_rows).to_csv(OUTPUT_DIR / "daily_accounting_ledger.csv", index=False)
    pd.DataFrame(scenario_rows).to_csv(OUTPUT_DIR / "distribution_tax_scenarios.csv", index=False)

    checks = {
        "input_sha_verified": True,
        "taxable_equal_gross_engine_match": scenario_rows[0]["engine_net_match"],
        "taxable_less_engine_match": scenario_rows[1]["engine_net_match"],
        "taxable_zero_engine_match": scenario_rows[2]["engine_net_match"],
        "pre_tax_engine_match": scenario_rows[3]["engine_net_match"],
        "taxable_less_withholding_lt_gross": (
            scenario_rows[1]["expected_withholding"]
            < scenario_rows[1]["expected_gross"] * 0.154
        ),
        "pre_tax_no_withholding": scenario_rows[3]["expected_withholding"] == 0.0,
        "sell_before_payment_entitlement": (
            scenario_rows[4]["sell_qty"] == 1000
            and all(
                pd.Timestamp(value).date() < pd.Timestamp("2026-09-02").date()
                for value in scenario_rows[4]["sell_dates"]
            )
            and abs(
                scenario_rows[4]["first_receivable"]
                - scenario_rows[4]["expected_net"]
            )
            < 1e-6
        ),
        "sell_before_payment_net_paid": (
            scenario_rows[4]["cash_delta_on_payment"] is not None
            and abs(
                scenario_rows[4]["cash_delta_on_payment"]
                - scenario_rows[4]["expected_net"]
            )
            < 1e-6
        ),
        "buy_on_ex_no_entitlement": (
            scenario_rows[5]["buy_qty"] > 0
            and "2026-08-28" in scenario_rows[5]["buy_dates"]
            and scenario_rows[5]["receivable_on_ex"] == 0.0
            and scenario_rows[5]["first_receivable"] == 0.0
            and scenario_rows[5]["cash_delta_on_payment"] == 0.0
            and scenario_rows[5]["expected_withholding"] == 0.0
        ),
        "missing_taxable_blocked": missing_blocked,
        "different_policy_restore_blocked": policy_mismatch_blocked,
        "restart_before_payment_net_match": all(
            row["cash_match"] and row["receivables_match"] and row["tax_policy_match"]
            for row in continuity_rows
            if row["scenario"] == "before_payment"
        ),
        "restart_after_payment_no_double_tax": all(
            row["cash_match"] and row["tax_policy_match"] and row["equity_match"]
            for row in continuity_rows
            if row["scenario"] == "after_payment"
        ),
    }
    result = {
        "generated_at": run_id,
        "policy": POLICY_DOC,
        "events_total": len(events),
        "events_in_window": len(in_window),
        "taxable_base_input_validation": base_validation,
        "event_type_counts": comparison["base_type"].value_counts().to_dict(),
        "comparison_totals": {
            "withholding_gross": float(comparison["withholding_gross_100"].sum()),
            "withholding_base": float(comparison["withholding_base_100"].sum()),
            "difference": float(comparison["withholding_diff_100"].sum()),
        },
        "scenarios": scenario_rows,
        "checks": checks,
        "blockers": [
            "INCOMPLETE_COVERAGE",
            "EX-DATE-DERIVED",
            "PRICE-BASIS",
            "INDEPENDENT-SOURCE",
            "TAX-BASE (역사적 과세 검증 미완료)",
        ],
        "research_only": True,
    }
    (OUTPUT_DIR / "pilot_validation.json").write_text(
        json.dumps(ca.dist.sanitize_json(result), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    (OUTPUT_DIR / "distribution_tax_policy.json").write_text(
        json.dumps(POLICY_DOC, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    provenance = {
        "run_id": run_id,
        "git_commit": ca.dist.git_commit(),
        "code_sha256": ca.dist.sha256_file(Path(__file__)),
        "engine_sha256": ca.dist.sha256_file(ROOT / "run_etf_backtest.py"),
        "source_events_sha256": ca.dist.sha256_file(args.p2a2a_dir / "distribution_events.csv"),
        "artifacts": {
            name: {
                "sha256": ca.dist.sha256_file(OUTPUT_DIR / name),
                "bytes": (OUTPUT_DIR / name).stat().st_size,
            }
            for name in [
                "distribution_tax_policy.json",
                "distribution_tax_events.csv",
                "distribution_tax_comparison.csv",
                "daily_accounting_ledger.csv",
                "state_continuity_comparison.csv",
                "pilot_validation.json",
            ]
            if (OUTPUT_DIR / name).exists()
        },
    }
    (OUTPUT_DIR / "provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    failed = [key for key, value in checks.items() if not value]
    print(
        f"[p2a2d2] 완료: {OUTPUT_DIR} events={len(in_window)} "
        f"checks={len(checks)-len(failed)}/{len(checks)}"
    )
    if failed:
        raise SystemExit(f"[p2a2d2] 실패: {failed}")


if __name__ == "__main__":
    main()
