"""Day-by-day parity harness: Forward Shadow vs ``run_etf_strategy`` (ohlcv_capacity).

Both sides get the same frozen parameters, initial cash, OHLCV, listing dates,
index series and capacity model.  The backtest uses its own preprocessing
(``load_etf_price``) and execution path.  Cash, equity and held tickers are
compared every day; share quantities and cost basis are compared through a
backtest run ending on each fill day, and must stay unchanged on other days.
Run directly to print the numbers::

    uv run python -m tests.shadow_compare

Not a test module.
"""

from __future__ import annotations

import datetime as dt
import itertools
import logging
import math
import os
import tempfile
from pathlib import Path
from unittest import mock

import pandas as pd

import run_etf_backtest as bt
from forward_shadow.market import FixtureProvider
from forward_shadow.runner import initialize_runtime, process_date
from forward_shadow.store import RuntimePaths, read_jsonl
from forward_shadow.warmup import WarmupInput
from strategy_freeze import load_frozen_strategy
from tests.shadow_fixtures import LISTING, UNIVERSE, WARMUP, make_synthetic

FROZEN = load_frozen_strategy()
PARAMS = FROZEN["parameters"]
BACKTEST_WARMUP = max(120, PARAMS["market_ma_days"] + PARAMS["market_slope_days"])
REAL_CACHE = Path(
    os.environ.get("SHADOW_REAL_CACHE", "/home/ubuntu/investment/kr-quant-engine/data_cache")
)


# ── scenarios ───────────────────────────────────────────────────────────────


def normal_market():
    return make_synthetic(230)


def thin_rotating_market():
    """Rotating leaders on thin, uneven volume: partial fills, carries, cash limits."""

    def shape(idx, j):
        return 10_000.0 + idx * 250.0 + j * 14.0 + 1_600.0 * math.sin(j / 13.0 + idx * 1.3)

    def volume(idx, j):
        return (60_000, 400, 0, 25_000, 1_500, 90_000, 10)[(idx * 3 + j) % 7]

    return make_synthetic(280, volume, shape=shape)


def risk_off_market():
    """KOSPI turns down while domestic, then every ETF falls (forced and empty exits)."""
    turn, crash = WARMUP + 30, WARMUP + 75

    def shape(idx, j):
        base = 10_000.0 + idx * 250.0 + 300.0 * math.sin(j / 9.0 + idx)
        domestic = idx < 5 or idx in (9, 10, 15)
        level = base + min(j, turn) * 14.0
        if j > turn:
            level += (j - turn) * (-45.0 if domestic else 22.0)
        if j > crash:
            level -= (j - crash) * 260.0 + (0.0 if domestic else (j - crash) * 22.0)
        return max(level, 2_000.0)

    def volume(idx, j):
        return 3_000.0 if j % 4 else 900_000.0

    def index_shape(j):
        return 2_000.0 + min(j, turn) * 3.0 - max(0, j - turn) * 22.0 if j < 260 else 700.0

    return make_synthetic(260, volume, shape=shape, index_shape=index_shape)


def real_market():
    """Cached KRX data (read-only); None when the cache is unavailable."""
    if not (REAL_CACHE / "index.parquet").exists():
        return None
    etf = {}
    for ticker in UNIVERSE:
        frame = pd.read_parquet(REAL_CACHE / f"{ticker}.parquet")
        frame["date"] = pd.to_datetime(frame["date"])
        etf[ticker] = frame
    index = pd.read_parquet(REAL_CACHE / "index.parquet")
    index["date"] = pd.to_datetime(index["date"])
    classification = pd.read_parquet(REAL_CACHE / "etf_tax_classification.parquet")
    classification = classification[classification["ISU_SRT_CD"].isin(UNIVERSE)]
    listing = {
        str(t): "".join(ch for ch in str(d) if ch.isdigit())
        for t, d in zip(classification["ISU_SRT_CD"], classification["LIST_DD"], strict=True)
    }
    return etf, index[["date", "close"]], listing


# ── the two engines ─────────────────────────────────────────────────────────


def _backtest_inputs(etf, index, listing, first, last):
    frames = {
        t: f[(f.date >= first) & (f.date <= last)].reset_index(drop=True)
        for t, f in etf.items()
    }
    with (
        mock.patch.object(bt, "get_price", lambda t, listing_dates=None: frames[t].copy()),
        mock.patch.object(bt, "get_listing_dates", lambda ticker_subset=None: dict(listing)),
        mock.patch("builtins.print"),
    ):
        price = bt.load_etf_price(UNIVERSE)
    series = index[(index.date >= first) & (index.date <= last)].reset_index(drop=True).copy()
    series["market_ma"] = series["close"].rolling(PARAMS["market_ma_days"]).mean()
    series["market_ma_slope"] = series["market_ma"] - series["market_ma"].shift(
        PARAMS["market_slope_days"]
    )
    series["risk_on"] = (series["close"] >= series["market_ma"]) & (
        series["market_ma_slope"] >= 0
    )
    return price, series


