#!/usr/bin/env python3
"""Forward Shadow MVP entrypoint (independent from the live daily runner).

Examples::

    uv run python run_forward_shadow.py --init \
        --runtime-root shadow_runtime \
        --initial-capital 100000000 \
        --observation-start 2026-11-30

    uv run python run_forward_shadow.py --runtime-root shadow_runtime \
        --date 2026-11-30 --provider pykrx

The initial capital / observation start / rebalance anchor are UNAPPROVED TEST
VALUES.  No live broker credentials are read and no order API is reachable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from forward_shadow.market import FixtureProvider, PykrxProvider
from forward_shadow.runner import initialize_runtime, process_date
from forward_shadow.store import RuntimePaths, read_json


def _load_fixture(path: Path) -> FixtureProvider:
    payload = json.loads(path.read_text(encoding="utf-8"))
    etf_frames = {
        str(ticker): pd.DataFrame(rows)
        for ticker, rows in payload.get("etf", {}).items()
    }
    index_frame = pd.DataFrame(payload.get("index", []))
    return FixtureProvider(etf_frames, index_frame, name=f"fixture:{path.name}")


def _load_frame(path: Path | None) -> pd.DataFrame | None:
    if path is None:
        return None
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Forward Shadow MVP (no real orders)")
    parser.add_argument("--runtime-root", default=os.environ.get("SHADOW_RUNTIME_ROOT", "shadow_runtime"))
    parser.add_argument("--init", action="store_true", help="initialize a new independent runtime")
    parser.add_argument("--initial-capital", type=float, default=100_000_000.0)
    parser.add_argument("--observation-start", default="2026-11-30")
    parser.add_argument("--decision-offset", type=int, default=0)
    parser.add_argument("--date", help="trading date to process (YYYY-MM-DD)")
    parser.add_argument("--provider", choices=("pykrx", "fixture"), default="pykrx")
    parser.add_argument("--fixture", type=Path, help="JSON fixture for --provider fixture")
    parser.add_argument("--seed-etf", type=Path, help="warm-up ETF OHLCV parquet/csv")
    parser.add_argument("--seed-index", type=Path, help="warm-up KOSPI index parquet/csv")
    parser.add_argument("--send-telegram", action="store_true", help="opt-in send (default: off)")
    parser.add_argument("--status", action="store_true", help="print manifest/state/integrity")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = RuntimePaths(Path(args.runtime_root))

    if args.init:
        state = initialize_runtime(
            args.runtime_root,
            initial_capital=args.initial_capital,
            observation_start_date=args.observation_start,
            decision_offset_days=args.decision_offset,
        )
        print(json.dumps({"initialized": True, "cash": state["cash"]}, ensure_ascii=False))
        return 0

    if args.status:
        manifest = read_json(paths.manifest)
        state = read_json(paths.state)
        from forward_shadow.store import ShadowStore

        print(json.dumps(
            {
                "manifest": {
                    "freeze_sha256": manifest["freeze_sha256"],
                    "code_sha256": manifest["code_sha256"],
                    "approval_status": manifest["approval_status"],
                },
                "state": {
                    "last_processed_date": state["last_processed_date"],
                    "cash": state["cash"],
                    "holdings": state["holdings"],
                    "forward_day_index": state["forward_day_index"],
                    "pending_signal": state["pending_signal"],
                },
                "integrity": ShadowStore(paths).verify_integrity(),
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0

    if not args.date:
        print("--date is required unless --init/--status is given", file=sys.stderr)
        return 2

    if args.send_telegram:
        os.environ["SHADOW_TELEGRAM_SEND"] = "1"

    if args.provider == "fixture":
        if args.fixture is None:
            print("--fixture is required for --provider fixture", file=sys.stderr)
            return 2
        provider = _load_fixture(args.fixture)
    else:
        provider = PykrxProvider(paths.cache)

    result = process_date(
        args.date,
        provider,
        args.runtime_root,
        seed_etf=_load_frame(args.seed_etf),
        seed_index=_load_frame(args.seed_index),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["status"] in {"DONE", "ALREADY_PROCESSED", "SKIPPED_HOLIDAY"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
