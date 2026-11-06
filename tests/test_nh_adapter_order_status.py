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

    def _call(self, path, input_0, base_url=None, retry_network=True):
        self.calls.append((path, input_0))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    def _check_live_enabled(self):
        return None


def _row(**kw):
    base = {
        "itg_orr_no": 3014, "iem_cd": "069500", "orr_qty": 1, "tot_cns_qty": 1,
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
        self.assertFalse(_Stub(_resp(row)).get_order_status("0").get("is_found"))

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

    def test_non_numeric_order_id_is_rejected_before_sending(self):
        stub = _Stub({"rsp_cd": "00000"})
        with self.assertRaises(ValueError):
            stub.cancel_order("abc", "069500")
        self.assertEqual(stub.calls, [])


if __name__ == "__main__":
    unittest.main()
