"""Forward Shadow MVP — synthetic end-to-end regression tests.

Run with::

    uv run python -m unittest tests.test_forward_shadow -v

No broker adapter is imported and no real order is placed by these tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from forward_shadow.config import build_manifest, verify_manifest
from forward_shadow.report import CapturingSink
from forward_shadow.runner import process_date
from forward_shadow.store import (
    IntegrityError,
    ShadowStore,
    atomic_write_json,
    file_lock,
    read_json,
)
from strategy_freeze import load_frozen_strategy
from tests.shadow_fixtures import CAPITAL, ROOT, ShadowCase


class ShadowE2ETest(ShadowCase):
    def test_signal_execution_separation_and_cadence(self) -> None:
        results = self.run_days(45)
        self.assertTrue(all(r["status"] == "DONE" for r in results), results)
        decisions = [j for j, r in enumerate(results) if r["decision_today"]]
        self.assertEqual(decisions, [0, 20, 40])
        # D signal is stored, D+1 fills it: no fills on the first decision day.
        self.assertEqual(results[0]["fills"], [])
        self.assertTrue(results[1]["fills"])
        first = self.events("SIGNAL")[0]["payload"]["signal"]
        self.assertEqual(first["decision_date"], self.day(0))
        self.assertEqual(first["execution_date"], self.day(1))
        self.assertTrue(first["targets"])
        self.assertEqual(len(self.state()["processed_dates"]), 45)

    def test_costs_cash_and_equity_accounting(self) -> None:
        self.run_days(5)
        state = self.state()
        self.assertGreaterEqual(state["cash"], 0)
        self.assertLess(state["cash"], CAPITAL)
        self.assertTrue(state["holdings"])
        curve = state["equity_curve"]
        self.assertEqual(len(curve), 5)
        for row in curve:
            self.assertAlmostEqual(row["equity"], row["cash"] + row["market_value"], places=4)
            self.assertLessEqual(row["mdd"], 0.0)
        fills = [f for e in self.events("FILL") for f in e["payload"]["fills"]]
        self.assertTrue(fills)
        cash = CAPITAL
        for fill in fills:
            self.assertGreater(fill["filled_qty"], 0)
            self.assertEqual(fill["side"], "BUY")
            # open * (1 + 1bp half spread), then 5bp slippage, then 1.5bp fee.
            self.assertAlmostEqual(fill["reference_price"], fill["open_price"] * 1.0001)
            gross = fill["filled_qty"] * fill["reference_price"] * 1.0005
            self.assertAlmostEqual(fill["net_value"], gross * 1.00015, places=6)
            self.assertAlmostEqual(fill["fee"], gross * 0.00015, places=6)
            cash -= fill["net_value"]
            self.assertAlmostEqual(fill["cash_after"], cash, places=6)
            self.assertAlmostEqual(
                state["cost_basis"][fill["ticker"]], fill["net_value"] / fill["filled_qty"]
            )

    def test_idempotency_holiday_and_report(self) -> None:
        sink = CapturingSink()
        self.run_days(2, sink=sink)
        before = len(self.events())
        again = self.run_day(1, sink=sink)
        self.assertEqual(again["status"], "ALREADY_PROCESSED")
        self.assertEqual(len(self.events()), before)
        holiday = process_date("2026-07-17", self.provider, self.root, sink=sink)
        self.assertEqual(holiday["status"], "SKIPPED_HOLIDAY")
        report = read_json(self.paths.reports / f"{self.day(1)}.json")
        for key in ("baseline", "market", "rebalance", "ranking", "virtual_fills",
                    "order_diagnostics", "portfolio", "data", "integrity"):
            self.assertIn(key, report)
        self.assertEqual(report["runtime_kind"], "TEST")
        self.assertEqual(report["observation_type"], "TEST")
        text = (self.paths.reports / f"{self.day(1)}.txt").read_text(encoding="utf-8")
        self.assertIn("미확보", text)
        self.assertEqual(len(sink.messages), 2)
        self.assertTrue(sink.messages[0].startswith("[Forward Shadow/TEST]"))

    def test_ledger_integrity_and_replay_recovery(self) -> None:
        self.run_days(3)
        store = ShadowStore(self.paths)
        state, integrity = store.load()
        self.assertTrue(integrity["ok"])
        self.assertEqual(state["last_event_seq"], integrity["commit_seq"])
        # Simulate crash after COMMIT but before the atomic state write.
        self.paths.state.write_text(json.dumps({**state, "cash": -1.0}), encoding="utf-8")
        repaired, _ = ShadowStore(self.paths).load()
        self.assertEqual(repaired["cash"], state["cash"])
        self.assertEqual(self.state()["cash"], state["cash"])
        # A torn final write is uncommitted by definition and is discarded.
        with self.paths.ledger.open("a", encoding="utf-8") as handle:
            handle.write('{"seq": 99, "ty')
        self.assertEqual(self.run_day(3)["status"], "DONE")
        # Tampering with the ledger must fail closed.
        lines = self.paths.ledger.read_text(encoding="utf-8").splitlines()
        payload = json.loads(lines[1])
        payload["payload"] = {"tampered": True}
        lines[1] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        self.paths.ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(IntegrityError):
            ShadowStore(self.paths).load()

    def test_freeze_and_config_drift_fail_closed(self) -> None:
        self.run_days(1)
        manifest = read_json(self.paths.manifest)
        manifest["freeze_sha256"] = "0" * 64
        atomic_write_json(self.paths.manifest, manifest)
        with self.assertRaisesRegex(RuntimeError, "fail-closed"):
            self.run_day(1)
        manifest["freeze_sha256"] = load_frozen_strategy()["sha256"]
        manifest["effective_parameters"]["max_asset_pct"] = 0.5
        atomic_write_json(self.paths.manifest, manifest)
        self.assertTrue(any("effective_parameters" in p for p in verify_manifest(manifest)))
        with self.assertRaisesRegex(RuntimeError, "fail-closed"):
            self.run_day(1)

    def test_concurrent_execution_blocked(self) -> None:
        with (
            file_lock(self.paths.lock),
            self.assertRaisesRegex(RuntimeError, "concurrent execution blocked"),
        ):
            self.run_day(0)

    def test_no_broker_import_and_no_order_api(self) -> None:
        forbidden = (
            "live_trading.kiwoom_adapter",
            "live_trading.kis_adapter",
            "live_trading.nh_adapter",
            "live_trading.kb_adapter",
        )
        # A clean interpreter proves the shadow package itself never imports a
        # broker adapter (other tests in the suite legitimately do).
        probe = (
            f"import sys; sys.path.insert(0, {str(ROOT)!r}); import run_forward_shadow; "
            f"print([m for m in {forbidden!r} if m in sys.modules], "
            "'run_etf_backtest' in sys.modules)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe], check=False, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(completed.stdout.strip(), "[] False", completed.stderr)
        for path in [*sorted((ROOT / "forward_shadow").glob("*.py")), ROOT / "run_forward_shadow.py"]:
            text = path.read_text(encoding="utf-8")
            for needle in ("KiwoomAdapter", "KisAdapter", "NhAdapter", "KbAdapter"):
                self.assertNotIn(needle, text, f"{path.name} references {needle}")

    def test_environment_drift_fail_closed(self) -> None:
        self.run_days(1)
        with (
            mock.patch.dict(os.environ, {"ETF_MOMENTUM_WEIGHT_60": "0.70"}),
            self.assertRaisesRegex(RuntimeError, "fail-closed"),
        ):
            self.run_day(1)

    def test_manifest_anchor_tamper_detected(self) -> None:
        self.run_days(1)
        manifest = read_json(self.paths.manifest)
        manifest["rebalance_anchor"]["interval_trading_days"] = 1
        atomic_write_json(self.paths.manifest, manifest)
        self.assertTrue(any("manifest_sha256" in p for p in verify_manifest(manifest)))
        with self.assertRaises(RuntimeError):
            self.run_day(1)

    def test_dates_before_observation_rejected(self) -> None:
        self.run_days(3)
        self.assertEqual(self.run_day(0)["status"], "ALREADY_PROCESSED")
        self.assertEqual(self.run_day(-1)["status"], "BEFORE_OBSERVATION_START")

    def test_drawdown_is_contemporaneous(self) -> None:
        self.run_days(6)
        curve = self.state()["equity_curve"]
        peak, worst = CAPITAL, 0.0
        for row in curve:
            peak = max(peak, row["equity"])
            worst = min(worst, row["equity"] / peak - 1.0)
            self.assertAlmostEqual(row["mdd"], row["equity"] / peak - 1.0)
            self.assertAlmostEqual(row["max_drawdown"], worst)

    def test_manifest_records_unapproved_values(self) -> None:
        manifest = build_manifest(initial_capital=1.0, observation_start_date="2026-03-03")
        self.assertEqual(manifest["approval_status"], "UNAPPROVED_TEST_VALUES")
        self.assertEqual(manifest["rebalance_anchor"]["status"], "UNAPPROVED_TEST_VALUE")
        self.assertEqual(manifest["observation_window"]["status"], "UNAPPROVED_TEST_VALUE")
        self.assertEqual(manifest["effective_parameters"]["momentum_weight_60"], 0.55)
        self.assertEqual(manifest["effective_parameters"]["max_asset_pct"], 0.85)
        self.assertEqual(manifest["effective_parameters"]["spread_pct"], 0.0002)
        self.assertEqual(manifest["effective_parameters"]["rebalance_step_days"], 20)
        self.assertEqual(verify_manifest(manifest), [])


class LowVolumeTest(ShadowCase):
    volume = 80.0  # capacity = 4 shares per ticker per day

    def test_partial_fill_carry_and_expiry(self) -> None:
        _, second, third = self.run_days(3)
        self.assertEqual(
            {d["decision"] for d in second["diagnostics"] if d["source"] == "REBALANCE"
             and d["capacity_qty"]},
            {"PARTIAL_CARRY"},
        )
        self.assertEqual({d["decision"] for d in third["diagnostics"]}, {"PARTIAL_CANCEL"})
        self.assertEqual({d["source"] for d in third["diagnostics"]}, {"CARRY"})
        self.assertEqual(self.state()["carry"], [])


if __name__ == "__main__":
    unittest.main()
