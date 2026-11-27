#!/usr/bin/env python3
"""Forward Shadow MVP entrypoint (independent from the live daily runner).

Examples::

    # TEST runtime from local warm-up files (never a forward record)
    uv run python run_forward_shadow.py --init --runtime-kind TEST \
        --runtime-root shadow_runtime_test --observation-start 2026-09-01 \
        --seed-etf etf.parquet --seed-index index.parquet --listing-dates listing.json

    # FORWARD runtime: warm-up and daily data come from the live provider only
    uv run python run_forward_shadow.py --init --runtime-kind FORWARD \
        --runtime-root shadow_runtime --observation-start 2026-11-30 \
        --krx-env-file ~/.config/kqe-shadow/krx.env

    uv run python run_forward_shadow.py --runtime-root shadow_runtime \
        --date 2026-11-30 --krx-env-file ~/.config/kqe-shadow/krx.env

The initial capital / observation start / rebalance anchor are UNAPPROVED TEST
VALUES.  No broker credentials are read and no order API is reachable.
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

from forward_shadow.market import FixtureProvider, PykrxProvider, scrub_secrets
from forward_shadow.runner import add_holiday, initialize_runtime, process_date, runtime_status
from forward_shadow.store import RuntimePaths
from forward_shadow.warmup import WarmupInput

# The only keys ever read from a credential file.
KRX_KEYS = ("KRX_ID", "KRX_PW")


def load_krx_credentials(path: Path) -> list[str]:
    """Load only the KRX query credentials; every other key is ignored.

    Returns the names (never the values) of the keys that were loaded.
    """
    loaded: list[str] = []
    for raw in path.expanduser().read_text(encoding="utf-8").splitlines():
        line = raw.strip().removeprefix("export ").strip()
        if "=" not in line or line.startswith("#"):
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key not in KRX_KEYS:
            continue
        if value[:1] in {'"', "'"} and value.endswith(value[0]):
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        if value:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def _load_frame(path: Path) -> pd.DataFrame:
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path, dtype={"ticker": str})


def _load_warmup(seed_etf: Path, seed_index: Path, listing: Path) -> WarmupInput:
    etf = _load_frame(seed_etf)
    etf["ticker"] = etf["ticker"].astype(str)
    return WarmupInput(
        etf_frames={t: frame for t, frame in etf.groupby("ticker")},
        index_frame=_load_frame(seed_index),
        listing_dates={
            str(k): str(v) for k, v in json.loads(listing.read_text(encoding="utf-8")).items()
        },
        source=f"files: {seed_etf.name}, {seed_index.name}, {listing.name}",
    )


def _load_fixture(path: Path) -> FixtureProvider:
    payload = json.loads(path.read_text(encoding="utf-8"))
    etf_frames = {
        str(ticker): pd.DataFrame(rows)
        for ticker, rows in payload.get("etf", {}).items()
    }
    index_frame = pd.DataFrame(payload.get("index", []))
    return FixtureProvider(etf_frames, index_frame, name=f"fixture:{path.name}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Forward Shadow MVP (no real orders)")
    parser.add_argument(
        "--runtime-root", default=os.environ.get("SHADOW_RUNTIME_ROOT", "shadow_runtime")
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--init", action="store_true", help="initialize a new independent runtime")
    action.add_argument("--status", action="store_true", help="read-only integrity verification")
    action.add_argument("--add-holiday", metavar="DATE", help="record an ad-hoc KRX closure")
    parser.add_argument("--reason", help="reason/source for --add-holiday")
    parser.add_argument("--runtime-kind", choices=("TEST", "FORWARD"), help="required for --init")
    parser.add_argument("--initial-capital", type=float, default=100_000_000.0)
    parser.add_argument("--observation-start", help="required for --init")
    parser.add_argument("--anchor-date", help="rebalance anchor (default: observation start)")
    parser.add_argument("--decision-offset", type=int, default=0)
    parser.add_argument("--seed-etf", type=Path, help="TEST --init: warm-up ETF parquet/csv")
    parser.add_argument("--seed-index", type=Path, help="TEST --init: warm-up KOSPI parquet/csv")
    parser.add_argument("--listing-dates", type=Path, help="TEST --init: {ticker: YYYYMMDD} JSON")
    parser.add_argument("--date", help="trading date to process (YYYY-MM-DD)")
    parser.add_argument("--recover", action="store_true", help="record a late day as RECOVERY")
    parser.add_argument("--provider", choices=("pykrx", "fixture"), default="pykrx")
    parser.add_argument("--fixture", type=Path, help="JSON fixture for --provider fixture")
    parser.add_argument(
        "--krx-env-file", type=Path, help="file from which only KRX_ID/KRX_PW are read"
    )
    parser.add_argument("--send-telegram", action="store_true", help="opt-in send (default: off)")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        # Every fail-closed condition ends here: report it and exit non-zero.
        print(scrub_secrets(f"{type(exc).__name__}: {exc}"), file=sys.stderr)
        return 1


def _run(argv: list[str] | None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = RuntimePaths(Path(args.runtime_root))
    seeds = (args.seed_etf, args.seed_index, args.listing_dates)
    if any(seeds) and not args.init:
        parser.error("warm-up files are accepted only by --init; the runtime copy is pinned")

    if args.status:
        report = runtime_status(args.runtime_root)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["consistent"] else 1

    if args.add_holiday:
        result = add_holiday(args.add_holiday, args.runtime_root, reason=args.reason or "")
        print(json.dumps(result, ensure_ascii=False))
        return 0

    if args.krx_env_file:
        # Must precede any pykrx import: pykrx reads the credentials at import.
        loaded = load_krx_credentials(args.krx_env_file)
        print(f"KRX credential keys loaded: {', '.join(loaded) or 'none'}", file=sys.stderr)

    if args.init:
        if not args.runtime_kind or not args.observation_start:
            parser.error("--init requires --runtime-kind and --observation-start")
        warmup = None
        provider = None
        if args.runtime_kind == "FORWARD":
            if any(seeds):
                parser.error("a FORWARD runtime does not accept warm-up files")
            paths.cache.mkdir(parents=True, exist_ok=True)
            provider = PykrxProvider(paths.cache)
        else:
            if not all(seeds):
                parser.error("TEST --init requires --seed-etf, --seed-index and --listing-dates")
            warmup = _load_warmup(*seeds)
        state = initialize_runtime(
            args.runtime_root,
            runtime_kind=args.runtime_kind,
            initial_capital=args.initial_capital,
            observation_start_date=args.observation_start,
            anchor_date=args.anchor_date,
            decision_offset_days=args.decision_offset,
            warmup=warmup,
            provider=provider,
        )
        print(json.dumps(
            {"initialized": True, "runtime_kind": args.runtime_kind, "cash": state["cash"]},
            ensure_ascii=False,
        ))
        return 0

    if not args.date:
        print("--date is required unless --init/--status/--add-holiday is given", file=sys.stderr)
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

    result = process_date(args.date, provider, args.runtime_root, recover=args.recover)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["status"] in {"DONE", "ALREADY_PROCESSED", "SKIPPED_HOLIDAY"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
