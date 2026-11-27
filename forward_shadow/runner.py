"""One-day Forward Shadow orchestration.

Invariants enforced here:
  * D-close signal -> next-trading-day-open execution, bound by date;
  * trading days are processed strictly in calendar order; a missing day blocks
    every later day until it is recovered;
  * the rebalance phase is a function of the calendar, not of processing success;
  * missing/invalid market data holds both signal and execution;
  * a FORWARD runtime accepts only confirmed data from the live provider inside
    the observation window; later processing is labelled RECOVERY;
  * every processed date is idempotent and committed through the ledger;
  * no broker adapter is imported and no real order API is reachable.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from forward_shadow.config import (
    CONFIRM_CUTOFF_KST,
    WINDOW_CLOSE_KST,
    EffectiveConfig,
    build_manifest,
    effective_parameters_from_freeze,
    verify_manifest,
)
from forward_shadow.engine import (
    build_history_frame,
    compute_signal,
    evaluate_equity,
    execute_day,
)
from forward_shadow.market import (
    MarketDay,
    PykrxProvider,
    compute_index_signal,
    load_snapshot,
    now_utc_iso,
    save_snapshot,
    scrub_secrets,
    snapshot_path,
    validate_market_day,
)
from forward_shadow.report import (
    build_report,
    observation_counts,
    publish_reports,
    report_is_current,
)
from forward_shadow.store import (
    IntegrityError,
    RuntimePaths,
    ShadowStore,
    file_lock,
    read_json,
)
from forward_shadow.trading_calendar import CalendarRangeError, TradingCalendar
from forward_shadow.warmup import (
    WarmupInput,
    build_bundle,
    bundle_summary,
    load_bundle,
    save_bundle,
)
from strategy_freeze import load_frozen_strategy

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
Clock = Callable[[], dt.datetime]


def system_clock() -> dt.datetime:
    return dt.datetime.now(KST)


def _at(day: dt.date, hhmm: str) -> dt.datetime:
    return dt.datetime.combine(day, dt.time.fromisoformat(hhmm), tzinfo=KST)


def observation_window(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """[data confirmed, next trading open) — the only window for a forward record.

    The deadline deliberately ignores ad-hoc closures: an operator entry that is
    not yet proven by market data must never lengthen the window.
    """
    return (
        _at(day, CONFIRM_CUTOFF_KST),
        _at(TradingCalendar().next_trading_day(day), WINDOW_CLOSE_KST),
    )


def _require_live_provider(provider: Any) -> None:
    if not isinstance(provider, PykrxProvider):
        raise RuntimeError(  # noqa: TRY004 - one fail-closed error class for the CLI
            "fail-closed: a FORWARD runtime accepts only the live pykrx provider, "
            f"got {type(provider).__name__}"
        )


def initialize_runtime(
    runtime_root: str | Path,
    *,
    initial_capital: float,
    observation_start_date: str,
    runtime_kind: str = "TEST",
    warmup: WarmupInput | None = None,
    provider: Any = None,
    anchor_date: str | None = None,
    decision_offset_days: int = 0,
    approval: dict[str, Any] | None = None,
    test_only_forward: bool = False,
    clock: Clock = system_clock,
) -> dict[str, Any]:
    """Create an independent runtime root with a pinned warm-up bundle.

    A TEST runtime takes ``warmup`` from the caller.  A FORWARD runtime fetches
    it from the live provider and refuses caller-supplied files.
    """
    calendar = TradingCalendar()
    start = dt.date.fromisoformat(observation_start_date)
    if not calendar.is_trading_day(start):
        raise ValueError(f"observation start {start} is not a KRX trading day")
    anchor = dt.date.fromisoformat(anchor_date or observation_start_date)
    if not calendar.is_trading_day(anchor):
        raise ValueError(f"anchor date {anchor} is not a KRX trading day")
    if not initial_capital > 0 or initial_capital == float("inf"):
        raise ValueError("initial capital must be finite and positive")
    if runtime_kind == "FORWARD":
        if warmup is not None:
            raise RuntimeError(
                "fail-closed: a FORWARD runtime fetches its own warm-up from the live provider"
            )
        _require_live_provider(provider)
        now = clock()
        if now >= observation_window(start)[1]:
            if calendar.is_trading_day(now.date()) and now.time() < dt.time(18, 0):
                proposed = now.date()
            else:
                proposed = calendar.next_trading_day(now.date())
            raise RuntimeError(
                f"fail-closed: observation window of {start} already closed; "
                "a FORWARD track cannot start retroactively. "
                f"Next feasible trading day: {proposed}"
            )

    frozen = load_frozen_strategy()
    params = effective_parameters_from_freeze(frozen)
    universe = [str(t) for t in frozen["universe"]]
    min_rows = int(params["warmup_days"])
    paths = RuntimePaths(Path(runtime_root))
    with file_lock(paths.lock):
        paths.ensure()
        if runtime_kind == "FORWARD":
            end = calendar.previous_trading_day(start)
            if clock() < _at(end, CONFIRM_CUTOFF_KST):
                raise RuntimeError(f"fail-closed: warm-up end {end} is not confirmed yet")
            # Calendar days comfortably covering the required trading rows.
            first = end - dt.timedelta(days=min_rows * 2)
            frames, index = provider.fetch_history(universe, str(first), str(end))
            warmup = WarmupInput(
                etf_frames=frames,
                index_frame=index,
                listing_dates=provider.fetch_listing_dates(universe),
                source=f"pykrx live fetch {first}..{end}",
            )
        if warmup is None:
            raise ValueError("a TEST runtime requires a warm-up input")
        if runtime_kind == "FORWARD" and approval is None and not test_only_forward:
            raise RuntimeError(
                "fail-closed: a FORWARD runtime requires an approved manifest; "
                "unapproved settings cannot start normal Forward observation"
            )
        bundle = build_bundle(
            warmup,
            universe=universe,
            observation_start=start,
            calendar=calendar,
            min_rows=min_rows,
        )
        manifest = build_manifest(
            initial_capital=initial_capital,
            observation_start_date=str(start),
            runtime_kind=runtime_kind,
            warmup=bundle_summary(bundle, now_utc_iso(clock())),
            anchor_date=str(anchor),
            decision_offset_days=decision_offset_days,
            approval=approval,
            test_only_forward=test_only_forward,
        )
        if paths.manifest.exists():
            raise FileExistsError(f"Runtime already initialized: {paths.root}")
        save_bundle(paths, bundle)
        return ShadowStore(paths).initialize(manifest)


class _Runtime:
    """Verified view of one runtime root (caller holds the lock)."""

    def __init__(self, paths: RuntimePaths, *, repair: bool) -> None:
        self.paths = paths
        self.manifest = read_json(paths.manifest)
        problems = verify_manifest(self.manifest)
        if problems:
            raise RuntimeError("fail-closed: " + "; ".join(problems))
        self.config = EffectiveConfig(self.manifest)
        self.store = ShadowStore(paths)
        self.state, self.integrity = self.store.load(repair=repair)
        self.warmup = load_bundle(paths, self.manifest["warmup"]["sha256"])
        self.calendar = TradingCalendar(self.state["extra_holidays"])
        self.history_days = [
            load_snapshot(paths, date, sha)
            for date, sha in sorted(self.state["snapshot_hashes"].items())
        ]

    def expected_date(self) -> dt.date:
        last = self.state["last_processed_date"]
        if last is None:
            return dt.date.fromisoformat(self.config.observation_start)
        return self.calendar.next_trading_day(dt.date.fromisoformat(last))

    def next_decision(self, state: dict[str, Any]) -> str | None:
        last = state["last_processed_date"]
        after = (
            dt.date.fromisoformat(last)
            if last
            else dt.date.fromisoformat(self.config.observation_start) - dt.timedelta(days=1)
        )
        try:
            found = self.calendar.next_decision_day(
                after,
                dt.date.fromisoformat(self.config.anchor_date),
                self.config.decision_offset,
                self.config.interval,
            )
        except CalendarRangeError:
            return None
        return None if found is None else str(found)


def _block(
    runtime: _Runtime,
    date: str,
    status: str,
    errors: list[str],
    *,
    day: MarketDay | None = None,
    sink: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    """Record a held day: no signal, no execution, no continuity advance."""
    store, state = runtime.store, runtime.state
    patch = {"last_attempt": {"date": date, "status": status}}
    state = {**state, **patch}
    store.append_event(
        date=date,
        event_type="ERROR",
        payload={"status": status, "reason": "signal and execution held", "errors": errors},
        state_patch=patch,
    )
    report = build_report(
        date=date, status=status, manifest=runtime.manifest, state=state, day=day,
        observation_type=None, signal=None, fills=[], diagnostics=[],
        integrity=runtime.integrity, next_decision_date=runtime.next_decision(state),
        errors=errors,
    )
    store.append_event(date=date, event_type="REPORT", payload={"report": report})
    store.commit(date=date, state=state, status=status)
    publish_reports(runtime.paths, store.committed_events(), sink)
    return {"date": date, "status": status, "errors": errors, **extra}


def _rebound(state: dict[str, Any], closed_or_reopened: TradingCalendar) -> dict[str, Any]:
    """Re-bind the pending signal and carry to the next trading day of a calendar."""
    last = state["last_processed_date"]
    if last is None:
        return {}
    following = str(closed_or_reopened.next_trading_day(dt.date.fromisoformat(last)))
    patch: dict[str, Any] = {"carry": [{**c, "due_date": following} for c in state["carry"]]}
    if state.get("pending_signal"):
        patch["pending_signal"] = {**state["pending_signal"], "execution_date": following}
    return patch


def _verify_closures(
    runtime: _Runtime, provider: Any, date: str
) -> tuple[list[str], list[str]]:
    """Check every ad-hoc closure this run would step over against market data.

    A closure for a day that did trade would silently drop that day and move the
    signal to a later one, so it is revoked in the ledger.  Absence counts only
    when the same successful response contains the later ``date``; a failed or
    incomplete response proves nothing.  Returns ``(revoked, unverified)``.
    """
    state = runtime.state
    after = state["last_processed_date"] or runtime.config.observation_start
    revoked: list[str] = []
    unverified: list[str] = []
    for closed in state["extra_holidays"]:
        if not after < closed < date:
            continue
        first = dt.date.fromisoformat(closed) - dt.timedelta(days=10)
        try:
            sessions = provider.index_dates(str(first), date)
        except Exception as exc:  # noqa: BLE001 - unknown, handled by blocking
            logger.warning("closure check failed: %s", scrub_secrets(str(exc)))
            unverified.append(closed)
            continue
        if closed in sessions:
            revoked.append(closed)
        elif date not in sessions:
            unverified.append(closed)
    if revoked:
        extra = [d for d in state["extra_holidays"] if d not in revoked]
        calendar = TradingCalendar(extra)
        patch = {"extra_holidays": extra, **_rebound(state, calendar)}
        state.update(patch)
        runtime.calendar = calendar
        runtime.store.append_event(
            date=date, event_type="CALENDAR",
            payload={"revoked_closures": revoked, "reason": "market data exists"},
            state_patch=patch,
        )
    return revoked, unverified


def _acquire_day(
    runtime: _Runtime, provider: Any, date: str, clock: Clock
) -> tuple[MarketDay, bool]:
    """Return the day's data: the pinned snapshot if one exists, else a fresh fetch.

    The two are never mixed.  A snapshot left by an interrupted run is reused as
    is, but only if the ledger recorded its hash; an unrecorded file (hand-placed,
    or written just before a crash) is set aside and never read.
    """
    paths, config = runtime.paths, runtime.config
    recorded = {
        e["payload"]["sha256"]
        for e in runtime.store.events
        if e["type"] == "SNAPSHOT" and e["date"] == date
    }
    if len(recorded) > 1:
        raise IntegrityError(f"ledger records several snapshots for {date}")
    target = snapshot_path(paths, date)
    if recorded:
        # A missing or altered recorded snapshot fails closed inside load_snapshot.
        day = load_snapshot(paths, date, next(iter(recorded)))
        if config.runtime_kind == "FORWARD":
            confirm = _at(dt.date.fromisoformat(date), CONFIRM_CUTOFF_KST)
            if dt.datetime.fromisoformat(day.retrieved_at) < confirm:
                raise IntegrityError(f"pinned snapshot of {date} predates data confirmation")
        return day, True
    if target.exists():
        attempt = len(list(paths.snapshots.glob(f"{date}.unrecorded-*")))
        target.rename(paths.snapshots / f"{date}.unrecorded-{attempt}")

    bars, index_row, fetch_errors = provider.fetch_day(date, config.universe)
    retrieved_at = now_utc_iso(clock())

    index_rows = list(runtime.warmup["index"]) + [
        {"date": d.date, "close": d.index_close} for d in runtime.history_days
    ]
    previous_index = float(index_rows[-1]["close"])
    close = index_row.get("close")
    if close is not None:
        index_rows.append({"date": date, "close": close})
    index = pd.DataFrame(index_rows)
    index["date"] = pd.to_datetime(index["date"])
    # The decision is labelled `date`, so that day's row must be in the MA window.
    signal = None
    if close is not None:
        signal = compute_index_signal(
            index,
            date,
            int(config.params["market_ma_days"]),
            int(config.params["market_slope_days"]),
        )
    previous_closes = {
        ticker: rows[-1]["close"] for ticker, rows in runtime.warmup["etf"].items() if rows
    }
    for earlier in runtime.history_days:
        previous_closes.update({t: bar["close"] for t, bar in earlier.bars.items()})
    validation = validate_market_day(
        tickers=config.universe,
        bars=bars,
        index_row=index_row,
        index_signal=signal,
        fetch_errors=fetch_errors,
        previous_closes=previous_closes,
        previous_index_close=previous_index,
    )
    return (
        MarketDay(
            date=date,
            bars=bars,
            index_close=validation["index_close"],
            index_signal=signal,
            validation=validation,
            source=getattr(provider, "name", type(provider).__name__),
            retrieved_at=retrieved_at,
        ),
        False,
    )


def process_date(
    date: str,
    provider: Any,
    runtime_root: str | Path,
    *,
    recover: bool = False,
    sink: Any = None,
    clock: Clock = system_clock,
) -> dict[str, Any]:
    """Process one trading date. Returns a status dictionary."""
    paths = RuntimePaths(Path(runtime_root))
    with file_lock(paths.lock):
        runtime = _Runtime(paths, repair=True)
        _settle_unconfirmed(runtime)
        # Restore reports lost to a crash between COMMIT and the file write.
        restored = publish_reports(paths, runtime.store.committed_events(), sink)
        result = _process_locked(runtime, date, provider, recover=recover, sink=sink, clock=clock)
        result.setdefault("restored_reports", restored)
        return result


def _process_locked(
    runtime: _Runtime,
    date: str,
    provider: Any,
    *,
    recover: bool,
    sink: Any,
    clock: Clock,
) -> dict[str, Any]:
    config, state, calendar = runtime.config, runtime.state, runtime.calendar
    forward = config.runtime_kind == "FORWARD"
    target = dt.date.fromisoformat(date)
    date = target.isoformat()
    try:
        trading = calendar.is_trading_day(target)
        if not trading:
            return {"date": date, "status": "SKIPPED_HOLIDAY"}
        if date < config.observation_start:
            return {"date": date, "status": "BEFORE_OBSERVATION_START"}
        if date in state["processed_dates"]:
            return {"date": date, "status": "ALREADY_PROCESSED"}
        if forward:
            _require_live_provider(provider)
            confirm, deadline = observation_window(target)
            started = clock()
            if started < confirm:
                # Future or unconfirmed intraday data: nothing is recorded.
                return {
                    "date": date,
                    "status": "REJECTED_NOT_CONFIRMED",
                    "errors": [f"data of {date} is not confirmed before {confirm.isoformat()}"],
                }
        expected = runtime.expected_date()
        if target < expected:
            return {"date": date, "status": "OUT_OF_SEQUENCE"}
        if target > expected:
            missing = [
                str(d) for d in calendar.trading_days(expected, target) if d < target
            ]
            return _block(
                runtime, date, "BLOCKED_GAP",
                [f"unprocessed trading days must be recovered first: {', '.join(missing)}"],
                sink=sink, missing_dates=missing,
            )
        revoked, unverified = _verify_closures(runtime, provider, date)
        if unverified:
            return _block(
                runtime, date, "BLOCKED_CALENDAR",
                [
                    (
                        f"cannot verify the registered closure of {', '.join(unverified)} "
                        "against market data; retry when the provider responds"
                    )
                ],
                sink=sink,
            )
        if revoked:
            return _block(
                runtime, date, "BLOCKED_CALENDAR",
                [
                    (
                        f"market data exists for {', '.join(revoked)}: the registered closure "
                        "was revoked; process those trading days first"
                    )
                ],
                sink=sink, revoked_closures=revoked,
            )
        # The signal of this date executes on the next trading day, so that day
        # must also be inside the verified calendar.
        next_day = calendar.next_trading_day(target)
    except CalendarRangeError as exc:
        return _block(runtime, date, "BLOCKED_CALENDAR", [str(exc)], sink=sink)

    if forward and started >= deadline and not recover:
        return _block(
            runtime, date, "BLOCKED_STALE",
            [
                (
                    f"observation window closed at {deadline.isoformat()}; "
                    "rerun with --recover to record a RECOVERY day"
                )
            ],
            sink=sink,
        )

    day, pinned = _acquire_day(runtime, provider, date, clock)
    if not day.ok:
        return _block(
            runtime, date, "BLOCKED_DATA", list(day.validation["errors"]), day=day, sink=sink
        )
    store = runtime.store
    if not pinned:
        # Record the hash right away: from here on a retry must reuse this data.
        save_snapshot(runtime.paths, day)
        store.append_event(
            date=date,
            event_type="SNAPSHOT",
            payload={
                "sha256": day.sha256,
                "source": day.source,
                "retrieved_at": day.retrieved_at,
            },
        )
    observation = "TEST"
    if forward:
        observation = "FORWARD" if clock() < deadline else "RECOVERY"
    while True:
        if observation == "RECOVERY" and not recover:
            return _block(
                runtime, date, "BLOCKED_STALE",
                ["processing overran the observation window; rerun with --recover"],
                day=day, sink=sink,
            )
        first_seq = len(store.events)
        final_state, outcome = _append_day(
            runtime, day, str(next_day), observation, pinned
        )
        # A forward record must be complete, signal included, while the window is
        # still open, so the deadline is read again right before the COMMIT.
        if observation == "FORWARD" and clock() >= deadline:
            store.abort(first_seq, "observation window closed before COMMIT")
            observation = "RECOVERY"
            continue
        store.commit(date=date, state=final_state, status="DONE")
        if observation == "FORWARD":
            # Only a clock read taken after the COMMIT is durable proves the day
            # was recorded in time; until then it is stored as UNCONFIRMED.
            observation = "FORWARD" if clock() < deadline else "RECOVERY"
            _confirm_observation(
                store, final_state, date, observation, "clock read after durable COMMIT",
                status="DONE",
            )
        break
    publish_reports(runtime.paths, store.committed_events(), sink)
    return {
        "date": date,
        "status": "DONE",
        "observation_type": observation,
        **outcome,
        "integrity": runtime.integrity,
    }


def _confirm_observation(
    store: ShadowStore, state: dict[str, Any], date: str, label: str, reason: str,
    *, status: str,
) -> dict[str, Any]:
    """Commit the final observation label of an UNCONFIRMED day with its report."""
    report = next(
        e["payload"]["report"]
        for e in reversed(store.committed_events())
        if e["type"] == "REPORT"
        and e["date"] == date
        and e["payload"]["report"]["run_status"] == "DONE"
    )
    patch = {"observations": {**state["observations"], date: label}}
    state = {**state, **patch}
    store.append_event(
        date=date, event_type="OBSERVATION",
        payload={"observation_type": label, "reason": reason},
        state_patch=patch,
    )
    store.append_event(
        date=date, event_type="REPORT",
        payload={
            "report": {
                **report,
                "observation_type": label,
                "observation_counts": observation_counts(state["observations"]),
            }
        },
    )
    store.commit(date=date, state=state, status=status)
    return {**state, "status": status}


def _settle_unconfirmed(runtime: _Runtime) -> None:
    """A run that died before confirming its day leaves no proof: it is RECOVERY."""
    for date, label in sorted(runtime.state["observations"].items()):
        if label == "UNCONFIRMED":
            runtime.state = _confirm_observation(
                runtime.store, runtime.state, date, "RECOVERY",
                "no completion evidence from the original run",
                status=runtime.state["status"],
            )


def _append_day(
    runtime: _Runtime,
    day: MarketDay,
    next_day: str,
    observation: str,
    pinned: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compute one day and append its events, uncommitted.

    Works on a copy of the state: nothing takes effect unless the caller COMMITs.
    """
    config, store, date = runtime.config, runtime.store, day.date
    state = dict(runtime.state)
    result = execute_day(state=state, day=day, config=config, next_trading_date=next_day)
    state.update(result["patch"])
    store.append_event(
        date=date,
        event_type="FILL",
        payload={
            "snapshot_sha256": day.sha256,
            "execution": result["execution"],
            "orders": result["orders"],
            "fills": result["fills"],
            "diagnostics": result["diagnostics"],
        },
        state_patch=result["patch"],
    )

    equity_row, equity_patch = evaluate_equity(state=state, day=day, config=config)
    equity_row["snapshot_sha256"] = day.sha256
    state.update(equity_patch)
    store.append_event(
        date=date, event_type="EQUITY",
        payload={"row": equity_row, "snapshot_sha256": day.sha256},
        state_patch=equity_patch,
    )

    signal: dict[str, Any] | None = None
    decision_today = runtime.calendar.is_decision_day(
        dt.date.fromisoformat(date),
        dt.date.fromisoformat(config.anchor_date),
        config.decision_offset,
        config.interval,
    )
    if decision_today:
        signal = compute_signal(
            history=build_history_frame(runtime.warmup, [*runtime.history_days, day]),
            date=date,
            execution_date=next_day,
            index_signal=day.index_signal,
            holdings=state["holdings"],
            config=config,
            listing_dates=runtime.warmup["listing_dates"],
        )
        signal["snapshot_sha256"] = day.sha256
        signal["warmup_sha256"] = runtime.manifest["warmup"]["sha256"]
        state["pending_signal"] = signal
        store.append_event(
            date=date, event_type="SIGNAL", payload={"signal": signal},
            state_patch={"pending_signal": signal},
        )

    recorded = "UNCONFIRMED" if observation == "FORWARD" else observation
    state_patch = {
        "last_processed_date": date,
        "processed_dates": [*state["processed_dates"], date],
        "observations": {**state["observations"], date: recorded},
        "snapshot_hashes": {**state["snapshot_hashes"], date: day.sha256},
        "last_attempt": {"date": date, "status": "DONE"},
    }
    state.update(state_patch)
    store.append_event(
        date=date, event_type="STATE",
        payload={
            "observation_type": recorded,
            "snapshot_sha256": day.sha256,
            "reused_pinned": pinned,
        },
        state_patch=state_patch,
    )
    report = build_report(
        date=date, status="DONE", manifest=runtime.manifest, state=state, day=day,
        observation_type=recorded, signal=signal, fills=result["fills"],
        diagnostics=result["diagnostics"], integrity=runtime.integrity,
        next_decision_date=runtime.next_decision(state),
    )
    store.append_event(date=date, event_type="REPORT", payload={"report": report})
    return state, {
        "fills": result["fills"],
        "diagnostics": result["diagnostics"],
        "decision_today": decision_today,
        "equity": equity_row["equity"],
    }


