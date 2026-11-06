"""NH 어댑터 주문 상태 조회/취소 요청 회귀 테스트 (네트워크 호출 없음).

배경: 모의서버 실응답(2026-11-06)은 itg_orr_no/orr_qty/tot_cns_qty/ny_cns_qty/can_qty 필드를
쓰는데 어댑터는 ord_no/ord_qty/ccld_qty를 찾아 체결된 주문도 is_found=False로 돌려줬다.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live_trading import nh_adapter  # noqa: E402


class _Stub(nh_adapter.NhAdapter):
    """__init__(토큰/네트워크)을 건너뛰고 _call만 가짜로 둔다."""

    def __init__(self, response):
        self.acct_no = "00000000000"
        self._response = response
        self.calls = []
        self.register("3014", itg="3014")

    def register(self, order_id, itg="3014", mkt="3014", ticker="069500", side="BUY"):
        self._order_registry()[str(order_id)] = {
            "mkt": mkt, "itg": itg, "ticker": ticker, "side": side,
        }

    def _call(self, path, input_0, base_url=None, retry_network=True):
        self.calls.append((path, input_0))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    def _check_live_enabled(self):
        return None


def _row(**kw):
    base = {
        "itg_orr_no": 3014, "iem_cd": "069500", "sby_dit_cd_nm": "현금매수",
        "orr_qty": 1, "tot_cns_qty": 1,
        "ny_cns_qty": 0, "can_qty": 0, "orr_rjt_rsn_cd_nm": "",
    }
    base.update(kw)
    return base


def _resp(*rows):
    return {"Output_0": list(rows), "rsp_cd": "00000", "rsp_msg": "ok"}


class OrderStatusTests(unittest.TestCase):
    def test_real_response_shape_full_fill(self):
        st = _Stub(_resp(_row())).get_order_status("3014", today="20261106")
        self.assertTrue(st["is_found"])
        self.assertEqual((st["order_qty"], st["filled_qty"], st["remaining_qty"]), (1, 1, 0))
        self.assertTrue(st["is_filled"])

    def test_id_normalisation_only_absorbs_zero_padding_and_type(self):
        for oid in ("3014", " 3014 ", "0000003014", 3014):
            st = _Stub(_resp(_row())).get_order_status(oid)
            self.assertTrue(st["is_found"], oid)

    def test_other_order_is_never_substituted(self):
        st = _Stub(_resp(_row(itg_orr_no=3015))).get_order_status("3014")
        self.assertFalse(st["is_found"])
        self.assertFalse(st["is_filled"])
        self.assertEqual(st["order_qty"], 0)

    def test_row_without_order_number_never_matches(self):
        row = _row()
        del row["itg_orr_no"]
        stub = _Stub(_resp(row))  # 3014 는 등록돼 있으므로 행 검사까지 실제로 도달한다
        st = stub.get_order_status("3014")
        self.assertEqual(len(stub.calls), 1)
        self.assertFalse(st["is_found"])

    def test_open_order(self):
        st = _Stub(_resp(_row(orr_qty=2, tot_cns_qty=0, ny_cns_qty=2))).get_order_status("3014")
        self.assertTrue(st["is_found"])
        self.assertEqual((st["filled_qty"], st["remaining_qty"]), (0, 2))
        self.assertFalse(st["is_filled"])

    def test_partial_fill_is_not_filled(self):
        st = _Stub(_resp(_row(orr_qty=2, tot_cns_qty=1, ny_cns_qty=1))).get_order_status("3014")
        self.assertEqual((st["filled_qty"], st["remaining_qty"]), (1, 1))
        self.assertFalse(st["is_filled"])

    def test_cancelled_order_reports_zero_remaining_and_partial_fill(self):
        st = _Stub(_resp(_row(orr_qty=2, tot_cns_qty=1, ny_cns_qty=0, can_qty=1))).get_order_status("3014")
        self.assertEqual((st["order_qty"], st["filled_qty"], st["remaining_qty"]), (2, 1, 0))
        self.assertFalse(st["is_filled"])
        self.assertEqual(st["cancelled_qty"], 1)

    def test_missing_remaining_field_is_derived(self):
        row = _row(orr_qty=3, tot_cns_qty=1, can_qty=0)
        del row["ny_cns_qty"]
        st = _Stub(_resp(row)).get_order_status("3014")
        self.assertEqual(st["remaining_qty"], 2)
        self.assertFalse(st["is_filled"])

    def test_reject_reason_is_exposed(self):
        st = _Stub(_resp(_row(tot_cns_qty=0, ny_cns_qty=0, orr_rjt_rsn_cd_nm="잔고부족"))).get_order_status("3014")
        self.assertEqual(st["reject_reason"], "잔고부족")
        self.assertFalse(st["is_filled"])

    def test_call_error_means_not_found(self):
        st = _Stub(RuntimeError("boom")).get_order_status("3014")
        self.assertFalse(st["is_found"])
        self.assertFalse(st["is_filled"])

    def test_request_uses_documented_fields(self):
        stub = _Stub(_resp(_row()))
        stub.get_order_status("3014", today="20261106")
        path, body = stub.calls[0]
        self.assertEqual(path, "/krstock/inquiry/v1/dailyOrderExecution")
        self.assertEqual(body, {"act_no": "00000000000", "orr_dt": "20261106"})


class IdentityTests(unittest.TestCase):
    """번호 체계가 검증되지 않았으므로 확인할 수 없으면 '미확정'이어야 한다 (Codex P1-1)."""

    def test_unregistered_order_is_unresolved_without_querying(self):
        stub = _Stub(_resp(_row()))
        st = stub.get_order_status("9999")
        self.assertFalse(st["is_found"])
        self.assertEqual(stub.calls, [])

    def test_order_without_itg_number_is_unresolved(self):
        stub = _Stub(_resp(_row()))
        stub.register("55", itg="", mkt="55")
        st = stub.get_order_status("55")
        self.assertFalse(st["is_found"])
        self.assertEqual(stub.calls, [])

    def test_mkt_number_colliding_with_other_orders_itg_is_not_confirmed(self):
        # A: mkt=3014, itg=4014 (아직 미체결). B: 다른 ETF가 itg=3014로 체결.
        a_open = _row(itg_orr_no=4014, orr_qty=1, tot_cns_qty=0, ny_cns_qty=1)
        b_filled = _row(itg_orr_no=3014, iem_cd="229200")
        stub = _Stub(_resp(b_filled, a_open))
        stub.register("3014", itg="4014", mkt="3014")
        st = stub.get_order_status("3014")
        self.assertTrue(st["is_found"])
        self.assertFalse(st["is_filled"])
        self.assertEqual(st["remaining_qty"], 1)

    def test_same_number_but_other_ticker_or_side_is_unresolved(self):
        for kw in ({"iem_cd": "229200"}, {"sby_dit_cd_nm": "현금매도"}, {"sby_dit_cd_nm": ""}):
            st = _Stub(_resp(_row(**kw))).get_order_status("3014")
            self.assertFalse(st["is_found"], kw)
            self.assertFalse(st["is_filled"], kw)

    def test_cancel_uses_registered_market_order_number(self):
        stub = _Stub({"Output_0": {}, "rsp_cd": "00000"})
        stub.register("3014", itg="4014", mkt="777")
        stub.cancel_order("3014", "069500")
        self.assertEqual(stub.calls[0][1]["org_mkt_orr_no"], 777)

    def test_place_order_registers_both_numbers(self):
        stub = _Stub({"Output_0": {"mkt_orr_no": "777", "itg_orr_no": "4014"}, "rsp_cd": "00000"})
        os.environ["LIVE_ORDER_ENABLED"] = "1"
        try:
            out = stub.place_order("BUY", "069500", 1, None, "MARKET")
        finally:
            os.environ.pop("LIVE_ORDER_ENABLED", None)
        self.assertEqual(out["order_id"], "777")
        self.assertEqual(
            stub._order_registry()["777"],
            {"mkt": "777", "itg": "4014", "ticker": "069500", "side": "BUY"},
        )


class RegistryCollisionTests(unittest.TestCase):
    """번호가 겹치면 먼저 낸 주문의 신원을 덮어쓰지 않는다 (Codex P1)."""

    def _place(self, stub, resp, ticker="069500", side="SELL"):
        stub._response = resp
        os.environ["LIVE_ORDER_ENABLED"] = "1"
        try:
            return stub.place_order(side, ticker, 1, None, "MARKET")
        finally:
            os.environ.pop("LIVE_ORDER_ENABLED", None)

    def test_normalised_duplicate_order_numbers_make_both_unresolved(self):
        stub = _Stub({})
        stub._order_registry().clear()
        self._place(stub, {"Output_0": {"ord_no": "7", "mkt_orr_no": "101", "itg_orr_no": "201"}})
        self._place(stub, {"Output_0": {"ord_no": "007", "mkt_orr_no": "102", "itg_orr_no": "202"}},
                    ticker="229200")
        # A 는 미체결, B 는 체결 — 어느 쪽 조회도 B 의 체결로 확정되면 안 된다.
        stub._response = _resp(
            _row(itg_orr_no=201, iem_cd="069500", sby_dit_cd_nm="현금매도", orr_qty=1,
                 tot_cns_qty=0, ny_cns_qty=1),
            _row(itg_orr_no=202, iem_cd="229200", sby_dit_cd_nm="현금매도"),
        )
        for oid in ("7", "007"):
            st = stub.get_order_status(oid)
            self.assertFalse(st["is_found"], oid)
            self.assertFalse(st["is_filled"], oid)
            with self.assertRaises(RuntimeError):
                stub.cancel_order(oid, "069500")

    def test_exact_reused_order_number_is_a_conflict(self):
        stub = _Stub({})
        stub._order_registry().clear()
        r = {"Output_0": {"mkt_orr_no": "55", "itg_orr_no": "66"}}
        self._place(stub, r)
        # 첫 주문의 체결 행이 실제로 있는 상태에서 같은 번호가 다시 나온다.
        stub._response = _resp(_row(itg_orr_no=66, sby_dit_cd_nm="현금매도"))
        self._place(stub, r)
        stub._response = _resp(_row(itg_orr_no=66, sby_dit_cd_nm="현금매도"))
        n = len(stub.calls)
        self.assertFalse(stub.get_order_status("55")["is_found"])
        self.assertEqual(len(stub.calls), n)  # 충돌 주문은 조회 요청도 보내지 않는다
        with self.assertRaises(RuntimeError):
            stub.cancel_order("55", "069500")
        self.assertEqual(len(stub.calls), n)

    def test_numbers_of_an_unstored_reused_order_are_still_poisoned(self):
        stub = _Stub({})
        stub._order_registry().clear()
        self._place(stub, {"Output_0": {"mkt_orr_no": "55", "itg_orr_no": "66"}})
        # 같은 주문번호 재사용: 두 번째 주문은 저장되지 않지만 그 번호(67)는 쓸 수 없어야 한다.
        self._place(stub, {"Output_0": {"mkt_orr_no": "55", "itg_orr_no": "67"}})
        self._place(stub, {"Output_0": {"mkt_orr_no": "56", "itg_orr_no": "67"}})
        stub._response = _resp(_row(itg_orr_no=67, sby_dit_cd_nm="현금매도"))
        self.assertFalse(stub.get_order_status("56")["is_found"])
        with self.assertRaises(RuntimeError):
            stub.cancel_order("56", "069500")

    def test_poisoned_inquiry_number_cannot_be_reused_by_a_later_order(self):
        stub = _Stub({})
        stub._order_registry().clear()
        self._place(stub, {"Output_0": {"mkt_orr_no": "10", "itg_orr_no": "99"}})
        self._place(stub, {"Output_0": {"mkt_orr_no": "11", "itg_orr_no": "099"}})
        self._place(stub, {"Output_0": {"mkt_orr_no": "12", "itg_orr_no": "99"}})
        stub._response = _resp(_row(itg_orr_no=99, sby_dit_cd_nm="현금매도"))
        for oid in ("10", "11", "12"):
            self.assertFalse(stub.get_order_status(oid)["is_found"], oid)
            with self.assertRaises(RuntimeError):
                stub.cancel_order(oid, "069500")

    def test_market_number_collision_blocks_cancel_of_both(self):
        stub = _Stub({})
        stub._order_registry().clear()
        self._place(stub, {"Output_0": {"ord_no": "1", "mkt_orr_no": "007", "itg_orr_no": "21"}})
        self._place(stub, {"Output_0": {"ord_no": "2", "mkt_orr_no": "7", "itg_orr_no": "22"}})
        n = len(stub.calls)
        for oid in ("1", "2"):
            with self.assertRaises(RuntimeError):
                stub.cancel_order(oid, "069500")
        self.assertEqual(len(stub.calls), n)  # org_mkt_orr_no=7 이 한 번도 나가지 않는다

    def test_poisoned_market_number_cannot_be_reused_either(self):
        stub = _Stub({})
        stub._order_registry().clear()
        self._place(stub, {"Output_0": {"ord_no": "1", "mkt_orr_no": "7", "itg_orr_no": "21"}})
        self._place(stub, {"Output_0": {"ord_no": "2", "mkt_orr_no": "07", "itg_orr_no": "22"}})
        self._place(stub, {"Output_0": {"ord_no": "3", "mkt_orr_no": "7", "itg_orr_no": "23"}})
        with self.assertRaises(RuntimeError):
            stub.cancel_order("3", "069500")

    def test_two_orders_sharing_one_inquiry_number_are_both_unresolved(self):
        stub = _Stub({})
        stub._order_registry().clear()
        self._place(stub, {"Output_0": {"mkt_orr_no": "10", "itg_orr_no": "99"}})
        self._place(stub, {"Output_0": {"mkt_orr_no": "11", "itg_orr_no": "099"}})
        stub._response = _resp(_row(itg_orr_no=99, sby_dit_cd_nm="현금매도"))
        for oid in ("10", "11"):
            self.assertFalse(stub.get_order_status(oid)["is_found"], oid)

    def test_distinct_orders_stay_independent(self):
        stub = _Stub({})
        stub._order_registry().clear()
        self._place(stub, {"Output_0": {"mkt_orr_no": "10", "itg_orr_no": "98"}})
        self._place(stub, {"Output_0": {"mkt_orr_no": "11", "itg_orr_no": "99"}}, ticker="229200")
        stub._response = _resp(
            _row(itg_orr_no=98, sby_dit_cd_nm="현금매도"),
            _row(itg_orr_no=99, iem_cd="229200", sby_dit_cd_nm="현금매도", tot_cns_qty=0,
                 ny_cns_qty=1),
        )
        self.assertTrue(stub.get_order_status("10")["is_filled"])
        self.assertFalse(stub.get_order_status("11")["is_filled"])


class CanonicalNumberTests(unittest.TestCase):
    """충돌 검사와 취소/조회가 같은 정식 표기를 쓴다 (Codex P1/P2)."""

    def test_canon(self):
        c = nh_adapter._canon_no
        for good, want in (("7", "7"), (" 007 ", "7"), (7, "7"), ("0", "0"), ("000", "0"), (0, "0")):
            self.assertEqual(c(good), want, good)
        for bad in ("+7", "-7", "７", "٧", "0_7", "7.0", "1e2", "", " ", "  ", None, True, 7.0,
                    -1, "1" * 19, "abc", [], {}, 10**18, "1" * 19):
            self.assertEqual(c(bad), "", bad)
        self.assertEqual(c(10**17), str(10**17))
        self.assertEqual(c("1" * 18), "1" * 18)
        self.assertEqual(c(int("1" * 18)), "1" * 18)

    def test_invalid_key_still_burns_and_conflicts_secondary_numbers(self):
        first_ids = (("1", "+2"), ("+2", "1"))
        for a_id, b_id in first_ids:
            stub = _Stub({})
            stub._order_registry().clear()
            self._place(stub, {"ord_no": a_id, "mkt_orr_no": "7", "itg_orr_no": "21"})
            self._place(stub, {"ord_no": b_id, "mkt_orr_no": "7", "itg_orr_no": "21"})
            reg = stub._order_registry()
            for rec in reg.values():
                self.assertEqual(rec.get("conflict"), "1", (a_id, b_id))
            self.assertIn("21", stub._burned_numbers()["itg"])
            self.assertIn("7", stub._burned_numbers()["mkt"])
            n = len(stub.calls)
            for oid in ("1", "+2"):
                with self.assertRaises(RuntimeError):
                    stub.cancel_order(oid, "069500")
                self.assertFalse(stub.get_order_status(oid)["is_found"])

    def test_empty_primary_id_still_burns_secondary_numbers(self):
        stub = _Stub({})
        stub._order_registry().clear()
        self._place(stub, {"ord_no": "1", "mkt_orr_no": "7", "itg_orr_no": "21"})
        self._place(stub, {"ord_no": " ", "itg_orr_no": "21"})
        self.assertEqual(stub._order_registry()["1"].get("conflict"), "1")
        self.assertIn("21", stub._burned_numbers()["itg"])
        stub._response = _resp(_row(itg_orr_no=21, iem_cd="069500", sby_dit_cd_nm="현금매도"))
        self.assertFalse(stub.get_order_status("1")["is_found"])
        n = len(stub.calls)
        with self.assertRaises(RuntimeError):
            stub.cancel_order("1", "069500")
        self.assertEqual(len(stub.calls), n)

    def test_oversized_int_numbers_not_registered(self):
        stub = _Stub({})
        stub._order_registry().clear()
        self._place(stub, {"ord_no": "1", "mkt_orr_no": 10**18, "itg_orr_no": 10**18})
        self.assertEqual(stub._order_registry()["1"]["mkt"], "")
        self.assertEqual(stub._order_registry()["1"]["itg"], "")
        with self.assertRaises(RuntimeError):
            stub.cancel_order("1", "069500")

    def _place(self, stub, out0, ticker="069500", side="SELL"):
        stub._response = {"Output_0": out0, "rsp_cd": "00000"}
        os.environ["LIVE_ORDER_ENABLED"] = "1"
        try:
            return stub.place_order(side, ticker, 1, None, "MARKET")
        finally:
            os.environ.pop("LIVE_ORDER_ENABLED", None)

    def test_non_canonical_market_numbers_are_never_used_to_cancel(self):
        for bad in ("+7", "７", "٧", "0_7", "7.0"):
            stub = _Stub({})
            stub._order_registry().clear()
            self._place(stub, {"ord_no": "1", "mkt_orr_no": bad, "itg_orr_no": "21"})
            n = len(stub.calls)
            with self.assertRaises(RuntimeError):
                stub.cancel_order("1", "069500")
            self.assertEqual(len(stub.calls), n, bad)

    def test_equivalent_spellings_of_one_market_number_collide(self):
        stub = _Stub({})
        stub._order_registry().clear()
        self._place(stub, {"ord_no": "1", "mkt_orr_no": "7", "itg_orr_no": "21"})
        self._place(stub, {"ord_no": "2", "mkt_orr_no": " 007 ", "itg_orr_no": "22"})
        n = len(stub.calls)
        for oid in ("1", "2"):
            with self.assertRaises(RuntimeError):
                stub.cancel_order(oid, "069500")
        self.assertEqual(len(stub.calls), n)

    def test_whitespace_candidates_do_not_mask_the_market_number(self):
        stub = _Stub({})
        stub._order_registry().clear()
        a = self._place(stub, {"ord_no": " ", "mkt_orr_no": "10", "itg_orr_no": "21"})
        b = self._place(stub, {"ord_no": "  ", "mkt_orr_no": "11", "itg_orr_no": "22"},
                        ticker="229200")
        self.assertEqual((a["order_id"], b["order_id"]), ("10", "11"))
        stub._response = {"Output_0": {}, "rsp_cd": "00000"}
        stub.cancel_order("10", "069500")
        self.assertEqual(stub.calls[-1][1]["org_mkt_orr_no"], 10)
        stub.cancel_order("11", "229200")
        self.assertEqual(stub.calls[-1][1]["org_mkt_orr_no"], 11)

    def test_unusable_order_key_is_not_registered(self):
        stub = _Stub({})
        stub._order_registry().clear()
        self._place(stub, {"ord_no": "+7", "mkt_orr_no": "", "itg_orr_no": "21"})
        self.assertEqual(stub._order_registry(), {})
        stub._response = _resp(_row(itg_orr_no=21, sby_dit_cd_nm="현금매도"))
        self.assertFalse(stub.get_order_status("+7")["is_found"])
        with self.assertRaises(RuntimeError):
            stub.cancel_order("+7", "069500")

    def test_unstored_reused_order_still_burns_its_market_number(self):
        # A(key=1,mkt=7) / B(key=1,mkt=8: 저장되지 않음) / C(key=3,mkt=8) — C 를 막는 것은 burn 기억뿐이다.
        stub = _Stub({})
        stub._order_registry().clear()
        self._place(stub, {"ord_no": "1", "mkt_orr_no": "7", "itg_orr_no": "21"})
        self._place(stub, {"ord_no": "1", "mkt_orr_no": "8", "itg_orr_no": "22"})
        self._place(stub, {"ord_no": "3", "mkt_orr_no": "8", "itg_orr_no": "23"})
        n = len(stub.calls)
        with self.assertRaises(RuntimeError):
            stub.cancel_order("3", "069500")
        self.assertEqual(len(stub.calls), n)


class DuplicateRowTests(unittest.TestCase):
    def _status(self, rows):
        stub = _Stub(_resp(*rows))
        return stub.get_order_status("3014", today="20261106")

    def test_contradicting_rows_are_unresolved_in_both_orders(self):
        cancelled = _row(orr_qty=1, tot_cns_qty=0, ny_cns_qty=0, can_qty=1)
        open_ = _row(orr_qty=1, tot_cns_qty=0, ny_cns_qty=1, can_qty=0)
        for rows in ((cancelled, open_), (open_, cancelled)):
            st = self._status(list(rows))
            self.assertFalse(st["is_found"])

    def test_bool_or_float_quantity_duplicate_is_not_equal_to_int(self):
        good = _row(orr_qty=1, tot_cns_qty=0, ny_cns_qty=0, can_qty=1)
        for bad in (True, 1.0, "1"):
            variant = _row(orr_qty=1, tot_cns_qty=0, ny_cns_qty=0, can_qty=bad)
            for rows in ((good, variant), (variant, good)):
                self.assertFalse(self._status(list(rows))["is_found"], (bad, rows[0] is good))

    def test_identical_duplicate_rows_are_accepted(self):
        st = self._status([_row(), _row()])
        self.assertTrue(st["is_found"])
        self.assertTrue(st["is_filled"])


class CancelEvidenceTests(unittest.TestCase):
    """거부/알 수 없는 행은 취소 확정이 될 수 없다 (Codex P1-2)."""

    def _st(self, **kw):
        return _Stub(_resp(_row(**kw))).get_order_status("3014")

    def test_rejected_row_is_not_a_confirmed_cancel(self):
        st = self._st(tot_cns_qty=0, ny_cns_qty=0, can_qty=0, orr_rjt_rsn_cd_nm="잔고부족")
        self.assertGreater(st["remaining_qty"], 0)
        self.assertFalse(st["is_filled"])

    def test_zero_everything_without_reason_is_not_a_cancel(self):
        st = self._st(tot_cns_qty=0, ny_cns_qty=0, can_qty=0)
        self.assertGreater(st["remaining_qty"], 0)

    def test_absent_open_quantity_is_derived_not_zero(self):
        row = _row(tot_cns_qty=0, can_qty=0)
        del row["ny_cns_qty"]
        st = _Stub(_resp(row)).get_order_status("3014")
        self.assertGreater(st["remaining_qty"], 0)

    def test_present_but_invalid_quantities_are_unresolved_even_with_cancel_evidence(self):
        # 값이 있는데 잘못된 필드는 취소/체결 확정에 쓰이지 않는다 (Codex P2).
        bad_values = (None, "", "abc", -1, "-1", 0.9, -0.9, "0.9", True, [], {})
        for field in ("ny_cns_qty", "can_qty", "tot_cns_qty", "orr_qty"):
            for bad in bad_values:
                row = _row(orr_qty=1, tot_cns_qty=0, ny_cns_qty=0, can_qty=1)
                row[field] = bad
                st = _Stub(_resp(row)).get_order_status("3014")
                self.assertFalse(st["is_found"], (field, bad))
                self.assertFalse(st["is_filled"], (field, bad))

    def test_fractional_quantities_never_become_a_confirmed_fill(self):
        st = _Stub(_resp(_row(orr_qty="1.9", tot_cns_qty="1.1"))).get_order_status("3014")
        self.assertFalse(st["is_found"])
        st = _Stub(_resp(_row(can_qty=0.9))).get_order_status("3014")
        self.assertFalse(st["is_filled"])

    def test_absurdly_long_digit_string_is_unknown_not_an_exception(self):
        self.assertIsNone(nh_adapter._strict_qty("1" * 5000))
        self.assertEqual(nh_adapter._strict_qty(" 12 "), 12)
        for bad in ("１", "1_0", "1e2", float("nan"), float("inf")):
            self.assertIsNone(nh_adapter._strict_qty(bad), bad)

    def test_integer_like_inputs_are_accepted(self):
        st = _Stub(_resp(_row(orr_qty="1", tot_cns_qty="1", ny_cns_qty="0", can_qty=0.0))).get_order_status("3014")
        self.assertTrue(st["is_filled"])

    def test_unknown_order_or_fill_quantity_is_unresolved(self):
        for k in ("orr_qty", "tot_cns_qty"):
            row = _row()
            row[k] = "n/a"
            self.assertFalse(_Stub(_resp(row)).get_order_status("3014")["is_found"], k)
        self.assertFalse(_Stub(_resp(_row(orr_qty=0))).get_order_status("3014")["is_found"])

    def test_real_cancel_has_affirmative_evidence(self):
        st = self._st(orr_qty=1, tot_cns_qty=0, ny_cns_qty=0, can_qty=1)
        self.assertEqual(st["remaining_qty"], 0)
        self.assertFalse(st["is_filled"])
        st = self._st(orr_qty=2, tot_cns_qty=1, ny_cns_qty=0, can_qty=1)
        self.assertEqual((st["filled_qty"], st["remaining_qty"]), (1, 0))

    def test_cancel_pending_still_has_remaining(self):
        st = self._st(orr_qty=2, tot_cns_qty=0, ny_cns_qty=1, can_qty=1)
        self.assertEqual(st["remaining_qty"], 1)

    def test_contradictory_accounting_blocks_both_fill_and_cancel(self):
        st = self._st(orr_qty=1, tot_cns_qty=1, ny_cns_qty=0, can_qty=1)
        self.assertGreaterEqual(st["remaining_qty"], 1)
        self.assertFalse(st["is_filled"])
        st = self._st(orr_qty=1, tot_cns_qty=0, ny_cns_qty=0, can_qty=2)
        self.assertGreaterEqual(st["remaining_qty"], 1)

    def test_fill_with_nonzero_cancel_quantity_is_not_complete(self):
        st = self._st(orr_qty=2, tot_cns_qty=2, ny_cns_qty=0, can_qty=1)
        self.assertFalse(st["is_filled"])


class CancelRequestTests(unittest.TestCase):
    def test_full_cancel_payload(self):
        stub = _Stub({"Output_0": {"ord_no": "9"}, "rsp_cd": "00000"})
        stub.cancel_order("3014", "069500")
        path, body = stub.calls[0]
        self.assertEqual(path, "/krstock/order/v1/cancel")
        self.assertEqual(
            body,
            {"act_no": "00000000000", "org_mkt_orr_no": 3014, "all_pat_dit_cd": "1",
             "iem_cd": "069500", "cor_qty": 0},
        )

    def test_partial_cancel_payload(self):
        stub = _Stub({"Output_0": {}, "rsp_cd": "00000"})
        stub.cancel_order("3014", "069500", qty=2)
        body = stub.calls[0][1]
        self.assertEqual((body["all_pat_dit_cd"], body["cor_qty"]), ("2", 2))

    def test_failure_response_is_not_reported_as_requested(self):
        stub = _Stub({"rsp_cd": "IGW40011", "rsp_msg": "bad request"})
        with self.assertRaises(nh_adapter.NhApiError):
            stub.cancel_order("3014", "069500")

    def test_unregistered_non_numeric_order_id_is_refused_before_sending(self):
        stub = _Stub({"rsp_cd": "00000"})
        with self.assertRaises(RuntimeError):
            stub.cancel_order("abc", "069500")
        self.assertEqual(stub.calls, [])

    def test_cancel_refused_without_confirmed_market_number(self):
        # 미등록(재시작/다른 프로세스), 응답에 mkt_orr_no 없음 -> 취소를 보내지 않는다 (Codex P2).
        stub = _Stub({"rsp_cd": "00000"})
        stub.register("7", itg="201", mkt="")
        for oid in ("7", "9999"):
            with self.assertRaises(RuntimeError):
                stub.cancel_order(oid, "069500")
        self.assertEqual(stub.calls, [])


if __name__ == "__main__":
    unittest.main()
