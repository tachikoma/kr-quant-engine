"""Regression tests for the PR #16 pre-merge findings (P1-1..P1-5 and operations).

Each test reproduces a defect observed at ``b95a65b`` or pins the new guard.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

import pandas as pd

import etf_shared
import run_forward_shadow
from forward_shadow import runner
from forward_shadow.config import EffectiveConfig, build_manifest
from forward_shadow.engine import evaluate_equity, execute_day
from forward_shadow.market import FixtureProvider, MarketDay, load_snapshot
from forward_shadow.report import (
    CapturingSink,
    ShadowTelegramSink,
    resolve_sink,
)
from forward_shadow.runner import (
    add_holiday,
    initialize_runtime,
    process_date,
    runtime_status,
)
from forward_shadow.store import (
    IntegrityError,
    RuntimePaths,
    ShadowStore,
    atomic_write_json,
    read_json,
)
from forward_shadow.trading_calendar import HOLIDAYS, CalendarRangeError, TradingCalendar
from forward_shadow.warmup import WarmupError
from tests.shadow_fixtures import (
    CAPITAL,
    OBSERVATION_START,
    ROOT,
    UNIVERSE,
    WARMUP,
    FakeLive,
    ShadowCase,
    kst,
    warmup_input,
)


class TradingContinuityTest(ShadowCase):
    """P1-1: continuity, signal binding, fixed phase and calendar coverage."""

    def test_gap_blocks_and_never_moves_the_signal(self) -> None:
        self.run_days(1)
        before = self.state()
        result = self.run_day(7)
        self.assertEqual(result["status"], "BLOCKED_GAP")
        self.assertEqual(result["missing_dates"], [self.day(j) for j in range(1, 7)])
        after = self.state()
        for key in ("last_processed_date", "cash", "holdings", "pending_signal", "carry"):
            self.assertEqual(after[key], before[key], key)
        # Recovery in order: the D signal fills on D+1 only.
        recovered = self.run_days(7, start=1)
        self.assertTrue(all(r["status"] == "DONE" for r in recovered), recovered)
        self.assertEqual({f["date"] for f in recovered[0]["fills"]}, {self.day(1)})

    def test_blocked_day_is_not_a_processed_day(self) -> None:
        self.run_days(1)
        bad = self.broken_provider(1, lambda f, m: f[UNIVERSE[0]].drop(
            f[UNIVERSE[0]].index[m], inplace=True))
        self.assertEqual(self.run_day(1, bad)["status"], "BLOCKED_DATA")
        self.assertEqual(self.state()["last_processed_date"], self.day(0))
        self.assertEqual(self.run_day(2)["status"], "BLOCKED_GAP")
        self.assertEqual(self.run_day(1)["status"], "DONE")

    def test_phase_is_unchanged_by_outages(self) -> None:
        self.run_days(10)
        self.run_day(25)  # blocked gap
        results = self.run_days(31, start=10)
        decisions = [10 + j for j, r in enumerate(results) if r["decision_today"]]
        self.assertEqual(decisions, [20, 40])
        report = read_json(self.paths.reports / f"{self.day(40)}.json")
        self.assertEqual(report["rebalance"]["next_decision_date"], self.day(60))

    def test_signal_refuses_any_other_execution_date(self) -> None:
        self.run_days(1)
        state = self.state()
        config = EffectiveConfig(read_json(self.paths.manifest))
        bars, _, _ = self.provider.fetch_day(self.day(2), UNIVERSE)
        with self.assertRaisesRegex(RuntimeError, "bound to"):
            execute_day(state=state, day=MarketDay(self.day(2), bars), config=config,
                        next_trading_date=self.day(3))

    def test_anchor_date_fixes_phase_independently_of_start(self) -> None:
        anchor = TradingCalendar().previous_trading_day(OBSERVATION_START)
        self.root = self.new_runtime("anchored", anchor_date=str(anchor))
        self.paths = RuntimePaths(self.root)
        results = self.run_days(22)
        self.assertEqual([j for j, r in enumerate(results) if r["decision_today"]], [19])

    def test_calendar_matches_live_runner_2026_and_covers_2027(self) -> None:
        source = (ROOT / "live_trading" / "etf_daily_runner.py").read_text(encoding="utf-8")
        block = source.split("_KRX_HOLIDAYS: set[dt.date] = {", 1)[1].split("\n}", 1)[0]
        live = {
            dt.date(*map(int, m))
            for m in re.findall(r"dt\.date\((\d+),\s*(\d+),\s*(\d+)\)", block)
        }
        self.assertEqual({d for d in HOLIDAYS if d.year == 2026}, live)
        calendar = TradingCalendar()
        self.assertFalse(calendar.is_trading_day(dt.date(2027, 2, 9)))
        self.assertFalse(calendar.is_trading_day(dt.date(2027, 12, 31)))
        self.assertEqual(calendar.next_trading_day(dt.date(2026, 12, 30)), dt.date(2027, 1, 4))
        self.assertEqual(
            sum(1 for d in HOLIDAYS if d.year == 2027 and d.weekday() < 5), 16
        )

    def test_out_of_coverage_blocks(self) -> None:
        for day in (dt.date(2025, 12, 30), dt.date(2028, 1, 3)):
            with self.assertRaises(CalendarRangeError):
                TradingCalendar().is_trading_day(day)
        self.assertIsNone(
            TradingCalendar().next_decision_day(
                dt.date(2027, 12, 20), dt.date(2027, 12, 20), 0, 20
            )
        )
        self.run_days(1)
        for date in ("2028-01-03", "2027-12-30"):
            result = process_date(date, self.provider, self.root)
            self.assertIn(result["status"], {"BLOCKED_CALENDAR", "BLOCKED_GAP"})
            self.assertEqual(self.state()["last_processed_date"], self.day(0))
        self.assertEqual(
            process_date("2028-01-03", self.provider, self.root)["status"], "BLOCKED_CALENDAR"
        )

    def test_ad_hoc_closure_rebinds_signal_without_rewriting_history(self) -> None:
        self.run_days(1)
        with self.assertRaisesRegex(ValueError, "not after"):
            add_holiday(self.day(0), self.root, reason="x")
        with self.assertRaisesRegex(ValueError, "reason"):
            add_holiday(self.day(1), self.root, reason=" ")
        add_holiday(self.day(1), self.root, reason="temporary closure (test)")
        self.assertEqual(self.state()["pending_signal"]["execution_date"], self.day(2))
        self.assertEqual(self.run_day(1)["status"], "SKIPPED_HOLIDAY")
        closed = pd.Timestamp(self.day(1))
        market = FixtureProvider(
            {t: f[f.date != closed] for t, f in self.etf.items()},
            self.index[self.index.date != closed],
        )
        results = self.run_days(21, start=2, provider=market)
        self.assertTrue(results[0]["fills"], results[0])
        # The closed day is not counted: the 20th trading day moves one later.
        decisions = [2 + j for j, r in enumerate(results) if r["decision_today"]]
        self.assertEqual(decisions, [21])


class FalseClosureTest(ShadowCase):
    def test_closing_a_day_that_traded_is_revoked(self) -> None:
        self.run_days(1)
        add_holiday(self.day(1), self.root, reason="wrong")
        result = self.run_day(2)
        self.assertEqual(result["status"], "BLOCKED_CALENDAR")
        self.assertEqual(result["revoked_closures"], [self.day(1)])
        state = self.state()
        self.assertEqual(state["extra_holidays"], [])
        self.assertEqual(state["pending_signal"]["execution_date"], self.day(1))
        self.assertEqual(state["last_processed_date"], self.day(0))
        self.assertEqual(self.run_day(2)["status"], "BLOCKED_GAP")
        fills = self.run_day(1)["fills"]
        self.assertEqual({f["date"] for f in fills}, {self.day(1)})
        self.assertTrue(runtime_status(self.root)["consistent"])


    def test_sessions_before_the_observation_start_cannot_be_closed(self) -> None:
        calendar = TradingCalendar()
        before = calendar.previous_trading_day(OBSERVATION_START)
        anchor = calendar.previous_trading_day(calendar.previous_trading_day(before))
        self.root = self.new_runtime("early-anchor", anchor_date=str(anchor))
        self.paths = RuntimePaths(self.root)
        ledger = self.paths.ledger.read_bytes()
        with self.assertRaisesRegex(ValueError, "before the observation start"):
            add_holiday(str(before), self.root, reason="wrong")
        self.assertEqual(self.paths.ledger.read_bytes(), ledger)

    def test_provider_failure_does_not_prove_a_closure(self) -> None:
        self.run_days(1)
        add_holiday(self.day(1), self.root, reason="wrong")

        class Down(FixtureProvider):
            def index_dates(self, start, end):
                raise RuntimeError("network down")

        class Partial(FixtureProvider):
            def index_dates(self, start, end):
                return set()

        for kind in (Down, Partial):
            result = self.run_day(2, kind(self.etf, self.index))
            self.assertEqual(result["status"], "BLOCKED_CALENDAR")
            self.assertIn("cannot verify", result["errors"][0])
            state = self.state()
            self.assertEqual(state["last_processed_date"], self.day(0))
            self.assertEqual(state["extra_holidays"], [self.day(1)])


class ForwardRuntimeTest(ShadowCase):
    """P1-2: TEST/FORWARD separation and observation integrity."""

    def forward(self):
        root = self.tmp / "forward"
        provider = FakeLive(self.tmp / "work", self.etf, self.index)
        initialize_runtime(
            root, initial_capital=CAPITAL, observation_start_date=str(OBSERVATION_START),
            runtime_kind="FORWARD", provider=provider,
            clock=lambda: kst(OBSERVATION_START, "10:00"),
        )
        return root, provider

    def run_forward(self, root, provider, offset, when, hhmm="18:30", **kwargs):
        return process_date(
            self.day(offset), provider, root, clock=lambda: kst(self.day(when), hhmm), **kwargs
        )

    def test_init_refuses_fixture_caller_warmup_and_retroactive_start(self) -> None:
        base = {"initial_capital": CAPITAL, "observation_start_date": str(OBSERVATION_START),
                    "runtime_kind": "FORWARD"}
        live = FakeLive(self.tmp / "work", self.etf, self.index)
        with self.assertRaisesRegex(RuntimeError, "live pykrx provider"):
            initialize_runtime(self.tmp / "a", provider=self.provider, **base)
        with self.assertRaisesRegex(RuntimeError, "own warm-up"):
            initialize_runtime(self.tmp / "b", provider=live,
                               warmup=warmup_input(self.etf, self.index), **base)
        with self.assertRaisesRegex(RuntimeError, "retroactively"):
            initialize_runtime(self.tmp / "c", provider=live,
                               clock=lambda: kst(self.day(5), "10:00"), **base)
        with self.assertRaisesRegex(RuntimeError, "not confirmed"):
            initialize_runtime(self.tmp / "d", provider=live,
                               clock=lambda: kst(self.day(-1), "12:00"), **base)

    def test_fixture_intraday_and_future_are_never_recorded(self) -> None:
        root, live = self.forward()
        ledger = RuntimePaths(root).ledger.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "live pykrx provider"):
            self.run_forward(root, self.provider, 0, 0)
        for offset, when, hhmm in ((0, 0, "11:00"), (0, 0, "17:59"), (3, 0, "18:30")):
            result = self.run_forward(root, live, offset, when, hhmm)
            self.assertEqual(result["status"], "REJECTED_NOT_CONFIRMED")
        self.assertEqual(RuntimePaths(root).ledger.read_bytes(), ledger)

    def test_forward_stale_and_recovery_are_distinct(self) -> None:
        root, live = self.forward()
        first = self.run_forward(root, live, 0, 0)
        self.assertEqual((first["status"], first["observation_type"]), ("DONE", "FORWARD"))
        # Next morning before the open is still inside the window of day 1.
        second = self.run_forward(root, live, 1, 2, "08:59")
        self.assertEqual(second["observation_type"], "FORWARD")
        late = self.run_forward(root, live, 2, 3, "09:00")
        self.assertEqual(late["status"], "BLOCKED_STALE")
        recovered = self.run_forward(root, live, 2, 5, recover=True)
        self.assertEqual(recovered["observation_type"], "RECOVERY")
        state = read_json(RuntimePaths(root).state)
        self.assertEqual(
            state["observations"],
            {self.day(0): "FORWARD", self.day(1): "FORWARD", self.day(2): "RECOVERY"},
        )
        self.assertEqual(state["status"], "DONE")
        self.assertTrue(runtime_status(root)["consistent"])
        report = read_json(RuntimePaths(root).reports / f"{self.day(2)}.json")
        self.assertEqual(report["observation_counts"], {"FORWARD": 2, "RECOVERY": 1})

    def test_slow_fetch_overrunning_the_window_is_not_forward(self) -> None:
        root, live = self.forward()
        moments = iter([kst(self.day(1), "08:58"), kst(self.day(1), "09:03"),
                        kst(self.day(1), "09:03")])
        result = process_date(self.day(0), live, root, clock=lambda: next(moments))
        self.assertEqual(result["status"], "BLOCKED_STALE")
        calls = live.calls
        again = self.run_forward(root, live, 0, 1, "10:00", recover=True)
        self.assertEqual(again["observation_type"], "RECOVERY")
        self.assertEqual(live.calls, calls)  # the first observed data is reused

    def test_window_closing_before_commit_is_not_forward(self) -> None:
        root, live = self.forward()
        paths = RuntimePaths(root)
        # Open at start, retrieval and first classification; closed at COMMIT.
        moments = iter([kst(self.day(1), "08:58"), kst(self.day(1), "08:59"),
                        kst(self.day(1), "08:59"), kst(self.day(1), "09:00")])
        result = process_date(self.day(0), live, root, clock=lambda: next(moments))
        self.assertEqual(result["status"], "BLOCKED_STALE")
        state, _ = ShadowStore(paths).load(repair=False)
        self.assertIsNone(state["last_processed_date"])
        self.assertIsNone(state["pending_signal"])
        self.assertEqual((state["observations"], state["equity_curve"]), ({}, []))
        self.assertTrue(runtime_status(root)["consistent"])
        again = self.run_forward(root, live, 0, 1, "10:00", recover=True)
        self.assertEqual(again["observation_type"], "RECOVERY")
        self.assertTrue(runtime_status(root)["consistent"])

    def test_window_closing_before_commit_with_recover_records_recovery(self) -> None:
        root, live = self.forward()
        moments = iter([kst(self.day(1), "08:58"), kst(self.day(1), "08:59"),
                        kst(self.day(1), "08:59"), kst(self.day(1), "09:00")])
        result = process_date(
            self.day(0), live, root, recover=True, clock=lambda: next(moments)
        )
        self.assertEqual(result["observation_type"], "RECOVERY")
        state = read_json(RuntimePaths(root).state)
        self.assertEqual(state["observations"], {self.day(0): "RECOVERY"})
        self.assertEqual(state["processed_dates"], [self.day(0)])
        self.assertEqual(len(state["equity_curve"]), 1)
        report = read_json(RuntimePaths(root).reports / f"{self.day(0)}.json")
        self.assertEqual(report["observation_type"], "RECOVERY")
        self.assertTrue(runtime_status(root)["consistent"])

    def test_commit_landing_after_the_deadline_is_not_forward(self) -> None:
        root, live = self.forward()
        # Every pre-COMMIT read is in time; the read after the durable COMMIT is late.
        moments = iter([kst(self.day(1), "08:58")] + [kst(self.day(1), "08:59")] * 3
                       + [kst(self.day(1), "09:01")])
        result = process_date(self.day(0), live, root, clock=lambda: next(moments))
        self.assertEqual(result["observation_type"], "RECOVERY")
        state = read_json(RuntimePaths(root).state)
        self.assertEqual(state["observations"], {self.day(0): "RECOVERY"})
        report = read_json(RuntimePaths(root).reports / f"{self.day(0)}.json")
        self.assertEqual(report["observation_counts"], {"RECOVERY": 1})
        self.assertTrue(runtime_status(root)["consistent"])

    def test_crash_before_confirmation_settles_as_recovery(self) -> None:
        root, live = self.forward()
        with (
            mock.patch.object(runner, "_confirm_observation", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.run_forward(root, live, 0, 0)
        paths = RuntimePaths(root)
        self.assertEqual(read_json(paths.state)["observations"], {self.day(0): "UNCONFIRMED"})
        status = runtime_status(root)
        self.assertFalse(status["consistent"])
        self.assertIn("unconfirmed observations", " ".join(status["problems"]))
        self.assertEqual(self.run_forward(root, live, 0, 0)["status"], "ALREADY_PROCESSED")
        self.assertEqual(read_json(paths.state)["observations"], {self.day(0): "RECOVERY"})
        report = read_json(paths.reports / f"{self.day(0)}.json")
        self.assertEqual(report["observation_type"], "RECOVERY")
        self.assertTrue(runtime_status(root)["consistent"])

    def test_unproven_closure_cannot_lengthen_the_window(self) -> None:
        root, live = self.forward()
        add_holiday(self.day(1), root, reason="not proven")
        result = self.run_forward(root, live, 0, 1, "18:30")
        self.assertEqual(result["status"], "BLOCKED_STALE")

    def test_snapshot_provenance_is_bound_to_signal_fill_and_equity(self) -> None:
        self.run_days(2)
        state = self.state()
        for date, sha in state["snapshot_hashes"].items():
            snapshot = load_snapshot(self.paths, date, sha)
            self.assertTrue(snapshot.retrieved_at)
            self.assertEqual(snapshot.source, "synthetic")
            for event in self.events():
                if event["date"] != date:
                    continue
                if event["type"] == "SIGNAL":
                    self.assertEqual(event["payload"]["signal"]["snapshot_sha256"], sha)
                if event["type"] in ("FILL", "EQUITY"):
                    self.assertEqual(event["payload"]["snapshot_sha256"], sha)
        path = self.paths.snapshots / f"{self.day(0)}.json"
        payload = read_json(path)
        payload["bars"][UNIVERSE[0]]["close"] += 1
        atomic_write_json(path, payload)
        with self.assertRaisesRegex(IntegrityError, "snapshot hash mismatch"):
            self.run_day(2)

    def test_retry_after_crash_reuses_the_pinned_snapshot_only(self) -> None:
        self.run_days(1)
        with (
            mock.patch.object(runner, "execute_day", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.run_day(1)
        pinned = load_snapshot(self.paths, self.day(1))
        def raise_open(frames, mask):
            for frame in frames.values():
                frame.loc[mask, "open"] = frame.loc[mask, "close"] * 1.005

        changed = self.broken_provider(1, raise_open)
        result = self.run_day(1, changed)
        self.assertEqual(result["status"], "DONE")
        for fill in result["fills"]:
            self.assertEqual(fill["open_price"], pinned.bars[fill["ticker"]]["open"])
        self.assertEqual(self.state()["snapshot_hashes"][self.day(1)], pinned.sha256)

    def test_unrecorded_snapshot_file_is_never_read(self) -> None:
        self.run_days(1)
        other = self.new_runtime("other")
        process_date(self.day(0), self.provider, other)
        forged = self.broken_provider(
            1, lambda f, m: f[UNIVERSE[0]].loc.__setitem__((m, "volume"), 1.0))
        process_date(self.day(1), forged, other)
        planted = (RuntimePaths(other).snapshots / f"{self.day(1)}.json").read_bytes()
        (self.paths.snapshots / f"{self.day(1)}.json").write_bytes(planted)
        self.assertEqual(self.run_day(1)["status"], "DONE")
        used = load_snapshot(self.paths, self.day(1))
        self.assertEqual(used.bars[UNIVERSE[0]]["volume"], self.volume)
        self.assertTrue(list(self.paths.snapshots.glob("*.unrecorded-*")))

    def test_rewritten_manifest_is_detected_by_genesis_binding(self) -> None:
        other = self.new_runtime("other", decision_offset_days=5)
        self.paths.manifest.write_bytes(RuntimePaths(other).manifest.read_bytes())
        with self.assertRaisesRegex(IntegrityError, "bound at genesis"):
            self.run_day(0)


class ExecutionPolicyTest(ShadowCase):
    """P1-3: baseline policy at the unit level (full parity is in the compare test)."""

    def execute(self, *, holdings, cash, ranked, risk_on, volume=1e7, carry=()):
        config = EffectiveConfig(read_json(self.paths.manifest))
        date = "2026-03-04"
        bar = {"open": 10_000.0, "high": 10_100.0, "low": 9_900.0, "close": 10_000.0,
                   "trading_value": 1e10, "nav": 10_000.0}
        volumes = volume if isinstance(volume, dict) else {}
        bars = {
            t: {**bar, "volume": float(volumes.get(t, 1e7 if volumes else volume))}
            for t in UNIVERSE
        }
        state = {
            "cash": cash,
            "holdings": holdings,
            "cost_basis": {t: 9_000.0 for t in holdings},
            "carry": list(carry),
            "pending_signal": {
                "decision_date": "2026-03-03", "execution_date": date,
                "ranked_tickers": ranked, "kospi_risk_on": risk_on,
            },
        }
        day = MarketDay(date, bars)
        result = execute_day(state=state, day=day, config=config,
                             next_trading_date="2026-03-05")
        return result, config, day, state

    def test_risk_off_with_no_candidates_sells_like_the_backtest(self) -> None:
        result, *_ = self.execute(holdings={"411060": 100}, cash=0.0, ranked=[], risk_on=False)
        self.assertTrue(result["execution"]["allow_empty_target_sell"])
        self.assertEqual([(f["ticker"], f["side"]) for f in result["fills"]], [("411060", "SELL")])
        self.assertEqual(result["patch"]["holdings"], {})
        # Tax applies to the gain on a taxable ETF.
        gain = 100 * (10_000 * 0.9999 * 0.9995 - 9_000)
        self.assertAlmostEqual(result["fills"][0]["estimated_tax"], gain * 0.154)

    def test_risk_on_with_no_candidates_keeps_holdings(self) -> None:
        result, *_ = self.execute(holdings={"069500": 10}, cash=0.0, ranked=[], risk_on=True)
        self.assertEqual(result["fills"], [])

    def test_group_forced_exit_in_risk_off(self) -> None:
        result, *_ = self.execute(
            holdings={"069500": 10, "360750": 10}, cash=0.0,
            ranked=["360750", "069500"], risk_on=False,
        )
        self.assertEqual(result["execution"]["forced_exit_tickers"], ["069500"])
        # The ineligible holding is force-sold; the proceeds buy the eligible target.
        self.assertEqual(
            [(f["ticker"], f["side"], f["reason"]) for f in result["fills"]],
            [("069500", "SELL", "ETF_RISK_GATE_EXIT"), ("360750", "BUY", "ETF_REBALANCE")],
        )

    def test_partial_sell_limits_buys_and_records_diagnostics(self) -> None:
        result, config, day, state = self.execute(
            holdings={"069500": 9_000}, cash=1_000_000.0,
            ranked=["360750", "133690"], risk_on=True,
            volume={"069500": 2_000, "360750": 100},
        )
        by_ticker = {d["ticker"]: d for d in result["diagnostics"]}
        self.assertEqual(by_ticker["069500"]["decision"], "PARTIAL_CARRY")
        self.assertEqual(by_ticker["069500"]["filled_qty"], 100)
        self.assertEqual(by_ticker["360750"]["decision"], "PARTIAL_CARRY")
        self.assertEqual(by_ticker["133690"]["decision"], "CASH_LIMITED")
        self.assertGreaterEqual(result["patch"]["cash"], 0.0)
        self.assertEqual(
            sorted(c["ticker"] for c in result["patch"]["carry"]), ["069500", "360750"]
        )
        state.update(result["patch"])
        state.update(initial_equity=CAPITAL, peak_equity=CAPITAL)
        row, _ = evaluate_equity(state=state, day=day, config=config)
        self.assertTrue(any("positions" in flag for flag in row["limit_flags"]))
        self.assertTrue(any("069500 weight" in flag for flag in row["limit_flags"]))

    def test_zero_volume_is_diagnosed_and_not_carried(self) -> None:
        result, *_ = self.execute(
            holdings={}, cash=CAPITAL, ranked=["360750", "133690"], risk_on=True, volume=0.0,
        )
        self.assertEqual({d["decision"] for d in result["diagnostics"]}, {"ZERO_VOLUME"})
        self.assertEqual(result["patch"]["carry"], [])
        self.assertEqual(result["fills"], [])

    def test_unaffordable_target_is_diagnosed(self) -> None:
        result, *_ = self.execute(
            holdings={}, cash=100.0, ranked=["360750"], risk_on=True,
        )
        self.assertEqual([d["decision"] for d in result["diagnostics"]], ["NO_ORDER"])


class DataValidationTest(ShadowCase):
    """P1-4: mandatory/finite data, series integrity and the pinned warm-up."""

    def assert_blocked(self, mutate, needle: str) -> None:
        result = self.run_day(0, self.broken_provider(0, mutate))
        self.assertEqual(result["status"], "BLOCKED_DATA", needle)
        self.assertIn(needle, " ".join(result["errors"]))
        self.assertIsNone(self.state()["pending_signal"])
        self.assertFalse((self.paths.snapshots / f"{self.day(0)}.json").exists())

    def test_invalid_day_values_block(self) -> None:
        first = UNIVERSE[0]
        for column, value, needle in (
            ("nav", float("nan"), "nav missing"),
            ("nav", 0.0, "nav missing"),
            ("trading_value", float("inf"), "trading_value missing"),
            ("volume", -1.0, "volume missing"),
            ("close", float("inf"), "close missing"),
            ("open", 1.0, "outside [low, high]"),
        ):
            def mutate(frames, mask, column=column, value=value):
                frames[first].loc[mask, column] = value
            self.assert_blocked(mutate, needle)

    def test_duplicate_rows_and_implausible_jump_block(self) -> None:
        first = UNIVERSE[0]

        def duplicate(frames, mask):
            frames[first] = pd.concat([frames[first], frames[first][mask]])

        def jump(frames, mask):
            for column in ("open", "high", "low", "close", "nav"):
                frames[first].loc[mask, column] *= 2.0

        self.assert_blocked(duplicate, "duplicate rows")
        self.assert_blocked(jump, "from the previous close")

    def test_defective_warmup_is_refused(self) -> None:
        first = UNIVERSE[0]
        end = self.dates[WARMUP - 1]

        def drop_gap(w):
            frame = w.etf_frames[first]
            w.etf_frames[first] = frame.drop(frame.index[100:140])

        def stale_end(w):
            for t, frame in w.etf_frames.items():
                w.etf_frames[t] = frame[frame.date < end]
            w.index_frame = w.index_frame[w.index_frame.date < end]

        def short(w):
            w.index_frame = w.index_frame[w.index_frame.date >= self.dates[WARMUP - 139]]

        def index_hole(w):
            w.index_frame = w.index_frame.drop(w.index_frame.index[WARMUP - 5])

        def duplicate(w):
            w.etf_frames[first] = pd.concat(
                [w.etf_frames[first], w.etf_frames[first].iloc[[3]]])

        def no_listing(w):
            del w.listing_dates[first]

        def nan_nav(w):
            w.etf_frames[first].loc[w.etf_frames[first].index[10], "nav"] = float("nan")

        for number, mutate in enumerate(
            (drop_gap, stale_end, short, index_hole, duplicate, no_listing, nan_nav)
        ):
            warmup = warmup_input(self.etf, self.index)
            mutate(warmup)
            with self.subTest(mutate.__name__), self.assertRaises(WarmupError):
                self.new_runtime(f"bad-{number}", warmup=warmup)
            self.assertFalse((self.tmp / f"bad-{number}" / "manifest.json").exists())

    def test_listing_filter_uses_the_pinned_listing_date(self) -> None:
        young = UNIVERSE[0]
        listed = self.dates[WARMUP - 30]
        etf = {t: f.copy() for t, f in self.etf.items()}
        etf[young] = etf[young][etf[young].date >= listed]
        warmup = warmup_input(etf, self.index)
        warmup.listing_dates[young] = listed.strftime("%Y%m%d")
        self.root = self.new_runtime("young", warmup=warmup)
        self.paths = RuntimePaths(self.root)
        provider = FixtureProvider(etf, self.index)
        self.assertEqual(self.run_day(0, provider)["status"], "DONE")
        signal = self.state()["pending_signal"]
        self.assertNotIn(young, signal["ranked_tickers"])

    def test_warmup_is_pinned_and_cannot_be_replaced(self) -> None:
        self.run_days(1)
        bundle = read_json(self.paths.warmup)
        bundle["etf"][UNIVERSE[0]][40]["close"] *= 1.01
        atomic_write_json(self.paths.warmup, bundle)
        with self.assertRaisesRegex(IntegrityError, "warm-up bundle hash mismatch"):
            self.run_day(1)
        self.assertFalse(runtime_status(self.root)["consistent"])
        with self.assertRaises(SystemExit):
            run_forward_shadow.main(
                ["--runtime-root", str(self.root), "--date", self.day(1),
                 "--seed-etf", "other.parquet"]
            )


class EffectiveConfigTest(ShadowCase):
    """P1-5: the live module values must equal the freeze, from initialization on."""

    def test_drift_present_at_initialization_is_refused(self) -> None:
        cases = {
            "GROUP_RISK_OVERRIDE": {"commodity"},
            "ETF_TICKER_GROUPS": {**etf_shared.ETF_TICKER_GROUPS, "069500": "commodity"},
            "ETF_DEVIATION_THRESHOLD_BY_GROUP": {"domestic_equity": 0.5},
            "ETF_DEVIATION_THRESHOLD_BY_TICKER": {},
            "TAXABLE_ETF_TICKERS": set(),
            "MIN_AVG_TRADING_VALUE": 1,
            "MIN_LISTING_DAYS": 0,
            "MAX_PREMIUM_DISCOUNT": 0.5,
            "BUY_FEE_PCT": 0.015,
            "ETF_TAXABLE_SELL_TAX_PCT": 0.0,
            "ETF_MAX_POSITIONS": 3,
        }
        for name, value in cases.items():
            with (
                self.subTest(name),
                mock.patch.object(etf_shared, name, value),
                self.assertRaisesRegex(RuntimeError, "differs from the freeze"),
            ):
                build_manifest(initial_capital=1.0, observation_start_date="2026-03-03")
        for key, value in (
            ("ETF_MOMENTUM_WEIGHT_60", "0.7"),
            ("ETF_RETURN_BASIS", "nav"),
            ("ETF_DISTRIBUTION_TAX_PCT", "0.05"),
            ("ETF_DISTRIBUTIONS_FILE", "/tmp/other.csv"),
        ):
            with (
                self.subTest(key),
                mock.patch.dict(os.environ, {key: value}),
                self.assertRaisesRegex(RuntimeError, "differs from the freeze"),
            ):
                build_manifest(initial_capital=1.0, observation_start_date="2026-03-03")

    def test_inherited_environment_is_refused_by_a_fresh_process(self) -> None:
        probe = (
            f"import sys; sys.path.insert(0, {str(ROOT)!r})\n"
            "from forward_shadow.config import effective_config_problems\n"
            "print('\\n'.join(effective_config_problems()))"
        )
        env = {**os.environ,
               "ETF_DEVIATION_THRESHOLD_BY_TICKER": "472150=0.5",
               "MIN_LISTING_DAYS": "5"}
        done = subprocess.run([sys.executable, "-c", probe], env=env, check=True,
                              capture_output=True, text=True, timeout=60)
        self.assertIn("deviation_threshold_by_ticker", done.stdout)
        self.assertIn("MIN_LISTING_DAYS", done.stdout)


class OperationsTest(ShadowCase):
    """Section 3: status verification, report recovery, notifications, credentials."""

    def tree(self) -> dict:
        return {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in self.root.rglob("*") if p.is_file()
        }

    def test_status_verifies_replay_without_writing(self) -> None:
        self.run_days(2)
        status = runtime_status(self.root)
        self.assertTrue(status["consistent"], status)
        self.assertEqual(status["state"]["next_expected_date"], self.day(2))
        self.assertEqual(status["state"]["next_decision_date"], self.day(20))
        self.paths.state.write_text(
            json.dumps({**self.state(), "cash": 1.0}), encoding="utf-8")
        ShadowStore(self.paths).load(repair=False)
        before = self.tree()
        status = runtime_status(self.root)
        self.assertFalse(status["consistent"])
        self.assertIn("state.json differs", " ".join(status["problems"]))
        self.assertEqual(self.tree(), before)
        self.assertEqual(
            run_forward_shadow.main(["--runtime-root", str(self.root), "--status"]), 1)

    def test_report_is_restored_after_crash_between_commit_and_write(self) -> None:
        sink = CapturingSink()
        with (
            mock.patch("forward_shadow.report.atomic_write_text", side_effect=OSError("disk")),
            self.assertRaises(OSError),
        ):
            self.run_day(0, sink=sink)
        self.assertEqual(self.state()["last_processed_date"], self.day(0))
        self.assertFalse((self.paths.reports / f"{self.day(0)}.txt").exists())
        self.assertFalse(runtime_status(self.root)["consistent"])
        result = self.run_day(0, sink=sink)
        self.assertEqual(result["status"], "ALREADY_PROCESSED")
        self.assertEqual(result["restored_reports"], [self.day(0)])
        self.assertEqual(len(sink.messages), 1)
        self.assertEqual(self.run_day(0, sink=sink)["restored_reports"], [])
        self.assertEqual(len(sink.messages), 1)
        self.assertTrue(runtime_status(self.root)["consistent"])

    def test_crash_between_report_files_and_send_still_delivers(self) -> None:
        class Crashing(CapturingSink):
            crash = True

            def send(self, message):
                if self.crash:
                    raise KeyboardInterrupt
                return super().send(message)

        sink = Crashing()
        with self.assertRaises(KeyboardInterrupt):
            self.run_day(0, sink=sink)
        self.assertTrue((self.paths.reports / f"{self.day(0)}.json").exists())
        sink.crash = False
        self.run_day(0, sink=sink)
        self.run_day(0, sink=sink)
        self.assertEqual(len(sink.messages), 1)

    def test_reports_written_without_a_sink_are_not_bulk_sent_later(self) -> None:
        self.run_days(3)
        sink = CapturingSink()
        self.run_day(2, sink=sink)
        self.assertEqual(sink.messages, [])

    def test_failed_send_is_retried_once(self) -> None:
        class Flaky(CapturingSink):
            fail = True

            def send(self, message):
                if self.fail:
                    return False
                return super().send(message)

        sink = Flaky()
        self.run_day(0, sink=sink)
        sink.fail = False
        self.run_day(0, sink=sink)
        self.run_day(0, sink=sink)
        self.assertEqual(len(sink.messages), 1)

    def test_blocked_report_names_last_valuation_date(self) -> None:
        self.run_days(2)
        sink = CapturingSink()
        self.run_day(5, sink=sink)
        report = read_json(self.paths.reports / f"{self.day(5)}.json")
        self.assertEqual(report["portfolio"]["valuation_date"], self.day(1))
        self.assertIn(f"마지막 정상 평가일={self.day(1)}", sink.messages[-1])
        self.assertEqual(report["rebalance"]["next_decision_date"], self.day(20))

    def test_telegram_is_opt_in_and_independent_of_the_live_channel(self) -> None:
        live_only = {"SHADOW_TELEGRAM_SEND": "1", "TELEGRAM_BOT_TOKEN": "live",
                     "TELEGRAM_CHAT_ID": "1"}
        with mock.patch.dict(os.environ, live_only):
            os.environ.pop("SHADOW_TELEGRAM_BOT_TOKEN", None)
            self.assertIsNone(resolve_sink(None))
        shadow = {"SHADOW_TELEGRAM_BOT_TOKEN": "s", "SHADOW_TELEGRAM_CHAT_ID": "2"}
        with mock.patch.dict(os.environ, {**shadow, "SHADOW_TELEGRAM_SEND": "0"}):
            self.assertIsNone(resolve_sink(None))
        calls = []
        sink = ShadowTelegramSink("secret-token", "2", post=lambda url, **kw: calls.append(
            (url, kw)) or mock.Mock())
        self.assertTrue(sink.send("x" * 5000))
        self.assertEqual(calls[0][1]["json"]["chat_id"], "2")
        self.assertEqual(len(calls[0][1]["json"]["text"]), 4096)

        def boom(url, **kw):
            raise RuntimeError(url)

        logging_target = "forward_shadow.report.logger.warning"
        with mock.patch(logging_target) as warning:
            self.assertFalse(ShadowTelegramSink("secret-token", "2", post=boom).send("x"))
        self.assertNotIn("secret-token", str(warning.call_args))

    def test_only_krx_keys_are_loaded_from_a_credential_file(self) -> None:
        path = self.tmp / "krx.env"
        path.write_text(
            "export KRX_ID=abc\nKRX_PW='p w'\nKIS_APP_SECRET=zzz\nLIVE_ORDER_ENABLED=1\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(run_forward_shadow.load_krx_credentials(path), ["KRX_ID", "KRX_PW"])
            self.assertEqual(dict(os.environ), {"KRX_ID": "abc", "KRX_PW": "p w"})

    def test_provider_output_and_errors_never_contain_credentials(self) -> None:
        class Leaky(FakeLive):
            def _index_frame(self, start, end):
                raise RuntimeError("login failed for secret-id")

        with mock.patch.dict(os.environ, {"KRX_ID": "secret-id", "KRX_PW": "secret-pw"}):
            result = self.run_day(0, Leaky(self.tmp / "w", self.etf, self.index))
        self.assertEqual(result["status"], "BLOCKED_DATA")
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"secret-id", path.read_bytes(), path.name)


if __name__ == "__main__":
    unittest.main()
