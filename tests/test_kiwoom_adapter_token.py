"""Kiwoom 어댑터: 본문 기준 토큰 무효(HTTP 200 + return_code=3 [8005]) 처리 회귀 테스트 (네트워크 호출 없음).

배경: 모의서버 실측(2026-11-09) 가짜/만료 토큰은 HTTP 401이 아니라 HTTP 200 + return_code=3 [8005:Token이 유효하지 않습니다]
로 내려오는데 어댑터는 401만 재발급해서, 실행 중 토큰이 만료되면 주문이 복구 없이 실패했다.
"""
import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live_trading import kiwoom_adapter  # noqa: E402

TOKEN_BAD = {"return_code": 3, "return_msg": "인증에 실패했습니다[8005:Token이 유효하지 않습니다]"}
KEY_BAD = {"return_code": 3, "return_msg": "인증에 실패했습니다[8001:App Key와 Secret Key 검증에 실패했습니다]"}
RATE = {"return_code": 5, "return_msg": "허용된 요청 개수를 초과하였습니다"}
OK = {"return_code": 0, "return_msg": "정상", "ord_no": "0000001"}


class _Resp:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status
        self.text = json.dumps(body)
        self.headers = {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, replies):
        self.replies = list(replies)
        self.posts = 0

    def post(self, *a, **k):
        self.posts += 1
        return self.replies.pop(0)


class _Stub(kiwoom_adapter.KiwoomAdapter):
    def __init__(self, replies):
        self.http_max_retries = 4
        self.http_retry_delay = 0.0
        self.timeout = 1
        self._throttle_lock = threading.Lock()
        self._last_request_ts = 0.0
        self._session = _Session(replies)
        self.reissued = 0
        self.account_no = ""

    def _resolve_url(self, endpoint):
        return "https://mock.invalid" + endpoint

    def _headers(self, api_id=None):
        return {}

    def _throttle_request(self):
        return None

    def _retry_delay(self, response):
        return 0.0

    def invalidate_token(self):
        self.reissued += 0

    def _ensure_token_valid(self):
        self.reissued += 1


class TokenInvalidBodyTests(unittest.TestCase):
    def test_token_invalid_then_success_resends_once_after_reissue(self):
        a = _Stub([_Resp(TOKEN_BAD), _Resp(OK)])
        data = a._post("/api/dostk/ordr", {"x": 1}, "kt10000", retry_network=False)
        self.assertEqual(data, OK)
        self.assertEqual(a._session.posts, 2)
        self.assertEqual(a.reissued, 1)

    def test_token_invalid_twice_is_returned_not_looped(self):
        a = _Stub([_Resp(TOKEN_BAD), _Resp(TOKEN_BAD), _Resp(OK)])
        data = a._post("/api/dostk/ordr", {"x": 1}, "kt10000", retry_network=False)
        self.assertEqual(data, TOKEN_BAD)
        self.assertEqual(a._session.posts, 2)  # 재전송은 1회만
        self.assertEqual(a.reissued, 1)

    def test_other_auth_failure_is_not_retried(self):
        a = _Stub([_Resp(KEY_BAD), _Resp(OK)])
        data = a._post("/api/dostk/ordr", {"x": 1}, "kt10000", retry_network=False)
        self.assertEqual(data, KEY_BAD)
        self.assertEqual(a._session.posts, 1)
        self.assertEqual(a.reissued, 0)

    def test_is_token_invalid_is_strict(self):
        f = kiwoom_adapter.KiwoomAdapter._is_token_invalid
        self.assertTrue(f(TOKEN_BAD))
        for other in (KEY_BAD, RATE, OK, {}, None, [], "8005",
                      {"return_code": 0, "return_msg": "8005"},
                      {"return_code": 3, "return_msg": ""}):
            self.assertFalse(f(other), other)

    def test_rate_limit_retry_unchanged(self):
        a = _Stub([_Resp(RATE), _Resp(OK)])
        data = a._post("/api/dostk/ordr", {"x": 1}, "kt10000", retry_network=False)
        self.assertEqual(data, OK)
        self.assertEqual(a._session.posts, 2)
        self.assertEqual(a.reissued, 0)

    def test_place_order_surfaces_error_when_token_still_invalid(self):
        a = _Stub([_Resp(TOKEN_BAD), _Resp(TOKEN_BAD)])
        os.environ["LIVE_ORDER_ENABLED"] = "1"
        try:
            with self.assertRaises(RuntimeError):
                a.place_order("BUY", "069500", 1, None, "MARKET")
        finally:
            os.environ.pop("LIVE_ORDER_ENABLED", None)
        self.assertEqual(a._session.posts, 2)


if __name__ == "__main__":
    unittest.main()
