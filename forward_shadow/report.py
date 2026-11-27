"""Daily shadow report: deterministic text plus an opt-in Telegram send path.

The live notification route is untouched.  Sending is disabled unless
``SHADOW_TELEGRAM_SEND=1`` and a notifier is explicitly injected by the caller;
tests inject a capturing sink instead of touching the live channel.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Protocol

from forward_shadow.store import atomic_write_json


class ReportSink(Protocol):
    def send(self, message: str) -> bool: ...


class NullSink:
    def send(self, message: str) -> bool:
        return False


class CapturingSink:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, message: str) -> bool:
        self.messages.append(message)
        return True


def build_report(
    *,
    date: str,
    status: str,
    manifest: dict[str, Any],
    state: dict[str, Any],
    day: Any,
    signal: dict[str, Any] | None,
    fills: list[dict[str, Any]],
    integrity: dict[str, Any],
    next_decision_date: str | None,
) -> dict[str, Any]:
    curve = state.get("equity_curve", [])
    last = curve[-1] if curve else {}
    initial = float(state.get("initial_equity", manifest["initial_capital"]))
    equity = float(last.get("equity", initial))
    cumulative = equity / initial - 1.0 if initial else 0.0
    validation = getattr(day, "validation", {}) or {}
    return {
        "trading_date": date,
        "run_status": status,
        "baseline": {
            "freeze_sha256": manifest["freeze_sha256"],
            "freeze_date": manifest["freeze_date"],
            "code_sha256": manifest["code_sha256"],
        },
        "market": {
            "risk_on": None if getattr(day, "index_signal", None) is None
            else bool(day.index_signal["risk_on"]),
            "allowed_groups": list((signal or {}).get("allowed_groups", [])),
            "index_close": getattr(day, "index_close", None),
        },
        "rebalance": {
            "decision_today": bool(signal),
            "next_decision_date": next_decision_date,
            "anchor_status": manifest["rebalance_anchor"]["status"],
        },
        "ranking": {
            "targets": list((signal or {}).get("targets", [])),
            "ranked": (signal or {}).get("ranked", []),
        },
        "virtual_fills": fills,
        "unfilled": [
            {
                "ticker": c["ticker"],
                "side": c["side"],
                "remaining_qty": c["remaining_qty"],
                "age": c["age"],
            }
            for c in state.get("carry", [])
        ],
        "portfolio": {
            "holdings": dict(state.get("holdings", {})),
            "cash": float(state.get("cash", 0.0)),
            "equity": equity,
            "cumulative_return": cumulative,
            "mdd": float(last.get("mdd", 0.0)),
            "stale_tickers": sorted(set(getattr(day, "stale_tickers", []) or [])),
        },
        "data": {
            "ok": bool(validation.get("ok")),
            "errors": list(validation.get("errors", [])),
            "missing_tickers": list(validation.get("missing_tickers", [])),
            "invalid_tickers": list(validation.get("invalid_tickers", [])),
            "source": getattr(day, "source", ""),
            "corporate_actions": "EMPTY template; no official coverage (CA-COVERAGE blocker)",
            "distributions": "EMPTY template; price-return basis only (DISTRIBUTION blocker)",
            "pit_classification": "unavailable (PIT-CLASS-TAX blocker)",
            "independent_source": "unavailable (INDEPENDENT-SOURCE blocker)",
        },
        "integrity": {
            "ledger_ok": bool(integrity.get("ok")),
            "consistent": bool(integrity.get("consistent", integrity.get("ok"))),
            "last_event_seq": state.get("last_event_seq"),
            "last_commit_seq": integrity.get("last_commit_seq"),
            "orphan_count": integrity.get("orphan_count", 0),
            "processed_dates": len(state.get("processed_dates", [])),
        },
    }


def format_report(report: dict[str, Any]) -> str:
    p = report["portfolio"]
    m = report["market"]
    r = report["rebalance"]
    d = report["data"]
    i = report["integrity"]
    lines = [
        f"[Forward Shadow] {report['trading_date']} — {report['run_status']}",
        (
            f"Baseline freeze={report['baseline']['freeze_sha256'][:12]}… "
            f"code={report['baseline']['code_sha256'][:12]}…"
        ),
        f"시장 risk_on={m['risk_on']} / 허용그룹={','.join(m['allowed_groups']) or '없음'}",
        (
            f"리밸런싱={'예' if r['decision_today'] else '아니오'} "
            f"/ 다음 결정일={r['next_decision_date']} (anchor={r['anchor_status']})"
        ),
        f"랭킹={','.join(report['ranking']['targets']) or '없음'}",
    ]
    for fill in report["virtual_fills"]:
        lines.append(
            f"  가상{fill['side']} {fill['ticker']} "
            f"{fill['filled_qty']}/{fill['requested_qty']}주 "
            f"({fill['decision']}, 잔량 {fill['remaining_qty']})"
        )
    if not report["virtual_fills"]:
        lines.append("  가상체결 없음")
    for unfilled in report["unfilled"]:
        lines.append(
            f"  미체결 carry {unfilled['side']} {unfilled['ticker']} "
            f"{unfilled['remaining_qty']}주 (age={unfilled['age']})"
        )
    lines.append(
        f"보유={p['holdings'] or '{}'} / 현금={p['cash']:,.0f} / 평가액={p['equity']:,.0f}"
    )
    lines.append(f"누적수익률={p['cumulative_return']:.2%} / MDD={p['mdd']:.2%}")
    lines.append(
        f"데이터 ok={d['ok']} / 누락={d['missing_tickers']} / 무효={d['invalid_tickers']} "
        f"/ stale={p['stale_tickers']}"
    )
    lines.append("한계: 기업행위·분배금 미확보, PIT/독립가격 미확보 (가격수익 기준)")
    lines.append(
        f"원장 ok={i['ledger_ok']} / 정합={i['consistent']} / seq={i['last_event_seq']} "
        f"/ orphan={i['orphan_count']}"
    )
    return "\n".join(lines)


def write_report(paths: Any, report: dict[str, Any]) -> Path:
    date = report["trading_date"]
    target_json = paths.reports / f"{date}.json"
    target_txt = paths.reports / f"{date}.txt"
    atomic_write_json(target_json, report)
    text = format_report(report) + "\n"
    target_txt.write_text(text, encoding="utf-8")
    return target_txt


def maybe_send(text: str, sink: ReportSink | None = None) -> bool:
    """Opt-in send path.  Default is a no-op; tests inject a capturing sink."""
    if sink is not None:
        # Explicitly injected sink (tests / dry-run transport): no env gate.
        return sink.send(text)
    if os.environ.get("SHADOW_TELEGRAM_SEND", "0") != "1":
        return False
    from live_trading.telegram_notifier import TelegramNotifier

    return TelegramNotifier().send(text)
