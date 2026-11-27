"""Forward Shadow MVP — synthetic and real-data E2E regression tests.

Run with::

    uv run python -m unittest tests.test_forward_shadow -v

No broker adapter is imported and no real order is placed by these tests.
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forward_shadow.config import build_manifest, verify_manifest
from forward_shadow.market import FixtureProvider
from forward_shadow.report import CapturingSink
from forward_shadow.runner import initialize_runtime, process_date
from forward_shadow.store import (
    IntegrityError,
    RuntimePaths,
    ShadowStore,
    file_lock,
    read_json,
    read_jsonl,
)
from strategy_freeze import load_frozen_strategy

UNIVERSE = [str(t) for t in load_frozen_strategy()["universe"]]


def make_synthetic(n_days: int = 200, volume: float = 10_000_000.0):
    dates = pd.bdate_range("2025-01-02", periods=n_days)
    etf: dict[str, pd.DataFrame] = {}
    for idx, ticker in enumerate(UNIVERSE):
        rows = []
        for j, day in enumerate(dates):
            close = 10_000.0 + idx * 250.0 + j * 12.0 + 300.0 * math.sin(j / 9.0 + idx)
            rows.append(
                {
                    "date": day,
                    "ticker": ticker,
                    "open": close * 0.999,
                    "high": close * 1.012,
                    "low": close * 0.988,
                    "close": close,
                    "volume": volume,
                    "trading_value": 1e10,
                    "nav": close * 1.001,
                }
            )
        etf[ticker] = pd.DataFrame(rows)
    index = pd.DataFrame(
        {"date": dates, "close": [2_000.0 + j * 3.0 for j in range(n_days)]}
    )
    return etf, index, dates


class ShadowE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="kqe-shadow-")
        self.root = Path(self.tmp.name) / "runtime"
        self.etf, self.index, self.dates = make_synthetic()
        self.warmup = 150
        self.seed_etf = pd.concat(
            [frame[frame["date"] <= self.dates[self.warmup - 1]] for frame in self.etf.values()],
            ignore_index=True,
        )
        self.seed_index = self.index[self.index["date"] <= self.dates[self.warmup - 1]]
        self.provider = FixtureProvider(self.etf, self.index, name="synthetic")
        self.sink = CapturingSink()
        initialize_runtime(
            self.root,
            initial_capital=100_000_000.0,
            observation_start_date=str(self.dates[self.warmup].date()),
            decision_offset_days=0,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_days(self, count: int) -> list[dict]:
        results = []
        for j in range(self.warmup, self.warmup + count):
            day = str(self.dates[j].date())
            results.append(
                process_date(
                    day,
                    self.provider,
                    self.root,
                    seed_etf=self.seed_etf,
                    seed_index=self.seed_index,
                    sink=self.sink,
                )
            )
        return results

    def test_01_signal_execution_separation_and_cadence(self) -> None:
        results = self.run_days(45)
        self.assertTrue(all(r["status"] == "DONE" for r in results), results)
        decisions = [j for j, r in enumerate(results) if r["decision_today"]]
        self.assertEqual(decisions[:3], [0, 20, 40])
        # D signal is stored, D+1 fills it: no fills on the first decision day.
        self.assertEqual(results[0]["fills"], [])
        self.assertTrue(len(results[1]["fills"]) > 0)
        state = read_json(RuntimePaths(self.root).state)
        self.assertEqual(state["forward_day_index"], 44)
        first_signal = next(
            r["payload"]["signal"]
            for r in read_jsonl(RuntimePaths(self.root).ledger)
            if r["type"] == "SIGNAL"
        )
        self.assertEqual(first_signal["decision_date"], str(self.dates[self.warmup].date()))
        self.assertTrue(first_signal["targets"])
        self.assertEqual(len(state["processed_dates"]), 45)

    def test_02_costs_cash_and_equity_accounting(self) -> None:
        self.run_days(5)
        state = read_json(RuntimePaths(self.root).state)
        self.assertGreater(state["cash"], 0)
        self.assertLess(state["cash"], 100_000_000.0)
        self.assertTrue(state["holdings"])
        curve = state["equity_curve"]
        self.assertEqual(len(curve), 5)
        for row in curve:
            self.assertAlmostEqual(row["equity"], row["cash"] + row["market_value"], places=4)
            self.assertLessEqual(row["mdd"], 0.0)
        fills = [f for r in read_jsonl(RuntimePaths(self.root).ledger)
                 if r["type"] == "FILL" for f in r["payload"]["fills"]]
        self.assertTrue(fills)
        for fill in fills:
            self.assertGreater(fill["filled_qty"], 0)
            self.assertGreater(fill["capacity_qty"], 0)
            self.assertIn(fill["decision"], ("FULL", "PARTIAL_CARRY", "PARTIAL_CANCEL"))

    def test_03_idempotency_holiday_and_report(self) -> None:
        self.run_days(2)
        paths = RuntimePaths(self.root)
        before = len(read_jsonl(paths.ledger))
        day = str(self.dates[self.warmup + 1].date())
        again = process_date(day, self.provider, self.root, seed_etf=self.seed_etf,
                             seed_index=self.seed_index, sink=self.sink)
        self.assertEqual(again["status"], "ALREADY_PROCESSED")
        self.assertEqual(len(read_jsonl(paths.ledger)), before)
        holiday = process_date("2026-07-17", self.provider, self.root, sink=self.sink)
        self.assertEqual(holiday["status"], "SKIPPED_HOLIDAY")
        report = read_json(paths.reports / f"{day}.json")
        for key in ("baseline", "market", "rebalance", "ranking", "virtual_fills",
                    "portfolio", "data", "integrity"):
            self.assertIn(key, report)
        self.assertIn("freeze_sha256", report["baseline"])
        self.assertIn("미확보", (paths.reports / f"{day}.txt").read_text(encoding="utf-8"))
        self.assertTrue(self.sink.messages)

    def test_04_ledger_integrity_and_replay_recovery(self) -> None:
        self.run_days(3)
        paths = RuntimePaths(self.root)
        store = ShadowStore(paths)
        state, integrity = store.load()
        self.assertTrue(integrity["ok"])
        self.assertEqual(state["last_event_seq"], integrity["last_commit_seq"])
        # Simulate crash after COMMIT but before the atomic state write.
        broken = dict(state)
        broken["cash"] = -1.0
        paths.state.write_text(json.dumps(broken), encoding="utf-8")
        repaired, _ = store.load()
        self.assertEqual(repaired["cash"], state["cash"])
        # Tampering with the ledger must fail closed.
        lines = paths.ledger.read_text(encoding="utf-8").splitlines()
        payload = json.loads(lines[1])
        payload["payload"] = {"tampered": True}
        lines[1] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        paths.ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(IntegrityError):
            ShadowStore(paths).load()

    def test_05_freeze_and_config_drift_fail_closed(self) -> None:
        self.run_days(1)
        paths = RuntimePaths(self.root)
        manifest = read_json(paths.manifest)
        manifest["freeze_sha256"] = "0" * 64
        from forward_shadow.store import atomic_write_json
        atomic_write_json(paths.manifest, manifest)
        with self.assertRaises(RuntimeError) as ctx:
            process_date(str(self.dates[self.warmup + 1].date()), self.provider, self.root)
        self.assertIn("fail-closed", str(ctx.exception))
        manifest = read_json(paths.manifest)
        manifest["freeze_sha256"] = load_frozen_strategy()["sha256"]
        manifest["effective_parameters"]["max_asset_pct"] = 0.5
        atomic_write_json(paths.manifest, manifest)
        problems = verify_manifest(manifest)
        self.assertTrue(any("effective_parameters" in p for p in problems))
        with self.assertRaises(RuntimeError) as ctx:
            process_date(str(self.dates[self.warmup + 1].date()), self.provider, self.root)
        self.assertIn("fail-closed", str(ctx.exception))

    def test_06_concurrent_execution_blocked(self) -> None:
        paths = RuntimePaths(self.root)
        with (
            file_lock(paths.lock),
            self.assertRaises(RuntimeError) as ctx,
            file_lock(paths.lock),
        ):
            pass
        self.assertIn("concurrent execution blocked", str(ctx.exception))

    def test_07_no_broker_import_and_no_order_api(self) -> None:
        import subprocess

        self.run_days(2)
        forbidden = (
            "live_trading.kiwoom_adapter",
            "live_trading.kis_adapter",
            "live_trading.nh_adapter",
            "live_trading.kb_adapter",
        )
        # A clean interpreter proves the shadow package itself never imports a
        # broker adapter (other tests in the suite legitimately do).
        probe = (
            f"import sys; sys.path.insert(0, {str(ROOT)!r}); import forward_shadow.runner; "
            f"print([m for m in {forbidden!r} if m in sys.modules])"
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe], check=False, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(completed.stdout.strip(), "[]", completed.stderr)
        for path in sorted((ROOT / "forward_shadow").glob("*.py")) + [ROOT / "run_forward_shadow.py"]:
            text = path.read_text(encoding="utf-8")
            for needle in ("KiwoomAdapter", "KisAdapter", "NhAdapter", "KbAdapter"):
                self.assertNotIn(needle, text, f"{path.name} references {needle}")

    def test_08_missing_data_holds_signal_and_execution(self) -> None:
        self.run_days(1)
        paths = RuntimePaths(self.root)
        before = read_json(paths.state)
        bad_day = str(self.dates[self.warmup + 1].date())
        broken = {t: frame.copy() for t, frame in self.etf.items()}
        broken[UNIVERSE[0]] = broken[UNIVERSE[0]][
            pd.to_datetime(broken[UNIVERSE[0]]["date"]) != pd.Timestamp(bad_day)
        ]
        provider = FixtureProvider(broken, self.index, name="broken")
        result = process_date(bad_day, provider, self.root, seed_etf=self.seed_etf,
                              seed_index=self.seed_index, sink=self.sink)
        self.assertEqual(result["status"], "BLOCKED_DATA")
        after = read_json(paths.state)
        self.assertEqual(after["cash"], before["cash"])
        self.assertEqual(after["holdings"], before["holdings"])
        self.assertEqual(after["pending_signal"], before["pending_signal"])
        self.assertEqual(after["forward_day_index"], before["forward_day_index"])

    def test_09_partial_fill_carry_and_expiry(self) -> None:
        etf, index, dates = make_synthetic(volume=80.0)
        tmp = tempfile.TemporaryDirectory(prefix="kqe-shadow-carry-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "runtime"
        seed_etf = pd.concat(
            [f[f["date"] <= dates[self.warmup - 1]] for f in etf.values()], ignore_index=True
        )
        seed_index = index[index["date"] <= dates[self.warmup - 1]]
        initialize_runtime(root, initial_capital=100_000_000.0,
                           observation_start_date=str(dates[self.warmup].date()))
        provider = FixtureProvider(etf, index, name="low-volume")
        first = process_date(str(dates[self.warmup].date()), provider, root,
                             seed_etf=seed_etf, seed_index=seed_index)
        second = process_date(str(dates[self.warmup + 1].date()), provider, root,
                              seed_etf=seed_etf, seed_index=seed_index)
        third = process_date(str(dates[self.warmup + 2].date()), provider, root,
                             seed_etf=seed_etf, seed_index=seed_index)
        self.assertEqual(first["status"], "DONE")
        self.assertEqual(second["status"], "DONE")
        self.assertEqual(third["status"], "DONE")
        self.assertTrue(any(f["decision"] == "PARTIAL_CARRY" for f in second["fills"]))
        self.assertTrue(any(f["decision"] == "PARTIAL_CANCEL" for f in third["fills"]))
        state = read_json(RuntimePaths(root).state)
        self.assertEqual(state["carry"], [])

    def test_11_environment_drift_fail_closed(self) -> None:
        self.run_days(1)
        with (
            mock.patch.dict(os.environ, {"ETF_MOMENTUM_WEIGHT_60": "0.70"}),
            self.assertRaises(RuntimeError) as ctx,
        ):
            process_date(str(self.dates[self.warmup + 1].date()), self.provider, self.root)
        self.assertIn("fail-closed", str(ctx.exception))

    def test_12_manifest_anchor_tamper_detected(self) -> None:
        self.run_days(1)
        paths = RuntimePaths(self.root)
        manifest = read_json(paths.manifest)
        manifest["rebalance_anchor"]["interval_trading_days"] = 1
        from forward_shadow.store import atomic_write_json
        atomic_write_json(paths.manifest, manifest)
        self.assertTrue(any("manifest_sha256" in p for p in verify_manifest(manifest)))
        with self.assertRaises(RuntimeError):
            process_date(str(self.dates[self.warmup + 1].date()), self.provider, self.root)

    def test_13_out_of_sequence_date_rejected(self) -> None:
        self.run_days(3)
        earlier = str(self.dates[self.warmup].date())
        result = process_date(earlier, self.provider, self.root)
        self.assertEqual(result["status"], "ALREADY_PROCESSED")
        unprocessed_earlier = str(self.dates[self.warmup - 1].date())
        result = process_date(unprocessed_earlier, self.provider, self.root)
        self.assertEqual(result["status"], "BEFORE_OBSERVATION_START")

    def test_14_mdd_is_contemporaneous_and_report_text_sent(self) -> None:
        self.run_days(6)
        curve = read_json(RuntimePaths(self.root).state)["equity_curve"]
        running_peak = float(curve[0]["equity"])
        for row in curve:
            running_peak = max(running_peak, float(row["equity"]))
            self.assertAlmostEqual(row["mdd"], float(row["equity"]) / running_peak - 1.0)
        self.assertTrue(self.sink.messages)
        self.assertTrue(self.sink.messages[0].startswith("[Forward Shadow]"))

    def test_10_manifest_records_unapproved_values(self) -> None:
        manifest = build_manifest(initial_capital=1.0, observation_start_date="2026-01-01")
        self.assertEqual(manifest["approval_status"], "UNAPPROVED_TEST_VALUES")
        self.assertEqual(manifest["rebalance_anchor"]["status"], "UNAPPROVED_TEST_VALUE")
        self.assertEqual(manifest["effective_parameters"]["momentum_weight_60"], 0.55)
        self.assertEqual(manifest["effective_parameters"]["max_asset_pct"], 0.85)
        self.assertEqual(manifest["effective_parameters"]["spread_pct"], 0.0002)
        self.assertEqual(manifest["effective_parameters"]["rebalance_step_days"], 20)
        self.assertEqual(verify_manifest(manifest), [])


if __name__ == "__main__":
    unittest.main()
