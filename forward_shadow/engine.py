"""Signal and virtual-execution engine built entirely on frozen helpers.

No new ranking, gating, sizing or cost logic is introduced here.  The only new
code is orchestration: strict data gating, D-close signal / D+1-open execution
separation, capacity decisions, and deterministic state accounting.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pandas as pd

import etf_shared
from etf_execution import OHLCVBar, OrderRequest, decide_execution
from forward_shadow.config import EffectiveConfig
from forward_shadow.market import MarketDay


def _assert_frozen_constants(config: EffectiveConfig) -> None:
    params = config.params
    checks = {
        "MIN_AVG_TRADING_VALUE": etf_shared.MIN_AVG_TRADING_VALUE,
        "MIN_LISTING_DAYS": etf_shared.MIN_LISTING_DAYS,
        "MARKET_MA_DAYS": etf_shared.MARKET_MA_DAYS,
        "MARKET_SLOPE_DAYS": etf_shared.MARKET_SLOPE_DAYS,
        "ETF_MAX_POSITIONS": etf_shared.ETF_MAX_POSITIONS,
        "ETF_SELL_RANK_BUFFER": etf_shared.ETF_SELL_RANK_BUFFER,
        "BUY_FEE_PCT": etf_shared.BUY_FEE_PCT,
        "SELL_FEE_PCT": etf_shared.SELL_FEE_PCT,
    }
    expected = {
        "MIN_AVG_TRADING_VALUE": params["min_avg_trading_value"],
        "MIN_LISTING_DAYS": params["min_listing_days"],
        "MARKET_MA_DAYS": params["market_ma_days"],
        "MARKET_SLOPE_DAYS": params["market_slope_days"],
        "ETF_MAX_POSITIONS": params["max_positions"],
        "ETF_SELL_RANK_BUFFER": params["sell_rank_buffer"],
        "BUY_FEE_PCT": params["buy_fee_pct"],
        "SELL_FEE_PCT": params["sell_fee_pct"],
    }
    drift = [f"{key}: code={checks[key]} freeze={expected[key]}" for key in checks if checks[key] != expected[key]]
    raw_weight = os.environ.get("ETF_MOMENTUM_WEIGHT_60")
    if raw_weight is not None:
        try:
            if float(raw_weight) != float(params["momentum_weight_60"]):
                drift.append(
                    f"ETF_MOMENTUM_WEIGHT_60: env={raw_weight} "
                    f"freeze={params['momentum_weight_60']}"
                )
        except ValueError:
            drift.append(f"ETF_MOMENTUM_WEIGHT_60: invalid env value {raw_weight!r}")
    if drift:
        raise RuntimeError("frozen constant drift: " + "; ".join(drift))


def build_history_frame(
    snapshots_dir: Path,
    tickers: list[str],
    up_to_date: str,
    seed: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build a long OHLCV frame from audit snapshots (plus optional warm-up seed)."""
    frames: list[pd.DataFrame] = []
    if seed is not None and not seed.empty:
        frames.append(seed.copy())
    if snapshots_dir.exists():
        for path in sorted(snapshots_dir.glob("*.json")):
            if path.stem > up_to_date:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not payload.get("validation", {}).get("ok"):
                continue
            rows = []
            for ticker, bar in payload.get("bars", {}).items():
                rows.append(
                    {
                        "date": pd.Timestamp(payload["date"]),
                        "ticker": str(ticker),
                        "open": bar.get("open"),
                        "high": bar.get("high"),
                        "low": bar.get("low"),
                        "close": bar.get("close"),
                        "volume": bar.get("volume"),
                        "trading_value": bar.get("trading_value"),
                        "nav": bar.get("nav"),
                    }
                )
            if rows:
                frames.append(pd.DataFrame(rows))
    if not frames:
        return pd.DataFrame(
            columns=[
                "date", "ticker", "open", "high", "low", "close",
                "volume", "trading_value", "nav",
            ]
        )
    frame = pd.concat(frames, ignore_index=True)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["ticker"] = frame["ticker"].astype(str)
    frame = frame[frame["ticker"].isin({str(t) for t in tickers})]
    frame = frame.dropna(subset=["date"]).sort_values(["ticker", "date"])
    return frame.drop_duplicates(subset=["ticker", "date"], keep="last").reset_index(drop=True)


