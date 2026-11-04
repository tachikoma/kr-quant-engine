"""broker_preflight 자격증명 목록·모드 해석 검증 (네트워크/실주문 없음, mock 기반)."""
import os
import re
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ADAPTER_SRC = {
    "KIWOOM": ROOT / "live_trading" / "kiwoom_adapter.py",
    "KIS": ROOT / "live_trading" / "kis" / "_kis_auth_manager.py",
    "NH": ROOT / "live_trading" / "nh_adapter.py",
    "KB": ROOT / "live_trading" / "kb_adapter.py",
}


class PreflightCredentialTests(unittest.TestCase):
    def test_required_env_names_are_the_ones_adapters_read(self):
        from live_trading.broker_preflight import CRED_OPTIONAL, CRED_REQUIRED

        for broker, src in ADAPTER_SRC.items():
            text = src.read_text(encoding="utf-8")
            read = set(re.findall(r'environ\.get\(\s*"([A-Z0-9_]+)"', text))
            for name in CRED_REQUIRED[broker] + CRED_OPTIONAL[broker]:
                self.assertIn(name, read, f"{broker}: preflight checks {name} but adapter never reads it")

    def test_every_credential_like_env_read_by_adapters_is_classified(self):
        """어댑터에 새 인증 변수가 추가되면 프리플라이트 목록 갱신 없이는 실패한다 (역방향 대조)."""
        from live_trading.broker_preflight import CRED_OPTIONAL, CRED_REQUIRED

        pat = re.compile(
            r'(?:environ\.get|getenv)\(\s*["\']([A-Z0-9_]*(?:APP_?KEY|SECRET|ACCT_NO|ACCOUNT_NO|ACCOUNT_PROD)[A-Z0-9_]*)["\']'
        )
        for broker, src in ADAPTER_SRC.items():
            found = set(pat.findall(src.read_text(encoding="utf-8")))
            known = set(CRED_REQUIRED[broker]) | set(CRED_OPTIONAL[broker])
            self.assertEqual(found - known, set(), f"{broker}: adapter reads unclassified credential env vars")

    def test_required_credentials_exact(self):
        from live_trading.broker_preflight import CRED_REQUIRED

        self.assertEqual(CRED_REQUIRED, {
            "KIS": ["KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO"],
            "KIWOOM": ["KIWOOM_APPKEY", "KIWOOM_SECRETKEY"],
            "NH": ["NHPLUG_APP_KEY", "NHPLUG_APP_SECRET", "NHPLUG_ACCT_NO"],
            "KB": ["KBSEC_APP_KEY", "KBSEC_APP_SECRET"],
        })

    def test_kiwoom_needs_no_account_number(self):
        from live_trading.broker_preflight import CRED_REQUIRED

        self.assertEqual(CRED_REQUIRED["KIWOOM"], ["KIWOOM_APPKEY", "KIWOOM_SECRETKEY"])

    def test_env_sample_lists_every_required_name(self):
        from live_trading.broker_preflight import CRED_REQUIRED

        sample = (ROOT / ".env.sample").read_text(encoding="utf-8")
        for broker, names in CRED_REQUIRED.items():
            for n in names:
                self.assertIn(n, sample, f"{broker}: {n} missing from .env.sample")


class CancelSemanticsTests(unittest.TestCase):
    def test_cancel_confirmed_rules(self):
        from live_trading.broker_preflight import cancel_confirmed as c

        self.assertTrue(c({"is_found": True, "order_qty": 1, "filled_qty": 0, "remaining_qty": 0}))
        self.assertFalse(c({"is_found": True, "order_qty": 1, "filled_qty": 1, "remaining_qty": 0}))
        self.assertFalse(c({"is_found": True, "order_qty": 1, "filled_qty": 0, "remaining_qty": 1}))
        self.assertFalse(c({"is_found": False, "order_qty": 0, "filled_qty": 0, "remaining_qty": 0}))
        self.assertFalse(c({"is_found": True, "order_qty": 1, "filled_qty": 0}))
        # 전량체결을 한 번 관측한 뒤 지연된 0/0 응답이 와도 취소로 판정하지 않는다 (러너와 동일)
        self.assertFalse(c({"is_found": True, "order_qty": 1, "filled_qty": 0, "remaining_qty": 0}, prior_filled=1))


