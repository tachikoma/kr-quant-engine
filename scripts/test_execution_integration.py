"""Offline integration checks for the opt-in OHLCV capacity scenario."""

from __future__ import annotations

import os
import sys
import types
from datetime import date
from decimal import Decimal
from math import isclose
from pathlib import Path

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

_saved_env = {key: os.environ.get(key) for key in ("KRX_ID", "KRX_PW")}
_saved_modules = {key: sys.modules.get(key) for key in ("pykrx", "pykrx.stock")}
try:
    os.environ["KRX_ID"] = ""
    os.environ["KRX_PW"] = ""
    pykrx = types.ModuleType("pykrx")
    pykrx_stock = types.ModuleType("pykrx.stock")
    pykrx.stock = pykrx_stock
    sys.modules["pykrx"] = pykrx
    sys.modules["pykrx.stock"] = pykrx_stock
    import run_etf_backtest as backtest
finally:
    for key, value in _saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    for key, value in _saved_modules.items():
        if value is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = value

DAYS = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
BUY_FEE_PCT = 0.00015
SELL_FEE_PCT = 0.00015
SELL_TAX_PCT = 0.154


def build_prices(
    volumes: list[float],
    opens: list[float | None] | None = None,
    closes: list[float | None] | None = None,
    ticker: str = "000001",
) -> pd.DataFrame:
    opens = opens or [10.0] * len(DAYS)
    closes = closes or [10.0] * len(DAYS)
    rows = []
    for dt, volume, open_price, close_price in zip(DAYS, volumes, opens, closes):
        close_price = 10.0 if close_price is None else close_price
        low_price = min(close_price, open_price) if open_price is not None else close_price
        high_price = max(close_price, open_price) if open_price is not None else close_price
        rows.append(
            {
                "date": dt,
                "ticker": ticker,
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
                "volume": volume,
                "trading_value": None if volume == 0 else close_price * volume,
                "ret_60": 1.0,
                "ret_120": 1.0,
                "trend_ok": True,
                "liquidity_ok": True,
                "listing_ok": True,
                "deviation_ok": True,
            }
        )
    return pd.DataFrame(rows)


def run_fixture(
    prices: pd.DataFrame,
    *,
    execution_mode: str = "legacy",
    initial_state: dict | None = None,
    initial_cash: float = 1_000.0,
    use_market_filter: bool = False,
    ticker: str = "000001",
    exit_check_days: int | None = None,
    trailing_stop_pct: float | None = None,
    portfolio_trailing_stop_pct: float | None = None,
    corporate_action_ledger: CorporateActionLedger | None = None,
):
    return backtest.run_etf_strategy(
        initial_cash=initial_cash,
        common_dates=list(DAYS),
        index_df=pd.DataFrame({"date": DAYS, "close": [100.0] * len(DAYS)}),
        use_market_filter=use_market_filter,
        max_positions=1,
        slippage=0.0,
        price_data=prices,
        max_asset_pct=0.0,
        target_weight_rebalance=False,
        rebalance_band_pct=0.0,
        universe_tickers=[ticker],
        initial_state=initial_state or {"cash": initial_cash, "holdings": {}},
        return_final_state=True,
        execution_mode=execution_mode,
        exit_check_days=exit_check_days,
        trailing_stop_pct=trailing_stop_pct,
        portfolio_trailing_stop_pct=portfolio_trailing_stop_pct,
        approval_strict=corporate_action_ledger is not None,
        corporate_action_ledger=corporate_action_ledger,
    )


def check_legacy_equality() -> None:
    prices = build_prices([1_000.0] * len(DAYS))
    default = run_fixture(prices)
    explicit = run_fixture(prices, execution_mode="legacy")
    pd.testing.assert_frame_equal(default[0], explicit[0])
    pd.testing.assert_frame_equal(default[1], explicit[1])
    assert default[2] == explicit[2]
    assert "execution_diagnostics" not in default[2]


