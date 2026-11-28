"""P2-A-2d-1: 실제 분배금 이벤트로 전략 엔진의 지급일·미수금 회계를 검증한다.

- P2-A-2a/2b에서 확보한 KODEX 4종 ledger(74건) 중 실제 이벤트를 사용한다.
- strict 회계 경로를 연구 전용 seam(`allow_partial_ledger`)으로 실행하되,
  공식 승인 경로는 BLOCKED로 유지한다.
- 단일 실행과 분할 실행의 상태 연속성, 독립 현금·미수금 원장을 대조한다.
- 동결 price 전략 성과는 별도 P0-B 기준 실행 산출물로 확인한다.

운영 경로·캐시·동결 파일은 변경하지 않는다.
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

import p2a2b_corporate_actions as ca

from etf_corporate_actions import (
    canonical_ledger_sha256,
    load_corporate_action_ledger,
)
from run_etf_backtest import (
    _validate_research_state,
    load_etf_price,
    run_etf_strategy,
)

OUTPUT_DIR = ROOT / "outputs_p2a2d1"
LEDGER_COLUMNS = [
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

EXPECTED_FROZEN = {
    "cagr": 0.245411377867,
    "mdd": -0.5121244554,
    "sharpe": 1.03913577,
}


def scenario_ledger(
    full: pd.DataFrame, tickers: list[str], start: date, end: date, name: str
) -> Any:
    """시나리오 구간의 이벤트만 담은 부분 ledger/manifest를 만든다."""
    rows = full[
        full["ticker"].isin(tickers)
        & (pd.to_datetime(full["event_date"]).dt.date >= start)
        & (pd.to_datetime(full["event_date"]).dt.date <= end)
    ].copy()
    if rows.empty:
        raise ValueError(f"시나리오 이벤트가 없습니다: {name}")
    ledger_dir = OUTPUT_DIR / "scenario_ledgers"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = ledger_dir / f"{name}.csv"
    manifest_path = ledger_dir / f"{name}.json"
    rows[LEDGER_COLUMNS].to_csv(ledger_path, index=False)
    documents = [
        {
            "document_id": str(document_id),
            "source_url": str(url),
            "sha256": str(sha),
        }
        for document_id, url, sha in sorted(
            {
                (row.source_document_id, row.source_url, row.source_sha256)
                for row in rows.itertuples()
            }
        )
    ]
    manifest = {
        "manifest_version": 1,
        "ledger_file": ledger_path.name,
        "ledger_sha256": "0" * 64,
        "verification_period": {
            "start": rows["event_date"].min().date().isoformat(),
            "end": rows["payment_date"].max().date().isoformat(),
        },
        "verification_tickers": sorted({str(t) for t in rows["ticker"]}),
        "source_documents": documents,
        "coverage_status": "PARTIAL",
        "notes": f"P2-A-2d-1 연구 시나리오 subset: {name}",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    provisional = load_corporate_action_ledger(ledger_path, manifest_path)
    manifest["ledger_sha256"] = canonical_ledger_sha256(provisional.events)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_corporate_action_ledger(ledger_path, manifest_path)


def synthetic_suspension_ledger(name: str) -> Any:
    """합성 거래정지 이벤트로 restart 시 lifecycle 복원을 검증한다(연구 fixture)."""
    ledger_dir = OUTPUT_DIR / "scenario_ledgers"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = ledger_dir / f"{name}.csv"
    manifest_path = ledger_dir / f"{name}.json"
    pd.DataFrame(
        [
            {
                "event_id": "SYNTH-SUSP-091170-20260824",
                "ticker": "091170",
                "event_type": "SUSPENSION_START",
                "event_date": "2026-08-24",
                "record_date": "",
                "ex_date": "",
                "payment_date": "",
                "settlement_date": "",
                "ratio_num": "",
                "ratio_den": "",
                "cash_amount": "",
                "currency": "KRW",
                "source_document_id": "synthetic_doc",
                "source_document": "synthetic_doc",
                "source_url": "https://example.test/synthetic-suspension",
                "source_sha256": "a" * 64,
                "notes": "합성 fixture: lifecycle 복원 검증",
            }
        ],
        columns=LEDGER_COLUMNS,
    ).to_csv(ledger_path, index=False)
    manifest = {
        "manifest_version": 1,
        "ledger_file": ledger_path.name,
        "ledger_sha256": "0" * 64,
        "verification_period": {"start": "2026-08-24", "end": "2026-08-28"},
        "verification_tickers": ["091170"],
        "source_documents": [
            {
                "document_id": "synthetic_doc",
                "source_url": "https://example.test/synthetic-suspension",
                "sha256": "a" * 64,
            }
        ],
        "coverage_status": "PARTIAL",
        "notes": "synthetic suspension fixture",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    provisional = load_corporate_action_ledger(ledger_path, manifest_path)
    manifest["ledger_sha256"] = canonical_ledger_sha256(provisional.events)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_corporate_action_ledger(ledger_path, manifest_path)


def engine_run(
    *,
    window: tuple[str, str],
    initial_cash: float,
    holdings: dict[str, int],
    cost_basis: dict[str, float],
    universe: list[str],
    price: pd.DataFrame,
    index_df: pd.DataFrame,
    ledger: Any,
    use_market_filter: bool,
) -> dict[str, Any]:
    start, end = (pd.Timestamp(value) for value in window)
    dates = [d for d in index_df["date"] if start <= d <= end]
    if len(dates) < 2:
        raise ValueError(f"시나리오 기간이 너무 짧습니다: {window}")
    init = {
        "cash": float(initial_cash),
        "holdings": dict(holdings),
        "holding_cost_basis": dict(cost_basis),
        "holding_peak_closes": {},
        "portfolio_peak_equity": None,
        "last_valid_closes": {},
    }
    result = run_etf_strategy(
        0.0,
        dates,
        index_df[index_df["date"].isin(dates)].reset_index(drop=True),
        use_market_filter=use_market_filter,
        price_data=price,
        universe_tickers=universe,
        initial_state=init,
        approval_strict=True,
        corporate_action_ledger=ledger,
        allow_partial_ledger=True,
        return_final_state=True,
    )
    curve, trades = result[0], result[1]
    final_state = result[-1]
    if trades is None or trades.empty:
        trades = pd.DataFrame(columns=["date", "ticker", "side", "qty", "price", "cash_flow"])
    return {
        "curve": curve.reset_index(drop=True),
        "trades": trades.reset_index(drop=True),
        "final": final_state,
        "dates": dates,
    }


def holdings_timeline(
    dates: list[pd.Timestamp], initial: dict[str, int], trades: pd.DataFrame
) -> dict[pd.Timestamp, dict[str, int]]:
    """체결 원장으로 날짜별 보유수량을 재구성한다(엔진 상태 미사용)."""
    timeline: dict[pd.Timestamp, dict[str, int]] = {}
    qty = {str(t): int(q) for t, q in initial.items() if int(q) != 0}
    trade_by_date: dict[pd.Timestamp, list[tuple[str, int]]] = {}
    if not trades.empty:
        for row in trades.itertuples():
            sign = 1 if str(row.side).upper() == "BUY" else -1
            trade_by_date.setdefault(pd.Timestamp(row.date), []).append(
                (str(row.ticker), sign * int(row.qty))
            )
    for day in dates:
        for ticker, delta in trade_by_date.get(day, []):
            qty[ticker] = qty.get(ticker, 0) + delta
            if qty[ticker] == 0:
                qty.pop(ticker)
        timeline[day] = dict(qty)
    return timeline


def independent_receivables(
    events: list[Any],
    timeline: dict[pd.Timestamp, dict[str, int]],
    initial: dict[str, int],
) -> dict[pd.Timestamp, float]:
    """배당락일 직전 보유 스냅샷과 이벤트로 미수금 잔액을 독립 계산한다."""
    result: dict[pd.Timestamp, float] = {}
    for day in sorted(timeline):
        total = 0.0
        for event in events:
            ex_day = pd.Timestamp(event.ex_date)
            pay_day = pd.Timestamp(event.payment_date)
            if ex_day <= day < pay_day:
                prior = [d for d in timeline if d < ex_day]
                if prior:
                    qty = timeline[max(prior)].get(event.ticker, 0)
                else:
                    # 배당락일이 실행 첫날이면 체결 전 초기 보유가 권리 경계다.
                    qty = int(initial.get(event.ticker, 0))
                if qty > 0:
                    total += qty * float(event.cash_amount)
        result[day] = total
    return result


def dividend_payment_timeline(
    events: list[Any], timeline: dict[pd.Timestamp, dict[str, int]], initial: dict[str, int]
) -> dict[pd.Timestamp, float]:
    """지급일별 분배금 현금(독립 계산)."""
    payments: dict[pd.Timestamp, float] = {}
    for event in events:
        ex_day = pd.Timestamp(event.ex_date)
        prior = [d for d in timeline if d < ex_day]
        qty = (
            timeline[max(prior)].get(event.ticker, 0)
            if prior
            else int(initial.get(event.ticker, 0))
        )
        if qty > 0:
            pay_day = pd.Timestamp(event.payment_date)
            payments[pay_day] = payments.get(pay_day, 0.0) + qty * float(event.cash_amount)
    return payments


def reconcile(
    run: dict[str, Any],
    events: list[Any],
    initial: dict[str, int],
    label: str,
    initial_cash: float,
    price: pd.DataFrame,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    curve = run["curve"]
    timeline = holdings_timeline(list(curve["date"]), initial, run["trades"])
    receivables = independent_receivables(events, timeline, initial)
    payments = dividend_payment_timeline(events, timeline, initial)
    closes = {
        (pd.Timestamp(row.date), str(row.ticker)): float(row.close)
        for row in price.itertuples()
    }
    trade_cash: dict[pd.Timestamp, float] = {}
    if not run["trades"].empty:
        for row in run["trades"].itertuples():
            day = pd.Timestamp(row.date)
            trade_cash[day] = trade_cash.get(day, 0.0) + float(row.cash_flow)
    rows = []
    max_equity_diff = 0.0
    max_cash_diff = 0.0
    max_mv_diff = 0.0
    for row in curve.itertuples():
        day = pd.Timestamp(row.date)
        independent_cash = initial_cash + sum(
            value for key, value in trade_cash.items() if key <= day
        ) + sum(value for key, value in payments.items() if key <= day)
        independent_mv = sum(
            qty * closes.get((day, ticker), 0.0) for ticker, qty in timeline[day].items()
        )
        expected_receivable = receivables.get(day, 0.0)
        expected_equity = independent_cash + independent_mv + expected_receivable
        equity_diff = abs(expected_equity - float(row.equity))
        cash_diff = abs(independent_cash - float(row.cash))
        mv_diff = abs(independent_mv - float(row.market_value))
        max_equity_diff = max(max_equity_diff, equity_diff)
        max_cash_diff = max(max_cash_diff, cash_diff)
        max_mv_diff = max(max_mv_diff, mv_diff)
        rows.append(
            {
                "scenario": label,
                "date": day.date().isoformat(),
                "cash": float(row.cash),
                "independent_cash": independent_cash,
                "market_value": float(row.market_value),
                "independent_market_value": independent_mv,
                "engine_receivables": float(getattr(row, "receivables", 0.0) or 0.0),
                "independent_receivables": expected_receivable,
                "engine_equity": float(row.equity),
                "expected_equity": expected_equity,
                "abs_diff": equity_diff,
                "cash_diff": cash_diff,
                "market_value_diff": mv_diff,
                "holdings": str(row.holdings),
            }
        )
    return rows, {
        "max_abs_diff": max_equity_diff,
        "max_cash_diff": max_cash_diff,
        "max_mv_diff": max_mv_diff,
        "rows": len(rows),
    }


def serialize_state(state: dict[str, Any]) -> dict[str, Any]:
    """연구 상태를 JSON 안전 dict로 직렬화한다(범용 저장 시스템 아님)."""
    return {
        "cash": float(state.get("cash", 0.0)),
        "holdings": {str(k): int(v) for k, v in dict(state.get("holdings", {})).items()},
        "holding_cost_basis": {
            str(k): str(v) for k, v in dict(state.get("holding_cost_basis", {})).items()
        },
        "holding_peak_closes": {
            str(k): float(v) for k, v in dict(state.get("holding_peak_closes", {})).items()
        },
        "portfolio_peak_equity": state.get("portfolio_peak_equity"),
        "last_valid_closes": {
            str(k): float(v) for k, v in dict(state.get("last_valid_closes", {})).items()
        },
        "rebalance_phase_offset": int(state.get("rebalance_phase_offset", 0)),
        "exit_phase_offset": int(state.get("exit_phase_offset", 0)),
        "pending_daily_risk_off_exits": dict(state.get("pending_daily_risk_off_exits", {}) or {}),
        "pending_receivables": [
            {
                "event_id": item.event_id,
                "ticker": item.ticker,
                "quantity": int(item.quantity),
                "amount": str(item.amount),
                "payment_date": item.payment_date.isoformat(),
                "paid": bool(item.paid),
            }
            for item in state.get("pending_receivables", ())
        ],
        "processed_action_ids": sorted(str(x) for x in state.get("processed_action_ids", ())),
        "corporate_action_states": [
            {
                "ticker": item.ticker,
                "quantity": int(item.quantity),
                "total_cost_basis": str(item.total_cost_basis),
                "lifecycle": item.lifecycle.value,
                "last_event_date": item.last_event_date.isoformat()
                if item.last_event_date
                else None,
            }
            for item in dict(state.get("corporate_action_states", {})).values()
        ],
        "strict_action_blockers": [
            {
                "code": item.code,
                "message": item.message,
                "event_id": item.event_id,
                "ticker": item.ticker,
                "event_date": item.event_date.isoformat() if item.event_date else None,
            }
            for item in state.get("strict_action_blockers", ())
        ],
        "research_only": bool(state.get("research_only")),
        "corporate_action_ledger_sha256": state.get("corporate_action_ledger_sha256"),
        "final_equity": float(state.get("final_equity", 0.0) or 0.0),
    }


def deserialize_state(payload: dict[str, Any], ledger: Any) -> dict[str, Any]:
    """직렬화된 연구 상태를 복원하고 같은 ledger인지 fail-closed로 검증한다."""
    from decimal import Decimal

    from etf_corporate_actions import (
        ApprovalBlocker,
        DistributionReceivable,
        HoldingState,
        LifecycleState,
    )

    required = (
        "cash",
        "holdings",
        "holding_cost_basis",
        "holding_peak_closes",
        "last_valid_closes",
        "pending_receivables",
        "processed_action_ids",
        "corporate_action_states",
        "strict_action_blockers",
        "research_only",
        "corporate_action_ledger_sha256",
    )
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValueError(f"재시작 상태 필수 필드 누락: {missing}")
    if type(payload.get("research_only")) is not bool:
        raise TypeError("research_only는 boolean이어야 합니다")
    for ticker, quantity in payload["holdings"].items():
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise TypeError(f"보유수량이 정수가 아닙니다: {ticker}={quantity!r}")
    receivable_ids = [item["event_id"] for item in payload.get("pending_receivables", ())]
    if len(receivable_ids) != len(set(receivable_ids)):
        raise ValueError("중복 미수금 이벤트가 있습니다")
    processed_payload = [str(x) for x in payload.get("processed_action_ids", ())]
    if len(processed_payload) != len(set(processed_payload)):
        raise ValueError("중복 processed action id가 있습니다")
    state_tickers = [item["ticker"] for item in payload.get("corporate_action_states", ())]
    if len(state_tickers) != len(set(state_tickers)):
        raise ValueError("중복 기업행위 상태 키가 있습니다")
    for item in payload.get("pending_receivables", ()):
        if isinstance(item.get("quantity"), bool) or not isinstance(item.get("quantity"), int):
            raise TypeError("미수금 수량이 정수가 아닙니다")
        if type(item.get("paid")) is not bool:
            raise TypeError("미수금 paid는 boolean이어야 합니다")
    for item in payload.get("corporate_action_states", ()):
        if isinstance(item.get("quantity"), bool) or not isinstance(item.get("quantity"), int):
            raise TypeError("기업행위 상태 수량이 정수가 아닙니다")
    state = {
        "cash": float(payload["cash"]),
        "holdings": {str(k): int(v) for k, v in payload["holdings"].items()},
        "holding_cost_basis": {
            str(k): float(v) for k, v in payload["holding_cost_basis"].items()
        },
        "holding_peak_closes": {
            str(k): float(v) for k, v in payload["holding_peak_closes"].items()
        },
        "portfolio_peak_equity": payload.get("portfolio_peak_equity"),
        "last_valid_closes": {
            str(k): float(v) for k, v in payload["last_valid_closes"].items()
        },
        "rebalance_phase_offset": int(payload.get("rebalance_phase_offset", 0)),
        "exit_phase_offset": int(payload.get("exit_phase_offset", 0)),
        "pending_daily_risk_off_exits": dict(payload.get("pending_daily_risk_off_exits", {})),
        "pending_receivables": tuple(
            DistributionReceivable(
                item["event_id"],
                item["ticker"],
                int(item["quantity"]),
                Decimal(str(item["amount"])),
                date.fromisoformat(item["payment_date"]),
                bool(item["paid"]),
            )
            for item in payload.get("pending_receivables", ())
        ),
        "processed_action_ids": tuple(payload.get("processed_action_ids", ())),
        "corporate_action_states": {
            item["ticker"]: HoldingState(
                item["ticker"],
                int(item["quantity"]),
                Decimal(str(item["total_cost_basis"])),
                LifecycleState(item["lifecycle"]),
                date.fromisoformat(item["last_event_date"])
                if item.get("last_event_date")
                else None,
            )
            for item in payload.get("corporate_action_states", ())
        },
        "strict_action_blockers": tuple(
            ApprovalBlocker(
                item["code"],
                item["message"],
                item.get("event_id"),
                item.get("ticker"),
                date.fromisoformat(item["event_date"]) if item.get("event_date") else None,
            )
            for item in payload.get("strict_action_blockers", ())
        ),
        "research_only": bool(payload.get("research_only")),
        "corporate_action_ledger_sha256": payload.get("corporate_action_ledger_sha256"),
        "final_equity": float(payload.get("final_equity", 0.0) or 0.0),
    }
    _validate_research_state(state, ledger, state["holdings"])
    return state


def state_signature(state: dict[str, Any]) -> dict[str, Any]:
    receivables = [
        (item.event_id, item.ticker, int(item.quantity), float(item.amount), str(item.payment_date), item.paid)
        for item in state.get("pending_receivables", ())
    ]
    return {
        "cash": float(state.get("cash", 0.0)),
        "holdings": {str(k): int(v) for k, v in dict(state.get("holdings", {})).items()},
        "cost_basis": {str(k): float(v) for k, v in dict(state.get("holding_cost_basis", {})).items()},
        "receivables": sorted(receivables),
        "processed_action_ids": sorted(state.get("processed_action_ids", ())),
        "lifecycles": {
            str(ticker): getattr(item, "lifecycle", None).value
            if getattr(item, "lifecycle", None) is not None
            else None
            for ticker, item in dict(state.get("corporate_action_states", {})).items()
        },
        "final_equity": float(state.get("final_equity", 0.0) or 0.0),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "data_cache")
    parser.add_argument(
        "--p2a2b-dir",
        type=Path,
        default=Path(
            "/home/ubuntu/investment/worktrees/p2a2b-ca/outputs_p2a2b"
        ),
    )
    args = parser.parse_args()

    ca.dist.load_env_file(Path(os.environ.get("ETF_ENV_FILE", ROOT / ".env")))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    run_id = ca.dist.utc_now()

    ledger_src = args.p2a2b_dir / "corporate_action_ledger.csv"
    manifest_src = args.p2a2b_dir / "corporate_action_manifest.json"
    full = pd.read_csv(ledger_src, dtype={"ticker": str})
    full["event_date"] = pd.to_datetime(full["event_date"])
    full["payment_date"] = pd.to_datetime(full["payment_date"])

    tickers = ["091170", "498400"]
    price = load_etf_price(tickers)
    index_df = pd.read_parquet(args.cache_dir / "index.parquet")
    index_df["date"] = pd.to_datetime(index_df["date"])

    # 시나리오 정의: (name, ticker, window, cash, holdings, universe, market_filter)
    scen = {
        "hold_before_ex": ("091170", ("2026-08-24", "2026-09-04"), 0.0, {"091170": 1000}, ["091170"], False),
        "buy_on_ex": ("091170", ("2026-08-27", "2026-09-04"), 1_000_000.0, {}, ["091170"], True),
        "sell_on_ex": ("498400", ("2026-09-11", "2026-09-18"), 0.0, {"498400": 1000}, ["498400"], True),
        "sell_after_ex": ("091170", ("2026-08-28", "2026-09-04"), 0.0, {"091170": 1000}, ["091170"], True),
        "receivable_not_spendable": (
            "091170",
            ("2026-01-30", "2026-02-02"),
            12_000.0,
            {"091170": 300},
            ["091170", "498400"],
            False,
        ),
        "end_before_payment": ("091170", ("2026-08-24", "2026-09-01"), 0.0, {"091170": 1000}, ["091170"], False),
        "multi_event_hold": ("091170", ("2026-06-22", "2026-09-04"), 0.0, {"091170": 1000}, ["091170"], False),
    }

    daily_rows: list[dict[str, Any]] = []
    scenario_rows: list[dict[str, Any]] = []
    runs: dict[str, dict[str, Any]] = {}
    cost_basis = {"091170": 16_000.0, "498400": 15_000.0}
    for name, (ticker, window, cash, holdings, universe, market_filter) in scen.items():
        ledger_start = date.fromisoformat(window[0]) - timedelta(days=10)
        ledger_end = date.fromisoformat(window[1]) + timedelta(days=10)
        events_ledger = scenario_ledger(
            full, [ticker], ledger_start, ledger_end, name
        )
        run = engine_run(
            window=window,
            initial_cash=cash,
            holdings=holdings,
            cost_basis={t: cost_basis[t] for t in holdings},
            universe=universe,
            price=price[price["ticker"].isin(universe)],
            index_df=index_df,
            ledger=events_ledger,
            use_market_filter=market_filter,
        )
        runs[name] = run
        rows, summary = reconcile(
            run,
            list(events_ledger.events),
            holdings,
            name,
            cash,
            price[price["ticker"].isin(universe)],
        )
        daily_rows.extend(rows)
        scenario_rows.append(
            {
                "scenario": name,
                "ticker": ticker,
                "window_start": window[0],
                "window_end": window[1],
                "events": len(events_ledger.events),
                "trades": len(run["trades"]),
                "reconciliation_max_abs_diff": summary["max_abs_diff"],
                "reconciliation_max_cash_diff": summary["max_cash_diff"],
                "reconciliation_max_mv_diff": summary["max_mv_diff"],
                "reconciled": max(
                    summary["max_abs_diff"], summary["max_cash_diff"], summary["max_mv_diff"]
                )
                < 1e-6,
                "final_cash": float(run["final"]["cash"]),
                "final_equity": float(run["final"]["final_equity"] or 0.0),
                "final_receivables_unpaid": sum(
                    1 for item in run["final"]["pending_receivables"] if not item.paid
                ),
            }
        )

    # 분할 실행 연속성: 리밸런싱 위상이 맞는 경계(20거래일)에서 자른다.
    continuity_rows: list[dict[str, Any]] = []
    all_dates = [d for d in index_df["date"] if d <= pd.Timestamp("2026-09-10")]
    segments = [
        ("split_before_payment", "2026-09-01"),
        ("split_after_payment", "2026-09-03"),
    ]
    for label, second_start in segments:
        split_day = pd.Timestamp(second_start)
        split_index = all_dates.index(split_day)
        aligned_start = all_dates[split_index - 20]
        first_window = (
            aligned_start.date().isoformat(),
            all_dates[split_index - 1].date().isoformat(),
        )
        second_window = (second_start, "2026-09-04")
        ledger_start = (aligned_start - pd.Timedelta(days=10)).date()
        ledger_end = date(2026, 9, 10)
        first_ledger = scenario_ledger(full, ["091170"], ledger_start, ledger_end, f"{label}_a")
        second_ledger = scenario_ledger(full, ["091170"], ledger_start, ledger_end, f"{label}_b")
        part_a = engine_run(
            window=first_window,
            initial_cash=0.0,
            holdings={"091170": 1000},
            cost_basis={"091170": 16_000.0},
            universe=["091170"],
            price=price[price["ticker"] == "091170"],
            index_df=index_df,
            ledger=first_ledger,
            use_market_filter=False,
        )
        single_run = engine_run(
            window=(aligned_start.date().isoformat(), second_window[1]),
            initial_cash=0.0,
            holdings={"091170": 1000},
            cost_basis={"091170": 16_000.0},
            universe=["091170"],
            price=price[price["ticker"] == "091170"],
            index_df=index_df,
            ledger=first_ledger,
            use_market_filter=False,
        )
        restored = part_a["final"]
        payload = json.loads(json.dumps(serialize_state(restored)))
        init_state = deserialize_state(payload, second_ledger)
        dates_b = [
            d
            for d in index_df["date"]
            if pd.Timestamp(second_window[0]) <= d <= pd.Timestamp(second_window[1])
        ]
        resumed = run_etf_strategy(
            0.0,
            dates_b,
            index_df[index_df["date"].isin(dates_b)].reset_index(drop=True),
            use_market_filter=False,
            price_data=price[price["ticker"] == "091170"],
            universe_tickers=["091170"],
            initial_state=init_state,
            approval_strict=True,
            corporate_action_ledger=second_ledger,
            allow_partial_ledger=True,
            return_final_state=True,
        )
        resumed_trades, resumed_final = resumed[1], resumed[-1]
        single_sig = state_signature(single_run["final"])
        resumed_sig = state_signature(resumed_final)
        combined_trades = pd.concat([part_a["trades"], resumed_trades], ignore_index=True)
        same_trades = combined_trades.sort_values(["date", "ticker", "side", "qty"]).reset_index(
            drop=True
        ).equals(
            single_run["trades"].sort_values(["date", "ticker", "side", "qty"]).reset_index(drop=True)
        )
        single_curve = single_run["curve"].set_index("date")
        daily_equity_diff = 0.0
        daily_cash_diff = 0.0
        daily_mv_diff = 0.0
        for row in resumed[0].itertuples():
            day = pd.Timestamp(row.date)
            if day not in single_curve.index:
                continue
            reference = single_curve.loc[day]
            daily_equity_diff = max(daily_equity_diff, abs(float(row.equity) - float(reference["equity"])))
            daily_cash_diff = max(daily_cash_diff, abs(float(row.cash) - float(reference["cash"])))
            daily_mv_diff = max(
                daily_mv_diff, abs(float(row.market_value) - float(reference["market_value"]))
            )
        phase_aligned = (split_index - all_dates.index(aligned_start)) % 20 == 0
        continuity_rows.append(
            {
                "scenario": label,
                "split_at": first_window[1],
                "phase_aligned": phase_aligned,
                "boundary_trading_day_gap": all_dates.index(split_day)
                - all_dates.index(pd.Timestamp(first_window[1])),
                "daily_equity_max_diff": daily_equity_diff,
                "daily_cash_max_diff": daily_cash_diff,
                "daily_mv_max_diff": daily_mv_diff,
                "single_cash": single_sig["cash"],
                "split_cash": resumed_sig["cash"],
                "cash_match": abs(single_sig["cash"] - resumed_sig["cash"]) < 1e-9,
                "holdings_match": single_sig["holdings"] == resumed_sig["holdings"],
                "receivables_match": single_sig["receivables"] == resumed_sig["receivables"],
                "processed_ids_match": single_sig["processed_action_ids"]
                == resumed_sig["processed_action_ids"],
                "lifecycle_match": single_sig["lifecycles"] == resumed_sig["lifecycles"],
                "equity_match": abs(single_sig["final_equity"] - resumed_sig["final_equity"]) < 1e-6,
                "trades_match": bool(same_trades),
                "single_receivable_count": len(single_sig["receivables"]),
                "split_receivable_count": len(resumed_sig["receivables"]),
                "scope": "distribution_accounting_continuity",
            }
        )

    # 합성 거래정지 restart: lifecycle 복원 검증
    susp_ledger = synthetic_suspension_ledger("synthetic_suspension")
    susp_a = engine_run(
        window=("2026-08-24", "2026-08-26"),
        initial_cash=0.0,
        holdings={"091170": 1000},
        cost_basis={"091170": 16_000.0},
        universe=["091170"],
        price=price[price["ticker"] == "091170"],
        index_df=index_df,
        ledger=susp_ledger,
        use_market_filter=False,
    )
    susp_restored = susp_a["final"]
    susp_payload = json.loads(json.dumps(serialize_state(susp_restored)))
    susp_init = deserialize_state(susp_payload, susp_ledger)
    dates_b = [d for d in index_df["date"] if pd.Timestamp("2026-08-27") <= d <= pd.Timestamp("2026-08-28")]
    susp_resumed = run_etf_strategy(
        0.0,
        dates_b,
        index_df[index_df["date"].isin(dates_b)].reset_index(drop=True),
        use_market_filter=False,
        price_data=price[price["ticker"] == "091170"],
        universe_tickers=["091170"],
        initial_state=susp_init,
        approval_strict=True,
        corporate_action_ledger=susp_ledger,
        allow_partial_ledger=True,
        return_final_state=True,
    )
    susp_final = susp_resumed[-1]
    susp_state = susp_final.get("corporate_action_states", {}).get("091170")
    lifecycle_preserved = bool(
        susp_state is not None and getattr(susp_state.lifecycle, "value", None) == "SUSPENDED"
    )
    research_reports = [
        run["final"].get("approval_report") for run in runs.values()
    ]
    research_blocked = all(
        report is not None
        and report.status == "BLOCKED"
        and any(blocker.code == "RESEARCH_PARTIAL_LEDGER" for blocker in report.blockers)
        for report in research_reports
    )

    # 시나리오별 핵심 체크
    checks: dict[str, Any] = {}
    hold = runs["hold_before_ex"]
    hold_curve = hold["curve"].set_index("date")
    checks["receivable_on_ex_date"] = float(hold_curve.loc[pd.Timestamp("2026-08-28"), "receivables"]) == 59_000.0
    checks["cash_before_payment_zero"] = float(hold_curve.loc[pd.Timestamp("2026-09-01"), "cash"]) == 0.0
    checks["payment_on_payment_date"] = float(hold_curve.loc[pd.Timestamp("2026-09-02"), "cash"]) == 59_000.0
    checks["no_double_count_on_payment"] = (
        abs(
            float(hold_curve.loc[pd.Timestamp("2026-09-02"), "equity"])
            - (
                float(hold_curve.loc[pd.Timestamp("2026-09-02"), "cash"])
                + float(hold_curve.loc[pd.Timestamp("2026-09-02"), "market_value"])
            )
        )
        < 1e-9
    )
    buy = runs["buy_on_ex"]
    checks["buy_on_ex_has_trade"] = bool(
        (buy["trades"]["date"] == pd.Timestamp("2026-08-28")).any()
    )
    checks["buy_on_ex_no_receivable"] = all(
        value == 0.0 for value in buy["curve"]["receivables"].fillna(0.0)
    )
    sell = runs["sell_on_ex"]
    checks["sell_on_ex_has_trade"] = bool(
        (sell["trades"]["date"] == pd.Timestamp("2026-09-14")).any()
    )
    checks["sell_on_ex_keeps_receivable"] = float(
        sell["curve"].set_index("date").loc[pd.Timestamp("2026-09-14"), "receivables"]
    ) == 300_000.0
    after = runs["sell_after_ex"]
    after_curve = after["curve"].set_index("date")
    checks["sell_after_ex_pays_after_sale"] = (
        abs(
            float(after_curve.loc[pd.Timestamp("2026-09-02"), "cash"])
            - float(after_curve.loc[pd.Timestamp("2026-09-01"), "cash"])
            - 59_000.0
        )
        < 1e-9
    )
    spend = runs["receivable_not_spendable"]
    spend_curve = spend["curve"].set_index("date")
    checks["receivable_not_spent"] = (
        bool(spend["trades"].empty)
        and float(spend["final"]["cash"]) == 12_000.0
        and float(spend_curve.loc[pd.Timestamp("2026-02-02"), "receivables"]) == 13_500.0
    )
    end = runs["end_before_payment"]
    checks["end_receivable_preserved"] = sum(
        1 for item in end["final"]["pending_receivables"] if not item.paid
    ) == 1
    multi = runs["multi_event_hold"]
    checks["multi_event_all_paid"] = float(multi["final"]["cash"]) == 174_000.0 and all(
        item.paid for item in multi["final"]["pending_receivables"]
    )
    checks["lifecycle_preserved_across_restart"] = lifecycle_preserved
    checks["research_report_forced_blocked"] = research_blocked

    accounting = pd.DataFrame(scenario_rows)
    daily = pd.DataFrame(daily_rows)
    continuity = pd.DataFrame(continuity_rows)
    accounting.to_csv(OUTPUT_DIR / "accounting_scenarios.csv", index=False)
    daily.to_csv(OUTPUT_DIR / "daily_accounting_ledger.csv", index=False)
    continuity.to_csv(OUTPUT_DIR / "state_continuity_comparison.csv", index=False)
    # 이벤트별 미수금/지급 대사
    recon_rows = []
    for name, run in runs.items():
        for item in run["final"]["pending_receivables"]:
            recon_rows.append(
                {
                    "scenario": name,
                    "event_id": item.event_id,
                    "ticker": item.ticker,
                    "quantity": int(item.quantity),
                    "amount": float(item.amount),
                    "payment_date": str(item.payment_date),
                    "paid": bool(item.paid),
                }
            )
    pd.DataFrame(recon_rows).to_csv(OUTPUT_DIR / "receivable_reconciliation.csv", index=False)

    preflight = ca.run_preflight(ledger_src, manifest_src, output_root=OUTPUT_DIR)
    preflight_blocked = bool(
        preflight.get("report_written_this_run")
        and preflight.get("status") == "BLOCKED"
        and any(item["code"] == "INCOMPLETE_COVERAGE" for item in preflight.get("blockers", []))
    )
    baseline_dir = OUTPUT_DIR / "baseline_compare"
    baseline = {"status": "not_available"}
    if all(
        (baseline_dir / name).exists()
        for name in ("baseline_equity.csv", "pr_equity.csv", "baseline_trades.csv", "pr_trades.csv")
    ):
        base_eq = pd.read_csv(baseline_dir / "baseline_equity.csv")
        pr_eq = pd.read_csv(baseline_dir / "pr_equity.csv")
        base_tr = pd.read_csv(baseline_dir / "baseline_trades.csv")
        pr_tr = pd.read_csv(baseline_dir / "pr_trades.csv")
        columns = ["equity_strategy", "cash", "market_value", "distribution_cash"]
        equity_diff = (
            max(float((base_eq[col] - pr_eq[col]).abs().max()) for col in columns)
            if base_eq["date"].equals(pr_eq["date"])
            else float("inf")
        )
        merged = base_tr.merge(
            pr_tr,
            on=["date", "ticker", "side", "qty"],
            suffixes=("_b", "_p"),
            how="outer",
            indicator=True,
        )
        trade_row_match = bool((merged["_merge"] == "both").all())
        trade_diff = (
            max(
                float((merged[f"{col}_b"] - merged[f"{col}_p"]).abs().max())
                for col in ("price", "net_value", "cash_flow")
            )
            if trade_row_match
            else float("inf")
        )
        baseline = {
            "status": "ok",
            "equity_rows": len(base_eq),
            "trade_rows": len(base_tr),
            "date_axis_match": bool(base_eq["date"].equals(pr_eq["date"])),
            "equity_max_abs_diff": equity_diff,
            "trade_rows_match": trade_row_match,
            "trade_max_abs_diff": trade_diff,
            "holdings_match": bool(
                base_eq["holdings"].fillna("").equals(pr_eq["holdings"].fillna(""))
            ),
            "row_by_row_match": bool(
                equity_diff == 0.0 and trade_diff == 0.0 and trade_row_match
            ),
        }
    frozen_summary = OUTPUT_DIR / "frozen_baseline" / "p0b_summary.json"
    frozen = {"status": "not_run"}
    if frozen_summary.exists():
        summary = json.loads(frozen_summary.read_text(encoding="utf-8"))
        base = summary["cost"]["base"]
        diffs = {
            key: abs(float(base[key]) - EXPECTED_FROZEN[key]) for key in EXPECTED_FROZEN
        }
        frozen = {
            "status": "ok",
            "cagr": float(base["cagr"]),
            "mdd": float(base["mdd"]),
            "sharpe": float(base["sharpe"]),
            "abs_diff": diffs,
            "matches_within_1e9": all(value < 1e-9 for value in diffs.values()),
            # 제시된 기대값은 샤프 8자리 반올림 값이라 1e-9 비교가 불가능하다.
            "matches_reported_precision": f"{float(base['sharpe']):.8f}"
            == f"{EXPECTED_FROZEN['sharpe']:.8f}"
            and diffs["cagr"] < 1e-9
            and diffs["mdd"] < 1e-9,
        }

    result = {
        "generated_at": run_id,
        "ledger_source": str(ledger_src),
        "ledger_sha256": ca.dist.sha256_file(ledger_src),
        "manifest_sha256": ca.dist.sha256_file(manifest_src),
        "research_only": True,
        "strict_approval": "BLOCKED (PARTIAL coverage, 승인 경로 미사용)",
        "scenarios": accounting.to_dict(orient="records"),
        "checks": checks,
        "strict_preflight": {
            "status": preflight.get("status"),
            "report_written_this_run": preflight.get("report_written_this_run"),
            "blockers": preflight.get("blockers"),
            "blocked_with_incomplete_coverage": preflight_blocked,
        },
        "continuity": continuity.to_dict(orient="records"),
        "frozen_baseline": frozen,
        "baseline_row_comparison": baseline,
        "limits": [
            "배당락일은 파생값이며 세전 회계만 검증",
            "실데이터 E2E는 091170/498400 두 종목의 2026년 이벤트에 한정",
            "전체 전략 엔진 E2E는 연구 seam(allow_partial_ledger) 사용, 승인 결과 아님",
        ],
    }
    (OUTPUT_DIR / "pilot_validation.json").write_text(
        json.dumps(ca.dist.sanitize_json(result), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    provenance = {
        "run_id": run_id,
        "git_commit": ca.dist.git_commit(),
        "code_sha256": ca.dist.sha256_file(Path(__file__)),
        "engine_sha256": ca.dist.sha256_file(ROOT / "run_etf_backtest.py"),
        "ledger_sha256": ca.dist.sha256_file(ledger_src),
        "artifacts": {
            name: {
                "sha256": ca.dist.sha256_file(OUTPUT_DIR / name),
                "bytes": (OUTPUT_DIR / name).stat().st_size,
            }
            for name in [
                "accounting_scenarios.csv",
                "daily_accounting_ledger.csv",
                "receivable_reconciliation.csv",
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
    recon_failed = accounting.loc[
        ~accounting["reconciled"].astype(bool), "scenario"
    ].tolist()
    continuity_failed = continuity.loc[
        ~continuity[
            [
                "cash_match",
                "holdings_match",
                "receivables_match",
                "processed_ids_match",
                "lifecycle_match",
                "equity_match",
                "trades_match",
                "phase_aligned",
            ]
        ].all(axis=1),
        "scenario",
    ].tolist()
    if recon_failed:
        failed.append(f"reconciliation:{recon_failed}")
    if continuity_failed:
        failed.append(f"continuity:{continuity_failed}")
    if continuity["daily_equity_max_diff"].max() > 1e-6 or continuity[
        "daily_cash_max_diff"
    ].max() > 1e-6 or continuity["daily_mv_max_diff"].max() > 1e-6:
        failed.append("continuity_daily_mismatch")
    if not preflight_blocked:
        failed.append("strict_preflight_not_blocked")
    if not frozen.get("matches_reported_precision"):
        failed.append("frozen_baseline_mismatch")
    if baseline.get("status") == "ok" and not baseline.get("row_by_row_match"):
        failed.append("baseline_row_mismatch")
    print(
        f"[p2a2d1] 완료: {OUTPUT_DIR} checks={len(checks)-len(failed)}/{len(checks)} "
        f"continuity={int(continuity[['cash_match','holdings_match','receivables_match','processed_ids_match','equity_match','trades_match']].all(axis=1).sum())}/{len(continuity)} "
        f"frozen={frozen.get('matches_reported_precision')}"
    )
    if failed:
        raise SystemExit(f"[p2a2d1] 실패 체크: {failed}")


if __name__ == "__main__":
    main()