class ModeResolutionTests(unittest.TestCase):
    ENV = {"MODE": "demo"}

    def _clean(self, extra=None):
        env = {k: v for k, v in os.environ.items() if k not in ("MODE", "BROKER_MODE", "ENV_MODE")}
        env.update(self.ENV)
        env.update(extra or {})
        return mock.patch.dict(os.environ, env, clear=True)

    def test_nh_uses_MODE_for_demo_url(self):
        from live_trading.nh_adapter import NhAdapter

        with self._clean({"NHPLUG_ACCT_NO": "50001002017", "NHPLUG_APP_KEY": "k", "NHPLUG_APP_SECRET": "s"}), \
                mock.patch.object(NhAdapter, "_setup_token_file"), \
                mock.patch.object(NhAdapter, "_issue_token_if_needed"):
            self.assertIn("moapi", NhAdapter().base_url)

    def test_nh_real_when_MODE_real(self):
        from live_trading.nh_adapter import NhAdapter

        with self._clean({"MODE": "real", "NHPLUG_ACCT_NO": "50001002017"}), \
                mock.patch.object(NhAdapter, "_setup_token_file"), \
                mock.patch.object(NhAdapter, "_issue_token_if_needed"):
            self.assertNotIn("moapi", NhAdapter().base_url)

    def test_kis_client_gets_demo_from_MODE(self):
        from live_trading import kis_adapter

        seen = {}
        class FakeClient:
            def __init__(self, auth, env_mode):
                seen["mode"] = env_mode
        fake_auth = mock.MagicMock(account="12345678-01", product_code="01")
        with self._clean(), \
                mock.patch.object(kis_adapter, "KisAuthManager", return_value=fake_auth), \
                mock.patch.object(kis_adapter, "KisApiClient", FakeClient), \
                mock.patch.object(kis_adapter.KisAdapter, "_verify_api_access"):
            kis_adapter.KisAdapter()
        self.assertEqual(seen["mode"], "demo")

    def test_preflight_overrides_env_mode(self):
        from live_trading import broker_preflight as bp

        with self._clean({"MODE": "real"}):
            fake = mock.MagicMock()
            with mock.patch("live_trading.kiwoom_adapter.KiwoomAdapter", fake):
                bp._build_adapter("KIWOOM", "demo")
            self.assertEqual(os.environ["MODE"], "demo")

    def test_real_with_order_refused_without_allow_real(self):
        from live_trading import broker_preflight as bp

        fake = mock.MagicMock()
        with self._clean(), mock.patch.object(bp, "_build_adapter", return_value=fake), \
                mock.patch.object(bp, "_is_market_hours", return_value=True):
            rep = bp.run_preflight("KIWOOM", "real", True, ["069500"])
        fake.place_order.assert_not_called()
        self.assertTrue(any(r["label"] == "real-mode guard" and r["status"] == "FAIL" for r in rep["results"]))



class FakeBroker:
    """주문/상태/취소를 시나리오대로 흉내내는 가짜 어댑터 (네트워크 없음)."""

    def __init__(self, base_url="https://mockapi.kiwoom.com", *, empty_id_first=False,
                 buy_status=None, sell_status=None, limit_status=None, cancel_raises=False):
        self.base_url = base_url
        self.places, self.cancels = [], []
        self.empty_id_first = empty_id_first
        self.cancel_raises = cancel_raises
        self.policy = {"BUY": buy_status, "SELL": sell_status, "LIMIT": limit_status}
        self.kind, self.calls = {}, {}

    FILLED = {"is_found": True, "order_qty": 1, "filled_qty": 1, "remaining_qty": 0, "is_filled": True}
    OPEN = {"is_found": True, "order_qty": 1, "filled_qty": 0, "remaining_qty": 1, "is_filled": False}
    CANCELLED = {"is_found": True, "order_qty": 1, "filled_qty": 0, "remaining_qty": 0, "is_filled": False}

    def get_holdings(self):
        return {}

    def get_prices(self, tickers):
        return {t: 10000 for t in tickers}

    def place_order(self, side, ticker, qty, price=None, order_type="MARKET"):
        kind = "LIMIT" if order_type == "LIMIT" else side
        self.places.append((side, order_type, price))
        if self.empty_id_first and len(self.places) == 1:
            return {"order_id": ""}
        oid = f"O{len(self.places)}"
        self.kind[oid], self.calls[oid] = kind, 0
        return {"order_id": oid}

    def get_order_status(self, order_id, today=None):
        self.calls[order_id] += 1
        pol = self.policy[self.kind[order_id]]
        return dict(pol(self, order_id) if callable(pol) else pol)

    def cancel_order(self, order_id, ticker, qty):
        self.cancels.append(order_id)
        if self.cancel_raises:
            raise TimeoutError("cancel timeout")
        self.cancelled = getattr(self, "cancelled", set()) | {order_id}
        return {}


