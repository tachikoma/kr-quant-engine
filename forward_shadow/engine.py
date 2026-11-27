"""Signal and virtual-execution engine built on the frozen helpers.

Ranking, gating, order building, capacity and cost functions are the frozen
``etf_shared`` / ``etf_execution`` helpers.  The orchestration around them
mirrors the ``execution_mode="ohlcv_capacity"`` path of
``run_etf_backtest.run_etf_strategy``, whose helpers are closures and cannot be
imported.  ``tests/test_forward_shadow_compare.py`` requires both paths to agree
order by order and day by day; no execution policy is defined here.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

import etf_shared
from etf_execution import DecisionType, OHLCVBar, OrderRequest, decide_execution
from forward_shadow.config import EffectiveConfig, effective_config_problems
from forward_shadow.market import MarketDay

HISTORY_COLUMNS = (
    "date", "ticker", "open", "high", "low", "close", "volume", "trading_value", "nav",
)


def build_history_frame(
    warmup: dict[str, Any], days: list[MarketDay]
) -> pd.DataFrame:
    """Long OHLCV frame from the pinned warm-up and the committed snapshots."""
    rows = [
        {**row, "ticker": ticker}
        for ticker, series in warmup["etf"].items()
        for row in series
    ]
    rows += [
        {**bar, "date": day.date, "ticker": ticker}
        for day in days
        for ticker, bar in day.bars.items()
    ]
    frame = pd.DataFrame(rows, columns=list(HISTORY_COLUMNS))
    frame["date"] = pd.to_datetime(frame["date"])
    frame["ticker"] = frame["ticker"].astype(str)
    if frame.duplicated(subset=["ticker", "date"]).any():
        raise ValueError("duplicate (ticker, date) rows in history")
    return frame.sort_values(["ticker", "date"]).reset_index(drop=True)


def prepare_snapshot(
    history: pd.DataFrame,
    date: str,
    config: EffectiveConfig,
    listing_dates: dict[str, str],
) -> pd.DataFrame:
    """Reproduce the frozen preprocessing pipeline for a single decision date."""
    problems = effective_config_problems()
    if problems:
        raise RuntimeError("frozen config drift: " + "; ".join(problems))
    if history.empty:
        return pd.DataFrame()
    frame = history[history["date"] <= pd.Timestamp(date)].copy()
    if frame.empty:
        return pd.DataFrame()
    frame = etf_shared.add_liquidity_flag(frame)
    frame = etf_shared.add_listing_flag(frame, listing_dates)
    frame = etf_shared.add_deviation_flag(frame)
    frame["close_adj"] = frame["close"]  # freeze: return_basis="price"
    grouped = frame.groupby("ticker")
    frame["ret_60"] = grouped["close_adj"].pct_change(60)
    frame["ret_120"] = grouped["close_adj"].pct_change(120)
    frame["ma20"] = grouped["close_adj"].transform(lambda x: x.rolling(20).mean())
    frame["ma60"] = grouped["close_adj"].transform(lambda x: x.rolling(60).mean())
    frame["trend_ok"] = (frame["close_adj"] > frame["ma20"]) & (frame["ma20"] > frame["ma60"])
    snapshot = frame[frame["date"] == pd.Timestamp(date)].copy()
    return snapshot.reset_index(drop=True)


def _gating(
    ranked_tickers: list[str],
    holdings: dict[str, int],
    kospi_risk_on: bool,
    config: EffectiveConfig,
) -> tuple[Any, list[str], bool]:
    """Gate, target list and empty-target policy exactly as the backtest derives them."""
    params = config.params
    liquidate = bool(params["liquidate_on_risk_off"])
    multi_index = bool(params["enable_multi_index_risk"])
    mode = str(params["multi_index_gating_mode"])
    gating = etf_shared.build_gating_decision(
        ranked_tickers,
        holdings,
        kospi_risk_on=kospi_risk_on,
        us_risk_on=True,  # freeze: enable_multi_index_risk=false
        enable_multi_index_risk=multi_index,
        gating_mode=mode,
        liquidate_on_risk_off=liquidate,
    )
    if not kospi_risk_on and not liquidate:
        targets: list[str] = []
    else:
        limit = int(params["max_positions"]) + int(params["sell_rank_buffer"])
        targets = gating.eligible_ranked[:limit]
    selective_empty_protection = multi_index and mode == "split"
    allow_empty_target_sell = (
        (not kospi_risk_on) and liquidate and not selective_empty_protection
    )
    return gating, targets, allow_empty_target_sell


def compute_signal(
    *,
    history: pd.DataFrame,
    date: str,
    execution_date: str,
    index_signal: dict[str, Any],
    holdings: dict[str, int],
    config: EffectiveConfig,
    listing_dates: dict[str, str],
) -> dict[str, Any]:
    """D-close decision bound to exactly one execution date (the next trading day)."""
    if index_signal is None:
        raise ValueError("index signal unavailable: signal must be held, not defaulted")
    snapshot = prepare_snapshot(history, date, config, listing_dates)
    ranked = etf_shared.rank_etfs(snapshot)
    ranked_tickers = [str(t) for t in ranked.get("ticker", [])]
    risk_on = bool(index_signal["risk_on"])
    gating, targets, allow_empty = _gating(ranked_tickers, holdings, risk_on, config)
    return {
        "decision_date": date,
        "execution_date": execution_date,
        "targets": targets,
        "allowed_groups": sorted(gating.allowed_groups),
        "kospi_risk_on": risk_on,
        "us_risk_on": True,
        "ranked_tickers": ranked_tickers,
        "ranked": [
            {
                "ticker": str(row["ticker"]),
                "score": float(row["score"]),
                "ret_60": None if pd.isna(row.get("ret_60")) else float(row["ret_60"]),
                "ret_120": None if pd.isna(row.get("ret_120")) else float(row["ret_120"]),
            }
            for _, row in ranked.iterrows()
        ],
        # Forced exits and the empty-target policy are re-derived at execution
        # from the then-current holdings; these are the D-close view.
        "forced_exit_tickers": sorted(str(t) for t in gating.forced_exit_tickers),
        "allow_empty_target_sell": allow_empty,
        "snapshot_rows": len(snapshot),
    }


class _Book:
    """Cash/holding/cost-basis mutation shared by carry and rebalance fills."""

    def __init__(self, state: dict[str, Any], day: MarketDay, config: EffectiveConfig) -> None:
        self.cash = float(state["cash"])
        self.holdings = {str(t): int(q) for t, q in state["holdings"].items()}
        self.cost_basis = {str(t): float(b) for t, b in state["cost_basis"].items()}
        self.day = day
        self.config = config
        self.slippage = float(config.params["slippage"])
        self.spread_half = float(config.params["spread_half_pct"])
        self.fills: list[dict[str, Any]] = []

    def reference_price(self, ticker: str, side: str) -> float:
        open_price = float(self.day.bars[ticker]["open"])
        return open_price * (1 + self.spread_half if side == "BUY" else 1 - self.spread_half)

    def decide(self, ticker: str, side: str, qty: int, carry_age: int) -> Any:
        bar = self.day.bars[ticker]
        params = self.config.params
        return decide_execution(
            OrderRequest(
                self.day.date,
                ticker,
                side,
                int(qty),
                float(params["capacity_participation_rate"]),
                carry_age,
                int(params["capacity_max_carry_days"]),
                False,
            ),
            OHLCVBar(
                self.day.date,
                ticker,
                bar["open"],
                bar["high"],
                bar["low"],
                bar["close"],
                bar["volume"],
                bar.get("trading_value"),
            ),
        )

    def reprice(self, order: dict[str, Any], qty: int) -> dict[str, Any]:
        """Mirror of the backtest ``_reprice_execution_order``."""
        adjusted = dict(order)
        ticker = str(order["ticker"])
        reference = float(order["reference_price"])
        adjusted["qty"] = int(qty)
        if order["side"] == "BUY":
            adjusted["estimated_value"] = float(
                int(qty) * etf_shared.apply_buy_cost(reference, self.slippage)
            )
            adjusted["estimated_tax"] = 0.0
            return adjusted
        basis = self.cost_basis.get(ticker)
        tax_rate = (
            float(self.config.params["sell_tax_pct"])
            if ticker in self.config.taxable_tickers
            else 0.0
        )
        value = etf_shared.apply_sell_value(
            reference, int(qty), tax_rate, self.slippage, cost_basis_per_share=basis
        )
        sell_price = reference * (1 - self.slippage)
        gain = max(0.0, int(qty) * (sell_price - basis)) if basis is not None else 0.0
        adjusted["estimated_value"] = float(value)
        adjusted["estimated_tax"] = float(gain * tax_rate)
        return adjusted

    def apply(self, order: dict[str, Any], source: str) -> int:
        """Mirror of the backtest ``_apply_filled_order``."""
        ticker = str(order["ticker"])
        side = str(order["side"])
        qty = int(order["qty"])
        if qty <= 0:
            return 0
        value = float(order["estimated_value"])
        if side == "SELL":
            qty = min(qty, self.holdings.get(ticker, 0))
            if qty <= 0:
                return 0
            remaining = self.holdings[ticker] - qty
            if remaining > 0:
                self.holdings[ticker] = remaining
            else:
                self.holdings.pop(ticker, None)
                self.cost_basis.pop(ticker, None)
            self.cash += value
        else:
            previous = self.holdings.get(ticker, 0)
            self.holdings[ticker] = previous + qty
            if ticker in self.cost_basis and previous > 0:
                self.cost_basis[ticker] = (
                    self.cost_basis[ticker] * previous + value
                ) / self.holdings[ticker]
            else:
                self.cost_basis[ticker] = value / qty
            self.cash = max(0.0, self.cash - value)
        reference = float(order["reference_price"])
        self.fills.append(
            {
                "date": self.day.date,
                "ticker": ticker,
                "side": side,
                "source": source,
                "reason": order.get("reason"),
                "filled_qty": qty,
                "reference_price": reference,
                "open_price": float(self.day.bars[ticker]["open"]),
                "net_value": value,
                "estimated_tax": float(order.get("estimated_tax", 0.0)),
                "fee": float(
                    qty
                    * reference
                    * (
                        (1 + self.slippage) * etf_shared.BUY_FEE_PCT
                        if side == "BUY"
                        else (1 - self.slippage) * etf_shared.SELL_FEE_PCT
                    )
                ),
                "cash_after": self.cash,
            }
        )
        return qty

    def affordable(self, qty: int, unit_cost: float) -> int:
        return min(qty, max(0, int(self.cash // unit_cost))) if unit_cost > 0 else 0


def _diagnostic(
    decision: Any, *, source: str, origin_date: str, filled_qty: int | None = None,
    label: str | None = None, reason: str | None = None,
) -> dict[str, Any]:
    filled = decision.filled_qty if filled_qty is None else filled_qty
    return {
        "ticker": decision.ticker,
        "side": decision.side,
        "source": source,
        "origin_date": origin_date,
        "decision": label or decision.decision.value,
        "capacity_decision": decision.decision.value,
        "requested_qty": decision.requested_qty,
        "capacity_filled_qty": decision.filled_qty,
        "filled_qty": filled,
        "remaining_qty": max(decision.requested_qty - filled, 0),
        "capacity_qty": decision.capacity_qty,
        "bar_volume": decision.bar_volume,
        "reason": reason or decision.reason,
    }


def execute_day(
    *,
    state: dict[str, Any],
    day: MarketDay,
    config: EffectiveConfig,
    next_trading_date: str,
) -> dict[str, Any]:
    """Apply due carries, then the previous decision, at today's open.

    Returns ``{"patch", "fills", "diagnostics", "orders", "execution"}``.  Every
    order attempt leaves a diagnostic, including zero fills and cash limits.
    """
    book = _Book(state, day, config)
    diagnostics: list[dict[str, Any]] = []
    carry_out: list[dict[str, Any]] = []
    excluded_buys: set[str] = set()

    # 1. Carried remainders: exactly one retry on their due date.
    for item in state.get("carry", []):
        if item["due_date"] != day.date:
            raise RuntimeError(
                f"carry due {item['due_date']} cannot execute on {day.date}: "
                "trading-day continuity violated"
            )
        ticker, side = str(item["ticker"]), str(item["side"])
        order = {
            "ticker": ticker,
            "side": side,
            "qty": int(item["remaining_qty"]),
            "reference_price": book.reference_price(ticker, side),
            "reason": "ETF_EXECUTION_CARRY",
        }
        decision = book.decide(ticker, side, order["qty"], carry_age=1)
        excluded_buys.add(ticker)
        fill_qty = decision.filled_qty
        label = reason = None
        if side == "BUY" and fill_qty > 0:
            unit = etf_shared.apply_buy_cost(order["reference_price"], book.slippage)
            affordable = book.affordable(fill_qty, unit)
            if affordable < fill_qty:
                label = "CARRY_CANCELLED"
                reason = "repriced carry BUY remainder cancelled by current cash"
            fill_qty = affordable
        diagnostics.append(
            _diagnostic(
                decision, source="CARRY", origin_date=item["origin_date"],
                filled_qty=fill_qty, label=label, reason=reason,
            )
        )
        if fill_qty > 0:
            book.apply(book.reprice(order, fill_qty), "CARRY")

    # 2. The pending decision, bound to this date.
    orders: list[dict[str, Any]] = []
    execution: dict[str, Any] | None = None
    pending = state.get("pending_signal")
    if pending:
        if pending["execution_date"] != day.date:
            raise RuntimeError(
                f"signal of {pending['decision_date']} is bound to "
                f"{pending['execution_date']}, refusing to execute on {day.date}"
            )
        params = config.params
        gating, targets, allow_empty = _gating(
            list(pending["ranked_tickers"]),
            book.holdings,
            bool(pending["kospi_risk_on"]),
            config,
        )
        buy_prices = {t: book.reference_price(t, "BUY") for t in day.bars}
        sell_prices = {t: book.reference_price(t, "SELL") for t in day.bars}
        orders = etf_shared.build_rebalance_orders(
            current_holdings=book.holdings,
            target_tickers=targets,
            latest_prices={t: float(bar["open"]) for t, bar in day.bars.items()},
            available_cash=book.cash,
            latest_buy_prices=buy_prices,
            latest_sell_prices=sell_prices,
            current_cost_basis=book.cost_basis,
            max_positions=int(params["max_positions"]),
            sell_rank_buffer=int(params["sell_rank_buffer"]),
            slippage=book.slippage,
            sell_tax_pct=float(params["sell_tax_pct"]),
            taxable_tickers=config.taxable_tickers,
            allow_empty_target_sell=allow_empty,
            forced_exit_tickers=set(gating.forced_exit_tickers),
            generate_orders=True,
            max_asset_pct=float(params["max_asset_pct"]),
            target_weight_rebalance=bool(params["target_weight_rebalance"]),
            rebalance_band_pct=float(params["rebalance_band_pct"]),
            trim_overweight_positions=bool(params["trim_overweight_positions"]),
        )
        origin = pending["decision_date"]
        skipped = [
            o for o in orders if o["side"] == "BUY" and str(o["ticker"]) in excluded_buys
        ]
        orders = [o for o in orders if o not in skipped]
        for order in skipped:
            diagnostics.append(
                _no_order(order["ticker"], "BUY", origin, "EXCLUDED_BY_CARRY",
                          "buy excluded: a carry for this ticker was processed today",
                          int(order["qty"]))
            )
        for ticker in targets[: int(params["max_positions"])]:
            if not any(o["ticker"] == ticker and o["side"] == "BUY" for o in orders + skipped):
                diagnostics.append(
                    _no_order(
                        ticker, "BUY", origin, "NO_ORDER",
                        "target produced no buy order (already held at/over budget, "
                        "asset cap, or less than one share affordable); "
                        f"cash={book.cash:.2f}, open={day.bars[ticker]['open']}",
                        0,
                    )
                )

        # Decide (and reprice against the pre-trade cost basis) every order first.
        approved: list[tuple[dict[str, Any], Any, int]] = []
        for order in orders:
            ticker, side = str(order["ticker"]), str(order["side"])
            decision = book.decide(ticker, side, int(order["qty"]), carry_age=0)
            diagnostics.append(_diagnostic(decision, source="REBALANCE", origin_date=origin))
            position = len(diagnostics) - 1
            if decision.decision is DecisionType.PARTIAL_CARRY:
                carry_out.append(
                    {
                        "ticker": ticker,
                        "side": side,
                        "remaining_qty": decision.remaining_qty,
                        "origin_date": origin,
                        "due_date": next_trading_date,
                        "age": 1,
                    }
                )
            if decision.filled_qty > 0:
                approved.append((book.reprice(order, decision.filled_qty), decision, position))

        for order, decision, position in approved:
            if order["side"] == "BUY":
                qty = int(order["qty"])
                unit = float(order["estimated_value"]) / qty
                affordable = book.affordable(qty, unit)
                if affordable < qty:
                    diagnostics[position] = _diagnostic(
                        decision, source="REBALANCE", origin_date=origin,
                        filled_qty=affordable, label="CASH_LIMITED",
                        reason="capacity BUY exceeds current cash; remainder cancelled",
                    )
                    if affordable <= 0:
                        continue
                    order = book.reprice(order, affordable)
            book.apply(order, "REBALANCE")
        execution = {
            "decision_date": origin,
            "targets": targets,
            "forced_exit_tickers": sorted(gating.forced_exit_tickers),
            "allow_empty_target_sell": allow_empty,
        }

    return {
        "patch": {
            "cash": book.cash,
            "holdings": book.holdings,
            "cost_basis": book.cost_basis,
            "carry": carry_out,
            "pending_signal": None,
        },
        "fills": book.fills,
        "diagnostics": diagnostics,
        "orders": [
            {key: o.get(key) for key in ("ticker", "side", "qty", "reference_price", "reason")}
            for o in orders
        ],
        "execution": execution,
    }


def _no_order(
    ticker: str, side: str, origin: str, label: str, reason: str, requested: int
) -> dict[str, Any]:
    return {
        "ticker": str(ticker),
        "side": side,
        "source": "REBALANCE",
        "origin_date": origin,
        "decision": label,
        "capacity_decision": None,
        "requested_qty": requested,
        "capacity_filled_qty": 0,
        "filled_qty": 0,
        "remaining_qty": requested,
        "capacity_qty": 0,
        "bar_volume": None,
        "reason": reason,
    }


def evaluate_equity(
    *, state: dict[str, Any], day: MarketDay, config: EffectiveConfig
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Mark holdings to market, explicitly flagging stale fallback prices."""
    current_prices = {t: bar["close"] for t, bar in day.bars.items()}
    last_valid = dict(state.get("last_valid_prices", {}))
    etf_shared.update_last_valid_prices(last_valid, current_prices)
    values: dict[str, float] = {}
    stale: list[str] = []
    for ticker, qty in state["holdings"].items():
        price = etf_shared.get_valuation_price(ticker, current_prices, last_valid)
        if ticker not in current_prices or price is None:
            stale.append(ticker)
        if price is not None:
            values[ticker] = int(qty) * float(price)
    market_value = sum(values.values())
    equity = float(state["cash"]) + market_value
    curve = list(state.get("equity_curve", []))
    previous = float(curve[-1]["equity"]) if curve else float(state["initial_equity"])
    daily_return = equity / previous - 1.0 if previous else 0.0
    peak = max(float(state.get("peak_equity", equity)), equity)
    # Partial sells can leave the portfolio over the position count or asset cap,
    # exactly as in the backtest.  Surface it; do not invent a correction.
    flags = []
    max_positions = int(config.params["max_positions"])
    if len(state["holdings"]) > max_positions:
        flags.append(f"positions {len(state['holdings'])} > {max_positions}")
    cap = float(config.params["max_asset_pct"])
    for ticker, value in sorted(values.items()):
        if cap > 0 and equity > 0 and value / equity > cap:
            flags.append(f"{ticker} weight {value / equity:.1%} > {cap:.0%}")
    mdd = float(equity / peak - 1.0)
    row = {
        "date": day.date,
        "equity": equity,
        "cash": float(state["cash"]),
        "market_value": market_value,
        "daily_return": daily_return,
        "peak_equity": peak,
        "mdd": mdd,
        "max_drawdown": min(mdd, float(curve[-1]["max_drawdown"]) if curve else 0.0),
        "stale_tickers": sorted(set(stale)),
        "holdings": dict(state["holdings"]),
        "cost_basis": dict(state["cost_basis"]),
        "limit_flags": flags,
    }
    curve.append(row)
    # Each row's drawdown stays relative to the peak known *at that time*.
    # Recomputing past rows against a later high would inject look-ahead and
    # rewrite reports that were already committed.
    return row, {"equity_curve": curve, "peak_equity": peak, "last_valid_prices": last_valid}