def check_partial_carry_and_repricing() -> None:
    original_rank = backtest.rank_etfs
    rank_calls = []
    backtest.rank_etfs = lambda frame: rank_calls.append(frame) or frame
    try:
        prices = build_prices(
            [1_000.0, 20.0, 2_000.0, 1_000.0],
            opens=[10.0, 10.0, 20.0, 20.0],
        )
        curve, trades, state = run_fixture(prices, execution_mode="ohlcv_capacity")
    finally:
        backtest.rank_etfs = original_rank
    diagnostics = state["execution_diagnostics"]
    first, second = diagnostics[:2]
    assert first["decision"] == "PARTIAL_CARRY"
    assert first["filled_qty"] == 1
    assert first["remaining_qty"] > 0
    assert second["carry_age"] == 1
    assert second["date"] == "2024-01-04"
    assert second["decision"] == "CARRY_CANCELLED"
    assert len(trades) == 2
    assert int(trades.iloc[0]["qty"]) == 1
    assert second["diagnostic_labels"][-1] == "CASH_LIMITED_CARRY_CANCEL"
    assert int(trades.iloc[1]["qty"]) == int(second["filled_qty"])
    assert float(trades.iloc[1]["price"]) > float(trades.iloc[0]["price"])
    expected_buy_costs = [
        int(row["qty"]) * float(row["price"]) * (1 + BUY_FEE_PCT) for _, row in trades.iterrows()
    ]
    assert all(
        isclose(float(actual), expected, rel_tol=1e-12, abs_tol=1e-12)
        for actual, expected in zip(trades["net_value"], expected_buy_costs)
    )
    expected_cash = 1_000.0
    for actual, cost in zip(trades["cash_after"], expected_buy_costs):
        expected_cash -= cost
        assert isclose(float(actual), expected_cash, rel_tol=1e-12, abs_tol=1e-12)
    assert state["holdings"]["000001"] == int(trades["qty"].sum())
    expected_basis = sum(
        int(row["qty"]) * float(row["price"]) * (1 + BUY_FEE_PCT) for _, row in trades.iterrows()
    ) / int(trades["qty"].sum())
    assert isclose(
        state["holding_cost_basis"]["000001"],
        expected_basis,
        rel_tol=1e-12,
        abs_tol=1e-12,
    )
    assert state["cash"] >= 0
    assert curve.iloc[-1]["equity"] > 0
    assert all(row["execution_mode"] == "ohlcv_capacity" for row in diagnostics)
    assert len(rank_calls) == 1, "carry must not trigger a nonperiodic ranking pass"
    assert all(trade["reason"] != "ETF_REBALANCE" for trade in trades.iloc[1:].to_dict("records"))


def check_sufficient_cash_carry() -> None:
    prices = build_prices(
        [1_000.0, 20.0, 2_000.0, 1_000.0],
        opens=[10.0, 10.0, 20.0, 20.0],
    )
    _, trades, state = run_fixture(
        prices,
        execution_mode="ohlcv_capacity",
        initial_cash=5_000.0,
    )
    assert [int(qty) for qty in trades["qty"]] == [1, 100]
    assert all(float(value) >= 0 for value in trades["cash_after"])
    assert state["cash"] >= 0
    assert not any(
        "CASH_LIMITED_CARRY_CANCEL" in row["diagnostic_labels"]
        for row in state["execution_diagnostics"]
    )


def check_capacity_no_fill() -> None:
    prices = build_prices([0.0] * len(DAYS))
    _, trades, state = run_fixture(prices, execution_mode="ohlcv_capacity")
    assert trades.empty
    assert any(row["decision"] == "POSSIBLE_LIMIT_LOCK" for row in state["execution_diagnostics"])
    assert state["cash"] == 1_000.0
    assert state["holdings"] == {}