def _after_cancel(open_state, done_state):
    return lambda b, oid: done_state if oid in getattr(b, "cancelled", set()) else open_state


class Stage2Tests(unittest.TestCase):
    def _run(self, broker):
        from live_trading import broker_preflight as bp

        rows = []
        record = lambda stage, label, status, note="": rows.append((label, status, note))
        with mock.patch.object(bp.time, "sleep", lambda x: None):
            bp._stage2_orders(broker, record)
        return rows

    def _failed(self, rows, label_part):
        return any(label_part in lbl and st == "FAIL" for lbl, st, _ in rows)

    def test_happy_path_roundtrip_and_cancel(self):
        F = FakeBroker
        b = F(buy_status=F.FILLED, sell_status=F.FILLED,
              limit_status=_after_cancel(F.OPEN, F.CANCELLED))
        rows = self._run(b)
        self.assertEqual([p[0] for p in b.places], ["BUY", "SELL", "BUY"])
        self.assertFalse(any(st == "FAIL" for _, st, _ in rows), rows)
        self.assertTrue(any("cancel_semantics" in l and st == "PASS" for l, st, _ in rows))

    def test_missing_order_id_stops_all_further_orders(self):
        F = FakeBroker
        b = F(empty_id_first=True, buy_status=F.FILLED, sell_status=F.FILLED, limit_status=F.OPEN)
        rows = self._run(b)
        self.assertEqual(len(b.places), 1)
        self.assertTrue(self._failed(rows, "BUY 1주 MARKET"))
        self.assertTrue(self._failed(rows, "stage2_unresolved"))

    def test_buy_unfilled_cancel_fails_unresolved_no_sell(self):
        F = FakeBroker
        b = F(buy_status=F.OPEN, cancel_raises=True)
        rows = self._run(b)
        self.assertEqual([p[0] for p in b.places], ["BUY"])
        self.assertTrue(self._failed(rows, "stage2_unresolved"))

    def test_buy_cancel_race_filled_triggers_sell(self):
        F = FakeBroker
        b = F(buy_status=_after_cancel(F.OPEN, F.FILLED), sell_status=F.FILLED,
              limit_status=_after_cancel(F.OPEN, F.CANCELLED))
        rows = self._run(b)
        self.assertIn("SELL", [p[0] for p in b.places])
        self.assertFalse(any(st == "FAIL" for _, st, _ in rows), rows)

    def test_buy_cancelled_skips_sell_and_continues(self):
        F = FakeBroker
        b = F(buy_status=_after_cancel(F.OPEN, F.CANCELLED),
              limit_status=_after_cancel(F.OPEN, F.CANCELLED))
        rows = self._run(b)
        self.assertNotIn("SELL", [p[0] for p in b.places])
        self.assertFalse(self._failed(rows, "stage2_unresolved"))
        # 시장가 체결/매도 경로를 검증하지 못했으므로 전체 결과는 성공이 될 수 없다
        self.assertTrue(self._failed(rows, "roundtrip"))

    def test_sell_timeout_is_failure_and_stops(self):
        F = FakeBroker
        b = F(buy_status=F.FILLED, sell_status=F.OPEN)
        rows = self._run(b)
        self.assertEqual(len(b.places), 2)  # BUY, SELL — 지정가 단계로 진행하지 않음
        self.assertTrue(self._failed(rows, "stage2_unresolved"))

    def test_limit_filled_race_is_failure(self):
        F = FakeBroker
        b = F(buy_status=F.FILLED, sell_status=F.FILLED,
              limit_status=_after_cancel(F.OPEN, F.FILLED))
        rows = self._run(b)
        self.assertTrue(self._failed(rows, "cancel_semantics"))

    def test_limit_cancel_unconfirmed_is_unresolved(self):
        F = FakeBroker
        b = F(buy_status=F.FILLED, sell_status=F.FILLED, limit_status=F.OPEN)
        rows = self._run(b)
        self.assertTrue(self._failed(rows, "stage2_unresolved"))


