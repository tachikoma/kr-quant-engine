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
        row = self._wait({"is_found": True, "order_qty": 10, "filled_qty": 10, "remaining_qty": 0, "is_filled": True})
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
        ns["_RUN_ACKED"] = []
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

    def _submit_ns2(self, tmp, hhmm_now=(9, 6)):
        ns = self._submit_ns(tmp)
        ns["_now_kst"] = lambda: dt.datetime(2026, 11, 4, *hhmm_now, tzinfo=dt.timezone.utc)
        ns["_parse_hhmm"] = lambda v: dt.time(int(v[:2]), int(v[3:]))
        return ns

    def test_submit_after_cutoff_blocked_without_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            ns = self._submit_ns2(tmp)
            called = []
            class Broker:
                def place_order(self, **kw):
                    called.append(1); return {"order_id": "1"}
            os.environ.pop("ALLOW_LATE_LIVE_ORDERS", None)
            res = ns["_submit_orders"](Broker(), "BUY", [{"ticker": "a", "qty": 1}], False, cutoff_hhmm="09:05")
            self.assertEqual(called, [])
            self.assertFalse(res[0]["submitted"])
            self.assertEqual(res[0]["error_code"], "CUTOFF_BLOCKED")
            self.assertFalse(ns["LIVE_ORDER_JOURNAL"].exists())

    def test_submit_after_cutoff_allowed_with_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            ns = self._submit_ns2(tmp)
            class Broker:
                def place_order(self, **kw):
                    return {"order_id": "1"}
            os.environ["ALLOW_LATE_LIVE_ORDERS"] = "1"
            try:
                res = ns["_submit_orders"](Broker(), "BUY", [{"ticker": "a", "qty": 1}], False, cutoff_hhmm="09:05")
            finally:
                os.environ.pop("ALLOW_LATE_LIVE_ORDERS", None)
            self.assertTrue(res[0]["submitted"])

    def test_before_cutoff_submits(self):
        with tempfile.TemporaryDirectory() as tmp:
            ns = self._submit_ns2(tmp, (9, 1))
            class Broker:
                def place_order(self, **kw):
                    return {"order_id": "1"}
            os.environ.pop("ALLOW_LATE_LIVE_ORDERS", None)
            res = ns["_submit_orders"](Broker(), "BUY", [{"ticker": "a", "qty": 1}], False, cutoff_hhmm="09:05")
            self.assertTrue(res[0]["submitted"])

    def test_kis_network_error_is_uncertain_not_rejection(self):
        ns = isolated_functions("_is_definite_rejection")
        class KisErr(Exception):
            def __init__(self, cd, st):
                self.msg_cd, self.http_status = cd, st
        f = ns["_is_definite_rejection"]
        self.assertFalse(f(KisErr("NETWORK_ERROR", 0)))
        self.assertFalse(f(KisErr("PARSE_ERROR", 200)))
        self.assertFalse(f(KisErr("X", 503)))
        self.assertTrue(f(KisErr("40250000", 200)))
        self.assertTrue(f(KisErr("EGW", 403)))
        self.assertFalse(f(TimeoutError()))

    def _refresh_ns(self):
        ns = isolated_functions("_refresh_order_statuses", "_build_retry_orders")
        ns["_now_kst"] = lambda: dt.datetime(2026, 11, 4, 9, 0, tzinfo=dt.timezone.utc)
        return ns

    def _cancelled_row(self):
        return {"ticker": "a", "submitted": True, "order_id": "1", "requested_qty": 10,
                "filled_qty": 4, "remaining_qty": 0, "cancel_submitted": True,
                "cancel_confirmed": True}

    def test_kis_partial_cancel_refresh_keeps_retry(self):
        ns = self._refresh_ns()
        row = self._cancelled_row()
        class Broker:  # KIS: filled>0 and remaining==0 -> is_filled=True even when cancelled
            def get_order_status(self, oid, today):
                return {"is_found": True, "order_qty": 10, "filled_qty": 4, "remaining_qty": 0, "is_filled": True}
        ns["_refresh_order_statuses"](Broker(), [row])
        self.assertFalse(row["is_filled"])
        self.assertEqual([r["qty"] for r in ns["_build_retry_orders"]([row])], [6])

    def test_cancel_reversed_by_refresh_blocks_retry(self):
        """취소 확정 후 재조회에서 잔량 8주가 다시 나타나면 재주문 0건 (원주문이 살아 있을 수 있다)."""
        ns = self._refresh_ns()
        row = self._cancelled_row()
        row.update(requested_qty=10, filled_qty=2)
        class Broker:
            def get_order_status(self, oid, today):
                return {"is_found": True, "order_qty": 10, "filled_qty": 2, "remaining_qty": 8, "is_filled": False}
        ns["_refresh_order_statuses"](Broker(), [row])
        self.assertIsNot(row.get("cancel_confirmed"), True)
        self.assertIn("cancel reversed", row.get("status_error", ""))
        self.assertEqual(ns["_build_retry_orders"]([row]), [])

    def test_build_retry_itself_refuses_rows_with_remaining(self):
        """재조회를 거치지 않은 행에 플래그가 남아 있어도 잔량이 있으면 재주문하지 않는다."""
        ns = isolated_functions("_build_retry_orders")
        row = {"ticker": "a", "submitted": True, "order_id": "1", "requested_qty": 10,
               "filled_qty": 2, "remaining_qty": 8, "cancel_confirmed": True}
        self.assertEqual(ns["_build_retry_orders"]([row]), [])

    def test_contradictory_quantities_block_retry(self):
        ns = self._refresh_ns()
        for filled, remaining in ((12, 0), (4, 20), (6, 6)):  # 체결>주문, 잔량>주문, 합>주문
            row = self._cancelled_row()
            class Broker:
                def get_order_status(self, oid, today, f=filled, r=remaining):
                    return {"is_found": True, "order_qty": 10, "filled_qty": f, "remaining_qty": r, "is_filled": False}
            ns["_refresh_order_statuses"](Broker(), [row])
            self.assertTrue(row.get("status_error"), (filled, remaining))  # 어느 경로로든 차단 사유가 남는다
            self.assertIsNot(row.get("cancel_confirmed"), True, (filled, remaining))
            self.assertEqual(ns["_build_retry_orders"]([row]), [], (filled, remaining))

    def test_each_contradiction_branch_is_detected_without_cancel_flag(self):
        """cancel_confirmed 가 없는 행에서 '수량 모순' 분기를 직접 검증 (취소 번복 분기에 가려지지 않게)."""
        ns = self._refresh_ns()
        for filled, remaining in ((12, 0), (4, 20), (6, 6)):  # 체결>주문, 잔량>주문, 합>주문
            row = {"ticker": "a", "submitted": True, "order_id": "1", "requested_qty": 10,
                   "filled_qty": 0, "remaining_qty": 0, "cancel_submitted": True}
            class Broker:
                def get_order_status(self, oid, today, f=filled, r=remaining):
                    return {"is_found": True, "order_qty": 10, "filled_qty": f, "remaining_qty": r, "is_filled": False}
            ns["_refresh_order_statuses"](Broker(), [row])
            self.assertIn("inconsistent quantities", row.get("status_error", ""), (filled, remaining))
            self.assertEqual(ns["_build_retry_orders"]([row]), [], (filled, remaining))

    def _side_ns(self):
        return isolated_functions("_is_side_fully_filled")

    def test_side_not_filled_when_remaining_or_error_even_if_filled_sum_matches(self):
        """매도 10주 / 체결 10주 / 잔량 1주 (is_filled=False) 와 상태 조회 오류는 '완료'가 아니다."""
        f = self._side_ns()["_is_side_fully_filled"]
        base = {"ticker": "a", "submitted": True, "order_id": "1", "requested_qty": 10,
                "filled_qty": 10, "remaining_qty": 0, "is_filled": True}
        self.assertTrue(f([dict(base)], []))
        self.assertFalse(f([dict(base, remaining_qty=1, is_filled=False)], []))
        self.assertFalse(f([dict(base, status_error="refresh failed: x")], []))
        self.assertFalse(f([dict(base)], [dict(base, order_id="2", remaining_qty=3)]))   # 재주문 쪽 잔량
        self.assertFalse(f([dict(base)], [dict(base, order_id="2", status_error="e")]))  # 재주문 쪽 오류

    def test_side_partial_fill_cancel_retry_still_counts_as_filled(self):
        f = self._side_ns()["_is_side_fully_filled"]
        primary = {"ticker": "a", "submitted": True, "order_id": "1", "requested_qty": 10,
                   "filled_qty": 4, "remaining_qty": 0, "cancel_confirmed": True}
        retry = {"ticker": "a", "submitted": True, "order_id": "2", "requested_qty": 6,
                 "filled_qty": 6, "remaining_qty": 0, "is_filled": True, "retry_from_order_id": "1"}
        self.assertTrue(f([primary], [retry]))
        self.assertFalse(f([primary], [dict(retry, filled_qty=5)]))
        self.assertFalse(f([primary], []))

    def test_side_overfill_is_rejected_not_masked(self):
        f = self._side_ns()["_is_side_fully_filled"]
        a = {"ticker": "a", "order_id": "1", "requested_qty": 10, "filled_qty": 15, "remaining_qty": 0}
        b = {"ticker": "b", "order_id": "2", "requested_qty": 10, "filled_qty": 5, "remaining_qty": 0}
        self.assertFalse(f([a, b], []))          # 한 주문 과체결 + 다른 주문 미체결
        self.assertFalse(f([a], []))             # 단독 과체결도 모순
        primary = {"ticker": "a", "order_id": "1", "requested_qty": 10, "filled_qty": 4,
                   "remaining_qty": 0, "cancel_confirmed": True}
        retry = {"ticker": "a", "order_id": "2", "requested_qty": 6, "filled_qty": 7, "remaining_qty": 0}
        self.assertFalse(f([primary], [retry]))  # 재주문 과체결 (Codex 재현: 4/10 취소 + 재주문 7/6)
        self.assertFalse(f([primary], [dict(retry, requested_qty=0, filled_qty=7)]))  # 합 초과

    def test_poll_marks_overfilled_order_inconsistent(self):
        """실제 폴링 함수가 과체결 상태를 완료로 받아들이지 않고 status_error 를 남긴다."""
        ns = self._poll_ns()
        row = {"ticker": "a", "side": "SELL", "submitted": True, "order_id": "1", "requested_qty": 6,
               "qty": 6, "filled_qty": 0, "remaining_qty": 6, "is_filled": False}
        class Broker:
            def get_order_status(self, oid, today):
                return {"is_found": True, "order_qty": 6, "filled_qty": 7, "remaining_qty": 0, "is_filled": True}
            def cancel_order(self, **kw):
                return {}
        out = ns["_poll_and_finalize_orders"](Broker(), [row], 1, 1, "09:05", False)
        self.assertFalse(out[0].get("is_filled"))
        self.assertIn("inconsistent", out[0].get("status_error", ""))
        # 판정 함수까지 연결: 4/10 취소 확정 + 재주문 7/6 과체결 → 매수 허용 안 됨
        f = isolated_functions("_is_side_fully_filled")["_is_side_fully_filled"]
        primary = {"ticker": "a", "order_id": "0", "requested_qty": 10, "filled_qty": 4,
                   "remaining_qty": 0, "cancel_confirmed": True}
        self.assertFalse(f([primary], out))

    def test_inconsistent_open_order_is_still_cancelled_at_cutoff(self):
        """수량 모순 행도 폴링/정리 대상에 남는다: 컷오프 후 취소가 전송되고 완료로 처리되지 않는다 (Codex P1 회귀)."""
        ns = self._poll_ns()
        cancels = []
        row = {"ticker": "a", "side": "SELL", "submitted": True, "order_id": "1", "requested_qty": 10,
               "qty": 10, "filled_qty": 0, "remaining_qty": 10, "is_filled": False}
        class Broker:
            def get_order_status(self, oid, today):
                return {"is_found": True, "order_qty": 10, "filled_qty": 6, "remaining_qty": 6, "is_filled": False}
            def cancel_order(self, **kw):
                cancels.append(kw); return {}
        out = ns["_poll_and_finalize_orders"](Broker(), [row], 1, 1, "09:05", True)
        self.assertEqual(len(cancels), 1)
        self.assertFalse(out[0]["is_filled"])
        self.assertTrue(out[0].get("status_error"))

    def test_transient_contradiction_recovers_on_next_poll(self):
        ns = self._poll_ns()
        seq = [{"is_found": True, "order_qty": 10, "filled_qty": 6, "remaining_qty": 6, "is_filled": False},
               {"is_found": True, "order_qty": 10, "filled_qty": 10, "remaining_qty": 0, "is_filled": True}]
        row = {"ticker": "a", "side": "SELL", "submitted": True, "order_id": "1", "requested_qty": 10,
               "qty": 10, "filled_qty": 0, "remaining_qty": 10, "is_filled": False}
        class Broker:
            def get_order_status(self, oid, today):
                return seq.pop(0) if len(seq) > 1 else seq[0]
            def cancel_order(self, **kw):
                raise AssertionError("must not cancel after recovery")
        out = ns["_poll_and_finalize_orders"](Broker(), [row], 1, 1, "09:05", True)
        self.assertTrue(out[0]["is_filled"])
        self.assertNotIn("status_error", out[0])

    def test_gate_uses_submitted_qty_not_broker_reported_order_qty(self):
        """제출 10주인데 증권사가 order_qty=6/체결 6/잔량 0 으로 보고 → 4주 미확인이므로 완료가 아니다."""
        ns = self._poll_ns()
        row = {"ticker": "a", "side": "SELL", "submitted": True, "order_id": "1", "requested_qty": 10,
               "qty": 10, "filled_qty": 0, "remaining_qty": 10, "is_filled": False}
        class Broker:
            def get_order_status(self, oid, today):
                return {"is_found": True, "order_qty": 6, "filled_qty": 6, "remaining_qty": 0, "is_filled": True}
        out = ns["_poll_and_finalize_orders"](Broker(), [row], 1, 1, "09:05", False)
        f = isolated_functions("_is_side_fully_filled")["_is_side_fully_filled"]
        self.assertFalse(f(out, []))
        # 12/12 같은 과체결 보고도 제출 10주 대비 모순
        over = dict(out[0], requested_qty=12, filled_qty=12, remaining_qty=0)
        self.assertFalse(f([over], []))

    def test_retry_chain_cannot_mask_another_orders_shortfall(self):
        f = isolated_functions("_is_side_fully_filled")["_is_side_fully_filled"]
        def prim(oid, filled):
            return {"ticker": oid, "order_id": oid, "qty": 10, "requested_qty": 10, "filled_qty": filled,
                    "remaining_qty": 0, "cancel_confirmed": True}
        def rty(parent, qty, filled):
            return {"ticker": parent, "order_id": "r" + parent, "qty": qty, "requested_qty": qty,
                    "filled_qty": filled, "remaining_qty": 0, "is_filled": True, "retry_from_order_id": parent}
        # A 4/10 + 재주문 7/7 (초과), B 9/10 (미재주문): 합 20/20 이지만 A 초과·B 부족
        self.assertFalse(f([prim("A", 4), prim("B", 9)], [rty("A", 7, 7)]))
        # 정상: A 4 + 6, B 9 + 1
        self.assertTrue(f([prim("A", 4), prim("B", 9)], [rty("A", 6, 6), rty("B", 1, 1)]))
        # 재주문 requested_qty 누락 시 qty 로 단독 과체결 검사
        self.assertFalse(f([prim("A", 4)], [{"qty": 6, "filled_qty": 7, "remaining_qty": 0,
                                              "retry_from_order_id": "A", "order_id": "rA"}]))

    def test_retry_parent_link_survives_uncertain_exit(self):
        """ID 누락(불확실)로 중단돼도 이미 접수된 재주문 행의 원주문 연결이 남는다."""
        with tempfile.TemporaryDirectory() as tmp:
            ns = self._submit_ns(tmp)
            class Broker:
                n = 0
                def place_order(self, **kw):
                    Broker.n += 1
                    return {"order_id": "R1"} if Broker.n == 1 else {}
            with self.assertRaises(RuntimeError) as cm:
                ns["_submit_orders"](Broker(), "SELL", [
                    {"ticker": "a", "qty": 6, "retry_from_order_id": "A"},
                    {"ticker": "b", "qty": 1, "retry_from_order_id": "B"}], False)
            rows = cm.exception.results
            self.assertEqual(rows[0]["order_id"], "R1")
            self.assertEqual(rows[0].get("retry_from_order_id"), "A")

    def test_submit_orders_preserves_retry_parent_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            ns = self._submit_ns(tmp)
            class Broker:
                n = 0
                def place_order(self, **kw):
                    Broker.n += 1; return {"order_id": f"R{Broker.n}"}
            res = ns["_submit_orders"](Broker(), "SELL", [
                {"ticker": "a", "qty": 6, "retry_from_order_id": "A"},
                {"ticker": "b", "qty": 1, "retry_from_order_id": "B"},
                {"ticker": "c", "qty": 2}], False)
            self.assertEqual([r.get("retry_from_order_id") for r in res], ["A", "B", None])

    def test_side_requires_explicit_fill_or_cancel_confirmation(self):
        """수량은 전량이어도 is_filled 확인이 없으면(timed_out 등) 완료가 아니다. 재주문으로 채운 원주문은 취소 확정 필요."""
        f = self._side_ns()["_is_side_fully_filled"]
        full = {"ticker": "a", "submitted": True, "order_id": "1", "qty": 10, "requested_qty": 10,
                "filled_qty": 10, "remaining_qty": 0, "is_filled": True}
        self.assertTrue(f([full], []))
        self.assertFalse(f([dict(full, is_filled=False, timed_out=True)], []))
        self.assertFalse(f([{k: v for k, v in full.items() if k != "is_filled"}], []))
        part = {"ticker": "a", "submitted": True, "order_id": "1", "qty": 10, "requested_qty": 10,
                "filled_qty": 4, "remaining_qty": 0, "cancel_confirmed": True}
        retry = {"ticker": "a", "submitted": True, "order_id": "2", "qty": 6, "requested_qty": 6,
                 "filled_qty": 6, "remaining_qty": 0, "is_filled": True, "retry_from_order_id": "1"}
        self.assertTrue(f([part], [retry]))
        self.assertFalse(f([dict(part, cancel_confirmed=False)], [retry]))          # 취소 확정 없음
        self.assertFalse(f([{k: v for k, v in part.items() if k != "cancel_confirmed"}], [retry]))
        self.assertFalse(f([part], [dict(retry, is_filled=False, timed_out=True)]))  # 재주문 체결 확인 없음

    def _gate(self):
        return isolated_functions("_is_side_fully_filled")["_is_side_fully_filled"]

    def test_timeout_cleanup_does_not_manufacture_confirmation(self):
        """증권사가 is_filled=False(order_qty=0, 체결 10/10, 잔량 0)로 답한 주문을 타임아웃 정리가 완료로 만들면 안 된다.
        원주문(매도/매수)과 재주문 모두, 폴링 → 게이트 경로 전체로 확인."""
        for side in ("SELL", "BUY"):
            ns = self._poll_ns()
            row = {"ticker": "a", "side": side, "submitted": True, "order_id": "1", "requested_qty": 10,
                   "qty": 10, "filled_qty": 0, "remaining_qty": 10, "is_filled": False}
            class Broker:
                def get_order_status(self, oid, today):
                    return {"is_found": True, "order_qty": 0, "filled_qty": 10, "remaining_qty": 0, "is_filled": False}
                def cancel_order(self, **kw):
                    return {}
            out = ns["_poll_and_finalize_orders"](Broker(), [row], 1, 1, "09:05", False)
            self.assertFalse(out[0]["is_filled"], side)
            self.assertTrue(out[0].get("timed_out"), side)
            self.assertFalse(self._gate()(out, []), side)                      # 원주문
            retry = dict(out[0], order_id="2", retry_from_order_id="0", qty=10)
            part = {"ticker": "a", "order_id": "0", "qty": 10, "requested_qty": 10, "filled_qty": 0,
                    "remaining_qty": 0, "cancel_confirmed": True}
            self.assertFalse(self._gate()([part], [retry]), side)               # 재주문

    def test_poll_confirmed_fill_then_stale_refresh_stays_confirmed(self):
        """폴링으로 10/10 확정 후, 재조회가 오래된 4/10(remaining 0, is_filled False)을 돌려줘도 확인은 유지된다."""
        poll = self._poll_ns()
        row = {"ticker": "a", "side": "SELL", "submitted": True, "order_id": "1", "requested_qty": 10,
               "qty": 10, "filled_qty": 0, "remaining_qty": 10, "is_filled": False}
        class Full:
            def get_order_status(self, oid, today):
                return {"is_found": True, "order_qty": 10, "filled_qty": 10, "remaining_qty": 0, "is_filled": True}
        out = poll["_poll_and_finalize_orders"](Full(), [row], 1, 1, "09:05", False)
        self.assertTrue(out[0]["is_filled"])
        ref = self._refresh_ns()
        class Stale:
            def get_order_status(self, oid, today):
                return {"is_found": True, "order_qty": 10, "filled_qty": 4, "remaining_qty": 0, "is_filled": False}
        ref["_refresh_order_statuses"](Stale(), out)
        self.assertTrue(out[0]["is_filled"])
        self.assertEqual(out[0]["filled_qty"], 10)
        self.assertEqual(ref["_build_retry_orders"](out), [])
        self.assertTrue(self._gate()(out, []))
        # 하지만 잔량이 다시 보이는 보고는 확인을 되돌리고 완료로 보지 않는다
        class Reopened:
            def get_order_status(self, oid, today):
                return {"is_found": True, "order_qty": 10, "filled_qty": 4, "remaining_qty": 6, "is_filled": False}
        ref["_refresh_order_statuses"](Reopened(), out)
        self.assertFalse(self._gate()(out, []))

    def test_full_quantities_without_broker_confirmation_in_cancel_wait_are_not_a_fill(self):
        ns = isolated_functions("_wait_for_cancellations")
        ns["time"] = SimpleNamespace(sleep=lambda x: None)
        ns["_now_kst"] = lambda: dt.datetime(2026, 11, 4, 9, 0, tzinfo=dt.timezone.utc)
        row = {"ticker": "a", "order_id": "1", "cancel_submitted": True, "qty": 10, "filled_qty": 0}
        class Broker:
            def get_order_status(self, oid, today):
                return {"is_found": True, "order_qty": 10, "filled_qty": 10, "remaining_qty": 0, "is_filled": False}
        ns["_wait_for_cancellations"](Broker(), [row], timeout_sec=0)
        self.assertFalse(row["is_filled"])
        self.assertIsNot(row.get("cancel_confirmed"), True)
        self.assertTrue(row.get("status_error"))

    def test_unconfirmed_fill_cannot_be_laundered_through_cancel_wait_and_refresh(self):
        """Codex 재현: 폴링에서 order_qty=0/체결 10/is_filled=False → 타임아웃 취소 → 취소 대기가 4/10 부분 취소를 보고
        → 재조회도 같은 보고. 어느 단계에서도 전량 체결 확인이 생기면 안 되고 게이트는 False 여야 한다."""
        poll = self._poll_ns()
        row = {"ticker": "a", "side": "SELL", "submitted": True, "order_id": "1", "requested_qty": 10,
               "qty": 10, "filled_qty": 0, "remaining_qty": 10, "is_filled": False}
        cancels = []
        class Poll:
            def get_order_status(self, oid, today):
                return {"is_found": True, "order_qty": 0, "filled_qty": 10, "remaining_qty": 0, "is_filled": False}
            def cancel_order(self, **kw):
                cancels.append(kw); return {}
        out = poll["_poll_and_finalize_orders"](Poll(), [row], 1, 1, "09:05", True)
        self.assertFalse(out[0]["is_filled"])
        self.assertTrue(out[0].get("cancel_submitted"))
        wait = isolated_functions("_wait_for_cancellations")
        wait["time"] = SimpleNamespace(sleep=lambda x: None)
        wait["_now_kst"] = lambda: dt.datetime(2026, 11, 4, 9, 0, tzinfo=dt.timezone.utc)
        partial = {"is_found": True, "order_qty": 10, "filled_qty": 4, "remaining_qty": 0, "is_filled": False}
        class Wait:
            def get_order_status(self, oid, today):
                return partial
        wait["_wait_for_cancellations"](Wait(), out, timeout_sec=0)
        self.assertFalse(out[0]["is_filled"])
        self.assertIsNot(out[0].get("cancel_confirmed"), True)
        ref = self._refresh_ns()
        class Ref:
            def get_order_status(self, oid, today):
                return partial
        ref["_refresh_order_statuses"](Ref(), out)
        self.assertFalse(out[0]["is_filled"])
        self.assertEqual(ref["_build_retry_orders"](out), [])
        self.assertFalse(self._gate()(out, []))

    def test_refresh_preserves_only_broker_confirmed_fills(self):
        """is_filled=True 라도 증권사 확인 출처(broker_confirmed_fill)가 없으면 오래된 보고에 의해 되돌려진다."""
        ref = self._refresh_ns()
        class Stale:
            def get_order_status(self, oid, today):
                return {"is_found": True, "order_qty": 10, "filled_qty": 4, "remaining_qty": 0, "is_filled": False}
        base = {"ticker": "a", "submitted": True, "order_id": "1", "qty": 10, "requested_qty": 10,
                "filled_qty": 10, "remaining_qty": 0, "is_filled": True}
        unconfirmed = dict(base)
        ref["_refresh_order_statuses"](Stale(), [unconfirmed])
        self.assertFalse(unconfirmed["is_filled"])
        confirmed = dict(base, broker_confirmed_fill=True)
        ref["_refresh_order_statuses"](Stale(), [confirmed])
        self.assertTrue(confirmed["is_filled"])

    def test_kis_shaped_partial_cancel_cannot_launder_unconfirmed_history_via_refresh(self):
        """Codex 재현(KIS 형태): 폴링 order_qty=0/체결 10/미확인 → 취소 대기 4/10 (is_filled=True, KIS 부분취소) →
        재조회 동일 보고. 과거 병합 수량(10)으로 전량 체결 확인이 만들어지면 안 된다. 매도/매수 모두."""
        for side in ("SELL", "BUY"):
            poll = self._poll_ns()
            row = {"ticker": "a", "side": side, "submitted": True, "order_id": "1", "requested_qty": 10,
                   "qty": 10, "filled_qty": 0, "remaining_qty": 10, "is_filled": False}
            class Poll:
                def get_order_status(self, oid, today):
                    return {"is_found": True, "order_qty": 0, "filled_qty": 10, "remaining_qty": 0, "is_filled": False}
                def cancel_order(self, **kw):
                    return {}
            out = poll["_poll_and_finalize_orders"](Poll(), [row], 1, 1, "09:05", True)
            kis_partial = {"is_found": True, "order_qty": 10, "filled_qty": 4, "remaining_qty": 0, "is_filled": True}
            wait = isolated_functions("_wait_for_cancellations")
            wait["time"] = SimpleNamespace(sleep=lambda x: None)
            wait["_now_kst"] = lambda: dt.datetime(2026, 11, 4, 9, 0, tzinfo=dt.timezone.utc)
            class Br:
                def get_order_status(self, oid, today):
                    return kis_partial
            wait["_wait_for_cancellations"](Br(), out, timeout_sec=0)
            ref = self._refresh_ns()
            ref["_refresh_order_statuses"](Br(), out)
            self.assertFalse(out[0]["is_filled"], side)
            self.assertIsNot(out[0].get("broker_confirmed_fill"), True, side)
            self.assertIn("stale/contradictory", out[0].get("status_error", ""), side)  # 사유가 남는다
            self.assertEqual(ref["_build_retry_orders"](out), [], side)
            self.assertFalse(self._gate()(out, []), side)

    def test_genuine_kis_partial_cancel_still_retries_after_refresh(self):
        """정상 KIS 부분취소(과거 값과 모순 없음)는 여전히 4주 체결 + 6주 재주문 대상."""
        ref = self._refresh_ns()
        row = {"ticker": "a", "submitted": True, "order_id": "1", "qty": 10, "requested_qty": 10,
               "filled_qty": 4, "remaining_qty": 0, "cancel_submitted": True, "cancel_confirmed": True}
        class Br:
            def get_order_status(self, oid, today):
                return {"is_found": True, "order_qty": 10, "filled_qty": 4, "remaining_qty": 0, "is_filled": True}
        ref["_refresh_order_statuses"](Br(), [row])
        self.assertFalse(row["is_filled"])
        self.assertEqual([r["qty"] for r in ref["_build_retry_orders"]([row])], [6])

    def test_buy_gate_uses_side_check_on_sell_results(self):
        """구조(AST): _run_daily_locked 의 can_buy 는 dry_run 이 아니면 _is_side_fully_filled(sell...) 결과만 따른다."""
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        fn = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_run_daily_locked"][0]
        gates = [ast.unparse(n.value) for n in ast.walk(fn)
                 if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "can_buy" for t in n.targets)]
        self.assertIn("dry_run or _is_side_fully_filled(sell_results, sell_retry_results)", gates)

    def test_refresh_failure_blocks_retry(self):
        ns = self._refresh_ns()
        for exc_or_status in (TimeoutError("x"), {"is_found": False, "remaining_qty": 0}):
            row = self._cancelled_row()
            class Broker:
                def get_order_status(self, oid, today):
                    if isinstance(exc_or_status, Exception):
                        raise exc_or_status
                    return exc_or_status
            ns["_refresh_order_statuses"](Broker(), [row])
            self.assertEqual(ns["_build_retry_orders"]([row]), [])

    def test_late_fill_seen_at_refresh_reduces_retry_qty(self):
        ns = self._refresh_ns()
        row = self._cancelled_row()
        class Broker:
            def get_order_status(self, oid, today):
                return {"is_found": True, "order_qty": 10, "filled_qty": 6, "remaining_qty": 0, "is_filled": True}
        ns["_refresh_order_statuses"](Broker(), [row])
        self.assertEqual([r["qty"] for r in ns["_build_retry_orders"]([row])], [4])
    def test_run_daily_blocks_concurrent_direct_call(self):
        import fcntl
        ns = isolated_functions("run_daily")
        with tempfile.TemporaryDirectory() as tmp:
            ns.update(fcntl=fcntl, STATE_DIR=Path(tmp), LIVE_RUN_LOCK=Path(tmp) / "l.lock")
            ran = []
            ns["_run_daily_locked"] = lambda: ran.append(1)
            fd = os.open(ns["LIVE_RUN_LOCK"], os.O_RDWR | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a+") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(SystemExit):
                    ns["run_daily"]()
            self.assertEqual(ran, [])
            ns["run_daily"]()  # 잠금 해제 후에는 정상 실행
            self.assertEqual(ran, [1])
            self.assertEqual(oct(ns["LIVE_RUN_LOCK"].stat().st_mode & 0o777), "0o600")

    def test_journal_write_failure_stops_and_keeps_prior_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            ns = self._submit_ns(tmp)
            n = []
            orig = ns["_write_live_order_intent"]
            def flaky(*a, **k):
                n.append(1)
                if len(n) == 2:
                    raise OSError("disk full")
                return orig(*a, **k)
            ns["_write_live_order_intent"] = flaky
            ns["_RUN_ACKED"] = []
            sent = []
            class Broker:
                def place_order(self, **kw):
                    sent.append(kw["ticker"]); return {"order_id": "A1"}
            with self.assertRaises(RuntimeError) as cm:
                ns["_submit_orders"](Broker(), "BUY", [{"ticker": "a", "qty": 1}, {"ticker": "b", "qty": 1}], False)
            self.assertEqual(sent, ["a"])
            self.assertEqual(len(cm.exception.results), 1)
            self.assertEqual(ns["_RUN_ACKED"][0]["order_id"], "A1")

    def _poll_ns(self):
        ns = isolated_functions("_poll_and_finalize_orders")
        t = {"n": 0}
        def now():
            t["n"] += 1
            return dt.datetime(2026, 11, 4, 9, 0, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=t["n"])
        ns.update(
            _now_kst=now, time=SimpleNamespace(sleep=lambda x: None),
            _cutoff_deadline=lambda hhmm, to: dt.datetime(2026, 11, 4, 9, 0, 30, tzinfo=dt.timezone.utc),
            _extract_exec_price=lambda r: None,
        )
        return ns

    def test_poll_does_not_treat_kis_partial_cancel_as_filled(self):
        ns = self._poll_ns()
        class Broker:
            def get_order_status(self, oid, today):
                return {"is_found": True, "order_qty": 10, "filled_qty": 4, "remaining_qty": 0, "is_filled": True}
            def cancel_order(self, **kw):
                return {}
        row = {"ticker": "a", "side": "BUY", "submitted": True, "order_id": "1",
               "requested_qty": 10, "qty": 10, "filled_qty": 0, "remaining_qty": 10, "is_filled": False}
        out = ns["_poll_and_finalize_orders"](Broker(), [row], 1, 1, "09:05", False)
        self.assertFalse(out[0]["is_filled"])
        self.assertEqual(out[0]["filled_qty"], 4)

    def test_poll_clears_transient_status_error(self):
        ns = self._poll_ns()
        calls = []
        class Broker:
            def get_order_status(self, oid, today):
                calls.append(1)
                if len(calls) == 1:
                    raise TimeoutError("x")
                return {"is_found": True, "order_qty": 5, "filled_qty": 5, "remaining_qty": 0, "is_filled": True}
        row = {"ticker": "a", "side": "BUY", "submitted": True, "order_id": "1",
               "requested_qty": 5, "qty": 5, "filled_qty": 0, "remaining_qty": 5, "is_filled": False}
        out = ns["_poll_and_finalize_orders"](Broker(), [row], 1, 1, "09:05", False)
        self.assertTrue(out[0]["is_filled"])
        self.assertNotIn("status_error", out[0])

    def test_kb_nh_order_post_not_resent_on_5xx(self):
        from live_trading.kb_adapter import KbAdapter
        from live_trading.nh_adapter import NhAdapter
        for cls, call in ((KbAdapter, lambda a: a._call("SSQM1", {}, retry_network=False)),
                          (NhAdapter, lambda a: a._call("/p", {}, retry_network=False))):
            a = object.__new__(cls)
            n = []
            class Resp:
                status_code = 503
                text = "{}"
                def json(self): return {}
            class Sess:
                def post(self, *x, **k): n.append(1); return Resp()
            for k, v in dict(_session=Sess(), http_max_retries=3, http_retry_delay=0, timeout=1,
                             base_url="http://x", ip_addr="", mac_addr="", acct_no="",
                             _throttle_lock=__import__("threading").Lock(), _last_request_ts=0.0).items():
                setattr(a, k, v)
            a._throttle_request = lambda: None
            a._headers = lambda *x, **k: {}
            try:
                call(a)
            except Exception:
                pass
            self.assertEqual(len(n), 1, cls.__name__)


class AdapterRowMatchTests(unittest.TestCase):
    """KB/NH get_order_status 는 주문번호가 일치하는 행만 사용해야 한다 (mock 응답, 네트워크 없음)."""

    ROW_OTHER = {"ord_no": "999", "ord_qty": "10", "ccld_qty": "4", "rmn_qty": "0"}
    ROW_SELF = {"ord_no": "123", "ord_qty": "10", "ccld_qty": "10", "rmn_qty": "0"}

    def _kb(self, rows):
        from live_trading.kb_adapter import KbAdapter
        a = object.__new__(KbAdapter)
        a._call = lambda api_id, body, **kw: {"dataBody": {"list": rows}}
        return a

    def _nh(self, rows):
        from live_trading.nh_adapter import NhAdapter
        a = object.__new__(NhAdapter)
        a.acct_no = "x"
        a._call = lambda path, payload, **kw: {"Output_0": rows}
        return a

    def test_single_foreign_row_is_not_found(self):
        for make in (self._kb, self._nh):
            st = make([self.ROW_OTHER]).get_order_status("123")
            self.assertFalse(st["is_found"], make.__name__)
            self.assertFalse(st["is_filled"])

    def test_matching_row_is_used(self):
        for make in (self._kb, self._nh):
            st = make([self.ROW_OTHER, self.ROW_SELF]).get_order_status("123")
            self.assertTrue(st["is_found"], make.__name__)
            self.assertEqual(st["filled_qty"], 10)

    def test_foreign_row_never_confirms_cancel(self):
        ns = isolated_functions("_wait_for_cancellations")
        ns["time"] = SimpleNamespace(sleep=lambda x: None)
        ns["_now_kst"] = lambda: dt.datetime(2026, 11, 4, 9, 0, tzinfo=dt.timezone.utc)
        for make in (self._kb, self._nh):
            row = {"ticker": "a", "order_id": "123", "cancel_submitted": True}
            ns["_wait_for_cancellations"](make([self.ROW_OTHER]), [row], timeout_sec=0)
            self.assertNotIn("cancel_confirmed", row, make.__name__)

if __name__ == "__main__":
    unittest.main()