def prepare_snapshot(
    history: pd.DataFrame,
    date: str,
    config: EffectiveConfig,
    listing_dates: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Reproduce the frozen preprocessing pipeline for a single decision date."""
    _assert_frozen_constants(config)
    if history.empty:
        return pd.DataFrame()
    frame = history[history["date"] <= pd.Timestamp(date)].copy()
    if frame.empty:
        return pd.DataFrame()
    frame = etf_shared.add_liquidity_flag(frame)
    frame = etf_shared.add_listing_flag(frame, listing_dates)
    frame = etf_shared.add_deviation_flag(frame)
    frame["close_adj"] = frame["close"]
    grouped = frame.groupby("ticker")
    frame["ret_60"] = grouped["close_adj"].pct_change(60)
    frame["ret_120"] = grouped["close_adj"].pct_change(120)
    frame["ma20"] = grouped["close_adj"].transform(lambda x: x.rolling(20).mean())
    frame["ma60"] = grouped["close_adj"].transform(lambda x: x.rolling(60).mean())
    frame["trend_ok"] = (frame["close_adj"] > frame["ma20"]) & (frame["ma20"] > frame["ma60"])
    snapshot = frame[frame["date"] == pd.Timestamp(date)].copy()
    return snapshot.reset_index(drop=True)


def compute_signal(
    *,
    history: pd.DataFrame,
    date: str,
    index_signal: dict[str, Any],
    holdings: dict[str, int],
    config: EffectiveConfig,
    listing_dates: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compute the D-close target list using only frozen strategy helpers."""
    if index_signal is None:
        raise ValueError("index signal unavailable: signal must be held, not defaulted")
    snapshot = prepare_snapshot(history, date, config, listing_dates)
    ranked = etf_shared.rank_etfs(snapshot)
    ranked_tickers = [str(t) for t in ranked.get("ticker", [])]
    params = config.params
    gating = etf_shared.build_gating_decision(
        ranked_tickers,
        holdings,
        kospi_risk_on=bool(index_signal["risk_on"]),
        us_risk_on=True,  # freeze: enable_multi_index_risk=false
        enable_multi_index_risk=False,
        gating_mode="split",  # freeze: multi_index_gating_mode=split
        liquidate_on_risk_off=bool(params["liquidate_on_risk_off"]),
    )
    targets = gating.eligible_ranked[: int(params["max_positions"]) + int(params["sell_rank_buffer"])]
    return {
        "decision_date": date,
        "targets": targets,
        "allowed_groups": sorted(gating.allowed_groups),
        "kospi_risk_on": bool(index_signal["risk_on"]),
        "us_risk_on": True,
        "ranked": [
            {
                "ticker": str(row["ticker"]),
                "score": float(row["score"]),
                "ret_60": None if pd.isna(row.get("ret_60")) else float(row["ret_60"]),
                "ret_120": None if pd.isna(row.get("ret_120")) else float(row["ret_120"]),
            }
            for _, row in ranked.iterrows()
        ],
        "forced_exit_tickers": sorted(str(t) for t in gating.forced_exit_tickers),
        "snapshot_rows": len(snapshot),
    }


def _order_price_maps(
    day: MarketDay, spread_half: float
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    latest: dict[str, float] = {}
    buys: dict[str, float] = {}
    sells: dict[str, float] = {}
    for ticker, bar in day.bars.items():
        op = bar.get("open")
        if op is None or not (float(op) > 0):
            continue
        price = float(op)
        latest[ticker] = price
        buys[ticker] = price * (1 + spread_half)
        sells[ticker] = price * (1 - spread_half)
    return latest, buys, sells


def _reprice(order: dict[str, Any], filled_qty: int, slippage: float, cost_basis: float | None,
             tax_rate: float) -> dict[str, Any]:
    adjusted = dict(order)
    reference = float(order.get("reference_price") or 0.0)
    adjusted["qty"] = int(filled_qty)
    if order.get("side") == "BUY":
        adjusted["estimated_value"] = float(
            filled_qty * etf_shared.apply_buy_cost(reference, slippage)
        )
        adjusted["estimated_tax"] = 0.0
        return adjusted
    net = etf_shared.apply_sell_value(
        reference,
        int(filled_qty),
        tax_rate,
        slippage,
        cost_basis_per_share=cost_basis,
    )
    sell_price = reference * (1 - slippage)
    gain = (
        max(0.0, int(filled_qty) * (sell_price - float(cost_basis)))
        if cost_basis is not None
        else 0.0
    )
    adjusted["estimated_value"] = float(net)
    adjusted["estimated_tax"] = float(gain * tax_rate)
    return adjusted


def execute_pending(
    *,
    state: dict[str, Any],
    day: MarketDay,
    config: EffectiveConfig,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply the previous decision day's signal using today's OHLCV."""
    pending = state.get("pending_signal")
    carry_in = list(state.get("carry", []))
    if not pending and not carry_in:
        return [], {}
    params = config.params
    slippage = float(params["slippage"])
    latest, buys, sells = _order_price_maps(day, float(params["spread_half_pct"]))
    if pending:
        orders = etf_shared.build_rebalance_orders(
            state["holdings"],
            list(pending["targets"]),
            latest,
            float(state["cash"]),
            latest_buy_prices=buys,
            latest_sell_prices=sells,
            current_cost_basis=state["cost_basis"],
            max_positions=int(params["max_positions"]),
            sell_rank_buffer=int(params["sell_rank_buffer"]),
            slippage=slippage,
            sell_tax_pct=float(params["sell_tax_pct"]),
            taxable_tickers=config.taxable_tickers,
            allow_empty_target_sell=False,
            forced_exit_tickers=set(pending.get("forced_exit_tickers", [])),
            max_asset_pct=float(params["max_asset_pct"]),
            target_weight_rebalance=bool(params["target_weight_rebalance"]),
            rebalance_band_pct=float(params["rebalance_band_pct"]),
        )
    else:
        orders = []
    # Carry retries happen on the next eligible trading date even when no
    # periodic decision is due (same rule as the frozen backtest).  Entries
    # whose ticker/side is not covered by a fresh order are retried here.
    order_keys = {(str(o.get("ticker")), str(o.get("side"))) for o in orders}
    for item in carry_in:
        ticker = str(item["ticker"])
        side = str(item["side"])
        if (ticker, side) in order_keys:
            continue
        price = buys.get(ticker) if side == "BUY" else sells.get(ticker)
        if price is None:
            continue
        orders.append(
            {
                "ticker": ticker,
                "side": side,
                "qty": int(item["remaining_qty"]),
                "reference_price": price,
                "reason": "CARRY_RETRY",
            }
        )
        order_keys.add((ticker, side))

    cash = float(state["cash"])
    holdings = dict(state["holdings"])
    cost_basis = dict(state["cost_basis"])
    carry = list(state.get("carry", []))
    fills: list[dict[str, Any]] = []

    for order in orders:
        ticker = str(order.get("ticker"))
        side = str(order.get("side"))
        requested = int(order.get("qty", 0) or 0)
        if requested <= 0 or ticker not in day.bars:
            continue
        existing = next(
            (c for c in carry if c["ticker"] == ticker and c["side"] == side), None
        )
        carry_age = int(existing["age"]) if existing else 0
        if existing:
            carry = [c for c in carry if c is not existing]
        bar_data = day.bars[ticker]
        bar = OHLCVBar(
            date=day.date,
            ticker=ticker,
            open=bar_data.get("open"),
            high=bar_data.get("high"),
            low=bar_data.get("low"),
            close=bar_data.get("close"),
            volume=bar_data.get("volume"),
        )
        requested_eff = requested
        if side == "BUY":
            # Sales may have been only partially filled by the capacity model,
            # so the order builder's cash budget can overstate available funds.
            unit_cost = etf_shared.apply_buy_cost(
                float(order.get("reference_price") or 0.0), slippage
            )
            affordable = int(cash / unit_cost) if unit_cost > 0 else 0
            requested_eff = min(requested, max(affordable, 0))
        if requested_eff <= 0:
            continue
        decision = decide_execution(
            OrderRequest(
                date=day.date,
                ticker=ticker,
                side=side,
                requested_qty=requested_eff,
                participation_rate=float(params["capacity_participation_rate"]),
                carry_age=carry_age,
                max_carry_days=int(params["capacity_max_carry_days"]),
                explicit_suspension=False,
            ),
            bar,
        )
        filled = int(decision.filled_qty)
        if filled > 0:
            basis = cost_basis.get(ticker)
            tax_rate = (
                float(params["sell_tax_pct"])
                if side == "SELL" and ticker in config.taxable_tickers
                else 0.0
            )
            priced = _reprice(order, filled, slippage, basis, tax_rate)
            value = float(priced["estimated_value"])
            if side == "SELL":
                qty = min(filled, int(holdings.get(ticker, 0) or 0))
                if qty <= 0:
                    continue
                remaining = int(holdings.get(ticker, 0) or 0) - qty
                if remaining > 0:
                    holdings[ticker] = remaining
                else:
                    holdings.pop(ticker, None)
                    cost_basis.pop(ticker, None)
                cash += value
            else:
                previous = int(holdings.get(ticker, 0) or 0)
                holdings[ticker] = previous + filled
                unit = value / filled
                if ticker in cost_basis and previous > 0:
                    cost_basis[ticker] = (
                        float(cost_basis[ticker]) * previous + value
                    ) / holdings[ticker]
                else:
                    cost_basis[ticker] = unit
                cash -= value  # affordable cap guarantees cash never goes negative
            fills.append(
                {
                    "date": day.date,
                    "ticker": ticker,
                    "side": side,
                    "requested_qty": requested,
                    "filled_qty": filled,
                    "remaining_qty": requested - filled,
                    "decision": decision.decision.value,
                    "reason": decision.reason,
                    "reference_price": order.get("reference_price"),
                    "net_value": value,
                    "estimated_tax": float(priced["estimated_tax"]),
                    "capacity_qty": int(decision.capacity_qty),
                    "carry_age": carry_age,
                }
            )
        unfilled = requested - filled
        if unfilled > 0 and carry_age < int(params["capacity_max_carry_days"]):
            carry.append(
                {
                    "ticker": ticker,
                    "side": side,
                    "remaining_qty": unfilled,
                    "age": carry_age + 1,
                    "origin_date": day.date,
                    "due_date": day.date,
                }
            )
    patch = {
        "cash": cash,
        "holdings": holdings,
        "cost_basis": cost_basis,
        "carry": carry,
    }
    return fills, patch


def evaluate_equity(
    *,
    state: dict[str, Any],
    day: MarketDay,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Mark holdings to market, explicitly flagging stale fallback prices."""
    current_prices = {
        ticker: bar.get("close")
        for ticker, bar in day.bars.items()
        if bar.get("close") is not None
    }
    last_valid = dict(state.get("last_valid_prices", {}))
    market_value = 0.0
    stale: list[str] = []
    for ticker, qty in state["holdings"].items():
        price = etf_shared.get_valuation_price(ticker, current_prices, last_valid)
        if price is None:
            stale.append(ticker)
            continue
        if ticker not in current_prices:
            stale.append(ticker)
        market_value += int(qty) * float(price)
    equity = float(state["cash"]) + market_value
    curve = list(state.get("equity_curve", []))
    previous = float(curve[-1]["equity"]) if curve else float(state["peak_equity"])
    daily_return = equity / previous - 1.0 if previous else 0.0
    peak = max(float(state.get("peak_equity", equity)), equity)
    row = {
        "date": day.date,
        "equity": equity,
        "cash": float(state["cash"]),
        "market_value": market_value,
        "daily_return": daily_return,
        "peak_equity": peak,
        "mdd": float(equity / peak - 1.0),
        "stale_tickers": sorted(set(stale)),
        "holdings": dict(state["holdings"]),
    }
    curve.append(row)
    # Each row's drawdown stays relative to the peak known *at that time*.
    # Recomputing past rows against a later high would inject look-ahead and
    # rewrite reports that were already committed.
    patch = {
        "equity_curve": curve,
        "peak_equity": peak,
        "last_valid_prices": last_valid,
    }
    return row, patch
