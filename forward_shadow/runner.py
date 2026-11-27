"""One-day Forward Shadow orchestration.

Invariants enforced here:
  * D-close signal -> D+1-open virtual execution (never same-day look-ahead);
  * missing/invalid market data holds both signal and execution;
  * no re-run of the full historical backtest to overwrite shadow state;
  * every processed date is idempotent and committed through the ledger;
  * no broker adapter is imported and no real order API is reachable.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pandas as pd

from forward_shadow.config import EffectiveConfig, verify_manifest
from forward_shadow.engine import (
    build_history_frame,
    compute_signal,
    evaluate_equity,
    execute_pending,
)
from forward_shadow.market import (
    MarketDay,
    compute_index_signal,
    save_snapshot,
    validate_market_day,
)
from forward_shadow.report import build_report, format_report, maybe_send, write_report
from forward_shadow.store import RuntimePaths, ShadowStore, file_lock, read_json

# Read-only copy of the frozen 2026 KRX calendar from live_trading/etf_daily_runner.py.
# It is duplicated deliberately so the shadow never imports broker adapters.
# Update annually, exactly as the live runner's list is updated.
_KRX_HOLIDAYS: set[dt.date] = {
    dt.date(2026, 1, 1), dt.date(2026, 2, 16), dt.date(2026, 2, 17),
    dt.date(2026, 2, 18), dt.date(2026, 3, 1), dt.date(2026, 3, 2),
    dt.date(2026, 5, 1), dt.date(2026, 5, 5), dt.date(2026, 5, 24),
    dt.date(2026, 5, 25), dt.date(2026, 6, 3), dt.date(2026, 6, 6),
    dt.date(2026, 7, 17), dt.date(2026, 8, 15), dt.date(2026, 8, 17),
    dt.date(2026, 9, 24), dt.date(2026, 9, 25), dt.date(2026, 9, 26),
    dt.date(2026, 10, 3), dt.date(2026, 10, 5), dt.date(2026, 10, 9),
    dt.date(2026, 12, 25), dt.date(2026, 12, 31),
}


def is_trading_day(day: dt.date) -> bool:
    return day.weekday() < 5 and day not in _KRX_HOLIDAYS


def _index_history_from_snapshots(paths: RuntimePaths, up_to: str, seed: pd.DataFrame | None) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if paths.snapshots.exists():
        for path in sorted(paths.snapshots.glob("*.json")):
            if path.stem > up_to:
                continue
            payload = read_json(path)
            if not payload.get("validation", {}).get("ok"):
                continue
            close = payload.get("index_close")
            if close is not None:
                rows.append({"date": pd.Timestamp(path.stem), "close": float(close)})
    frame = pd.DataFrame(rows)
    if seed is not None and not seed.empty:
        seed = seed.copy()
        seed["date"] = pd.to_datetime(seed["date"], errors="coerce")
        frame = pd.concat([seed[["date", "close"]], frame], ignore_index=True)
    if frame.empty:
        return frame
    return (
        frame.dropna(subset=["date", "close"])
        .drop_duplicates(subset=["date"], keep="last")
        .sort_values("date")
        .reset_index(drop=True)
    )


def _next_decision_hint(state: dict[str, Any], config: EffectiveConfig) -> str:
    idx = int(state["forward_day_index"])
    offset = config.decision_offset
    interval = config.interval
    elapsed = (idx - offset) % interval
    remaining = (interval - elapsed) % interval
    target = idx + remaining
    return f"index {target} (+{remaining} trading days)"


def process_date(
    date: str,
    provider: Any,
    runtime_root: str | Path,
    *,
    seed_etf: pd.DataFrame | None = None,
    seed_index: pd.DataFrame | None = None,
    sink: Any = None,
) -> dict[str, Any]:
    """Process one confirmed trading date. Returns a status dictionary."""
    paths = RuntimePaths(Path(runtime_root))
    with file_lock(paths.lock):
        manifest = read_json(paths.manifest)
        problems = verify_manifest(manifest)
        if problems:
            raise RuntimeError("fail-closed: " + "; ".join(problems))
        config = EffectiveConfig(manifest)
        store = ShadowStore(paths)
        state, integrity = store.load()

        target = dt.date.fromisoformat(date)
        if not is_trading_day(target):
            return {"date": date, "status": "SKIPPED_HOLIDAY", "integrity": integrity}
        if date < config.observation_start:
            return {"date": date, "status": "BEFORE_OBSERVATION_START", "integrity": integrity}
        if store.is_processed(date):
            return {"date": date, "status": "ALREADY_PROCESSED", "integrity": integrity}
        last_date = state.get("last_processed_date")
        if last_date and date < last_date:
            return {"date": date, "status": "OUT_OF_SEQUENCE", "integrity": integrity}

        bars, index_row, fetch_errors = provider.fetch_day(date, config.universe)
        index_history = _index_history_from_snapshots(paths, date, seed_index)
        # The decision is labelled with `date`; the index row for that same
        # date must therefore be part of the MA/slope calculation.
        today_close = index_row.get("close")
        if today_close is not None:
            if index_history.empty:
                index_history = pd.DataFrame(columns=["date", "close"])
            today_row = pd.DataFrame(
                [{"date": pd.Timestamp(date), "close": float(today_close)}]
            )
            index_history = (
                pd.concat([index_history, today_row], ignore_index=True)
                .drop_duplicates(subset=["date"], keep="last")
                .sort_values("date")
                .reset_index(drop=True)
            )
        index_signal = compute_index_signal(
            index_history,
            date,
            int(config.params["market_ma_days"]),
            int(config.params["market_slope_days"]),
        )
        validation = validate_market_day(
            date=date,
            tickers=config.universe,
            bars=bars,
            index_row=index_row,
            index_signal=index_signal,
            fetch_errors=fetch_errors,
        )
        day = MarketDay(
            date=date,
            bars=bars,
            index_close=index_row.get("close"),
            index_signal=index_signal,
            validation=validation,
            source=getattr(provider, "name", type(provider).__name__),
        )

        if not validation["ok"]:
            store.append_event(
                date=date,
                event_type="ERROR",
                payload={"reason": "market data invalid; signal and execution held",
                         "validation": validation},
                state_patch={},
            )
            store.commit(date=date, state=state, status="BLOCKED_DATA")
            report = build_report(
                date=date, status="BLOCKED_DATA", manifest=manifest, state=state,
                day=day, signal=None, fills=[], integrity=integrity,
                next_decision_date=_next_decision_hint(state, config),
            )
            write_report(paths, report)
            maybe_send(format_report(report), sink)
            return {"date": date, "status": "BLOCKED_DATA", "errors": validation["errors"]}

        save_snapshot(paths, day)
        history = build_history_frame(paths.snapshots, config.universe, date, seed=seed_etf)

        fills, fill_patch = execute_pending(state=state, day=day, config=config)
        fill_patch["pending_signal"] = None
        state.update(fill_patch)
        store.append_event(
            date=date, event_type="FILL",
            payload={"fills": fills, "orders_consumed": state.get("pending_signal") is None},
            state_patch=fill_patch,
        )

        equity_row, equity_patch = evaluate_equity(state=state, day=day)
        state.update(equity_patch)
        store.append_event(
            date=date, event_type="EQUITY", payload={"row": equity_row},
            state_patch=equity_patch,
        )

        next_index = int(state["forward_day_index"]) + 1
        decision_today = (next_index - config.decision_offset) % config.interval == 0
        signal: dict[str, Any] | None = None
        if decision_today:
            signal = compute_signal(
                history=history,
                date=date,
                index_signal=index_signal,
                holdings=state["holdings"],
                config=config,
            )
            store.append_event(
                date=date, event_type="SIGNAL", payload={"signal": signal},
                state_patch={"pending_signal": signal},
            )
            state["pending_signal"] = signal

        state_patch = {
            "processed_dates": sorted(set(state.get("processed_dates", [])) | {date}),
            "forward_day_index": next_index,
        }
        state.update(state_patch)
        store.append_event(
            date=date, event_type="STATE", payload={"forward_day_index": next_index},
            state_patch=state_patch,
        )
        store.commit(date=date, state=state, status="DONE")

        report = build_report(
            date=date, status="DONE", manifest=manifest, state=state, day=day,
            signal=signal, fills=fills, integrity=integrity,
            next_decision_date=_next_decision_hint(state, config),
        )
        write_report(paths, report)
        maybe_send(format_report(report), sink)
        return {
            "date": date,
            "status": "DONE",
            "fills": fills,
            "decision_today": decision_today,
            "equity": equity_row["equity"],
            "integrity": integrity,
        }


def initialize_runtime(
    runtime_root: str | Path,
    *,
    initial_capital: float,
    observation_start_date: str,
    initial_holdings: dict[str, int] | None = None,
    decision_offset_days: int = 0,
) -> dict[str, Any]:
    """Create an independent runtime root with an unapproved test manifest."""
    from forward_shadow.config import build_manifest

    paths = RuntimePaths(Path(runtime_root))
    with file_lock(paths.lock):
        manifest = build_manifest(
            initial_capital=initial_capital,
            initial_holdings=initial_holdings,
            observation_start_date=observation_start_date,
            decision_offset_days=decision_offset_days,
        )
        return ShadowStore(paths).initialize(manifest)
