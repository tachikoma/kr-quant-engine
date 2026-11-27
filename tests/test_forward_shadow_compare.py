"""Forward Shadow must reproduce the frozen backtest order by order, day by day.

See ``tests/shadow_compare.py`` for what is compared.  Any difference fails.
"""

from __future__ import annotations

import unittest

from tests.shadow_compare import compare, scenarios


class BacktestParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scenarios = scenarios()

    def run_scenario(self, name: str) -> dict:
        if name not in self.scenarios:
            self.skipTest("cached KRX data unavailable")
        result = compare(**self.scenarios[name])
        self.assertEqual(result["differences"], [])
        self.assertAlmostEqual(
            result["final_equity_shadow"], result["final_equity_backtest"], places=5
        )
        self.assertAlmostEqual(result["mdd_shadow"], result["mdd_backtest"], places=12)
        return result

    def test_normal_market(self) -> None:
        result = self.run_scenario("synthetic_normal")
        self.assertGreaterEqual(result["decisions"], 4)
        self.assertGreater(result["sell_fills"], 0)

    def test_thin_volume_partial_fills_carry_and_cash_limits(self) -> None:
        outcomes = self.run_scenario("synthetic_thin_rotation")["order_outcomes"]
        for outcome in ("PARTIAL_CARRY", "PARTIAL_CANCEL", "CARRY_CANCELLED",
                        "CASH_LIMITED", "ZERO_VOLUME", "NO_CAPACITY", "FULL"):
            self.assertIn(outcome, outcomes)

    def test_risk_off_forced_exits_and_empty_targets(self) -> None:
        result = self.run_scenario("synthetic_risk_off")
        self.assertGreater(result["forced_exit_decisions"], 0)
        self.assertGreater(result["empty_target_sell_decisions"], 0)
        self.assertGreater(result["costs"]["tax"], 0)

    def test_real_cached_market_2026(self) -> None:
        self.assertGreater(self.run_scenario("real_2026_1e8")["fills"], 0)

    def test_real_cached_market_2026_capacity_bound(self) -> None:
        result = self.run_scenario("real_2026_5e10")
        self.assertGreater(result["carry_fills"], 0)


if __name__ == "__main__":
    unittest.main()