def check_cash_limited_rebalance_buy() -> None:
    """capacity 승인 매수가 가용 현금을 초과하면 잔량이 취소된다(유령 자본 방지)."""
    original_build = backtest.build_rebalance_orders

    def inflate_buy_qty(*args, **kwargs):
        orders = original_build(*args, **kwargs)
        return [
            {**order, "qty": int(order["qty"]) * 3} if str(order.get("side")) == "BUY" else order
            for order in orders
        ]

    try:
        backtest.build_rebalance_orders = inflate_buy_qty
        prices = build_prices([1_000_000.0] * len(DAYS))
        _, trades, state = run_fixture(prices, execution_mode="ohlcv_capacity")
    finally:
        backtest.build_rebalance_orders = original_build
    buys = trades[trades["side"] == "BUY"]
    assert not buys.empty
    limited = [
        row for row in state["execution_diagnostics"] if row.get("decision") == "CASH_LIMITED"
    ]
    assert limited, "cash-limited rebalance BUY must be diagnosed"
    assert all("CASH_LIMITED_FILL_CANCEL" in row["diagnostic_labels"] for row in limited)
    # 어떤 주문도 현금을 초과해 체결되지 않는다.
    total_cost = float(buys["net_value"].sum())
    assert isclose(state["cash"], 1_000.0 - total_cost, rel_tol=1e-12, abs_tol=1e-9)
    assert state["cash"] >= 0
    # diagnostic/trade 수량이 주문 ID 기준으로 일치한다.
    diag_qty: dict[str, int] = {}
    for row in state["execution_diagnostics"]:
        order_id = str(row["execution_order_id"])
        diag_qty[order_id] = diag_qty.get(order_id, 0) + int(row.get("filled_qty", 0) or 0)
    for row in trades.to_dict("records"):
        order_id = str(row.get("execution_order_id"))
        assert diag_qty.get(order_id, 0) == int(row["qty"]), order_id
    assert all(float(value) >= 0 for value in trades["cash_after"])


def check_full_sell() -> None:
    original_risk = backtest.is_risk_on
    try:
        backtest.is_risk_on = lambda *_args, **_kwargs: False
        prices = build_prices([1_000.0] * len(DAYS))
        _, trades, state = run_fixture(
            prices,
            execution_mode="ohlcv_capacity",
            initial_state={
                "cash": 0.0,
                "holdings": {"000001": 10},
                "holding_cost_basis": {"000001": 10.0},
            },
            use_market_filter=True,
        )
        sells = trades[trades["side"] == "SELL"]
        assert len(sells) == 1
        assert int(sells.iloc[0]["qty"]) == 10
        assert state["holdings"] == {}
    finally:
        backtest.is_risk_on = original_risk


def check_partial_exit_reconciliation() -> None:
    ticker = "143850"
    prices = build_prices(
        [1_000.0, 20.0, 1_000.0, 1_000.0],
        opens=[10.0, 8.0, 20.0, 20.0],
        closes=[9.0, 8.0, 20.0, 20.0],
        ticker=ticker,
    )
    initial_state = {
        "cash": 0.0,
        "holdings": {ticker: 10},
        "holding_cost_basis": {ticker: 5.0},
        "holding_peak_closes": {ticker: 10.0},
    }
    for exit_kwargs in (
        {"trailing_stop_pct": 0.10},
        {"portfolio_trailing_stop_pct": 0.10, "portfolio_peak_equity": 100.0},
    ):
        state_input = dict(initial_state)
        if "portfolio_peak_equity" in exit_kwargs:
            state_input["portfolio_peak_equity"] = exit_kwargs.pop("portfolio_peak_equity")
        _, trades, state = run_fixture(
            prices,
            execution_mode="ohlcv_capacity",
            initial_state=state_input,
            ticker=ticker,
            exit_check_days=20,
            **exit_kwargs,
        )
        sells = trades[trades["side"] == "SELL"]
        assert [int(qty) for qty in sells["qty"]] == [1, 9]
        expected_values = []
        expected_taxes = []
        expected_cash = 0.0
        for _, row in sells.iterrows():
            qty = int(row["qty"])
            gross = qty * float(row["price"])
            taxable_gain = max(0.0, gross - qty * 5.0)
            tax = taxable_gain * SELL_TAX_PCT
            net = gross * (1 - SELL_FEE_PCT) - tax
            expected_values.append(net)
            expected_taxes.append(tax)
            expected_cash += net
        assert all(
            isclose(float(actual), expected, rel_tol=1e-12, abs_tol=1e-12)
            for actual, expected in zip(sells["net_value"], expected_values)
        )
        assert all(
            isclose(float(actual), expected, rel_tol=1e-12, abs_tol=1e-12)
            for actual, expected in zip(sells["estimated_tax"], expected_taxes)
        )
        assert isclose(state["cash"], expected_cash, rel_tol=1e-12, abs_tol=1e-12)
        running_cash = 0.0
        for actual, net in zip(sells["cash_after"], expected_values):
            running_cash += net
            assert isclose(float(actual), running_cash, rel_tol=1e-12, abs_tol=1e-12)
        assert state["holdings"] == {}
        assert ticker not in state["holding_cost_basis"]
        assert all(trade["side"] == "SELL" for trade in trades.to_dict("records"))
        assert state["execution_diagnostics"][0]["decision"] == "PARTIAL_CARRY"
        assert state["execution_diagnostics"][1]["decision"] == "FULL"


