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
               "remaining_qty": 0, "cancel_submitted": True}
        self.assertEqual(ns["_build_retry_orders"]([row]), [])
        row["cancel_confirmed"] = True  # 취소 후 API 잔량은 0 → 재주문 수량은 requested-filled
        retry = ns["_build_retry_orders"]([row])
        self.assertEqual([r["qty"] for r in retry], [8])
        row["filled_qty"] = 10
        self.assertEqual(ns["_build_retry_orders"]([row]), [])

    def test_cancel_missing_order_never_counts_as_confirmed(self):
        ns = isolated_functions("_wait_for_cancellations")
        row = {"ticker": "069500", "order_id": "id1", "cancel_submitted": True}
        ns["time"] = SimpleNamespace(sleep=lambda x: None)
        ns["_now_kst"] = lambda: dt.datetime(2026, 11, 4, 9, 0, tzinfo=dt.timezone.utc)
        calls = []
        class Broker:
            def get_order_status(self, order_id, today):
                calls.append(order_id)
                return {"is_found": False, "remaining_qty": 0}
        ns["_wait_for_cancellations"](Broker(), [row], timeout_sec=0)
        self.assertTrue(calls, "status must be queried at least once")
        self.assertNotIn("cancel_confirmed", row)

    def test_intent_blocks_restarts_until_manual_reconciliation(self):
        ns = isolated_functions("_load_live_order_journal", "_persist_live_order_journal", "_write_live_order_intent", "_assert_live_order_journal_clear")
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
        with tempfile.TemporaryDirectory() as tmp:
            ns = self._submit_ns(tmp)
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

    def _wait(self, status, timeout=0):
        ns = isolated_functions("_wait_for_cancellations")
        ns["time"] = SimpleNamespace(sleep=lambda x: None)
        ns["_now_kst"] = lambda: dt.datetime(2026, 11, 4, 9, 0, tzinfo=dt.timezone.utc)
        row = {"ticker": "069500", "order_id": "id1", "cancel_submitted": True}
        class Broker:
            def get_order_status(self, order_id, today):
                if isinstance(status, Exception):
                    raise status
                return status
        ns["_wait_for_cancellations"](Broker(), [row], timeout_sec=timeout)
        return row

    def test_cancel_status_query_failure_blocks_retry(self):
        row = self._wait(TimeoutError("x"))
        self.assertNotIn("cancel_confirmed", row)

    def test_partial_fill_then_cancel_confirmed(self):
        row = self._wait({"is_found": True, "order_qty": 10, "filled_qty": 4, "remaining_qty": 0})
        self.assertIs(row["cancel_confirmed"], True)
        self.assertEqual(row["cancelled_qty"], 6)

    def test_full_fill_is_not_cancel(self):
        row = self._wait({"is_found": True, "order_qty": 10, "filled_qty": 10, "remaining_qty": 0})
        self.assertIs(row["cancel_confirmed"], False)
        self.assertTrue(row["is_filled"])
        ns = isolated_functions("_build_retry_orders")
        row.update(submitted=True)
        self.assertEqual(ns["_build_retry_orders"]([row]), [])

    def test_still_open_is_not_confirmed(self):
        row = self._wait({"is_found": True, "order_qty": 10, "filled_qty": 4, "remaining_qty": 6})
        self.assertNotIn("cancel_confirmed", row)

    def _submit_ns(self, tmp):
        ns = isolated_functions(
            "_is_definite_rejection", "_load_live_order_journal", "_persist_live_order_journal",
            "_write_live_order_intent", "_update_live_order_intent", "_submit_orders",
            "_assert_live_order_journal_clear",
        )
        class Uncertain(RuntimeError):
            def __init__(self, m, results=None):
                super().__init__(m); self.results = results or []
        ns["OrderSubmissionUncertain"] = Uncertain
        ns["STATE_DIR"] = Path(tmp)
        ns["LIVE_ORDER_JOURNAL"] = Path(tmp) / "intents.json"
        return ns

    def test_missing_order_id_blocks_further_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            ns = self._submit_ns(tmp)
            calls = []
            class Broker:
                def place_order(self, **kw):
                    calls.append(kw); return {"order_id": ""}
            with self.assertRaises(RuntimeError):
                ns["_submit_orders"](Broker(), "BUY", [{"ticker": "a", "qty": 1}, {"ticker": "b", "qty": 1}], False)
            self.assertEqual(len(calls), 1)

    def test_definite_rejection_continues_and_marks_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ns = self._submit_ns(tmp)
            class Rej(Exception):
                msg_cd = "40250000"
            n = []
            class Broker:
                def place_order(self, **kw):
                    n.append(1)
                    if len(n) == 1:
                        raise Rej("no cash")
                    return {"order_id": "9"}
            res = ns["_submit_orders"](Broker(), "BUY", [{"ticker": "a", "qty": 1}, {"ticker": "b", "qty": 1}], False)
            self.assertEqual([r["submitted"] for r in res], [False, True])
            st = [e["status"] for e in json.loads(ns["LIVE_ORDER_JOURNAL"].read_text())]
            self.assertEqual(st, ["REJECTED", "ACKED"])
            self.assertEqual(oct(ns["LIVE_ORDER_JOURNAL"].stat().st_mode & 0o777), "0o600")

    def test_corrupt_journal_fails_closed_before_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            ns = self._submit_ns(tmp)
            ns["LIVE_ORDER_JOURNAL"].write_text("{broken", encoding="utf-8")
            called = []
            class Broker:
                def place_order(self, **kw):
                    called.append(1); return {"order_id": "1"}
            with self.assertRaises(RuntimeError):
                ns["_submit_orders"](Broker(), "BUY", [{"ticker": "a", "qty": 1}], False)
            self.assertEqual(called, [])

    def test_journal_persisted_before_broker_call_and_survives_kill(self):
        with tempfile.TemporaryDirectory() as tmp:
            ns = self._submit_ns(tmp)
            seen = []
            class Broker:
                def place_order(self, **kw):
                    seen.append(ns["LIVE_ORDER_JOURNAL"].exists())
                    raise KeyboardInterrupt  # 프로세스 강제 종료 모사
            with self.assertRaises(KeyboardInterrupt):
                ns["_submit_orders"](Broker(), "BUY", [{"ticker": "a", "qty": 1}], False)
            self.assertEqual(seen, [True])
            with self.assertRaises(RuntimeError):
                ns["_assert_live_order_journal_clear"]()

    def test_dry_run_writes_no_journal_and_no_broker_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            ns = self._submit_ns(tmp)
            res = ns["_submit_orders"](object(), "BUY", [{"ticker": "a", "qty": 3}], True)
            self.assertEqual(res[0]["mode"], "DRY_RUN")
            self.assertFalse(ns["LIVE_ORDER_JOURNAL"].exists())

    def test_normal_full_fill_acked(self):
        with tempfile.TemporaryDirectory() as tmp:
            ns = self._submit_ns(tmp)
            class Broker:
                def place_order(self, **kw):
                    return {"order_id": "7"}
            res = ns["_submit_orders"](Broker(), "SELL", [{"ticker": "a", "qty": 2}], False)
            self.assertTrue(res[0]["submitted"] and res[0]["order_id"] == "7")

    def test_flock_blocks_second_process(self):
        import fcntl, subprocess, sys
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "l.lock"
            with lock.open("a+") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                code = (
                    "import fcntl,sys\nf=open(sys.argv[1],'a+')\n"
                    "try:\n fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
                    "except BlockingIOError:\n sys.exit(7)\n"
                )
                r = subprocess.run([sys.executable, "-c", code, str(lock)])
                self.assertEqual(r.returncode, 7)

    def test_late_live_requires_explicit_opt_in(self):
        src = SOURCE.read_text(encoding="utf-8")
        i = src.index('os.environ.get("ALLOW_LATE_LIVE_ORDERS") != "1"')
        self.assertIn("return", src[i:i + 300])


if __name__ == "__main__":
    unittest.main()