def _run_backtest(price, index, common_dates, capital, observer=None):
    with (
        mock.patch.object(bt, "SPREAD_PCT", float(PARAMS["spread_pct"])),
        mock.patch.object(bt, "REBALANCE_STEP_DAYS", int(PARAMS["rebalance_step_days"])),
        mock.patch.object(bt, "MULTI_INDEX_GATING_MODE", PARAMS["multi_index_gating_mode"]),
    ):
        return bt.run_etf_strategy(
            capital,
            common_dates,
            index,
            use_market_filter=True,
            max_positions=int(PARAMS["max_positions"]),
            slippage=float(PARAMS["slippage"]),
            risk_off_liquidate=bool(PARAMS["liquidate_on_risk_off"]),
            price_data=price,
            max_asset_pct=float(PARAMS["max_asset_pct"]),
            target_weight_rebalance=bool(PARAMS["target_weight_rebalance"]),
            rebalance_band_pct=float(PARAMS["rebalance_band_pct"]),
            trim_overweight_positions=bool(PARAMS["trim_overweight_positions"]),
            exit_check_days=0,
            trailing_stop_pct=0.0,
            portfolio_trailing_stop_pct=0.0,
            enable_multi_index_risk=False,
            universe_tickers=UNIVERSE,
            ticker_groups=dict(FROZEN["ticker_groups"]),
            rebalance_observer=observer,
            return_final_state=True,
            execution_mode="ohlcv_capacity",
            participation_rate=0.05,
        )


def _day(value) -> str:
    return str(pd.Timestamp(value).date())


def compare(market, *, start: int, days: int, capital: float, listing=None) -> dict:
    """Run both engines over ``days`` observation days and return every difference."""
    etf, index = market[0], market[1]
    listing = listing or LISTING
    dates = list(index["date"])
    observed = dates[start : start + days]
    first = dates[start - WARMUP]
    # One extra date lets the backtest keep the last day's carry alive.
    last = dates[min(start + days, len(dates) - 1)]
    previous_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        return _compare(etf, index, listing, dates, observed, first, last, start, capital)
    finally:
        logging.disable(previous_level)


