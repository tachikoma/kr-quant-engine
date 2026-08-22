"""Offline checks for lazy auto-universe initialization and mutable state use."""

from __future__ import annotations

import os
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import etf_shared
import etf_universe
import pykrx_utils
import scripts.check_strategy_freeze as check_freeze
import scripts.prefetch_universe as prefetch
import scripts.validate_etf_distributions as validate_distributions


def _fixture_result():
    config = SimpleNamespace(to_dict=lambda: {"fixture": True})
    return SimpleNamespace(
        tickers=["000003"],
        ticker_groups={"000003": "foreign_investment"},
        config=config,
        universe_sha256="fixture-sha",
    )


def check_shared_initialization() -> None:
    original = {
        "requested": etf_shared._auto_universe_requested,
        "initialized": etf_shared._universe_initialized,
        "overridden": etf_shared._universe_overridden,
        "mode": etf_shared.UNIVERSE_MODE,
        "tickers": list(etf_shared.ETF_LIST),
        "groups": dict(etf_shared.ETF_TICKER_GROUPS),
        "result": etf_shared._UNIVERSE_BUILD_RESULT,
        "load_tax": pykrx_utils.load_tax_classification,
        "taxable": pykrx_utils.get_taxable_tickers,
        "build": etf_universe.build_universe,
        "config": etf_universe.config_from_env,
    }
    calls: list[str] = []
    result = _fixture_result()
    try:
        etf_shared._auto_universe_requested = True
        etf_shared._universe_initialized = False
        etf_shared._universe_overridden = False
        etf_shared.UNIVERSE_MODE = "auto"
        etf_shared.ETF_LIST[:] = ["old"]
        pykrx_utils.load_tax_classification = lambda: calls.append("classification") or object()
        pykrx_utils.get_taxable_tickers = lambda **_kwargs: set()
        etf_universe.config_from_env = dict
        etf_universe.build_universe = lambda *_args, **_kwargs: calls.append("build") or result
        etf_shared.ensure_universe_initialized()
    finally:
        pykrx_utils.load_tax_classification = original["load_tax"]
        pykrx_utils.get_taxable_tickers = original["taxable"]
        etf_universe.build_universe = original["build"]
        etf_universe.config_from_env = original["config"]
        etf_shared._auto_universe_requested = original["requested"]
        etf_shared._universe_initialized = original["initialized"]
        etf_shared._universe_overridden = original["overridden"]
        etf_shared.UNIVERSE_MODE = original["mode"]
        etf_shared.ETF_LIST[:] = original["tickers"]
        etf_shared.ETF_TICKER_GROUPS.clear()
        etf_shared.ETF_TICKER_GROUPS.update(original["groups"])
        etf_shared._UNIVERSE_BUILD_RESULT = original["result"]
    if calls != ["classification", "build"]:
        raise AssertionError(f"unexpected lazy initialization calls: {calls}")


def check_prefetch_and_freeze_mutability() -> None:
    original_ensure = etf_shared.ensure_universe_initialized
    original_mode = etf_shared.UNIVERSE_MODE
    original_tickers = list(etf_shared.ETF_LIST)
    original_result = etf_shared._UNIVERSE_BUILD_RESULT
    calls: list[str] = []
    try:
        etf_shared.UNIVERSE_MODE = "auto"
        etf_shared.ETF_LIST[:] = ["000004"]
        etf_shared.ensure_universe_initialized = lambda: calls.append("prefetch")
        old_argv = sys.argv
        sys.argv = ["prefetch_universe.py", "--dry-run"]
        try:
            prefetch.main()
        finally:
            sys.argv = old_argv
        if calls != ["prefetch"]:
            raise AssertionError(f"prefetch did not initialize auto universe: {calls}")

        result = _fixture_result()
        etf_shared._UNIVERSE_BUILD_RESULT = result
        etf_shared.ETF_LIST[:] = ["000005"]
        etf_shared.ensure_universe_initialized = lambda: None
        payload = check_freeze.current_strategy_payload()
        if payload["universe"] != ["000005"] or payload.get("universe_sha256") != "fixture-sha":
            raise AssertionError(f"freeze payload did not observe mutable universe state: {payload}")
    finally:
        etf_shared.ensure_universe_initialized = original_ensure
        etf_shared.UNIVERSE_MODE = original_mode
        etf_shared.ETF_LIST[:] = original_tickers
        etf_shared._UNIVERSE_BUILD_RESULT = original_result


def check_distribution_validation_mutability() -> None:
    original_ensure = etf_shared.ensure_universe_initialized
    original_mode = etf_shared.UNIVERSE_MODE
    original_tickers = list(etf_shared.ETF_LIST)
    original_load = validate_distributions.load_distributions
    original_path = validate_distributions.distributions_path
    original_sha = validate_distributions.distributions_file_sha256
    calls: list[str] = []
    try:
        etf_shared.UNIVERSE_MODE = "auto"
        etf_shared.ETF_LIST[:] = ["000006"]
        etf_shared.ensure_universe_initialized = lambda: calls.append("validation")
        validate_distributions.load_distributions = lambda *_args, **_kwargs: pd.DataFrame(
            [{"ticker": "000006", "ex_date": pd.Timestamp("2024-01-02")}]
        )
        validate_distributions.distributions_path = lambda: Path("fixture.csv")
        validate_distributions.distributions_file_sha256 = lambda _path: "fixture-sha"
        output = StringIO()
        with redirect_stdout(output):
            validate_distributions.main()
        if calls != ["validation"]:
            raise AssertionError(f"distribution validation skipped auto initialization: {calls}")
        if '"000006"' not in output.getvalue() or '"universe_without_events": []' not in output.getvalue():
            raise AssertionError(f"distribution validation used stale universe: {output.getvalue()}")
    finally:
        etf_shared.ensure_universe_initialized = original_ensure
        etf_shared.UNIVERSE_MODE = original_mode
        etf_shared.ETF_LIST[:] = original_tickers
        validate_distributions.load_distributions = original_load
        validate_distributions.distributions_path = original_path
        validate_distributions.distributions_file_sha256 = original_sha


def main() -> None:
    check_shared_initialization()
    check_prefetch_and_freeze_mutability()
    check_distribution_validation_mutability()
    print("lazy universe checks passed")


if __name__ == "__main__":
    main()
