"""Focused, side-effect-free tests for LIVE safety guards.

Extract functions through AST: importing the full runner may authenticate to KRX.
No broker/API calls, orders, or runtime_state files in the project.
"""
import ast
import datetime as dt
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "live_trading" / "etf_daily_runner.py"


def isolated_functions(*names):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(functions) == len(names), f"Functions missing: {names}"
    ns = {
        "Any": object, "dt": dt, "json": json, "os": os, "logger": logging.getLogger(__name__),
        "_now_kst": lambda: dt.datetime(2026, 11, 4, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=9))),
        "_today_kst": lambda: dt.date(2026, 11, 4),
        "_date_to_iso": lambda x: x.isoformat(),
        "_date_to_krx": lambda x: x.strftime("%Y%m%d"),
    }
    compiled = compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), "exec")
    exec(compiled, ns)
    return ns


class LiveOrderSafetyTests(unittest.TestCase):
    def test_retry_requires_confirmed_cancel(self):
        ns = isolated_functions("_build_retry_orders")
        row = {"ticker": "069500", "side": "SELL", "submitted": True,
               "order_id": "id1", "requested_qty": 10, "filled_qty": 2,
               "remaining_qty": 8, "cancel_submitted": True}
        self.assertEqual(ns["_build_retry_orders"]([row]), [])
        row["cancel_confirmed"] = True
        retry = ns["_build_retry_orders"]([row])
        self.assertEqual(len(retry), 1)
        self.assertEqual(retry[0]["qty"], 8)
        row["remaining_qty"] = 0
        self.assertEqual(ns["_build_retry_orders"]([row]), [])

    def test_cancel_missing_order_never_counts_as_confirmed(self):
        ns = isolated_functions("_wait_for_cancellations")
        row = {"ticker": "069500", "order_id": "id1", "cancel_submitted": True}
        ns["_cutoff_unused"] = None
        class Broker:
            def get_order_status(self, order_id, today):
                return {"is_found": False, "remaining_qty": 0}
        ns["_wait_for_cancellations"](Broker(), [row], timeout_sec=0)
        self.assertNotIn("cancel_confirmed", row)

    def test_intent_blocks_restarts_until_manual_reconciliation(self):
        ns = isolated_functions("_write_live_order_intent", "_assert_live_order_journal_clear")
        with tempfile.TemporaryDirectory() as tmp:
            ns["STATE_DIR"] = Path(tmp)
            ns["LIVE_ORDER_JOURNAL"] = Path(tmp) / "intents.json"
            ns["_write_live_order_intent"]("BUY", "069500", 2, 1)
            saved = json.loads(ns["LIVE_ORDER_JOURNAL"].read_text(encoding="utf-8"))
            self.assertEqual(saved[0]["qty"], 2)
            with self.assertRaises(RuntimeError):
                ns["_assert_live_order_journal_clear"]()
            ns["LIVE_ORDER_JOURNAL"].unlink()
            ns["_assert_live_order_journal_clear"]()

    def test_submit_failure_stops_following_orders(self):
        ns = isolated_functions("_write_live_order_intent", "_submit_orders")
        with tempfile.TemporaryDirectory() as tmp:
            ns["STATE_DIR"] = Path(tmp)
            ns["LIVE_ORDER_JOURNAL"] = Path(tmp) / "intents.json"
            called = []
            class Broker:
                def place_order(self, **kwargs):
                    called.append(kwargs)
                    raise TimeoutError("unknown broker acknowledgement")
            orders = [{"ticker": "069500", "qty": 1}, {"ticker": "091160", "qty": 1}]
            with self.assertRaises(RuntimeError):
                ns["_submit_orders"](Broker(), "BUY", orders, False)
            self.assertEqual(len(called), 1)
            self.assertTrue(ns["LIVE_ORDER_JOURNAL"].exists())


if __name__ == "__main__":
    unittest.main()
