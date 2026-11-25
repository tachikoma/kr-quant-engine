"""P1-A-1: 위험 관리 오버레이 검증(연구용).

동결 전략(20일 리밸런싱)에 대해 위험 관리 주기를 분리했을 때의 성과·낙폭·비용을 비교한다.
새 전략 채택이 아니라 counterfactual 연구이며, 신규 opt-in 옵션(기본 비활성)만 사용한다.

시나리오(사전 고정, 결과 후 임계값 추가 탐색 없음):
- baseline: 동결
- A: 일별 risk_off 청산(`daily_risk_off_exit=True`)
- B1/B2/B3: 일별 portfolio trailing stop 10/15/20%(`exit_check_days=1`)
- C: 리밸런싱 10일

실행::

    uv run python scripts/p1a1_risk_overlay.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import p0b_execution_stress as p0b
import p0c_regime_drawdown as p0c

import run_etf_backtest as rtb

OUTPUT_DIR = ROOT / "outputs_p1a1"
CAP = 0.85
AUM = float(rtb.INITIAL_CASH)
BASE_SLIPPAGE = 0.0005
STRESS_SLIPPAGE = 0.005
STRESS_FEE_MULTIPLIER = 3.0
TOP_DRAWDOWNS = 5

SCENARIOS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("baseline", {}),
    ("A_daily_risk_off", {"daily_risk_off_exit": True}),
    ("B1_pstop_10", {"exit_check_days": 1, "portfolio_trailing_stop_pct": 0.10}),
    ("B2_pstop_15", {"exit_check_days": 1, "portfolio_trailing_stop_pct": 0.15}),
    ("B3_pstop_20", {"exit_check_days": 1, "portfolio_trailing_stop_pct": 0.20}),
    ("C_rebalance_10", {"rebalance_step_days": 10}),
)


def run_scenario(
    price: pd.DataFrame,
    index_df: pd.DataFrame,
    dates: list[pd.Timestamp],
    *,
    slippage: float,
    fee_multiplier: float,
    daily_risk_off_exit: bool = False,
    exit_check_days: int | None = None,
    portfolio_trailing_stop_pct: float | None = None,
    rebalance_step_days: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    original_step = rtb.REBALANCE_STEP_DAYS
    if rebalance_step_days is not None:
        rtb.REBALANCE_STEP_DAYS = int(rebalance_step_days)
    try:
        with p0b.patched_costs(fee_multiplier):
            curve, trades = rtb.run_etf_strategy(
                AUM,
                dates,
                index_df,
                use_market_filter=True,
                max_positions=int(p0b.APPLIED["max_positions"]),
                slippage=slippage,
                risk_off_liquidate=True,
                price_data=price,
                max_asset_pct=CAP,
                exit_check_days=exit_check_days,
                trailing_stop_pct=0.0 if portfolio_trailing_stop_pct is not None else None,
                portfolio_trailing_stop_pct=portfolio_trailing_stop_pct,
                daily_risk_off_exit=daily_risk_off_exit,
            )
    finally:
        rtb.REBALANCE_STEP_DAYS = original_step
    p0b.assert_legacy_reconciled(curve, trades, AUM, "p1a1 scenario")
    return curve, trades


def slice_stats(curve: pd.DataFrame, start: pd.Timestamp | None, end: pd.Timestamp | None) -> dict:
    """구간 통계. ``start``가 있으면 직전 평가일을 기준점으로 포함해 첫날 수익/낙폭을 잡는다."""
    frame = curve.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    window = frame
    if start is not None:
        window = frame[frame["date"] >= start]
    if end is not None:
        window = window[window["date"] <= end]
    if len(window) < 2:
        return {
            "cagr": None,
            "mdd": None,
            "sharpe": None,
            "total_return": None,
            "days": len(window),
        }
    base = frame[frame["date"] < start].tail(1) if start is not None else frame.iloc[0:0]
    evaluation = pd.concat([base, window]).drop_duplicates("date", keep="last")
    stats = rtb.calc_stats(evaluation, "equity")
    return {
        "cagr": float(stats["cagr"]),
        "mdd": float(stats["mdd"]),
        "sharpe": None if pd.isna(stats["sharpe"]) else float(stats["sharpe"]),
        "total_return": float(stats["total_return"]),
        "days": len(window),
    }


def _risk_run_start(
    labels: pd.DataFrame, dates: list[pd.Timestamp], index: int
) -> pd.Timestamp | None:
    """청산 실행일 직전의 가장 최근 risk_off 구간 시작일(신호일)을 찾는다(폴백)."""
    for cursor in range(index - 1, -1, -1):
        date = pd.Timestamp(dates[cursor])
        if date in labels.index and labels.at[date, "risk"] == "risk_off":
            origin = cursor
            while origin > 0:
                previous = pd.Timestamp(dates[origin - 1])
                if previous in labels.index and labels.at[previous, "risk"] == "risk_off":
                    origin -= 1
                    continue
                break
            return pd.Timestamp(dates[origin])
    return None


def exit_events(
    trades: pd.DataFrame,
    reason: str | None,
    price: pd.DataFrame,
    dates: list[pd.Timestamp],
    labels: pd.DataFrame,
    *,
    risk_based: bool,
) -> pd.DataFrame:
    if reason is None:
        return pd.DataFrame()
    sells = trades[
        (trades["side"] == "SELL") & (trades["reason"].astype(str) == reason)
    ].copy()
    if sells.empty:
        return pd.DataFrame()
    closes = p0c.close_matrix(price)
    position = {pd.Timestamp(d): index for index, d in enumerate(dates)}
    rows = []
    for trade in sells.itertuples():
        date = pd.Timestamp(trade.date)
        ticker = str(trade.ticker)
        index = position.get(date, -1)
        recorded_origin = getattr(trade, "exit_origin_date", None)
        if recorded_origin is None or pd.isna(recorded_origin):
            recorded_origin = None
        decision: pd.Timestamp | None
        if risk_based:
            signal = (
                pd.Timestamp(recorded_origin)
                if recorded_origin is not None
                else _risk_run_start(labels, dates, index)
            )
            decision = signal
        else:
            decision = pd.Timestamp(dates[index - 1]) if index > 0 else None
            signal = decision
        forward: dict[str, float | None] = {}
        for horizon in (5, 20):
            target = index + horizon
            if index >= 0 and target < len(dates):
                later = dates[target]
                if ticker in closes.columns and pd.notna(closes.at[later, ticker]):
                    forward[f"fwd{horizon}"] = float(
                        closes.at[later, ticker] / closes.at[date, ticker] - 1
                    )
                else:
                    forward[f"fwd{horizon}"] = None
            else:
                forward[f"fwd{horizon}"] = None
        rows.append(
            {
                "exec_date": date,
                "decision_date": decision,
                "signal_date": signal,
                "event_id": signal if signal is not None else decision,
                "lag_days": (
                    None
                    if decision is None or index < 0
                    else int(index - position.get(decision, index))
                ),
                "ticker": ticker,
                "qty": int(trade.qty),
                "price": float(trade.price),
                "reason": reason,
                **forward,
            }
        )
    return pd.DataFrame(rows)


def whipsaw(events: pd.DataFrame, trades: pd.DataFrame, dates: list[pd.Timestamp]) -> dict[str, Any]:
    if events.empty:
        return {"count": 0, "avg_price_change": None}
    position = {pd.Timestamp(d): index for index, d in enumerate(dates)}
    changes = []
    for event in events.itertuples():
        date = pd.Timestamp(event.exec_date)
        ticker = str(event.ticker)
        index = position.get(date, -1)
        if index < 0:
            continue
        window_end = dates[min(index + 20, len(dates) - 1)]
        buys = trades[
            (trades["side"] == "BUY")
            & (trades["ticker"].astype(str) == ticker)
            & (trades["date"] > date)
            & (trades["date"] <= window_end)
        ]
        if buys.empty:
            continue
        buy = buys.iloc[0]
        changes.append(float(buy["price"]) / float(event.price) - 1)
    return {
        "count": len(changes),
        "avg_price_change": float(np.mean(changes)) if changes else None,
    }


def scenario_metrics(
    label: str,
    cost_case: str,
    curve: pd.DataFrame,
    trades: pd.DataFrame,
    dates: list[pd.Timestamp],
    price: pd.DataFrame,
    labels: pd.DataFrame,
    slippage: float,
    fee_multiplier: float,
    reason: str | None,
    oos_start: pd.Timestamp,
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
    stats = p0b.performance(AUM, dates, curve)
    trading = p0b.trading_stats(curve, trades)
    costs = p0b.cost_breakdown(trades, slippage, fee_multiplier)
    cash_weight = (curve["cash"] / curve["equity"]).replace([np.inf, -np.inf], np.nan)
    exposure = (curve["market_value"] / curve["equity"]).replace([np.inf, -np.inf], np.nan)
    equity = p0c.equity_with_initial(curve, dates)
    episodes = p0c.drawdown_episodes(equity)
    top = episodes.head(TOP_DRAWDOWNS)
    worst = top.iloc[0]
    risk_based = reason == "ETF_DAILY_RISK_OFF"
    events = exit_events(trades, reason, price, dates, labels, risk_based=risk_based)
    whip = whipsaw(events, trades, dates)
    lag_series = (
        pd.to_numeric(events["lag_days"], errors="coerce").dropna()
        if not events.empty
        else pd.Series(dtype=float)
    )
    event_ids = (
        pd.to_datetime(events["event_id"]).dropna().drop_duplicates()
        if not events.empty
        else pd.Series(dtype="datetime64[ns]")
    )
    same_day = 0
    for date, group in trades.groupby("date"):
        sides = group.groupby(group["ticker"].astype(str))["side"].apply(set)
        same_day += int(sum(1 for value in sides if {"BUY", "SELL"} <= value))
    if same_day:
        raise AssertionError(f"{label}/{cost_case}: 같은 실행일 매수·매도 혼재 {same_day}건")
    equity_path = p0c.equity_with_initial(curve, dates)
    is_stats = slice_stats(equity_path, None, oos_start - pd.Timedelta(days=1))
    oos_stats = slice_stats(equity_path, oos_start, None)
    reentry = 0
    if not events.empty:
        for event in events.itertuples():
            buys = trades[
                (trades["side"] == "BUY")
                & (trades["ticker"].astype(str) == str(event.ticker))
                & (trades["date"] > pd.Timestamp(event.exec_date))
            ]
            reentry += int(not buys.empty)
    row = {
        "scenario": label,
        "cost_case": cost_case,
        **stats,
        "calmar": stats["calmar"],
        **trading,
        "avg_cash_weight": float(cash_weight.mean()),
        "avg_exposure": float(exposure.mean()),
        "total_cost": costs["total_run_rate_cost"],
        "cost_pct": costs["cost_pct_of_notional"],
        "risk_exit_count": len(events),
        "reentry_count": reentry,
        "same_day_reentry": same_day,
        "event_basis": "risk_off_signal" if risk_based else "portfolio_stop_decision",
        "independent_event_count": len(event_ids),
        "event_years": ",".join(sorted({str(pd.Timestamp(x).year) for x in event_ids})),
        "retry_exit_count": int((lag_series > 1).sum()) if not lag_series.empty else 0,
        "signal_to_exit_median_days": (
            float(lag_series.median()) if risk_based and not lag_series.empty else None
        ),
        "signal_to_exit_max_days": (
            float(lag_series.max()) if risk_based and not lag_series.empty else None
        ),
        "decision_to_exec_lag_days": (
            float(lag_series.median()) if not lag_series.empty else None
        ),
        "decision_to_exec_max_days": (
            float(lag_series.max()) if not lag_series.empty else None
        ),
        "whipsaw_count": whip["count"],
        "whipsaw_avg_price_change": whip["avg_price_change"],
        "missed_fwd5_mean": (
            float(pd.to_numeric(events["fwd5"], errors="coerce").mean())
            if not events.empty
            else None
        ),
        "missed_fwd20_mean": (
            float(pd.to_numeric(events["fwd20"], errors="coerce").mean())
            if not events.empty
            else None
        ),
        "missed_fwd5_positive_share": (
            float((pd.to_numeric(events["fwd5"], errors="coerce") > 0).mean())
            if not events.empty
            else None
        ),
        "missed_fwd20_positive_share": (
            float((pd.to_numeric(events["fwd20"], errors="coerce") > 0).mean())
            if not events.empty
            else None
        ),
        "worst_mdd": float(worst["mdd"]),
        "worst_trough_days": int(worst["trough_days"]),
        "worst_recovery_days": (
            None if pd.isna(worst["recovery_days"]) else int(worst["recovery_days"])
        ),
        "worst_unrecovered": bool(worst["unrecovered"]),
        "is_cagr": is_stats["cagr"],
        "is_total_return": is_stats["total_return"],
        "is_mdd": is_stats["mdd"],
        "is_sharpe": is_stats["sharpe"],
        "oos_cagr": oos_stats["cagr"],
        "oos_total_return": oos_stats["total_return"],
        "oos_mdd": oos_stats["mdd"],
        "oos_sharpe": oos_stats["sharpe"],
        "oos_days": oos_stats["days"],
    }
    drawdowns = pd.DataFrame(
        [
            {
                "scenario": label,
                "cost_case": cost_case,
                "rank": rank,
                "peak_date": row_data["peak_date"].strftime("%Y-%m-%d"),
                "trough_date": row_data["trough_date"].strftime("%Y-%m-%d"),
                "recovery_date": (
                    row_data["recovery_date"].strftime("%Y-%m-%d")
                    if pd.notna(row_data["recovery_date"])
                    else None
                ),
                "mdd": float(row_data["mdd"]),
                "trough_days": int(row_data["trough_days"]),
                "recovery_days": (
                    None
                    if pd.isna(row_data["recovery_days"])
                    else int(row_data["recovery_days"])
                ),
            }
            for rank, (_, row_data) in enumerate(top.iterrows(), start=1)
        ]
    )
    if not events.empty:
        events = events.assign(scenario=label, cost_case=cost_case)
        events["date"] = pd.to_datetime(events["exec_date"])
        events["signal_date"] = pd.to_datetime(events["signal_date"])
        events["decision_date"] = pd.to_datetime(events["decision_date"])
    return row, drawdowns, events


def main() -> int:
    frozen = p0b.configure_frozen_settings(CAP)
    p0b.rtb.START, p0b.rtb.END = p0b.DEFAULT_START, p0b.DEFAULT_END
    preflight = p0b.preflight_cache(p0b.DEFAULT_START, p0b.DEFAULT_END)
    digest_before = p0b.data_cache_digest()
    price, index_df, dates = p0b.load_inputs()
    p0b.verify_applied_settings()
    missing = sorted({str(t) for t in rtb.ETF_LIST} - set(price["ticker"].astype(str)))
    if missing:
        raise SystemExit(f"[p1a1] 필수 종목 누락: {missing}")
    labels = p0c.regime_labels(index_df, dates)

    baseline_curve, _ = run_scenario(
        price, index_df, dates, slippage=BASE_SLIPPAGE, fee_multiplier=1.0
    )
    baseline = p0b.performance(AUM, dates, baseline_curve)
    expected = {"cagr": 0.2454113778672673, "mdd": -0.5121244554166072, "sharpe": 1.0391357658469746}
    deltas = {key: abs(baseline[key] - expected[key]) for key in expected}
    if any(value > 1e-9 for value in deltas.values()):
        raise SystemExit(f"[p1a1] 기준 재현 실패: {baseline} deltas={deltas}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    metrics_rows: list[dict[str, Any]] = []
    drawdown_frames: list[pd.DataFrame] = []
    exit_frames: list[pd.DataFrame] = []
    for cost_case, slippage, multiplier in (
        ("base", BASE_SLIPPAGE, 1.0),
        ("stress_50bp_3x", STRESS_SLIPPAGE, STRESS_FEE_MULTIPLIER),
    ):
        for label, kwargs in SCENARIOS:
            reason = (
                "ETF_DAILY_RISK_OFF"
                if "daily_risk_off_exit" in kwargs
                else "ETF_PORTFOLIO_TRAILING_STOP"
                if "portfolio_trailing_stop_pct" in kwargs
                else None
            )
            curve, trades = run_scenario(
                price,
                index_df,
                dates,
                slippage=slippage,
                fee_multiplier=multiplier,
                **kwargs,
            )
            row, drawdowns, events = scenario_metrics(
                label,
                cost_case,
                curve,
                trades,
                dates,
                price,
                labels,
                slippage,
                multiplier,
                reason,
                pd.Timestamp(frozen["oos_start_date"]),
            )
            metrics_rows.append(row)
            drawdown_frames.append(drawdowns)
            if not events.empty:
                exit_frames.append(events)
            print(
                f"[p1a1] {cost_case:14s} {label:18s} cagr={row['cagr']:.3%} "
                f"mdd={row['mdd']:.2%} sharpe={row['sharpe']:.3f} "
                f"cost={row['total_cost']:,.0f}"
            )

    metrics = pd.DataFrame(metrics_rows)
    metrics.to_csv(OUTPUT_DIR / "scenario_metrics.csv", index=False, encoding="utf-8-sig")
    pd.concat(drawdown_frames, ignore_index=True).to_csv(
        OUTPUT_DIR / "scenario_drawdowns.csv", index=False, encoding="utf-8-sig"
    )
    if exit_frames:
        pd.concat(exit_frames, ignore_index=True).to_csv(
            OUTPUT_DIR / "exit_events.csv", index=False, encoding="utf-8-sig"
        )

    if p0b.data_cache_digest() != digest_before:
        raise SystemExit("[p1a1] 실행 중 data_cache가 변경되었습니다.")

    metadata = {
        "phase": "P1-A-1",
        "period": {"start": p0b.DEFAULT_START, "end": p0b.DEFAULT_END},
        "trading_days": len(dates),
        "max_asset_pct": CAP,
        "spread_pct": frozen["spread_pct"],
        "freeze_sha256": frozen["freeze_sha256"],
        "oos_start_date": frozen["oos_start_date"],
        "scenarios": [label for label, _ in SCENARIOS],
        "cost_cases": {"base": BASE_SLIPPAGE, "stress": [STRESS_SLIPPAGE, STRESS_FEE_MULTIPLIER]},
        "preflight": preflight,
        "git_commit": p0b.git_commit(),
        "data_cache_digest": digest_before,
        "data_cache_unchanged": True,
        "baseline": baseline,
        "baseline_matches_p0b": True,
        "artifacts": sorted(path.name for path in OUTPUT_DIR.glob("*")),
    }
    (OUTPUT_DIR / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"[p1a1] 완료: {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
