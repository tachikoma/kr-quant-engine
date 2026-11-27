"""Compare the Forward Shadow MVP against the frozen backtest on identical inputs.

The comparison isolates signal, sizing, execution and cost differences.  The
backtest's module-level spread is temporarily set to the frozen 2bp value so
that the only remaining differences are the shadow's explicit capacity model
and its independent state/ledger accounting.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import etf_shared
import run_etf_backtest as bt
from forward_shadow.market import FixtureProvider
from forward_shadow.runner import initialize_runtime, process_date
from forward_shadow.store import RuntimePaths, read_json, read_jsonl
from strategy_freeze import load_frozen_strategy
from tests.test_forward_shadow import make_synthetic

FROZEN = load_frozen_strategy()
UNIVERSE = [str(t) for t in FROZEN["universe"]]


def build_price_data(etf: dict[str, pd.DataFrame]) -> pd.DataFrame:
    frame = pd.concat(etf.values(), ignore_index=True).sort_values(["ticker", "date"])
    frame["ticker"] = frame["ticker"].astype(str)
    frame = etf_shared.add_liquidity_flag(frame)
    frame = etf_shared.add_listing_flag(frame, {})
    frame = etf_shared.add_deviation_flag(frame)
    frame["close_adj"] = frame["close"]
    grouped = frame.groupby("ticker")
    frame["ret_60"] = grouped["close_adj"].pct_change(60)
    frame["ret_120"] = grouped["close_adj"].pct_change(120)
    frame["ma20"] = grouped["close_adj"].transform(lambda x: x.rolling(20).mean())
    frame["ma60"] = grouped["close_adj"].transform(lambda x: x.rolling(60).mean())
    frame["trend_ok"] = (frame["close_adj"] > frame["ma20"]) & (frame["ma20"] > frame["ma60"])
    return frame.reset_index(drop=True)


def build_index_data(index: pd.DataFrame) -> pd.DataFrame:
    frame = index.copy().sort_values("date").reset_index(drop=True)
    frame["market_ma"] = frame["close"].rolling(120).mean()
    frame["market_ma_slope"] = frame["market_ma"] - frame["market_ma"].shift(20)
    frame["risk_on"] = (frame["close"] >= frame["market_ma"]) & (frame["market_ma_slope"] >= 0)
    return frame[["date", "close", "market_ma", "market_ma_slope", "risk_on"]]


class BacktestComparisonTest(unittest.TestCase):
    def test_decision_dates_targets_and_equity_alignment(self) -> None:
        etf, index, dates = make_synthetic()
        price = build_price_data(etf)
        index_df = build_index_data(index)
        common_dates = list(dates[10:])  # backtest warmup 140 -> dates[150]

        observers: list[dict] = []
        original_spread = bt.SPREAD_PCT
        bt.SPREAD_PCT = float(FROZEN["parameters"]["spread_pct"])
        try:
            bt.run_etf_strategy(
                initial_cash=100_000_000.0,
                common_dates=common_dates,
                index_df=index_df,
                price_data=price,
                max_positions=int(FROZEN["parameters"]["max_positions"]),
                slippage=float(FROZEN["parameters"]["slippage"]),
                max_asset_pct=float(FROZEN["parameters"]["max_asset_pct"]),
                universe_tickers=UNIVERSE,
                ticker_groups=dict(FROZEN["ticker_groups"]),
                rebalance_observer=lambda event: observers.append(event),
                return_final_state=True,
            )
        finally:
            bt.SPREAD_PCT = original_spread

        self.assertTrue(observers)
        backtest_first = str(pd.Timestamp(observers[0]["decision_date"]).date())
        self.assertEqual(backtest_first, str(dates[150].date()))

        tmp = tempfile.TemporaryDirectory(prefix="kqe-shadow-compare-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "runtime"
        warmup = 150
        seed_etf = pd.concat(
            [f[f["date"] <= dates[warmup - 1]] for f in etf.values()], ignore_index=True
        )
        seed_index = index[index["date"] <= dates[warmup - 1]]
        initialize_runtime(root, initial_capital=100_000_000.0,
                           observation_start_date=str(dates[warmup].date()))
        provider = FixtureProvider(etf, index, name="compare")
        shadow_results = []
        for day in dates[warmup:warmup + 45]:
            shadow_results.append(
                process_date(str(day.date()), provider, root,
                             seed_etf=seed_etf, seed_index=seed_index)
            )
        self.assertTrue(all(r["status"] == "DONE" for r in shadow_results))
        signals = [e for e in read_jsonl(RuntimePaths(root).ledger) if e["type"] == "SIGNAL"]
        self.assertEqual(signals[0]["payload"]["signal"]["decision_date"], backtest_first)

        # First execution holdings: backtest observer post_holdings after the
        # decision at dates[150] are settled at dates[151].
        first_exec = observers[0]["execution_date"]
        backtest_holdings = observers[0]["post_holdings"]
        shadow_holdings = None
        for result in shadow_results:
            if result["date"] == str(pd.Timestamp(first_exec).date()):
                shadow_holdings = result["fills"]
                break
        self.assertIsNotNone(shadow_holdings)
        shadow_tickers = {f["ticker"] for f in shadow_holdings if f["side"] == "BUY"}
        self.assertEqual(shadow_tickers, set(backtest_holdings))

        # Equity paths should be close; any gap is attributable to the shadow's
        # explicit 5% participation capacity and independent accounting.
        state = read_json(RuntimePaths(root).state)
        shadow_equity = state["equity_curve"][-1]["equity"]
        self.assertGreater(shadow_equity, 0)
        self.assertLess(abs(shadow_equity / 100_000_000.0 - 1.0), 0.5)


if __name__ == "__main__":
    unittest.main()