def add_holiday(
    date: str,
    runtime_root: str | Path,
    *,
    reason: str,
) -> dict[str, Any]:
    """Record an ad-hoc KRX closure (e.g. a temporary holiday) in the ledger.

    Only a not-yet-processed trading day without pinned data can be closed, so
    recorded history is never rewritten.  A pending signal or carry bound to the
    closed day moves to the following trading day, as in the backtest, which
    only ever sees real trading days.  The next run past the closed day revokes
    the closure if market data exists for it.
    """
    if not str(reason).strip():
        raise ValueError("a reason (with source) is required")
    paths = RuntimePaths(Path(runtime_root))
    with file_lock(paths.lock):
        runtime = _Runtime(paths, repair=True)
        state, calendar = runtime.state, runtime.calendar
        target = dt.date.fromisoformat(date)
        last = state["last_processed_date"]
        if not calendar.is_trading_day(target):
            raise ValueError(f"{target} is not a trading day")
        if last and target <= dt.date.fromisoformat(last):
            raise ValueError(f"{target} is not after the last processed date {last}")
        if str(target) < runtime.config.observation_start:
            # Earlier sessions are part of the pinned warm-up and are never
            # re-verified; closing one would only shift the rebalance phase.
            raise ValueError(f"{target} is before the observation start")
        if str(target) in (runtime.config.observation_start, runtime.config.anchor_date):
            raise ValueError("the observation start/anchor date cannot be closed")
        if snapshot_path(paths, str(target)).exists():
            raise ValueError(f"valid market data exists for {target}; it was a trading day")
        extra = sorted({*state["extra_holidays"], str(target)})
        patch = {"extra_holidays": extra, **_rebound(state, TradingCalendar(extra))}
        state.update(patch)
        runtime.store.append_event(
            date=str(target), event_type="CALENDAR",
            payload={"closed_date": str(target), "reason": str(reason).strip()},
            state_patch=patch,
        )
        runtime.store.commit(date=str(target), state=state, status="CALENDAR_UPDATED")
        return {"date": str(target), "status": "CALENDAR_UPDATED", "extra_holidays": extra}


