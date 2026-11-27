"""Real-data E2E for the Forward Shadow MVP.

Uses cached KRX OHLCV (read-only) from an existing checkout and runs in a
separate temporary TEST runtime.  These are HISTORICAL observations for pipeline
verification only; they are never registered as Forward performance.

Run with::

    uv run python -m unittest tests.test_forward_shadow_real -v
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from forward_shadow.engine import build_history_frame, prepare_snapshot
from forward_shadow.market import FixtureProvider, PykrxProvider, load_snapshot
from forward_shadow.report import CapturingSink
from forward_shadow.runner import initialize_runtime, process_date, runtime_status
from forward_shadow.store import RuntimePaths, read_json, read_jsonl
from forward_shadow.warmup import WarmupInput
from tests.shadow_compare import real_market
from tests.shadow_fixtures import UNIVERSE


class RealDataE2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        market = real_market()
        if market is None:
            raise unittest.SkipTest("real cache not available")
        cls.etf, cls.index, cls.listing = market
        dates = [d for d in cls.index["date"] if d.date() >= dt.date(2026, 6, 1)]
        if len(dates) < 15:
            raise unittest.SkipTest("cached history too short")
        cls.days = [str(d.date()) for d in dates]

    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        tmp = tempfile.TemporaryDirectory(prefix="kqe-shadow-real-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "runtime"
        start = self.index[self.index.date >= self.days[0]].index[0]
        first = self.index.date.iloc[start - 150]
        initialize_runtime(
            self.root,
            initial_capital=100_000_000.0,
            observation_start_date=self.days[0],
            warmup=WarmupInput(
                {t: f[f.date >= first] for t, f in self.etf.items()},
                self.index[self.index.date >= first],
                self.listing,
                "cached KRX parquet",
            ),
        )
        self.provider = FixtureProvider(self.etf, self.index, name="cached-krx")

    def test_real_data_pipeline_and_idempotency(self) -> None:
        sink = CapturingSink()
        results = [
            process_date(day, self.provider, self.root, sink=sink) for day in self.days[:12]
        ]
        self.assertTrue(all(r["status"] == "DONE" for r in results), results)
        paths = RuntimePaths(self.root)
        state = read_json(paths.state)
        self.assertEqual(len(state["snapshot_hashes"]), 12)
        for date, sha in state["snapshot_hashes"].items():
            snapshot = load_snapshot(paths, date, sha)
            self.assertTrue(snapshot.ok)
            self.assertEqual(len(snapshot.bars), 16)
        self.assertEqual(len(state["equity_curve"]), 12)
        signals = [e for e in read_jsonl(paths.ledger) if e["type"] == "SIGNAL"]
        self.assertEqual(len(signals), 1)
        self.assertTrue(signals[0]["payload"]["signal"]["targets"])
        before = len(read_jsonl(paths.ledger))
        again = process_date(self.days[11], self.provider, self.root, sink=sink)
        self.assertEqual(again["status"], "ALREADY_PROCESSED")
        self.assertEqual(len(read_jsonl(paths.ledger)), before)
        report = read_json(paths.reports / f"{self.days[11]}.json")
        self.assertEqual(report["data"]["source"], "cached-krx")
        self.assertTrue(report["data"]["ok"])
        self.assertEqual(len(sink.messages), 12)
        self.assertTrue(runtime_status(self.root)["consistent"])

    def test_ranking_matches_shared_strategy(self) -> None:
        result = process_date(self.days[0], self.provider, self.root)
        self.assertEqual(result["status"], "DONE")
        paths = RuntimePaths(self.root)
        from forward_shadow.config import EffectiveConfig

        bundle = read_json(paths.warmup)
        history = build_history_frame(bundle, [load_snapshot(paths, self.days[0])])
        snapshot = prepare_snapshot(
            history, self.days[0], EffectiveConfig(read_json(paths.manifest)),
            bundle["listing_dates"],
        )
        self.assertEqual(len(snapshot), len(UNIVERSE))
        import etf_shared

        ranked = [str(t) for t in etf_shared.rank_etfs(snapshot)["ticker"]]
        signal = read_json(paths.state)["pending_signal"]
        self.assertEqual(signal["ranked_tickers"], ranked)

    def test_live_provider_without_credentials_never_defaults_to_risk_on(self) -> None:
        """A pykrx KOSPI fetch without KRX credentials must fail closed."""
        env = {k: v for k, v in os.environ.items() if k not in ("KRX_ID", "KRX_PW")}
        with mock.patch.dict(os.environ, env, clear=True):
            result = process_date(
                self.days[0], PykrxProvider(self.tmp / "live"), self.root
            )
        self.assertEqual(result["status"], "BLOCKED_DATA")
        self.assertIn("KOSPI", " ".join(result["errors"]))
        state = read_json(RuntimePaths(self.root).state)
        self.assertIsNone(state["pending_signal"])
        self.assertEqual(state["holdings"], {})


if __name__ == "__main__":
    unittest.main()