def _strict_fixture_ledger(
    ticker: str, events: tuple[CorporateAction, ...]
) -> CorporateActionLedger:
    manifest = CoverageManifest(
        1,
        "synthetic.csv",
        "a" * 64,
        date(2024, 1, 1),
        date(2024, 1, 31),
        (ticker,),
        (),
        "VERIFIED",
        "synthetic integration fixture",
    )
    return CorporateActionLedger(events, manifest, "a" * 64, ())


def check_lifecycle_cancels_carry() -> None:
    ticker = "000001"
    lifecycle_cases = {
        EventType.SUSPENSION_START: (
            CorporateAction("suspend", ticker, EventType.SUSPENSION_START, date(2024, 1, 3)),
        ),
        EventType.DELISTING: (
            CorporateAction("delist", ticker, EventType.DELISTING, date(2024, 1, 3)),
        ),
        EventType.CASH_SETTLEMENT: (
            CorporateAction("delist", ticker, EventType.DELISTING, date(2024, 1, 3)),
            CorporateAction(
                "settle",
                ticker,
                EventType.CASH_SETTLEMENT,
                date(2024, 1, 4),
                settlement_date=date(2024, 1, 4),
                cash_amount=Decimal(1),
            ),
        ),
    }
    for event_type, events in lifecycle_cases.items():
        _, trades, state = run_fixture(
            build_prices([1_000.0, 20.0, 1_000.0, 1_000.0], ticker=ticker),
            execution_mode="ohlcv_capacity",
            corporate_action_ledger=_strict_fixture_ledger(ticker, events),
            ticker=ticker,
        )
        assert state["pending_execution_carries"] == ()
        assert any(
            row["decision"] == "CARRY_CANCELLED" and row["ticker"] == ticker
            for row in state["execution_diagnostics"]
        ), event_type
        assert len(trades[trades["reason"] == "ETF_EXECUTION_CARRY"]) == 0


def check_empty_due_date_cancels_carry() -> None:
    prices = build_prices([1_000.0, 20.0, 1_000.0, 1_000.0])
    prices = prices[prices["date"] != DAYS[2]].copy()
    _, _, state = run_fixture(prices, execution_mode="ohlcv_capacity")
    assert state["pending_execution_carries"] == ()
    assert any(
        "empty today or next-day row" in row["reason"] for row in state["execution_diagnostics"]
    )


def main() -> None:
    original_step = backtest.REBALANCE_STEP_DAYS
    original_rank = backtest.rank_etfs
    try:
        backtest.REBALANCE_STEP_DAYS = 20
        backtest.rank_etfs = lambda frame: frame
        check_legacy_equality()
        check_partial_carry_and_repricing()
        check_sufficient_cash_carry()
        check_capacity_no_fill()
        check_cash_limited_rebalance_buy()
        check_full_sell()
        check_partial_exit_reconciliation()
        check_lifecycle_cancels_carry()
        check_empty_due_date_cancels_carry()
    finally:
        backtest.REBALANCE_STEP_DAYS = original_step
        backtest.rank_etfs = original_rank
    print("execution integration checks passed")


if __name__ == "__main__":
    main()