class DestinationGuardTests(unittest.TestCase):
    def _preflight(self, broker, mode, api, allow_real=False, live="1"):
        from live_trading import broker_preflight as bp

        env = {"LIVE_ORDER_ENABLED": live}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(bp, "_build_adapter", return_value=api), \
                mock.patch.object(bp, "_is_market_hours", return_value=True), \
                mock.patch.object(bp, "_stage2_orders") as stage2, \
                mock.patch.object(bp.time, "sleep", lambda x: None):
            rep = bp.run_preflight(broker, mode, True, ["069500"], allow_real=allow_real)
        return rep, stage2

    def _guard(self, rep):
        return [r["status"] for r in rep["results"] if r["label"] == "order-destination guard"]

    def test_demo_mode_but_real_nh_url_is_refused(self):
        api = FakeBroker(base_url="https://api.nhplug.com:8443")
        rep, stage2 = self._preflight("NH", "demo", api)
        self.assertEqual(self._guard(rep), ["FAIL"])
        stage2.assert_not_called()

    def test_nh_moapi_passes(self):
        api = FakeBroker(base_url="https://moapi.nhplug.com:8443")
        rep, stage2 = self._preflight("NH", "demo", api)
        self.assertEqual(self._guard(rep), ["PASS"])
        stage2.assert_called_once()

    def test_kb_demo_requires_explicit_approval(self):
        api = FakeBroker(base_url="https://developer.kbsec.com:32484")
        rep, stage2 = self._preflight("KB", "demo", api)
        self.assertEqual(self._guard(rep), ["FAIL"])
        stage2.assert_not_called()
        rep, stage2 = self._preflight("KB", "demo", api, allow_real=True)
        self.assertEqual(self._guard(rep), ["PASS"])

    def test_kiwoom_real_url_with_demo_mode_refused(self):
        rep, stage2 = self._preflight("KIWOOM", "demo", FakeBroker(base_url="https://api.kiwoom.com"))
        self.assertEqual(self._guard(rep), ["FAIL"])
        stage2.assert_not_called()

    def test_kis_uses_auth_base_url(self):
        from types import SimpleNamespace

        demo = SimpleNamespace(_auth=SimpleNamespace(base_url="https://openapivts.koreainvestment.com:29443"))
        real = SimpleNamespace(_auth=SimpleNamespace(base_url="https://openapi.koreainvestment.com:9443"))
        from live_trading import broker_preflight as bp

        self.assertTrue(bp.order_destination_ok(demo, "KIS", "demo", False)[0])
        self.assertFalse(bp.order_destination_ok(real, "KIS", "demo", False)[0])


