"""Offline safety regression tests: never contact a broker or place an order."""
import json
import tempfile
import unittest
from pathlib import Path
from live_trading.order_safety import (
    OrderIntentJournal, OrderSafetyBlocked, exclusive_runner_lock,
)


class OrderSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "intent.json"

    def test_pre_submit_marker_survives_reopen(self):
        journal = OrderIntentJournal(self.path)
        journal.before_submit(side="BUY", ticker="069500", qty=2, attempt=1)
        with self.assertRaises(OrderSafetyBlocked):
            OrderIntentJournal(self.path).assert_clear()
        self.assertEqual(json.loads(self.path.read_text())["records"][0]["state"], "UNKNOWN")

    def test_unknown_ack_cannot_clear(self):
        journal = OrderIntentJournal(self.path)
        i = journal.before_submit(side="SELL", ticker="069500", qty=3, attempt=1)
        journal.mark_unknown(i)
        self.assertFalse(journal.clear_if_verified([]))
        self.assertTrue(self.path.exists())

    def test_filled_broker_confirmed_can_clear(self):
        journal = OrderIntentJournal(self.path)
        i = journal.before_submit(side="BUY", ticker="069500", qty=3, attempt=1)
        journal.mark_accepted(i, "order-1")
        self.assertTrue(journal.clear_if_verified([{
            "order_id": "order-1", "side": "BUY", "ticker": "069500",
            "attempt": 1, "requested_qty": 3, "filled_qty": 3,
            "is_filled": True, "status_verified": True,
        }]))
        self.assertFalse(self.path.exists())

    def test_cancel_submission_not_confirmation(self):
        journal = OrderIntentJournal(self.path)
        i = journal.before_submit(side="BUY", ticker="069500", qty=3, attempt=1)
        journal.mark_accepted(i, "order-2")
        result = {
            "order_id": "order-2", "side": "BUY", "ticker": "069500",
            "attempt": 1, "requested_qty": 3, "filled_qty": 1,
            "remaining_qty": 2, "is_filled": False, "cancel_submitted": True,
        }
        self.assertFalse(journal.clear_if_verified([result]))
        result.update(cancel_confirmed=True, status_verified=True, remaining_qty=0)
        self.assertTrue(journal.clear_if_verified([result]))

    def test_wrong_order_id_does_not_clear(self):
        journal = OrderIntentJournal(self.path)
        i = journal.before_submit(side="BUY", ticker="069500", qty=2, attempt=1)
        journal.mark_accepted(i, "order-3")
        self.assertFalse(journal.clear_if_verified([{
            "order_id": "unrelated", "side": "BUY", "ticker": "069500",
            "attempt": 1, "requested_qty": 2, "filled_qty": 2, "is_filled": True,
        }]))

    def test_concurrent_runner_rejected(self):
        path = self.root / ".runner.lock"
        with exclusive_runner_lock(path):
            with self.assertRaises(OrderSafetyBlocked):
                with exclusive_runner_lock(path):
                    pass
        with exclusive_runner_lock(path):
            pass


class LiveFunctionRegressionTests(unittest.TestCase):
    """Execute the exact production helper functions without importing pykrx."""

    @staticmethod
    def function_from_source(name):
        import ast
        import datetime as dt
        import logging
        from typing import Any

        file_path = Path(__file__).resolve().parents[1] / "live_trading" / "etf_daily_runner.py"
        parsed = ast.parse(file_path.read_text(encoding="utf-8"))
        fn = next(node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == name)
        code = compile(ast.Module(body=[fn], type_ignores=[]), str(file_path), "exec")
        env = {
            "__name__": "offline_safety_test",
            "Any": Any,
            "OrderIntentJournal": OrderIntentJournal,
            "OrderSafetyBlocked": OrderSafetyBlocked,
            "logger": logging.getLogger("offline_test"),
            "dt": dt,
            "_today_kst": lambda: dt.date(2026, 11, 4),
            "_now_kst": lambda: dt.datetime(2026, 11, 4, 9, 6, tzinfo=dt.timezone(dt.timedelta(hours=9))),
            "_parse_hhmm": lambda value: dt.time.fromisoformat(value),
        }
        exec(code, env)
        return env[name]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "intent.json"

    def test_expired_cutoff_prevents_actual_api_call(self):
        submit = self.function_from_source("_submit_orders")
        class FakeBroker:
            calls = 0
            def place_order(self, **kwargs):
                self.calls += 1
                return {"order_id": "123"}
        broker = FakeBroker()
        with self.assertRaises(OrderSafetyBlocked):
            submit(broker, "BUY", [{"ticker": "069500", "qty": 2}],
                   dry_run=False, journal=OrderIntentJournal(self.path),
                   cutoff_time_hhmm="09:05")
        self.assertEqual(broker.calls, 0)
        self.assertFalse(self.path.exists())

    def test_unknown_broker_response_leaves_marker(self):
        submit = self.function_from_source("_submit_orders")
        class FakeBroker:
            def place_order(self, **kwargs):
                raise TimeoutError("broker reply lost")
        with self.assertRaises(OrderSafetyBlocked):
            submit(FakeBroker(), "BUY", [{"ticker": "069500", "qty": 2}],
                   dry_run=False, journal=OrderIntentJournal(self.path))
        self.assertEqual(json.loads(self.path.read_text())["records"][0]["state"], "UNKNOWN")

    def test_cancel_unconfirmed_never_retries(self):
        build = self.function_from_source("_build_retry_orders")
        record = {
            "ticker": "069500", "order_id": "o-1", "side": "SELL",
            "submitted": True, "requested_qty": 8, "filled_qty": 2,
            "remaining_qty": 6, "cancel_submitted": True,
        }
        self.assertEqual(build([record]), [])
        record.update(cancel_confirmed=True, status_verified=True, remaining_qty=0)
        self.assertEqual(build([record])[0]["qty"], 6)

    def test_missing_order_id_blocks_new_order(self):
        submit = self.function_from_source("_submit_orders")
        class FakeBroker:
            def place_order(self, **kwargs):
                return {"order_id": ""}
        with self.assertRaises(OrderSafetyBlocked):
            submit(FakeBroker(), "SELL", [{"ticker": "069500", "qty": 1}],
                   dry_run=False, journal=OrderIntentJournal(self.path))
        self.assertTrue(self.path.exists())


if __name__ == "__main__":
    unittest.main()