def runtime_status(runtime_root: str | Path) -> dict[str, Any]:
    """Read-only verification: manifest, ledger replay, state digest, data hashes."""
    paths = RuntimePaths(Path(runtime_root))
    if not paths.manifest.exists():
        raise FileNotFoundError(f"Shadow runtime not initialized: {paths.manifest}")
    with file_lock(paths.lock):
        try:
            runtime = _Runtime(paths, repair=False)
        except (RuntimeError, OSError, ValueError, KeyError) as exc:
            return {"consistent": False, "problems": [f"{type(exc).__name__}: {exc}"]}
        state, integrity = runtime.state, runtime.integrity
        problems: list[str] = []
        if not integrity["state_file_matches"]:
            problems.append("state.json differs from the ledger replay")
        if integrity["orphan_count"]:
            problems.append(f"{integrity['orphan_count']} uncommitted ledger events")
        if integrity["torn_tail"]:
            problems.append("ledger has a torn final line")
        latest = {
            e["date"]: e["payload"]["report"]
            for e in runtime.store.committed_events()
            if e["type"] == "REPORT"
        }
        stale = sorted(d for d, r in latest.items() if not report_is_current(paths, d, r))
        if stale:
            problems.append(f"reports missing or altered: {', '.join(stale)}")
        pending = sorted(d for d, v in state["observations"].items() if v == "UNCONFIRMED")
        if pending:
            problems.append(f"unconfirmed observations (will be RECOVERY): {', '.join(pending)}")
        curve = state["equity_curve"]
        try:
            expected = str(runtime.expected_date())
        except CalendarRangeError:
            expected = None
        return {
            "consistent": not problems,
            "problems": problems,
            "runtime_kind": runtime.config.runtime_kind,
            "manifest": {
                "freeze_sha256": runtime.manifest["freeze_sha256"],
                "code_sha256": runtime.manifest["code_sha256"],
                "warmup_sha256": runtime.manifest["warmup"]["sha256"],
                "approval_status": runtime.manifest["approval_status"],
                "rebalance_anchor": runtime.manifest["rebalance_anchor"],
            },
            "state": {
                "status": state["status"],
                "last_attempt": state.get("last_attempt"),
                "last_processed_date": state["last_processed_date"],
                "last_valuation_date": curve[-1]["date"] if curve else None,
                "next_expected_date": expected,
                "next_decision_date": runtime.next_decision(state),
                "cash": state["cash"],
                "holdings": state["holdings"],
                "cost_basis": state["cost_basis"],
                "pending_signal": state["pending_signal"],
                "carry": state["carry"],
                "extra_holidays": state["extra_holidays"],
                "observations": state["observations"],
            },
            "integrity": integrity,
        }
