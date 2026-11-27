"""Real-data E2E for the Forward Shadow MVP.

Uses cached KRX OHLCV (read-only) from an existing checkout and runs in a
separate temporary runtime.  These are HISTORICAL observations for pipeline
verification only; they are never registered as Forward performance.

Run with::

    uv run python -m unittest tests.test_forward_shadow_real -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forward_shadow.engine import prepare_snapshot
from forward_shadow.market import FixtureProvider, PykrxProvider
from forward_shadow.report import CapturingSink
from forward_shadow.runner import initialize_runtime, process_date
from forward_shadow.store import RuntimePaths, read_json, read_jsonl
from strategy_freeze import load_frozen_strategy

UNIVERSE = [str(t) for t in load_frozen_strategy()["universe"]]
CACHE = Path(os.environ.get("SHADOW_REAL_CACHE", "/home/ubuntu/investment/kr-quant-engine/data_cache"))


def load_cached() -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for ticker in UNIVERSE:
        path = CACHE / f"{ticker}.parquet"
        if not path.exists():
            raise FileNotFoundError(path)
        frame = pd.read_parquet(path)
        frame["date"] = pd.to_datetime(frame["date"])
        frames[ticker] = frame
    index = pd.read_parquet(CACHE / "index.parquet")
    index["date"] = pd.to_datetime(index["date"])
    return frames, index


class RealDataE2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not CACHE.exists():
            raise unittest.SkipTest(f"real cache not available: {CACHE}")
        cls.etf, cls.index = load_cached()
        cls.common = sorted(
            set(cls.index["date"]).intersection(*(set(f["date"]) for f in cls.etf.values()))
        )
        if len(cls.common) < 170:
            raise unittest.SkipTest("cached history too short for warm-up")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="kqe-shadow-real-")
        self.root = Path(self.tmp.name) / "runtime"
        self.warmup = 150
        self.seed_etf = pd.concat(
            [f[f["date"] <= self.common[self.warmup - 1]] for f in self.etf.values()],
            ignore_index=True,
        )
        self.seed_index = self.index[self.index["date"] <= self.common[self.warmup - 1]]
        self.provider = FixtureProvider(self.etf, self.index, name="cached-krx")
        initialize_runtime(
            self.root,
            initial_capital=100_000_000.0,
            observation_start_date=str(self.common[self.warmup].date()),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_real_data_pipeline_and_idempotency(self) -> None:
        sink = CapturingSink()
        results = []
        for day in self.common[self.warmup:self.warmup + 12]:
            results.append(
                process_date(
                    str(day.date()), self.provider, self.root,
                    seed_etf=self.seed_etf, seed_index=self.seed_index, sink=sink,
                )
            )
        self.assertTrue(all(r["status"] == "DONE" for r in results), results)
        paths = RuntimePaths(self.root)
        snapshots = sorted(paths.snapshots.glob("*.json"))
        self.assertEqual(len(snapshots), 12)
        for snap in snapshots:
            payload = read_json(snap)
            self.assertTrue(payload["validation"]["ok"])
            self.assertEqual(len(payload["bars"]), 16)
            self.assertGreater(payload["index_close"], 0)
        state = read_json(paths.state)
        self.assertEqual(len(state["equity_curve"]), 12)
        signals = [e for e in read_jsonl(paths.ledger) if e["type"] == "SIGNAL"]
        self.assertEqual(len(signals), 1)
        self.assertTrue(signals[0]["payload"]["signal"]["targets"])
        self.assertTrue(state["equity_curve"][-1]["equity"] > 0)
        # Same input replayed on the same date must not create a second event.
        before = len(read_jsonl(paths.ledger))
        again = process_date(
            str(self.common[self.warmup + 11].date()), self.provider, self.root,
            seed_etf=self.seed_etf, seed_index=self.seed_index, sink=sink,
        )
        self.assertEqual(again["status"], "ALREADY_PROCESSED")
        self.assertEqual(len(read_jsonl(paths.ledger)), before)
        report = read_json(paths.reports / f"{self.common[self.warmup + 11].date()}.json")
        self.assertEqual(report["data"]["source"], "cached-krx")
        self.assertFalse(report["data"]["ok"] is False)
        self.assertTrue(sink.messages)

    def test_ranking_matches_shared_strategy(self) -> None:
        day = self.common[self.warmup]
        manifest = read_json(RuntimePaths(self.root).manifest)
        from forward_shadow.config import EffectiveConfig
        config = EffectiveConfig(manifest)
        history = pd.concat(
            [self.seed_etf] + [f[f["date"] == day] for f in self.etf.values()],
            ignore_index=True,
        )
        snapshot = prepare_snapshot(history, str(day.date()), config)
        self.assertEqual(len(snapshot), 16)
        self.assertTrue(snapshot["ret_120"].notna().all())
        import etf_shared

        ranked_shared = etf_shared.rank_etfs(snapshot)
        ranked_from_shared = [str(t) for t in ranked_shared["ticker"]]
        result = process_date(
            str(day.date()), self.provider, self.root,
            seed_etf=self.seed_etf, seed_index=self.seed_index,
        )
        self.assertEqual(result["status"], "DONE")
        signal = read_json(RuntimePaths(self.root).state)["pending_signal"]
        self.assertEqual([r["ticker"] for r in signal["ranked"]], ranked_from_shared)
        self.assertEqual(signal["targets"][:2], ranked_from_shared[:2])

    def test_live_index_failure_never_defaults_to_risk_on(self) -> None:
        """pykrx KOSPI fetch without KRX credentials must fail closed."""
        day = self.common[-1]
        provider = PykrxProvider(Path(self.tmp.name) / "live-cache")
        result = process_date(str(day.date()), provider, self.root)
        self.assertEqual(result["status"], "BLOCKED_DATA")
        self.assertIn("KOSPI", " ".join(result["errors"]))
        state = read_json(RuntimePaths(self.root).state)
        self.assertIsNone(state["pending_signal"])
        self.assertEqual(state["holdings"], {})


if __name__ == "__main__":
    unittest.main()