def _compare(etf, index, listing, dates, observed, first, last, start, capital) -> dict:
    with tempfile.TemporaryDirectory(prefix="kqe-shadow-compare-") as tmp:
        root = Path(tmp) / "runtime"
        initialize_runtime(
            root,
            initial_capital=capital,
            observation_start_date=_day(observed[0]),
            warmup=WarmupInput(
                {t: f[f.date >= first] for t, f in etf.items()},
                index[index.date >= first],
                dict(listing),
                "compare",
            ),
        )
        provider = FixtureProvider(etf, index, name="compare")
        for day in observed:
            result = process_date(_day(day), provider, root)
            if result["status"] != "DONE":
                raise AssertionError(f"shadow {_day(day)}: {result}")
        events = read_jsonl(RuntimePaths(root).ledger)

    price, series = _backtest_inputs(etf, index, listing, first, last)
    window_start = start - BACKTEST_WARMUP
    full_dates = [d for d in dates[window_start:] if d <= last]
    observers: list[dict] = []
    curve, trades, final = _run_backtest(price, series, full_dates, capital, observers.append)

    horizon = {_day(d) for d in observed}
    diffs: list[str] = []

    def close_enough(a, b) -> bool:
        return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-6)

    def check(label, shadow, backtest) -> None:
        if shadow != backtest:
            diffs.append(f"{label}: shadow={shadow!r} backtest={backtest!r}")

    signals = {
        e["date"]: e["payload"]["signal"] for e in events if e["type"] == "SIGNAL"
    }
    fills_by_day = {
        e["date"]: {**e["payload"], "carry": e["state_patch"]["carry"]}
        for e in events
        if e["type"] == "FILL"
    }
    rows = {e["date"]: e["payload"]["row"] for e in events if e["type"] == "EQUITY"}

    # 1. decision days, ranking, targets and gate outcome
    expected = {
        _day(o["decision_date"]): o
        for o in observers
        if _day(o["execution_date"]) in horizon
    }
    check(
        "decision days",
        sorted(d for d, s in signals.items() if s["execution_date"] in horizon),
        sorted(expected),
    )
    for date, observer in expected.items():
        signal = signals.get(date)
        if signal is None:
            continue
        execution = fills_by_day[_day(observer["execution_date"])]["execution"]
        check(f"{date} ranking", signal["ranked_tickers"], observer["ranked_tickers"])
        check(f"{date} targets", execution["targets"], observer["targets"])
        check(
            f"{date} forced exits",
            execution["forced_exit_tickers"],
            observer["forced_exit_tickers"],
        )
        check(
            f"{date} empty-target policy",
            execution["allow_empty_target_sell"],
            observer["allow_empty_target_sell"],
        )

    # 2. every order attempt: direction, requested and filled quantity, outcome
    backtest_orders = {
        (d["date"], d["ticker"], d["side"]): (
            int(d["requested_qty"]), int(d["filled_qty"]), d["decision"]
        )
        for d in final["execution_diagnostics"]
        if d["date"] in horizon and d.get("terminal_applied")
    }
    shadow_orders = {
        (date, d["ticker"], d["side"]): (d["requested_qty"], d["filled_qty"], d["decision"])
        for date, payload in fills_by_day.items()
        for d in payload["diagnostics"]
        if d["decision"] not in ("NO_ORDER", "EXCLUDED_BY_CARRY")
    }
    for key in sorted(set(backtest_orders) | set(shadow_orders)):
        check(f"order {key}", shadow_orders.get(key), backtest_orders.get(key))

    # 3. fills: quantity, reference price (spread), cash flow (slippage+fee), tax
    backtest_fills = {
        (_day(t["date"]), t["ticker"], t["side"]): t
        for t in trades.to_dict("records")
        if _day(t["date"]) in horizon
    }
    shadow_fills = {
        (f["date"], f["ticker"], f["side"]): f
        for payload in fills_by_day.values()
        for f in payload["fills"]
    }
    check("fill keys", sorted(shadow_fills), sorted(backtest_fills))
    max_price = max_flow = max_tax = max_formula = 0.0
    totals = {"fee": 0.0, "tax": 0.0, "slippage": 0.0, "spread": 0.0}
    for key in sorted(set(shadow_fills) & set(backtest_fills)):
        ours, theirs = shadow_fills[key], backtest_fills[key]
        check(f"fill {key} qty", ours["filled_qty"], int(theirs["qty"]))
        max_price = max(max_price, abs(ours["reference_price"] - theirs["price"]))
        max_flow = max(max_flow, abs(ours["net_value"] - theirs["net_value"]))
        max_tax = max(max_tax, abs(ours["estimated_tax"] - theirs["estimated_tax"]))
        sign = 1 if ours["side"] == "BUY" else -1
        qty, opened = ours["filled_qty"], ours["open_price"]
        reference = opened * (1 + sign * PARAMS["spread_pct"] / 2)
        traded = reference * (1 + sign * PARAMS["slippage"])
        formula = qty * traded * (1 + sign * 0.00015)
        if sign < 0:
            formula -= ours["estimated_tax"]
        max_formula = max(max_formula, abs(formula - ours["net_value"]))
        totals["fee"] += ours["fee"]
        totals["tax"] += ours["estimated_tax"]
        totals["spread"] += qty * abs(reference - opened)
        totals["slippage"] += qty * abs(traded - reference)

    # 4/5. daily cash, holdings, cost basis, equity and carry
    backtest_equity = {_day(r["date"]): r for r in curve.to_dict("records")}
    max_cash = max_equity = max_basis = 0.0
    fill_days = {k[0] for k in backtest_fills} | {k[0] for k in shadow_fills}
    state_checks = 0
    for previous_value, date_value in itertools.pairwise(observed):
        date = _day(date_value)
        row = rows[date]
        theirs = backtest_equity[date]
        max_cash = max(max_cash, abs(row["cash"] - theirs["cash"]))
        max_equity = max(max_equity, abs(row["equity"] - theirs["equity"]))
        check(
            f"{date} held tickers",
            ",".join(sorted(row["holdings"])),
            theirs["holdings"],
        )
        if date in fill_days or date_value == observed[-1]:
            state_checks += 1
            position = dates.index(date_value)
            _, _, state = _run_backtest(
                price, series, dates[window_start : position + 1], capital
            )
            check(
                f"{date} quantities",
                row["holdings"],
                {t: int(q) for t, q in state["holdings"].items()},
            )
            check(
                f"{date} cost basis tickers",
                sorted(row["cost_basis"]),
                sorted(state["holding_cost_basis"]),
            )
            for ticker, basis in state["holding_cost_basis"].items():
                max_basis = max(max_basis, abs(row["cost_basis"].get(ticker, 0.0) - basis))
        else:
            before = rows[_day(previous_value)]
            for key in ("holdings", "cost_basis"):
                check(f"{date} unchanged {key}", row[key], before[key])
        # A remainder is carried exactly when a first attempt filled only part of
        # the request for capacity reasons (a later cash cap does not remove it).
        check(
            f"{date} carry",
            sorted(
                (c["ticker"], c["side"], c["remaining_qty"])
                for c in fills_by_day[date]["carry"]
            ),
            sorted(
                (d["ticker"], d["side"], int(d["requested_qty"] - d["capacity_qty"]))
                for d in final["execution_diagnostics"]
                if d["date"] == date
                and d.get("carry_age") == 0
                and 0 < d["capacity_qty"] < d["requested_qty"]
            ),
        )
    for label, value in (
        ("price", max_price), ("cash flow", max_flow), ("tax", max_tax),
        ("cost formula", max_formula), ("cash", max_cash), ("equity", max_equity),
        ("cost basis", max_basis),
    ):
        if value > 1e-6:
            diffs.append(f"max {label} difference {value}")

    shadow_curve = [capital] + [rows[_day(d)]["equity"] for d in observed[1:]]
    other_curve = [capital] + [backtest_equity[_day(d)]["equity"] for d in observed[1:]]
    shadow_mdd = _mdd(shadow_curve)
    backtest_mdd = _mdd(other_curve)
    if not close_enough(shadow_mdd, backtest_mdd):
        diffs.append(f"MDD: shadow={shadow_mdd} backtest={backtest_mdd}")
    if not close_enough(rows[_day(observed[-1])]["max_drawdown"], backtest_mdd):
        diffs.append("reported max_drawdown differs from the backtest MDD")

    labels: dict[str, int] = {}
    for _, _, decision in shadow_orders.values():
        labels[decision] = labels.get(decision, 0) + 1
    return {
        "period": f"{_day(observed[0])}..{_day(observed[-1])}",
        "days": len(observed),
        "capital": capital,
        "decisions": len(expected),
        "forced_exit_decisions": sum(1 for o in expected.values() if o["forced_exit_tickers"]),
        "empty_target_sell_decisions": sum(
            1 for o in expected.values()
            if not o["targets"] and o["allow_empty_target_sell"]
        ),
        "orders": len(shadow_orders),
        "order_outcomes": dict(sorted(labels.items())),
        "fills": len(shadow_fills),
        "sell_fills": sum(1 for k in shadow_fills if k[2] == "SELL"),
        "quantity_cost_basis_checkpoints": state_checks,
        "carry_fills": sum(1 for f in shadow_fills.values() if f["source"] == "CARRY"),
        "final_equity_shadow": shadow_curve[-1],
        "final_equity_backtest": other_curve[-1],
        "mdd_shadow": shadow_mdd,
        "mdd_backtest": backtest_mdd,
        "max_abs_diff": {
            "price": max_price, "cash_flow": max_flow, "tax": max_tax, "cash": max_cash,
            "cost_basis": max_basis, "equity": max_equity, "cost_formula": max_formula,
        },
        "costs": totals,
        "differences": diffs,
    }


def _mdd(values) -> float:
    peak, worst = values[0], 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


def scenarios() -> dict:
    result = {
        "synthetic_normal": {"market": normal_market(), "start": WARMUP, "days": 75, "capital": 1e8},
        "synthetic_thin_rotation": {
            "market": thin_rotating_market(), "start": WARMUP, "days": 125, "capital": 1e8
        },
        "synthetic_risk_off": {
            "market": risk_off_market(), "start": WARMUP, "days": 105, "capital": 1e8
        },
    }
    real = real_market()
    if real is not None:
        dates = list(real[1]["date"])
        start = next(i for i, d in enumerate(dates) if d.date() >= dt.date(2026, 1, 5))
        for name, capital in (("real_2026_1e8", 1e8), ("real_2026_5e10", 5e10)):
            result[name] = {
                "market": real, "start": start, "days": len(dates) - start - 1,
                "capital": capital, "listing": real[2],
            }
    return result


if __name__ == "__main__":
    import json

    for name, arguments in scenarios().items():
        print(name, json.dumps(compare(**arguments), ensure_ascii=False, indent=1))
