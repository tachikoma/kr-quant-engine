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


if __name__ == "__main__":
    unittest.main()
