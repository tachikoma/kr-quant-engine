"""Deterministic Phase 0 integrity checks for the ETF backtest."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import sys
import types
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from etf_corporate_actions import (
    CorporateAction,
    CorporateActionLedger,
    CoverageManifest,
    EventType,
)

# run_etf_backtest imports pykrx at module load.  Blank credentials for that
# import so a locally configured environment cannot trigger authentication.
_import_env = {key: os.environ.get(key) for key in ("KRX_ID", "KRX_PW")}
_import_modules = {key: sys.modules.get(key) for key in ("pykrx", "pykrx.stock")}
try:
    os.environ["KRX_ID"] = ""
    os.environ["KRX_PW"] = ""
    _stub_pykrx = types.ModuleType("pykrx")
    _stub_stock = types.ModuleType("pykrx.stock")
    setattr(_stub_pykrx, "stock", _stub_stock)  # noqa: B010 - install an import-only stub
    sys.modules["pykrx"] = _stub_pykrx
    sys.modules["pykrx.stock"] = _stub_stock
    import run_etf_backtest as backtest
finally:
    for _key, _value in _import_env.items():
        if _value is None:
            os.environ.pop(_key, None)
        else:
            os.environ[_key] = _value
    for _key, _value in _import_modules.items():
        if _value is None:
            sys.modules.pop(_key, None)
        else:
            sys.modules[_key] = _value

_etf_shared = importlib.import_module("etf_shared")
BUY_FEE_PCT = _etf_shared.BUY_FEE_PCT
SELL_FEE_PCT = _etf_shared.SELL_FEE_PCT


INITIAL_CASH = 1_000_000.0
DATES = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
NAME_MAP = {"000001": "Fixture Alpha", "000002": "Fixture Beta"}


def assert_close(actual: float, expected: float, message: str, tolerance: float = 1e-8) -> None:
    if abs(float(actual) - float(expected)) > tolerance:
        raise AssertionError(f"{message}: expected={expected}, actual={actual}")


def _json_value(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"provenance input contains non-finite numeric value: {value!r}")
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "item"):
        return _json_value(value.item())
    return value


def _frame_records(frame: pd.DataFrame, sort_columns: list[str]) -> list[dict[str, Any]]:
    ordered = frame.sort_values(sort_columns, kind="mergesort").reset_index(drop=True)
    return _json_value(ordered.to_dict(orient="records"))


def canonical_digest(
    price_data: pd.DataFrame,
    index_data: pd.DataFrame,
    strategy_config: dict[str, Any],
    run_arguments: dict[str, Any],
    distribution_data: pd.DataFrame,
    distribution_source: str,
) -> str:
    """Hash local inputs only; this is metadata determinism, not persistence."""
    distribution_json = json.dumps(
        _frame_records(distribution_data, ["ticker", "ex_date"]),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload = {
        "fixture_data": _frame_records(price_data, ["date", "ticker"]),
        "index_data": _frame_records(index_data, ["date"]),
        "strategy_config": strategy_config,
        "run_arguments": run_arguments,
        "distribution": {
            "sha256": hashlib.sha256(distribution_json.encode("utf-8")).hexdigest(),
            "source": distribution_source,
        },
    }
    canonical = json.dumps(
        _json_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_price_fixture(
    tickers: list[str],
    opens: dict[str, list[float]],
    closes: dict[str, list[float]],
    distributions: dict[tuple[str, str], float] | None = None,
    momentum: dict[str, tuple[float, float]] | None = None,
) -> pd.DataFrame:
    distributions = distributions or {}
    momentum = momentum or {}
    rows: list[dict[str, Any]] = []
    for ticker in tickers:
        ret_60, ret_120 = momentum.get(ticker, (1.0, 1.0))
        for position, dt in enumerate(DATES):
            rows.append(
                {
                    "date": dt,
                    "ticker": ticker,
                    "open": opens[ticker][position],
                    "close": closes[ticker][position],
                    "distribution": distributions.get((ticker, dt.strftime("%Y-%m-%d")), 0.0),
                    "ret_60": ret_60,
                    "ret_120": ret_120,
                    "trend_ok": True,
                    "liquidity_ok": True,
                    "listing_ok": True,
                    "deviation_ok": True,
                }
            )
    return pd.DataFrame(rows)


def build_index_fixture() -> pd.DataFrame:
    return pd.DataFrame({"date": DATES, "close": [100.0, 101.0, 102.0]})


def effective_strategy_config(slippage: float) -> dict[str, Any]:
    return {
        "rebalance_step_days": int(backtest.REBALANCE_STEP_DAYS),
        "sell_rank_buffer": int(backtest.ETF_SELL_RANK_BUFFER),
        "buy_fee_pct": float(BUY_FEE_PCT),
        "sell_fee_pct": float(SELL_FEE_PCT),
        "spread_pct": float(backtest.SPREAD_PCT),
        "slippage_pct": float(slippage),
        "distribution_tax_pct": float(
            backtest.parse_pct_env("ETF_DISTRIBUTION_TAX_PCT", 0.0)
        ),
        "sell_tax_pct": float(backtest.ETF_TAXABLE_SELL_TAX_PCT),
        "taxable_tickers": sorted(map(str, backtest.TAXABLE_ETF_TICKERS)),
    }


def effective_run_arguments(
    price_data: pd.DataFrame,
    initial_state: dict[str, Any],
    max_positions: int,
    initial_cash: float,
    slippage: float,
) -> dict[str, Any]:
    return {
        "initial_cash": initial_cash,
        "common_dates": [dt.isoformat() for dt in DATES],
        "index_argument": "build_index_fixture",
        "use_market_filter": False,
        "max_positions": max_positions,
        "slippage": slippage,
        "risk_off_liquidate": True,
        "max_asset_pct": 0.0,
        "target_weight_rebalance": False,
        "rebalance_band_pct": 0.0,
        "trim_overweight_positions": False,
        "exit_check_days": 0,
        "trailing_stop_pct": 0.0,
        "portfolio_trailing_stop_pct": 0.0,
        "universe_tickers": sorted(price_data["ticker"].astype(str).unique()),
        "initial_state": initial_state,
        "return_final_state": True,
    }


def run_fixture(
    price_data: pd.DataFrame,
    initial_state: dict[str, Any],
    max_positions: int,
    *,
    initial_cash: float = INITIAL_CASH,
    slippage: float = 0.0,
):
    tickers = sorted(price_data["ticker"].astype(str).unique())
    result = backtest.run_etf_strategy(
        initial_cash=initial_cash,
        common_dates=list(DATES),
        index_df=build_index_fixture(),
        use_market_filter=False,
        max_positions=max_positions,
        slippage=slippage,
        risk_off_liquidate=True,
        price_data=price_data,
        max_asset_pct=0.0,
        target_weight_rebalance=False,
        rebalance_band_pct=0.0,
        trim_overweight_positions=False,
        exit_check_days=0,
        trailing_stop_pct=0.0,
        portfolio_trailing_stop_pct=0.0,
        universe_tickers=tickers,
        initial_state=initial_state,
        return_final_state=True,
    )
    return cast(tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]], result)


def strict_ledger(
    events: list[CorporateAction],
    tickers: list[str],
    *,
    start: date = date(2024, 1, 2),
    end: date = date(2024, 1, 4),
) -> CorporateActionLedger:
    manifest = CoverageManifest(
        1,
        "fixture.csv",
        "fixture-ledger",
        start,
        end,
        tuple(sorted(tickers)),
        (),
        "VERIFIED",
        "offline fixture",
    )
    return CorporateActionLedger(tuple(events), manifest, "fixture-ledger", ())


def strict_action(
    event_id: str,
    ticker: str,
    event_type: EventType,
    event_date: date,
    **kwargs: Any,
) -> CorporateAction:
    return CorporateAction(
        event_id,
        ticker,
        event_type,
        event_date,
        source_document_id="fixture-doc",
        source_document="fixture-doc",
        source_url="https://example.test/fixture",
        source_sha256="a" * 64,
        **kwargs,
    )


def run_strict_fixture(
    price_data: pd.DataFrame,
    ledger: CorporateActionLedger,
    initial_state: dict[str, Any],
    *,
    max_positions: int = 1,
    common_dates: list[pd.Timestamp] | None = None,
    ticker_groups: dict[str, str] | None = None,
    exit_check_days: int = 0,
    trailing_stop_pct: float = 0.0,
    portfolio_trailing_stop_pct: float = 0.0,
):
    dates = common_dates or list(DATES)
    result = backtest.run_etf_strategy(
        initial_cash=float(initial_state.get("cash", 0.0)),
        common_dates=dates,
        index_df=pd.DataFrame({"date": dates, "close": [100.0] * len(dates)}),
        use_market_filter=False,
        max_positions=max_positions,
        slippage=0.0,
        price_data=price_data,
        max_asset_pct=0.0,
        universe_tickers=sorted(price_data["ticker"].astype(str).unique()),
        initial_state=initial_state,
        exit_check_days=exit_check_days,
        trailing_stop_pct=trailing_stop_pct,
        portfolio_trailing_stop_pct=portfolio_trailing_stop_pct,
        approval_strict=True,
        corporate_action_ledger=ledger,
        ticker_groups=ticker_groups,
    )
    return cast(tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]], result)


def check_t_plus_one_and_accounting() -> None:
    fixture = build_price_fixture(
        ["000001"],
        {"000001": [100.0, 250.0, 260.0]},
        {"000001": [100.0, 255.0, 300.0]},
    )
    equity, trades, _ = run_fixture(fixture, {"cash": INITIAL_CASH, "holdings": {}}, 1)
    if trades.empty:
        raise AssertionError("t+1 fixture produced no rebalance trade")

    trade = trades.iloc[0]
    decision_close = float(fixture.loc[fixture["date"] == DATES[0], "close"].iloc[0])
    next_open = float(fixture.loc[fixture["date"] == DATES[1], "open"].iloc[0])
    if pd.Timestamp(trade["date"]) != DATES[1]:
        raise AssertionError(f"rebalance executed on {trade['date']}, expected {DATES[1]}")
    if abs(next_open - decision_close) / decision_close < 0.5:
        raise AssertionError("t+1 fixture prices are not materially different")
    expected_reference = next_open * (1 + backtest.SPREAD_PCT / 2)
    assert_close(float(trade["price"]), expected_reference, "trade used next-day open reference")
    if abs(float(trade["price"]) - decision_close) < 1e-6:
        raise AssertionError("trade price incorrectly used decision-day close")

    expected_cash = INITIAL_CASH
    quantities: dict[str, int] = {}
    for row in trades.itertuples(index=False):
        cash_flow = float(row.cash_flow)
        expected_cash += cash_flow
        assert_close(float(row.cash_after), expected_cash, "trade cash_after reconciliation")
        ticker = str(row.ticker)
        qty = int(row.qty)
        if row.side == "BUY":
            quantities[ticker] = quantities.get(ticker, 0) + qty
        elif row.side == "SELL":
            quantities[ticker] = quantities.get(ticker, 0) - qty
            if quantities[ticker] < 0:
                raise AssertionError(f"trade log sold more than held for {ticker}")
        else:
            raise AssertionError(f"unknown trade side in emitted log: {row.side}")

    final_row = equity.iloc[-1]
    final_date = pd.Timestamp(final_row["date"])
    final_closes = fixture[fixture["date"] == final_date].set_index("ticker")["close"]
    reconstructed_market_value = sum(
        qty * float(final_closes[ticker]) for ticker, qty in quantities.items() if qty
    )
    reconstructed_equity = expected_cash + reconstructed_market_value
    assert_close(float(final_row["cash"]), expected_cash, "final cash from trade log")
    assert_close(
        float(final_row["equity"]),
        reconstructed_equity,
        "final equity equals cash plus final-close positions",
    )


def check_buy_sell_accounting() -> None:
    tickers = ["000001", "000002", "000003", "000004", "000005"]
    fixture = build_price_fixture(
        tickers,
        {
            "000001": [100.0, 200.0, 210.0],
            "000002": [100.0, 150.0, 155.0],
            "000003": [100.0, 140.0, 145.0],
            "000004": [100.0, 130.0, 135.0],
            "000005": [100.0, 80.0, 85.0],
        },
        {
            "000001": [100.0, 205.0, 220.0],
            "000002": [100.0, 152.0, 156.0],
            "000003": [100.0, 142.0, 146.0],
            "000004": [100.0, 132.0, 136.0],
            "000005": [100.0, 81.0, 86.0],
        },
        momentum={
            "000001": (5.0, 5.0),
            "000002": (4.0, 4.0),
            "000003": (3.0, 3.0),
            "000004": (2.0, 2.0),
            "000005": (1.0, 1.0),
        },
    )
    initial_state = {
        "cash": 50_000.0,
        "holdings": {"000005": 10},
        "holding_cost_basis": {"000005": 50.0},
    }
    equity, trades, final_state = run_fixture(
        fixture,
        initial_state,
        1,
        initial_cash=50_000.0,
        slippage=0.001,
    )
    side_counts = trades["side"].value_counts().to_dict()
    if side_counts.get("SELL") != 1 or side_counts.get("BUY") != 1:
        raise AssertionError(f"initial-holding fixture did not produce SELL and BUY: {trades}")
    sell = trades.loc[trades["side"] == "SELL"].iloc[0]
    buy = trades.loc[trades["side"] == "BUY"].iloc[0]
    if str(sell["ticker"]) != "000005" or str(buy["ticker"]) != "000001":
        raise AssertionError(f"unexpected replacement trades: sell={sell}, buy={buy}")
    if pd.Timestamp(sell["date"]) != DATES[1] or pd.Timestamp(buy["date"]) != DATES[1]:
        raise AssertionError("replacement SELL and BUY were not both executed at t+1")

    spread = float(backtest.SPREAD_PCT)
    slippage = 0.001
    tax_rate = float(backtest.ETF_TAXABLE_SELL_TAX_PCT)
    sell_open = float(fixture.loc[fixture["ticker"] == "000005", "open"].iloc[1])
    sell_reference = sell_open * (1 - spread / 2)
    sell_price_after_slippage = sell_reference * (1 - slippage)
    sell_qty = int(initial_state["holdings"]["000005"])
    gross_sell = sell_qty * sell_price_after_slippage
    taxable_gain = max(0.0, gross_sell - sell_qty * initial_state["holding_cost_basis"]["000005"])
    expected_tax = taxable_gain * tax_rate
    expected_proceeds = gross_sell * (1 - SELL_FEE_PCT) - expected_tax
    assert_close(float(sell["price"]), sell_reference, "SELL next-open reference")
    assert_close(float(sell["net_value"]), expected_proceeds, "independent SELL proceeds")
    assert_close(float(sell["cash_flow"]), expected_proceeds, "SELL cash flow")
    assert_close(float(sell["estimated_tax"]), expected_tax, "SELL tax")

    cash_after_sell = initial_state["cash"] + expected_proceeds
    buy_open = float(fixture.loc[fixture["ticker"] == "000001", "open"].iloc[1])
    buy_reference = buy_open * (1 + spread / 2)
    unit_buy_cost = buy_reference * (1 + slippage) * (1 + BUY_FEE_PCT)
    expected_buy_qty = int(cash_after_sell // unit_buy_cost)
    expected_buy_cost = expected_buy_qty * unit_buy_cost
    assert_close(float(buy["price"]), buy_reference, "BUY next-open reference")
    if int(buy["qty"]) != expected_buy_qty or expected_buy_qty <= 0:
        raise AssertionError(f"BUY quantity mismatch: expected={expected_buy_qty}, actual={buy['qty']}")
    assert_close(float(buy["net_value"]), expected_buy_cost, "independent BUY cost")
    assert_close(float(buy["cash_flow"]), -expected_buy_cost, "BUY cash flow")

    reconstructed_cash = float(initial_state["cash"])
    reconstructed_holdings = {str(ticker): int(qty) for ticker, qty in initial_state["holdings"].items()}
    for row in trades.itertuples(index=False):
        reconstructed_cash += float(row.cash_flow)
        assert_close(float(row.cash_after), reconstructed_cash, "replacement cash_after")
        ticker = str(row.ticker)
        if row.side == "BUY":
            reconstructed_holdings[ticker] = reconstructed_holdings.get(ticker, 0) + int(row.qty)
        else:
            reconstructed_holdings[ticker] = reconstructed_holdings.get(ticker, 0) - int(row.qty)
    reconstructed_holdings = {
        ticker: qty for ticker, qty in reconstructed_holdings.items() if qty
    }
    expected_cash = cash_after_sell - expected_buy_cost
    expected_holdings = {"000001": expected_buy_qty}
    if reconstructed_holdings != expected_holdings:
        raise AssertionError(f"trade-log holdings mismatch: {reconstructed_holdings}")
    assert_close(reconstructed_cash, expected_cash, "trade-log reconstructed cash")
    if final_state["holdings"] != reconstructed_holdings:
        raise AssertionError(f"final state holdings mismatch: {final_state['holdings']}")
    assert_close(float(final_state["cash"]), expected_cash, "final state cash")

    final_close = float(fixture.loc[fixture["ticker"] == "000001", "close"].iloc[2])
    expected_market_value = expected_buy_qty * final_close
    expected_equity = expected_cash + expected_market_value
    final_row = equity.iloc[-1]
    assert_close(float(final_row["cash"]), expected_cash, "equity final cash")
    assert_close(float(final_row["market_value"]), expected_market_value, "final market value")
    assert_close(float(final_row["equity"]), expected_equity, "final equity")


def check_distribution_ordering() -> None:
    fixture = build_price_fixture(
        ["000001", "000002"],
        {"000001": [10.0, 11.0, 12.0], "000002": [20.0, 21.0, 22.0]},
        {"000001": [10.0, 11.0, 12.0], "000002": [20.0, 21.0, 22.0]},
        {("000001", "2024-01-03"): 2.0, ("000002", "2024-01-03"): 7.0},
        momentum={"000001": (2.0, 2.0), "000002": (1.0, 1.0)},
    )
    initial_holdings = {"000001": 10}
    equity, trades, final_state = run_fixture(
        fixture,
        {"cash": 10_000.0, "holdings": initial_holdings, "holding_cost_basis": {"000001": 10.0}},
        2,
        initial_cash=10_000.0,
    )
    day_one = equity.loc[equity["date"] == DATES[1]].iloc[0]
    entitlement = 10 * 2.0
    assert_close(float(day_one["distribution_cash"]), entitlement, "pre-existing holding distribution")

    buys = trades[(trades["date"] == DATES[1]) & (trades["side"] == "BUY")].copy()
    acquired_a = int(buys.loc[buys["ticker"].astype(str) == "000001", "qty"].sum())
    acquired_b = int(buys.loc[buys["ticker"].astype(str) == "000002", "qty"].sum())
    if acquired_a <= 0:
        raise AssertionError("distribution fixture did not make a same-ticker incremental purchase")
    if acquired_b <= 0:
        raise AssertionError("distribution fixture did not acquire the second ticker at next open")
    if final_state["holdings"].get("000001") != 10 + acquired_a:
        raise AssertionError("final same-ticker quantity omitted the incremental purchase")
    last_trade = trades.iloc[-1]
    if pd.Timestamp(last_trade["date"]) != DATES[1]:
        raise AssertionError("distribution fixture unexpectedly traded after the ex-date")
    expected_final_cash = float(last_trade["cash_after"]) + entitlement
    assert_close(float(final_state["cash"]), expected_final_cash, "cash plus pre-existing entitlement")
    assert_close(float(day_one["cash"]), expected_final_cash, "equity cash after distribution")


def check_legacy_mode_is_ledger_independent() -> None:
    fixture = build_price_fixture(
        ["000001"],
        {"000001": [10.0, 11.0, 12.0]},
        {"000001": [10.0, 11.0, 12.0]},
        momentum={"000001": (2.0, 2.0)},
    )
    initial_state = {"cash": 1000.0, "holdings": {"000001": 10}, "holding_cost_basis": {"000001": 10.0}}
    empty_ledger = strict_ledger([], ["000001"])
    baseline = run_fixture(fixture, initial_state, 1, initial_cash=1000.0)
    explicit_legacy = backtest.run_etf_strategy(
        initial_cash=1000.0,
        common_dates=list(DATES),
        index_df=build_index_fixture(),
        use_market_filter=False,
        max_positions=1,
        slippage=0.0,
        price_data=fixture,
        max_asset_pct=0.0,
        universe_tickers=["000001"],
        initial_state=initial_state,
        return_final_state=True,
        approval_strict=False,
        corporate_action_ledger=empty_ledger,
    )
    explicit = cast(tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]], explicit_legacy)
    pd.testing.assert_frame_equal(baseline[0], explicit[0])
    pd.testing.assert_frame_equal(baseline[1], explicit[1])
    if baseline[2] != explicit[2]:
        raise AssertionError("supplying a ledger changed legacy final state")


def check_strict_causal_entitlement_and_payment() -> None:
    fixture = build_price_fixture(
        ["000001", "000002"],
        {
            "000001": [10.0, 10.0, 10.0],
            "000002": [20.0, 20.0, 20.0],
        },
        {
            "000001": [10.0, 10.0, 10.0],
            "000002": [20.0, 20.0, 20.0],
        },
        momentum={"000001": (2.0, 2.0), "000002": (1.0, 1.0)},
    )
    action = strict_action(
        "dist-causal",
        "000001",
        EventType.CASH_DISTRIBUTION,
        date(2024, 1, 3),
        record_date=date(2024, 1, 3),
        ex_date=date(2024, 1, 3),
        payment_date=date(2024, 1, 4),
        cash_amount=Decimal(2),
        currency="KRW",
    )
    # No holding before 2024-01-03: the 2024-01-03 next-open buy is not entitled.
    _, _, no_holding_state = run_strict_fixture(
        fixture,
        strict_ledger([action], ["000001", "000002"]),
        {"cash": 1000.0, "holdings": {}},
        max_positions=1,
    )
    if no_holding_state["pending_receivables"]:
        raise AssertionError("same-day next-open buy created an unearned receivable")

    # A next-open sell does not erase the pre-order entitlement snapshot.
    sell_fixture = build_price_fixture(
        ["000001", "000002"],
        {
            "000001": [10.0, 10.0, 10.0],
            "000002": [20.0, 20.0, 20.0],
        },
        {
            "000001": [10.0, 10.0, 10.0],
            "000002": [20.0, 20.0, 20.0],
        },
        momentum={"000001": (1.0, 1.0), "000002": (2.0, 2.0)},
    )
    original_sell_rank_buffer = backtest.ETF_SELL_RANK_BUFFER
    backtest.ETF_SELL_RANK_BUFFER = 0
    try:
        result, trades, state = run_strict_fixture(
            sell_fixture,
            strict_ledger([action], ["000001", "000002"]),
            {"cash": 0.0, "holdings": {"000001": 10}, "holding_cost_basis": {"000001": 10.0}},
            max_positions=1,
        )
    finally:
        backtest.ETF_SELL_RANK_BUFFER = original_sell_rank_buffer
    if trades.empty or not (trades["side"] == "SELL").any():
        raise AssertionError("causal entitlement fixture did not execute the next-open sell")
    day_after_ex = result.loc[result["date"] == DATES[1]].iloc[0]
    assert_close(float(day_after_ex["receivables"]), 20.0, "pre-order entitlement receivable")
    paid_row = result.loc[result["date"] == DATES[2]].iloc[0]
    assert_close(float(paid_row["distribution_cash"]), 20.0, "payment cash on eligible strategy date")
    if state["pending_receivables"][0].paid is not True:
        raise AssertionError("distribution payment was not marked paid")

    # ex_date before record_date is deferred until record-date evidence exists.
    ordinary = strict_action(
        "dist-ex-before-record",
        "000001",
        EventType.CASH_DISTRIBUTION,
        date(2024, 1, 2),
        record_date=date(2024, 1, 3),
        ex_date=date(2024, 1, 2),
        payment_date=date(2024, 1, 4),
        cash_amount=Decimal(1),
        currency="KRW",
    )
    _, _, ordinary_state = run_strict_fixture(
        sell_fixture,
        strict_ledger([ordinary], ["000001", "000002"]),
        {"cash": 0.0, "holdings": {"000001": 10}, "holding_cost_basis": {"000001": 10.0}},
        max_positions=1,
    )
    if ordinary_state["approval_report"].status != "APPROVED":
        raise AssertionError("normal ex_date < record_date input was incorrectly blocked")

    weekend_dates = list(pd.to_datetime(["2024-01-05", "2024-01-08"]))
    weekend_fixture = pd.DataFrame(
        [
            {
                "date": dt,
                "ticker": "000001",
                "open": 10.0,
                "close": 10.0,
                "ret_60": 1.0,
                "ret_120": 1.0,
                "trend_ok": True,
                "liquidity_ok": True,
                "listing_ok": True,
                "deviation_ok": True,
            }
            for dt in weekend_dates
        ]
    )
    weekend_action = strict_action(
        "dist-weekend",
        "000001",
        EventType.CASH_DISTRIBUTION,
        date(2024, 1, 5),
        record_date=date(2024, 1, 5),
        ex_date=date(2024, 1, 5),
        payment_date=date(2024, 1, 6),
        cash_amount=Decimal(2),
        currency="KRW",
    )
    weekend_result, _, _ = run_strict_fixture(
        weekend_fixture,
        strict_ledger([weekend_action], ["000001"], start=date(2024, 1, 5), end=date(2024, 1, 8)),
        {"cash": 0.0, "holdings": {"000001": 10}, "holding_cost_basis": {"000001": 10.0}},
        common_dates=weekend_dates,
    )
    weekend_row = weekend_result.iloc[-1]
    if pd.Timestamp(weekend_row["date"]).date() != date(2024, 1, 8):
        raise AssertionError("weekend payment changed the source/strategy row date")
    assert_close(float(weekend_row["cash"]), 20.0, "weekend payment cash on next strategy date")
    assert_close(float(weekend_row["receivables"]), 0.0, "weekend payment receivable clearing")


def check_legacy_ticker_failure_is_isolated() -> None:
    original = {
        name: getattr(backtest, name)
        for name in (
            "get_price",
            "get_listing_dates",
            "load_distributions",
            "add_distributions",
            "add_liquidity_flag",
            "add_listing_flag",
            "add_deviation_flag",
            "add_price_basis_columns",
        )
    }
    calls: list[str] = []

    def fake_get_price(ticker: str, *, listing_dates: dict[str, Any]) -> pd.DataFrame:
        calls.append(ticker)
        if ticker == "000001":
            raise RuntimeError("fixture fetch failure")
        return pd.DataFrame(
            {
                "date": DATES,
                "ticker": ticker,
                "open": [10.0, 10.0, 10.0],
                "close": [10.0, 10.0, 10.0],
            }
        )

    try:
        backtest.get_price = fake_get_price
        backtest.get_listing_dates = lambda **_kwargs: {}
        backtest.load_distributions = lambda **_kwargs: pd.DataFrame()
        backtest.add_distributions = lambda frame, _distributions: frame
        backtest.add_liquidity_flag = lambda frame: frame.assign(liquidity_ok=True)
        backtest.add_listing_flag = lambda frame, _dates: frame.assign(listing_ok=True)
        backtest.add_deviation_flag = lambda frame: frame.assign(deviation_ok=True)
        backtest.add_price_basis_columns = lambda frame: frame.assign(close_adj=frame["close"])
        loaded = backtest.load_etf_price(["000001", "000002"])
    finally:
        for name, value in original.items():
            setattr(backtest, name, value)

    if calls != ["000001", "000002"]:
        raise AssertionError(f"legacy fetch did not continue after one ticker failed: {calls}")
    if set(loaded["ticker"].astype(str)) != {"000002"}:
        raise AssertionError("legacy fetch failure polluted or aborted the next ticker")


def check_strict_lifecycle_and_final_valuation() -> None:
    fixture = build_price_fixture(
        ["000001"],
        {"000001": [10.0, 10.0, 10.0]},
        {"000001": [10.0, 10.0, 10.0]},
        momentum={"000001": (1.0, 1.0)},
    )
    split = strict_action(
        "split",
        "000001",
        EventType.SPLIT,
        date(2024, 1, 3),
        ratio_num=2,
        ratio_den=1,
    )
    _, _, split_state = run_strict_fixture(
        fixture,
        strict_ledger([split], ["000001"]),
        {"cash": 0.0, "holdings": {"000001": 10}, "holding_cost_basis": {"000001": 10.0}},
    )
    if split_state["holdings"].get("000001") != 20:
        raise AssertionError("strict split did not transform the held quantity")

    suspension = strict_action(
        "suspend",
        "000001",
        EventType.SUSPENSION_START,
        date(2024, 1, 3),
    )
    original_step = backtest.REBALANCE_STEP_DAYS
    backtest.REBALANCE_STEP_DAYS = 1
    try:
        _, _, suspended_state = run_strict_fixture(
            fixture,
            strict_ledger([suspension], ["000001"]),
            {"cash": 0.0, "holdings": {"000001": 10}, "holding_cost_basis": {"000001": 10.0}},
        )
    finally:
        backtest.REBALANCE_STEP_DAYS = original_step
    if not suspended_state["blocked_orders"]:
        raise AssertionError("suspended target produced no blocked-order diagnostic")
    blocked = suspended_state["blocked_orders"][0]
    if blocked["lifecycle_state"] != "SUSPENDED" or blocked["event_id"] != "suspend":
        raise AssertionError(f"incomplete blocked-order diagnostic: {blocked}")

    trailing_dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
    trailing_fixture = pd.DataFrame(
        [
            {
                "date": dt,
                "ticker": "000001",
                "open": 10.0 if dt != trailing_dates[2] else 8.0,
                "close": 10.0 if dt != trailing_dates[2] else 8.0,
                "ret_60": 1.0,
                "ret_120": 1.0,
                "trend_ok": True,
                "liquidity_ok": True,
                "listing_ok": True,
                "deviation_ok": True,
            }
            for dt in trailing_dates
        ]
    )
    trailing_ledger = strict_ledger(
        [suspension], ["000001"], end=date(2024, 1, 5)
    )
    _, trailing_trades, trailing_state = run_strict_fixture(
        trailing_fixture,
        trailing_ledger,
        {"cash": 0.0, "holdings": {"000001": 10}, "holding_cost_basis": {"000001": 10.0}},
        common_dates=list(trailing_dates),
        exit_check_days=1,
        trailing_stop_pct=0.1,
    )
    if not trailing_trades.empty:
        raise AssertionError("ETF_TRAILING_STOP incorrectly filled suspended holding")
    matching = [
        row for row in trailing_state["blocked_orders"] if row["intent"] == "ETF_TRAILING_STOP"
    ]
    if len(matching) != 1:
        raise AssertionError(f"expected exactly one trailing rejection diagnostic: {matching}")
    blocked = matching[0]
    if blocked["lifecycle_state"] != "SUSPENDED" or blocked["event_id"] != "suspend":
        raise AssertionError(f"trailing diagnostic lost suspension state: {blocked}")

    non_trigger_fixture = trailing_fixture.copy()
    non_trigger_fixture.loc[non_trigger_fixture["date"] == trailing_dates[2], "close"] = 9.5
    _, _, non_trigger_state = run_strict_fixture(
        non_trigger_fixture,
        trailing_ledger,
        {"cash": 0.0, "holdings": {"000001": 10}, "holding_cost_basis": {"000001": 10.0}},
        common_dates=list(trailing_dates),
        exit_check_days=1,
        trailing_stop_pct=0.1,
    )
    if any(row["intent"] == "ETF_TRAILING_STOP" for row in non_trigger_state["blocked_orders"]):
        raise AssertionError("non-triggering trailing check emitted a lifecycle rejection")

    delisting = strict_action("delist", "000001", EventType.DELISTING, date(2024, 1, 3))
    settlement = strict_action(
        "settle",
        "000001",
        EventType.CASH_SETTLEMENT,
        date(2024, 1, 4),
        settlement_date=date(2024, 1, 4),
        cash_amount=Decimal(5),
        currency="KRW",
    )
    result, _, settled_state = run_strict_fixture(
        fixture,
        strict_ledger([delisting, settlement], ["000001"]),
        {"cash": 0.0, "holdings": {"000001": 10}, "holding_cost_basis": {"000001": 10.0}},
    )
    if settled_state["holdings"] or settled_state["approval_report"].status != "APPROVED":
        raise AssertionError("settlement did not clear the position or approve")
    assert_close(float(result.iloc[-1]["cash"]), 50.0, "authoritative settlement cash")

    stale_fixture = fixture.copy()
    stale_fixture.loc[stale_fixture["date"] == DATES[2], "close"] = float("nan")
    _, _, stale_state = run_strict_fixture(
        stale_fixture,
        strict_ledger([], ["000001"]),
        {
            "cash": 0.0,
            "holdings": {"000001": 10},
            "holding_cost_basis": {"000001": 10.0},
            "last_valid_closes": {"000001": 10.0},
        },
    )
    stale_codes = {blocker.code for blocker in stale_state["approval_report"].blockers}
    if "FINAL_POSITION_STALE" not in stale_codes:
        raise AssertionError("last-valid fallback incorrectly certified final strict valuation")


def check_strict_coverage_and_group_restore() -> None:
    fixture = build_price_fixture(
        ["000001"],
        {"000001": [10.0, 10.0, 10.0]},
        {"000001": [10.0, 10.0, 10.0]},
        momentum={"000001": (1.0, 1.0)},
    )
    narrow = strict_ledger([], ["000001"], end=date(2024, 1, 3))
    blockers = backtest._strict_coverage_blockers(narrow, list(DATES), ["000001", "000002"])
    codes = {blocker.code for blocker in blockers}
    if "RUN_END_OUTSIDE_VERIFICATION_PERIOD" not in codes or "UNIVERSE_OUTSIDE_VERIFICATION_TICKERS" not in codes:
        raise AssertionError(f"coverage mismatch was not actionable: {codes}")

    valid = strict_ledger([], ["000001"])
    original_groups = _etf_shared.ETF_TICKER_GROUPS
    original_rank = backtest.rank_etfs
    try:
        backtest.rank_etfs = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fixture failure"))
        try:
            run_strict_fixture(
                fixture,
                valid,
                {"cash": 0.0, "holdings": {"000001": 1}, "holding_cost_basis": {"000001": 10.0}},
                ticker_groups={"000001": "foreign_investment"},
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("forced strict exception did not propagate")
    finally:
        backtest.rank_etfs = original_rank
    if _etf_shared.ETF_TICKER_GROUPS is not original_groups:
        raise AssertionError("ticker groups were not restored after strict exception")


def check_provenance_digest() -> None:
    fixture = build_price_fixture(
        ["000001", "000002"],
        {"000001": [10.0, 11.0, 12.0], "000002": [20.0, 21.0, 22.0]},
        {"000001": [10.0, 11.0, 12.0], "000002": [20.0, 21.0, 22.0]},
        {("000001", "2024-01-03"): 2.0, ("000002", "2024-01-03"): 7.0},
        momentum={"000001": (2.0, 2.0), "000002": (1.0, 1.0)},
    )
    index = build_index_fixture()
    distribution = pd.DataFrame(
        [
            {
                "ticker": "000001",
                "ex_date": "2024-01-03",
                "amount_per_share": 2.0,
                "source": "inline",
            },
            {
                "ticker": "000002",
                "ex_date": "2024-01-03",
                "amount_per_share": 7.0,
                "source": "inline",
            },
        ]
    )
    initial_state = {"cash": 10_000.0, "holdings": {"000001": 10}}
    provenance_run = run_fixture(
        fixture, initial_state, 2, initial_cash=10_000.0, slippage=0.0
    )
    if provenance_run[0].empty:
        raise AssertionError("provenance fixture produced no execution rows")
    config = effective_strategy_config(slippage=0.0)
    run_arguments = effective_run_arguments(
        fixture, initial_state, max_positions=2, initial_cash=10_000.0, slippage=0.0
    )
    digest_one = canonical_digest(
        fixture, index, config, run_arguments, distribution, "inline:phase0-distribution"
    )
    reordered_config = {key: config[key] for key in reversed(list(config))}
    reordered_state = {"holdings": {"000001": 10}, "cash": 10_000.0}
    reordered_arguments = {
        key: run_arguments[key] for key in reversed(list(run_arguments))
    }
    reordered_arguments["initial_state"] = reordered_state
    digest_two = canonical_digest(
        fixture.sample(frac=1.0, random_state=17),
        index.iloc[::-1].reset_index(drop=True),
        reordered_config,
        reordered_arguments,
        distribution.iloc[::-1].reset_index(drop=True),
        "inline:phase0-distribution",
    )
    if digest_one != digest_two:
        raise AssertionError("row/dict order changed the provenance digest")

    data_changed = fixture.copy()
    data_changed.loc[0, "close"] = float(data_changed.loc[0, "close"]) + 1.0
    if canonical_digest(
        data_changed, index, config, run_arguments, distribution, "inline:phase0-distribution"
    ) == digest_one:
        raise AssertionError("changing fixture data did not change provenance digest")

    index_changed = index.copy()
    index_changed.loc[0, "close"] = float(index_changed.loc[0, "close"]) + 1.0
    if canonical_digest(
        fixture, index_changed, config, run_arguments, distribution, "inline:phase0-distribution"
    ) == digest_one:
        raise AssertionError("changing index data did not change provenance digest")

    config_changed = {**config, "rebalance_step_days": config["rebalance_step_days"] + 1}
    if canonical_digest(
        fixture, index, config_changed, run_arguments, distribution, "inline:phase0-distribution"
    ) == digest_one:
        raise AssertionError("changing execution config did not change provenance digest")

    distribution_changed = distribution.copy()
    distribution_changed.loc[0, "amount_per_share"] = 3.0
    if canonical_digest(
        fixture,
        index,
        config,
        run_arguments,
        distribution_changed,
        "inline:phase0-distribution",
    ) == digest_one:
        raise AssertionError("changing distribution input did not change provenance digest")

    nonfinite_fixture = fixture.copy()
    nonfinite_fixture.loc[0, "open"] = float("nan")
    try:
        canonical_digest(
            nonfinite_fixture,
            index,
            config,
            run_arguments,
            distribution,
            "inline:phase0-distribution",
        )
    except ValueError as exc:
        if "non-finite" not in str(exc):
            raise AssertionError(f"non-finite rejection had an unclear error: {exc}") from exc
    else:
        raise AssertionError("non-finite fixture input was accepted by provenance digest")


def main() -> None:
    original_name_lookup = backtest.get_ticker_name
    original_spread = backtest.SPREAD_PCT
    original_rebalance_days = backtest.REBALANCE_STEP_DAYS
    original_sell_rank_buffer = backtest.ETF_SELL_RANK_BUFFER
    original_sell_tax = backtest.ETF_TAXABLE_SELL_TAX_PCT
    original_taxable_tickers = backtest.TAXABLE_ETF_TICKERS
    previous_tax_env = os.environ.get("ETF_DISTRIBUTION_TAX_PCT")
    backtest.get_ticker_name = lambda ticker: NAME_MAP.get(str(ticker), str(ticker))
    backtest.SPREAD_PCT = 0.002
    backtest.REBALANCE_STEP_DAYS = 20
    backtest.ETF_SELL_RANK_BUFFER = 3
    backtest.ETF_TAXABLE_SELL_TAX_PCT = 0.10
    backtest.TAXABLE_ETF_TICKERS = {"000005"}
    os.environ["ETF_DISTRIBUTION_TAX_PCT"] = "0"
    try:
        check_t_plus_one_and_accounting()
        check_buy_sell_accounting()
        check_distribution_ordering()
        check_legacy_mode_is_ledger_independent()
        check_strict_causal_entitlement_and_payment()
        check_legacy_ticker_failure_is_isolated()
        check_strict_lifecycle_and_final_valuation()
        check_strict_coverage_and_group_restore()
        check_provenance_digest()
    finally:
        backtest.get_ticker_name = original_name_lookup
        backtest.SPREAD_PCT = original_spread
        backtest.REBALANCE_STEP_DAYS = original_rebalance_days
        backtest.ETF_SELL_RANK_BUFFER = original_sell_rank_buffer
        backtest.ETF_TAXABLE_SELL_TAX_PCT = original_sell_tax
        backtest.TAXABLE_ETF_TICKERS = original_taxable_tickers
        if previous_tax_env is None:
            os.environ.pop("ETF_DISTRIBUTION_TAX_PCT", None)
        else:
            os.environ["ETF_DISTRIBUTION_TAX_PCT"] = previous_tax_env
    print("backtest integrity Phase 0 checks passed")


if __name__ == "__main__":
    main()
