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


if __name__ == "__main__":
    unittest.main()