class AdversarialDestinationTests(unittest.TestCase):
    def _ok(self, broker, url, mode="demo", allow_real=False):
        from live_trading import broker_preflight as bp

        return bp.order_destination_ok(FakeBroker(base_url=url), broker, mode, allow_real)[0]

    def test_exact_demo_hosts_pass(self):
        self.assertTrue(self._ok("NH", "https://moapi.nhplug.com:8443"))
        self.assertTrue(self._ok("KIWOOM", "https://mockapi.kiwoom.com"))

    def test_url_tricks_are_rejected(self):
        for url in (
            "https://moapi.nhplug.com@api.nhplug.com:8443",        # userinfo trick: 실제 호스트는 실전
            "https://api.nhplug.com:8443/?h=moapi.nhplug.com",     # 쿼리에 표식
            "https://api.nhplug.com:8443/moapi.nhplug.com",        # 경로에 표식
            "https://moapi.nhplug.com.evil.example",               # 접미사 도메인
            "https://evilmoapi.nhplug.com",                        # 접두 도메인
            "http://moapi.nhplug.com:8443",                        # 평문 스킴
            "https://api.nhplug.com:8443",
            "",
        ):
            self.assertFalse(self._ok("NH", url), url)

    def test_real_mode_requires_allow_real_inside_guard(self):
        self.assertFalse(self._ok("KIWOOM", "https://api.kiwoom.com", mode="real"))
        self.assertTrue(self._ok("KIWOOM", "https://api.kiwoom.com", mode="real", allow_real=True))

    def test_stage2_unexpected_exception_is_recorded_as_failure(self):
        from live_trading import broker_preflight as bp

        api = FakeBroker(base_url="https://moapi.nhplug.com:8443")
        with mock.patch.dict(os.environ, {"LIVE_ORDER_ENABLED": "1"}), \
                mock.patch.object(bp, "_build_adapter", return_value=api), \
                mock.patch.object(bp, "_is_market_hours", return_value=True), \
                mock.patch.object(bp, "_stage2_orders", side_effect=RuntimeError("boom")):
            rep = bp.run_preflight("NH", "demo", True, ["069500"])
        self.assertTrue(any(r["label"] == "stage2_unresolved" and r["status"] == "FAIL" for r in rep["results"]))


    def test_more_url_oddities(self):
        self.assertTrue(self._ok("NH", "https://MOAPI.NHPLUG.COM:8443"))      # 호스트명은 대소문자 무시
        for url in (
            "https://moapi.nhplug.com.:8443",                # 끝 점(FQDN) — 정확 일치 아님, 보수적으로 거부
            "https://[::1]:8443",                            # IPv6
            "https://moapi.nhplug.com\\@api.nhplug.com",     # 백슬래시 변형
            "https://moapi.nhplug.com:99999999",             # 비정상 포트(파싱 오류여도 예외 없이 거부 또는 통과 후 요청 실패)
            "https://móapi.nhplug.com",                      # 유니코드 위장
        ):
            try:
                ok = self._ok("NH", url)
            except Exception as e:  # 가드가 예외를 밖으로 내면 안 된다
                self.fail(f"guard raised for {url!r}: {e}")
            if "99999999" not in url:
                self.assertFalse(ok, url)

    def test_adapter_without_any_url_attribute_is_refused(self):
        from live_trading import broker_preflight as bp

        class NoUrl:
            pass

        self.assertFalse(bp.order_destination_ok(NoUrl(), "NH", "demo", False)[0])


class FillConsistencyTests(unittest.TestCase):
    def test_filled_but_remaining_nonzero_is_not_terminal(self):
        from live_trading.broker_preflight import fully_filled as f

        self.assertFalse(f({"is_found": True, "order_qty": 1, "filled_qty": 1, "remaining_qty": 1}))
        self.assertFalse(f({"is_found": True, "order_qty": 1, "filled_qty": 1}))
        self.assertTrue(f({"is_found": True, "order_qty": 1, "filled_qty": 1, "remaining_qty": 0}))
        self.assertFalse(f({"is_found": False, "order_qty": 1, "filled_qty": 1, "remaining_qty": 0}))
        self.assertFalse(f({"is_found": True, "order_qty": 0, "filled_qty": 0, "remaining_qty": 0}))

    def test_inconsistent_status_stops_stage2_without_next_order(self):
        from live_trading import broker_preflight as bp

        incons = {"is_found": True, "order_qty": 1, "filled_qty": 1, "remaining_qty": 1, "is_filled": True}
        b = FakeBroker(buy_status=incons)
        rows = []
        with mock.patch.object(bp.time, "sleep", lambda x: None):
            bp._stage2_orders(b, lambda st, lb, s_, n="": rows.append((lb, s_, n)))
        self.assertEqual([p[0] for p in b.places], ["BUY"])
        self.assertTrue(any(l == "stage2_unresolved" and s_ == "FAIL" for l, s_, _ in rows))

    def test_status_regression_keeps_max_fill(self):
        from live_trading.broker_preflight import cancel_confirmed

        regressed = {"is_found": True, "order_qty": 1, "filled_qty": 0, "remaining_qty": 0}
        self.assertFalse(cancel_confirmed(regressed, prior_filled=1))


