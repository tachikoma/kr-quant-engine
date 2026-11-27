"""Daily shadow report: deterministic text plus an opt-in, independent send path.

The report is committed to the ledger before any file is written, so a crash
after COMMIT can always regenerate it.  Sending uses shadow-only credentials
(``SHADOW_TELEGRAM_BOT_TOKEN`` / ``SHADOW_TELEGRAM_CHAT_ID``) and never falls
back to the live notification channel.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Protocol

from forward_shadow.market import content_sha256
from forward_shadow.store import atomic_write_json, atomic_write_text, read_json

logger = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096


class ReportSink(Protocol):
    def send(self, message: str) -> bool: ...


class CapturingSink:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, message: str) -> bool:
        self.messages.append(message)
        return True


class ShadowTelegramSink:
    """Plain-text Telegram sender dedicated to the shadow track."""

    def __init__(self, token: str, chat_id: str, post: Any = None) -> None:
        self._token = token
        self._chat_id = chat_id
        self._post = post

    @classmethod
    def from_env(cls) -> ShadowTelegramSink | None:
        token = os.environ.get("SHADOW_TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("SHADOW_TELEGRAM_CHAT_ID", "").strip()
        return cls(token, chat_id) if token and chat_id else None

    def send(self, message: str) -> bool:
        try:
            post = self._post
            if post is None:
                import requests

                post = requests.post
            response = post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": self._chat_id, "text": message[:TELEGRAM_LIMIT]},
                timeout=10,
            )
            response.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001 - notification must not fail the run
            # The exception text can contain the request URL (and so the token).
            logger.warning("shadow Telegram send failed: %s", type(exc).__name__)
            return False


def resolve_sink(sink: ReportSink | None) -> ReportSink | None:
    """Injected sink, else the opt-in shadow channel, else no sending."""
    if sink is not None:
        return sink
    if os.environ.get("SHADOW_TELEGRAM_SEND", "0") != "1":
        return None
    configured = ShadowTelegramSink.from_env()
    if configured is None:
        logger.warning("SHADOW_TELEGRAM_SEND=1 but shadow Telegram credentials are missing")
    return configured


def observation_counts(observations: dict[str, str]) -> dict[str, int]:
    values = list(observations.values())
    return {kind: values.count(kind) for kind in sorted(set(values))}


def build_report(
    *,
    date: str,
    status: str,
    manifest: dict[str, Any],
    state: dict[str, Any],
    day: Any,
    observation_type: str | None,
    signal: dict[str, Any] | None,
    fills: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
    integrity: dict[str, Any],
    next_decision_date: str | None,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    curve = state.get("equity_curve", [])
    last = curve[-1] if curve else {}
    initial = float(state.get("initial_equity", manifest["initial_capital"]))
    equity = float(last.get("equity", initial))
    cumulative = equity / initial - 1.0 if initial else 0.0
    validation = getattr(day, "validation", None) or {}
    index_signal = getattr(day, "index_signal", None)
    observations = state.get("observations", {})
    return {
        "trading_date": date,
        "run_status": status,
        "runtime_kind": manifest["runtime_kind"],
        "observation_type": observation_type,
        "observation_counts": observation_counts(observations),
        "baseline": {
            "freeze_sha256": manifest["freeze_sha256"],
            "freeze_date": manifest["freeze_date"],
            "code_sha256": manifest["code_sha256"],
            "warmup_sha256": (manifest.get("warmup") or {}).get("sha256"),
        },
        "market": {
            "risk_on": None if index_signal is None else bool(index_signal["risk_on"]),
            "allowed_groups": list((signal or {}).get("allowed_groups", [])),
            "index_close": getattr(day, "index_close", None),
        },
        "rebalance": {
            "decision_today": bool(signal),
            "execution_date": (signal or {}).get("execution_date"),
            "next_decision_date": next_decision_date,
            "anchor_status": manifest["rebalance_anchor"]["status"],
            "anchor_date": manifest["rebalance_anchor"]["anchor_date"],
            "ad_hoc_closures": list(state.get("extra_holidays", [])),
        },
        "ranking": {
            "targets": list((signal or {}).get("targets", [])),
            "ranked": (signal or {}).get("ranked", []),
        },
        "virtual_fills": fills,
        "order_diagnostics": diagnostics,
        "unfilled": [
            {
                "ticker": c["ticker"],
                "side": c["side"],
                "remaining_qty": c["remaining_qty"],
                "due_date": c["due_date"],
            }
            for c in state.get("carry", [])
        ],
        "portfolio": {
            "valuation_date": last.get("date"),
            "holdings": dict(state.get("holdings", {})),
            "cost_basis": dict(state.get("cost_basis", {})),
            "cash": float(state.get("cash", 0.0)),
            "equity": equity,
            "cumulative_return": cumulative,
            "mdd": float(last.get("mdd", 0.0)),
            "max_drawdown": float(last.get("max_drawdown", 0.0)),
            "stale_tickers": list(last.get("stale_tickers", [])),
            "limit_flags": list(last.get("limit_flags", [])),
        },
        "data": {
            "ok": bool(validation.get("ok")),
            "errors": list(errors if errors is not None else validation.get("errors", [])),
            "missing_tickers": list(validation.get("missing_tickers", [])),
            "invalid_tickers": list(validation.get("invalid_tickers", [])),
            "source": getattr(day, "source", ""),
            "retrieved_at": getattr(day, "retrieved_at", ""),
            "snapshot_sha256": getattr(day, "sha256", ""),
            "corporate_actions": "EMPTY template; no official coverage (CA-COVERAGE blocker)",
            "distributions": "EMPTY template; price-return basis only (DISTRIBUTION blocker)",
            "pit_classification": "unavailable (PIT-CLASS-TAX blocker)",
            "independent_source": "unavailable (INDEPENDENT-SOURCE blocker)",
        },
        "integrity": {
            "ledger_ok": bool(integrity.get("ok")),
            "prior_commit_seq": integrity.get("commit_seq"),
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
        (
            f"[Forward Shadow/{report['runtime_kind']}] {report['trading_date']} — "
            f"{report['run_status']} (관측={report['observation_type'] or '없음'})"
        ),
        (
            f"Baseline freeze={report['baseline']['freeze_sha256'][:12]}… "
            f"code={report['baseline']['code_sha256'][:12]}…"
        ),
    ]
    blocked = report["run_status"] != "DONE"
    if blocked:
        lines.append(f"마지막 정상 평가일={p['valuation_date'] or '없음 (평가 이력 없음)'}")
        lines.extend(f"  사유: {error}" for error in d["errors"][:10])
    else:
        lines.append(
            f"시장 risk_on={m['risk_on']} / 허용그룹={','.join(m['allowed_groups']) or '-'}"
        )
    lines.append(
        f"리밸런싱={'예' if r['decision_today'] else '아니오'} "
        f"/ 다음 결정일={r['next_decision_date'] or '달력 범위 밖'} "
        f"(anchor={r['anchor_date']}, {r['anchor_status']})"
    )
    if r["ad_hoc_closures"]:
        lines.append(f"임시 휴장 등록={','.join(r['ad_hoc_closures'])}")
    if r["decision_today"]:
        lines.append(f"체결 예정일={r['execution_date']}")
        lines.append(f"랭킹={','.join(report['ranking']['targets']) or '없음'}")
    for fill in report["virtual_fills"]:
        lines.append(
            f"  가상{fill['side']} {fill['ticker']} {fill['filled_qty']}주 "
            f"@ {fill['reference_price']:,.2f} ({fill['source']})"
        )
    if not report["virtual_fills"]:
        lines.append("  가상체결 없음")
    for item in report["order_diagnostics"]:
        if item["decision"] != "FULL":
            lines.append(
                f"  진단 {item['side']} {item['ticker']} {item['decision']} "
                f"{item['filled_qty']}/{item['requested_qty']}주"
            )
    for unfilled in report["unfilled"]:
        lines.append(
            f"  미체결 carry {unfilled['side']} {unfilled['ticker']} "
            f"{unfilled['remaining_qty']}주 (due={unfilled['due_date']})"
        )
    as_of = f" [기준일 {p['valuation_date']}]" if blocked and p["valuation_date"] else ""
    lines.append(
        f"보유={p['holdings'] or '{}'} / 현금={p['cash']:,.0f} / 평가액={p['equity']:,.0f}{as_of}"
    )
    lines.append(
        f"누적수익률={p['cumulative_return']:.2%} / 현재DD={p['mdd']:.2%} "
        f"/ MDD={p['max_drawdown']:.2%}"
    )
    if p["limit_flags"]:
        lines.append(f"한도 초과(부분체결): {'; '.join(p['limit_flags'])}")
    lines.append(
        f"데이터 ok={d['ok']} / 누락={d['missing_tickers']} / 무효={d['invalid_tickers']} "
        f"/ stale={p['stale_tickers']}"
    )
    if d["snapshot_sha256"]:
        lines.append(
            f"snapshot={d['snapshot_sha256'][:12]}… source={d['source']} "
            f"조회={d['retrieved_at']}"
        )
    lines.append("한계: 기업행위·분배금 미확보, PIT/독립가격 미확보 (가격수익 기준)")
    lines.append(
        f"원장 ok={i['ledger_ok']} / 처리일수={i['processed_dates']} "
        f"/ 관측구분={report['observation_counts']} / orphan={i['orphan_count']}"
    )
    return "\n".join(lines)


def _delivered_path(paths: Any) -> Path:
    return paths.reports / "delivered.json"


def publish_reports(paths: Any, events: list[dict[str, Any]], sink: ReportSink | None) -> list[str]:
    """Write (or restore) the report files of the latest committed REPORT per date.

    Returns the dates whose files were written.  Undelivered reports are sent
    once through ``sink`` when one is configured.
    """
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["type"] == "REPORT":
            latest[event["date"]] = event["payload"]["report"]
    stale = [d for d, report in latest.items() if not report_is_current(paths, d, report)]
    target = resolve_sink(sink)
    marker = _delivered_path(paths)
    try:
        delivered = read_json(marker)
    except (OSError, ValueError):
        delivered = {}
    if target is not None and stale:
        # Outbox: an empty entry means "pending" and is durable before any file is
        # written, so a crash at any later point still leaves the send to retry.
        # Only reports (re)written here become pending; history is never re-sent.
        for date in stale:
            if delivered.get(date) != content_sha256(latest[date]):
                delivered[date] = ""
        atomic_write_json(marker, delivered)
    for date in stale:
        atomic_write_text(paths.reports / f"{date}.txt", format_report(latest[date]) + "\n")
        atomic_write_json(paths.reports / f"{date}.json", latest[date])
    if target is None:
        return stale
    for date in sorted(latest):
        if delivered.get(date) == "" and target.send(format_report(latest[date])):
            delivered[date] = content_sha256(latest[date])
            atomic_write_json(marker, delivered)
    return stale


def report_is_current(paths: Any, date: str, report: dict[str, Any]) -> bool:
    try:
        stored = read_json(paths.reports / f"{date}.json")
        text = (paths.reports / f"{date}.txt").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    return stored == report and text == format_report(report) + "\n"