class ScrubTests(unittest.TestCase):
    def test_credential_values_are_masked_in_notes(self):
        from live_trading import broker_preflight as bp

        with mock.patch.dict(os.environ, {"KIWOOM_SECRETKEY": "TopSecretValue123", "KIS_ACCOUNT_NO": "12345678"}):
            out = bp._scrub("auth failed secret=TopSecretValue123 acct 12345678-01")
        self.assertNotIn("TopSecretValue123", out)
        self.assertNotIn("12345678", out)

    def test_secret_straddling_truncation_boundary_is_not_leaked(self):
        """실제 경로: 어댑터 초기화 예외 메시지의 비밀값이 기록 길이 한도(400자) 경계에 걸쳐도 노출되지 않는다."""
        from live_trading import broker_preflight as bp

        secret = "S3cretValueThatIsLongEnough0123456789"
        for pad in (370, 380, 395, 399):  # 경계 전후로 위치를 바꿔 본다
            with mock.patch.dict(os.environ, {"KIWOOM_SECRETKEY": secret}), \
                    mock.patch.object(bp, "_build_adapter",
                                      side_effect=RuntimeError("x" * pad + secret)):
                rep = bp.run_preflight("KIWOOM", "demo", False, ["069500"])
            notes = " ".join(r["note"] for r in rep["results"])
            self.assertNotIn(secret[:6], notes, f"pad={pad}")
            self.assertTrue(any(r["label"] == "adapter_init" and r["status"] == "FAIL" for r in rep["results"]))

    def test_whitespace_padded_account_normalized_form_is_masked(self):
        from live_trading import broker_preflight as bp

        with mock.patch.dict(os.environ, {"NHPLUG_ACCT_NO": " 500-01-002017 "}):
            out = bp._scrub("act_no=50001002017; raw= 500-01-002017 ;")
        self.assertNotIn("50001002017", out)
        self.assertNotIn("002017", out)

    def test_degenerate_values_do_not_corrupt_notes(self):
        from live_trading import broker_preflight as bp

        for weird in ("----", "a---", "   ", "-- --"):
            with mock.patch.dict(os.environ, {"KIWOOM_SECRETKEY": weird}):
                out = bp._scrub("plain diagnostic message abc")
            self.assertEqual(out, "plain diagnostic message abc", repr(weird))

    def test_overlapping_credentials_longest_first(self):
        from live_trading import broker_preflight as bp

        env = {"KIS_APP_KEY": " InnerSecret123 ", "NHPLUG_APP_SECRET": "prefixInnerSecret123suffix"}
        with mock.patch.dict(os.environ, env):
            out = bp._scrub("err prefixInnerSecret123suffix and InnerSecret123")
        self.assertNotIn("InnerSecret123", out)
        self.assertNotIn("prefix", out)
        self.assertNotIn("suffix", out)

    def test_space_before_hyphen_account_piece_is_masked(self):
        from live_trading import broker_preflight as bp

        with mock.patch.dict(os.environ, {"KIS_ACCOUNT_NO": "12345678 -01"}):
            out = bp._scrub("cano=12345678 failed")
        self.assertNotIn("12345678", out)

    def test_url_encoded_and_json_escaped_forms_are_masked(self):
        from live_trading import broker_preflight as bp

        with mock.patch.dict(os.environ, {"KBSEC_APP_SECRET": "abcd/efgh"}):
            for echoed in ("abcd/efgh", "abcd%2Fefgh", "abcd\\/efgh"):
                out = bp._scrub(f"resp: {echoed} bad")
                self.assertNotIn("abcd", out, echoed)
                self.assertNotIn("efgh", out, echoed)
        with mock.patch.dict(os.environ, {"KBSEC_APP_SECRET": 'ab"cd"ef'}):
            out = bp._scrub('{"secret":"ab\\"cd\\"ef"}')
        self.assertNotIn('cd', out)

    def test_short_non_secret_values_are_left_alone(self):
        from live_trading import broker_preflight as bp

        with mock.patch.dict(os.environ, {"KIS_ACCOUNT_PROD_CD": "01"}):
            self.assertEqual(bp._scrub("code 01 ok"), "code 01 ok")

    def test_nh_normalized_account_is_masked(self):
        from live_trading import broker_preflight as bp

        with mock.patch.dict(os.environ, {"NHPLUG_ACCT_NO": "500-01-002017"}):
            out = bp._scrub("act_no=50001002017 failed; raw 500-01-002017")
        self.assertNotIn("50001002017", out)
        self.assertNotIn("002017", out)


if __name__ == "__main__":
    unittest.main()
