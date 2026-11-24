"""P0-C: Market Regime 및 MDD 원인 분석.

P0-B 동결 전략(85% cap)을 기준으로 낙폭 구간·레짐별 성과·위험 필터 counterfactual·
비중 한도 비교를 수행한다. 새 전략/최적화가 아니라 손실 구조 진단이다.

재사용:
- `scripts/p0b_execution_stress.py`: 동결 설정 강제·캐시 가드·기준 실행·회계 검산
- `scripts/trade_performance_attribution.py`: FIFO 실현손익
- `run_etf_backtest.py`: 전략 엔진, `calc_stats`, `calc_trading_stats`

산출물은 `outputs_p0c/`에만 쓴다.

실행::

    uv run python scripts/p0c_regime_drawdown.py
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

import factorial_ablation as ablation_mod
import p0b_execution_stress as p0b
import trade_performance_attribution as attribution

import etf_shared
import run_etf_backtest as rtb

OUTPUT_DIR = ROOT / "outputs_p0c"
CAP_FROZEN = 0.85
CAP_COMPARE = 0.5
FROZEN_SLIPPAGE = 0.0005
AUM = float(rtb.INITIAL_CASH)
TOP_DRAWDOWNS = 5
TREND_WINDOW = 60
TREND_THRESHOLD = 0.05
VOL_WINDOW = 20
VOL_MEDIAN_WINDOW = 250


# --------------------------------------------------------------------------- #
# 데이터/기준 실행
# --------------------------------------------------------------------------- #
def run_variant(
    price: pd.DataFrame,
    index_df: pd.DataFrame,
    dates: list[pd.Timestamp],
    *,
    cap: float,
    use_market_filter: bool = True,
    group_override: tuple[str, ...] | None = None,
    label: str = "variant",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """동결 조건에서 필터/override만 바꾼 counterfactual 실행(회계 검산 포함)."""
    original_override = set(etf_shared.GROUP_RISK_OVERRIDE)
    if group_override is not None:
        etf_shared.GROUP_RISK_OVERRIDE.clear()
        etf_shared.GROUP_RISK_OVERRIDE.update(group_override)
    try:
        curve, trades = rtb.run_etf_strategy(
            AUM,
            dates,
            index_df,
            use_market_filter=use_market_filter,
            max_positions=int(p0b.APPLIED["max_positions"]),
            slippage=FROZEN_SLIPPAGE,
            risk_off_liquidate=True,
            price_data=price,
            max_asset_pct=cap,
        )
    finally:
        etf_shared.GROUP_RISK_OVERRIDE.clear()
        etf_shared.GROUP_RISK_OVERRIDE.update(original_override)
    p0b.assert_legacy_reconciled(curve, trades, AUM, label)
    return curve, trades


def equity_with_initial(curve: pd.DataFrame, dates: list[pd.Timestamp]) -> pd.DataFrame:
    return p0b.evaluation_curve(AUM, dates, curve)


def close_matrix(price: pd.DataFrame) -> pd.DataFrame:
    matrix = price.pivot_table(index="date", columns="ticker", values="close")
    return matrix.sort_index().ffill()


def contributions(
    curve: pd.DataFrame,
    trades: pd.DataFrame,
    closes: pd.DataFrame,
    initial_cash: float = AUM,
) -> pd.DataFrame:
    """일자·종목별 손익 기여(MTM + 체결 현금흐름 + 신규/청산분 평가).

    identity: Σ contribution + Σ distribution_cash == equity[-1] - 초기자본.
    (첫 평가일의 기여는 초기 현금 대비 변화를 포함한다.)
    """
    by_date = {pd.Timestamp(d): grp for d, grp in trades.groupby("date")}
    rows: list[dict[str, Any]] = []
    qty: dict[str, float] = {}
    prev_date: pd.Timestamp | None = None
    for date in pd.to_datetime(curve["date"]):
        date = pd.Timestamp(date)
        if prev_date is not None:
            for ticker, held in qty.items():
                if held == 0 or ticker not in closes.columns:
                    continue
                prev_close = closes.at[prev_date, ticker]
                close = closes.at[date, ticker]
                if pd.notna(prev_close) and pd.notna(close):
                    rows.append(
                        {
                            "date": date,
                            "ticker": ticker,
                            "component": "mtm",
                            "contribution": float(held * (close - prev_close)),
                        }
                    )
        group = by_date.get(date)
        if group is not None:
            for trade in group.itertuples():
                ticker = str(trade.ticker)
                signed = int(trade.qty) if str(trade.side).upper() == "BUY" else -int(trade.qty)
                close = closes.at[date, ticker] if ticker in closes.columns else np.nan
                delta_value = float(signed) * float(close) if pd.notna(close) else 0.0
                rows.append(
                    {
                        "date": date,
                        "ticker": ticker,
                        "component": "trade",
                        "contribution": float(trade.cash_flow) + delta_value,
                    }
                )
                qty[ticker] = qty.get(ticker, 0) + signed
        prev_date = date
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise RuntimeError("기여도 계산 결과가 비어 있습니다")
    total = float(frame["contribution"].sum())
    distribution = float(curve.get("distribution_cash", pd.Series(dtype=float)).sum())
    equity_delta = float(curve["equity"].iloc[-1] - initial_cash)
    if abs(total + distribution - equity_delta) > max(1e-6, abs(equity_delta) * 1e-9):
        raise AssertionError(
            "기여도 합계가 평가액 변화와 다릅니다: "
            f"contrib={total + distribution} equity_delta={equity_delta}"
        )
    frame["group"] = frame["ticker"].map(etf_shared.get_etf_group)
    return frame


# --------------------------------------------------------------------------- #
# A. Drawdown
# --------------------------------------------------------------------------- #
def drawdown_episodes(equity: pd.DataFrame) -> pd.DataFrame:
    values = equity["equity"].to_numpy(dtype=float)
    dates = pd.to_datetime(equity["date"]).to_numpy()
    peak_value = values[0]
    peak_index = 0
    in_drawdown = False
    trough_index = 0
    episodes: list[dict[str, Any]] = []
    for index in range(1, len(values)):
        if not in_drawdown:
            if values[index] >= peak_value:
                peak_value = values[index]
                peak_index = index
            else:
                in_drawdown = True
                trough_index = index
        if in_drawdown:
            if values[index] < values[trough_index]:
                trough_index = index
            if values[index] >= peak_value:
                episodes.append(
                    {
                        "peak_index": peak_index,
                        "trough_index": trough_index,
                        "recovery_index": index,
                        "peak_date": pd.Timestamp(dates[peak_index]),
                        "trough_date": pd.Timestamp(dates[trough_index]),
                        "recovery_date": pd.Timestamp(dates[index]),
                        "mdd": values[trough_index] / values[peak_index] - 1.0,
                        "peak_equity": float(values[peak_index]),
                        "trough_equity": float(values[trough_index]),
                        "unrecovered": False,
                    }
                )
                in_drawdown = False
                peak_value = values[index]
                peak_index = index
    if in_drawdown:
        episodes.append(
            {
                "peak_index": peak_index,
                "trough_index": trough_index,
                "recovery_index": None,
                "peak_date": pd.Timestamp(dates[peak_index]),
                "trough_date": pd.Timestamp(dates[trough_index]),
                "recovery_date": pd.NaT,
                "mdd": values[trough_index] / values[peak_index] - 1.0,
                "peak_equity": float(values[peak_index]),
                "trough_equity": float(values[trough_index]),
                "unrecovered": True,
            }
        )
    frame = pd.DataFrame(episodes)
    if frame.empty:
        return frame
    frame["trough_days"] = frame["trough_index"] - frame["peak_index"]
    frame["recovery_days"] = frame.apply(
        lambda row: (
            int(row["recovery_index"] - row["trough_index"])
            if pd.notna(row["recovery_index"])
            else None
        ),
        axis=1,
    )
    return frame.sort_values("mdd").reset_index(drop=True)


def series_at(frame: pd.DataFrame, date: pd.Timestamp, column: str) -> float:
    rows = frame[pd.to_datetime(frame["date"]) <= pd.Timestamp(date)]
    if rows.empty:
        return np.nan
    return float(rows.iloc[-1][column])


def episode_detail(
    episode: pd.Series,
    curve: pd.DataFrame,
    trades: pd.DataFrame,
    contrib: pd.DataFrame,
    price: pd.DataFrame,
    index_df: pd.DataFrame,
) -> dict[str, Any]:
    peak = pd.Timestamp(episode["peak_date"])
    trough = pd.Timestamp(episode["trough_date"])
    window = curve[(pd.to_datetime(curve["date"]) > peak) & (pd.to_datetime(curve["date"]) <= trough)]
    cash_weight = (window["cash"] / window["equity"]).replace([np.inf, -np.inf], np.nan)
    closes = close_matrix(price)
    positions = []
    top1 = []
    for row in window.itertuples():
        held = [t for t in str(row.holdings).split(",") if t]
        positions.append(len(held))
        if not held or row.equity <= 0:
            top1.append(0.0)
            continue
        values = []
        for ticker in held:
            close = closes.at[pd.Timestamp(row.date), ticker]
            value = 0.0 if pd.isna(close) else None
            values.append((ticker, value))
        weights = []
        for ticker, _ in values:
            match = trades[
                (trades["ticker"].astype(str) == ticker)
                & (pd.to_datetime(trades["date"]) <= pd.Timestamp(row.date))
            ]
            qty = int(
                np.sign(len(match) + 1)
                * (
                    match.loc[match["side"].astype(str).str.upper() == "BUY", "qty"].sum()
                    - match.loc[match["side"].astype(str).str.upper() == "SELL", "qty"].sum()
                )
            )
            close = closes.at[pd.Timestamp(row.date), ticker]
            if pd.notna(close):
                weights.append(qty * float(close) / float(row.equity))
        top1.append(max(weights) if weights else 0.0)
    contribution = contrib[
        (pd.to_datetime(contrib["date"]) > peak) & (pd.to_datetime(contrib["date"]) <= trough)
    ]
    total_contrib = float(contribution["contribution"].sum())
    equity_peak = series_at(curve, peak, "equity")
    equity_trough = series_at(curve, trough, "equity")
    episode_delta = float(equity_trough - equity_peak)
    if abs(total_contrib - episode_delta) > max(1e-6, abs(episode_delta) * 1e-9):
        raise AssertionError(
            f"낙폭 구간 기여도 불일치 {peak.date()}~{trough.date()}: "
            f"contrib={total_contrib} equity_delta={episode_delta}"
        )
    by_ticker = (
        contribution.groupby(["ticker", "group"], as_index=False)["contribution"]
        .sum()
        .sort_values("contribution")
    )
    by_group = (
        contribution.groupby("group", as_index=False)["contribution"]
        .sum()
        .sort_values("contribution")
    )
    window_trades = trades[
        (pd.to_datetime(trades["date"]) > peak) & (pd.to_datetime(trades["date"]) <= trough)
    ]
    costs = p0b.cost_breakdown(window_trades, FROZEN_SLIPPAGE, 1.0)
    try:
        realized_df, _, _ = attribution.attribute_trades(trades)
        realized = realized_df[
            (pd.to_datetime(realized_df["sell_date"]) > peak)
            & (pd.to_datetime(realized_df["sell_date"]) <= trough)
        ]
        realized_pnl = float(realized["net_pnl"].sum())
    except (RuntimeError, ValueError):
        realized_pnl = float("nan")
    index_ret = series_at(index_df, trough, "close") / series_at(index_df, peak, "close") - 1.0
    bench = price[price["ticker"].astype(str) == rtb.BENCHMARK_TICKER][["date", "close"]]
    bench_ret = series_at(bench, trough, "close") / series_at(bench, peak, "close") - 1.0
    return {
        "peak_date": peak.strftime("%Y-%m-%d"),
        "trough_date": trough.strftime("%Y-%m-%d"),
        "recovery_date": (
            pd.Timestamp(episode["recovery_date"]).strftime("%Y-%m-%d")
            if pd.notna(episode["recovery_date"])
            else None
        ),
        "mdd": float(episode["mdd"]),
        "trough_days": int(episode["trough_days"]),
        "recovery_days": episode["recovery_days"],
        "unrecovered": bool(episode["unrecovered"]),
        "market_return": index_ret,
        "benchmark_return": bench_ret,
        "avg_cash_weight": float(cash_weight.mean()),
        "max_cash_weight": float(cash_weight.max()),
        "avg_positions": float(np.mean(positions)) if positions else 0.0,
        "avg_top1_weight": float(np.mean(top1)) if top1 else 0.0,
        "max_top1_weight": float(np.max(top1)) if top1 else 0.0,
        "episode_equity_delta": episode_delta,
        "contribution_matches_delta": True,
        "total_contribution": total_contrib,
        "realized_fifo_pnl": realized_pnl,
        "unrealized_and_cost_remainder": total_contrib - realized_pnl,
        "fee_cost": costs["fee_cost"],
        "tax_cost": costs["tax_cost"],
        "slippage_cost": costs["slippage_cost"],
        "trades": len(window_trades),
        "holdings": ",".join(sorted(by_ticker["ticker"].tolist())),
        "group_contribution": json.dumps(
            {row.group: round(float(row.contribution), 2) for row in by_group.itertuples()},
            ensure_ascii=False,
        ),
        "ticker_contribution": json.dumps(
            {
                row.ticker: round(float(row.contribution), 2)
                for row in by_ticker.itertuples()
            },
            ensure_ascii=False,
        ),
    }


# --------------------------------------------------------------------------- #
# B. Regime
# --------------------------------------------------------------------------- #
def regime_labels(
    index_df: pd.DataFrame, dates: list[pd.Timestamp]
) -> pd.DataFrame:
    frame = index_df[["date", "close", "risk_on"]].copy().sort_values("date")
    frame["date"] = pd.to_datetime(frame["date"])
    ret_60 = frame["close"] / frame["close"].shift(TREND_WINDOW) - 1.0
    ret_1 = frame["close"].pct_change()
    vol_20 = ret_1.rolling(VOL_WINDOW).std(ddof=0) * np.sqrt(252)
    vol_median = vol_20.rolling(VOL_MEDIAN_WINDOW, min_periods=60).median()
    frame["trend"] = np.where(
        ret_60 > TREND_THRESHOLD,
        "up",
        np.where(ret_60 < -TREND_THRESHOLD, "down", "sideways"),
    )
    frame["trend"] = frame["trend"].where(ret_60.notna(), "warmup")
    frame["vol"] = np.where(vol_20 >= vol_median, "high_vol", "low_vol")
    frame["vol"] = frame["vol"].where(vol_20.notna() & vol_median.notna(), "warmup")
    frame["risk"] = np.where(frame["risk_on"].astype(bool), "risk_on", "risk_off")
    labels = frame.set_index("date")[["risk", "trend", "vol"]]
    calendar = pd.DatetimeIndex(sorted(pd.to_datetime(dates)))
    return labels.reindex(labels.index.union(calendar)).ffill().reindex(calendar)


def _calendar_runs(mask: pd.Series) -> list[tuple[int, int]]:
    """거래일 캘린더에서 같은 레짐이 연속되는 구간(위치 기준)을 반환한다."""
    positions = np.flatnonzero(mask.to_numpy(dtype=bool))
    if len(positions) == 0:
        return []
    runs: list[tuple[int, int]] = []
    start = previous = int(positions[0])
    for position in positions[1:]:
        position = int(position)
        if position == previous + 1:
            previous = position
            continue
        runs.append((start, previous))
        start = previous = position
    runs.append((start, previous))
    return runs


def regime_stats(
    equity: pd.DataFrame,
    curve: pd.DataFrame,
    trades: pd.DataFrame,
    contrib: pd.DataFrame,
    labels: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    series = equity.set_index("date")["equity"].sort_index()
    calendar = series.index
    returns = series.pct_change().dropna()
    curve_idx = curve.set_index("date")
    rows: list[dict[str, Any]] = []
    asset_rows: list[dict[str, Any]] = []
    for axis in ("risk", "trend", "vol"):
        axis_labels = labels[axis].reindex(calendar)
        for label in sorted(set(axis_labels.dropna().unique())):
            if label == "warmup":
                continue
            mask = (axis_labels == label).reindex(calendar).fillna(False)
            days = calendar[mask.to_numpy(dtype=bool)]
            rets = returns.reindex(days).dropna()
            in_curve = curve_idx.reindex(days).dropna(subset=["equity"])
            exposure = (
                float((in_curve["market_value"] / in_curve["equity"]).mean())
                if not in_curve.empty
                else np.nan
            )
            cash = (
                float((in_curve["cash"] / in_curve["equity"]).mean())
                if not in_curve.empty
                else np.nan
            )
            # 거래일 라벨 전환으로 구간을 나누고, 첫날 손실을 잡기 위해 직전 평가일을 포함한다.
            runs = _calendar_runs(mask)
            worst_dd = 0.0
            for start, end in runs:
                path = series.iloc[max(0, start - 1) : end + 1]
                if len(path) < 2:
                    continue
                dd = (path / path.cummax() - 1.0).min()
                worst_dd = min(worst_dd, float(dd))
            day_trades = trades[pd.to_datetime(trades["date"]).isin(set(days))]
            notional = float(
                (pd.to_numeric(day_trades["qty"], errors="coerce").fillna(0)
                 * pd.to_numeric(day_trades["price"], errors="coerce").fillna(0)).sum()
            )
            avg_equity = float(in_curve["equity"].mean()) if not in_curve.empty else np.nan
            day_contrib = contrib[pd.to_datetime(contrib["date"]).isin(set(days))]
            by_group = day_contrib.groupby("group")["contribution"].sum()
            rows.append(
                {
                    "axis": axis,
                    "regime": label,
                    "days": len(days),
                    "segments": len(runs),
                    "ann_return": float(rets.mean() * 252),
                    "cum_return": float((1 + rets).prod() - 1),
                    "vol": float(rets.std(ddof=0) * np.sqrt(252)),
                    "sharpe": (
                        float(rets.mean() / rets.std(ddof=0) * np.sqrt(252))
                        if rets.std(ddof=0) > 0
                        else None
                    ),
                    "worst_segment_mdd": worst_dd if worst_dd < 0 else None,
                    "avg_exposure": exposure,
                    "avg_cash_weight": cash,
                    "trades": len(day_trades),
                    "turnover": (notional / avg_equity) if avg_equity else None,
                }
            )
            for group_name, value in by_group.items():
                asset_rows.append(
                    {
                        "axis": axis,
                        "regime": label,
                        "group": group_name,
                        "contribution": float(value),
                    }
                )
    return pd.DataFrame(rows), pd.DataFrame(asset_rows)


# --------------------------------------------------------------------------- #
# C/D. 필터 실효성·집중도
# --------------------------------------------------------------------------- #
def variant_row(
    label: str,
    curve: pd.DataFrame,
    trades: pd.DataFrame,
    dates: list[pd.Timestamp],
) -> dict[str, Any]:
    stats = p0b.performance(AUM, dates, curve)
    trading = p0b.trading_stats(curve, trades)
    costs = p0b.cost_breakdown(trades, FROZEN_SLIPPAGE, 1.0)
    cash_weight = (curve["cash"] / curve["equity"]).replace([np.inf, -np.inf], np.nan)
    return {
        "variant": label,
        "cagr": stats["cagr"],
        "mdd": stats["mdd"],
        "sharpe": stats["sharpe"],
        "calmar": stats["calmar"],
        "final": stats["final"],
        "avg_cash_weight": float(cash_weight.mean()),
        "trades": trading["trade_count"],
        "turnover": trading["turnover"],
        "total_cost": costs["total_run_rate_cost"],
    }


def crosscheck_with_factorial(
    label: str,
    price: pd.DataFrame,
    index_df: pd.DataFrame,
    dates: list[pd.Timestamp],
    expected: dict[str, Any],
    *,
    use_market_filter: bool = True,
    group_override: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """기존 factorial_ablation 시나리오 러너로 동일 조건을 재실행해 교차 검증한다."""
    original_override = set(etf_shared.GROUP_RISK_OVERRIDE)
    if group_override is not None:
        etf_shared.GROUP_RISK_OVERRIDE.clear()
        etf_shared.GROUP_RISK_OVERRIDE.update(group_override)
    try:
        result = ablation_mod.run_scenario(
            common_dates=dates,
            index_df=index_df,
            price_data=price,
            us_index_df=None,
            label=label,
            use_market_filter=use_market_filter,
            risk_off_liquidate=True,
            enable_multi_index_risk=False,
            max_positions=int(p0b.APPLIED["max_positions"]),
            rebalance_step_days=int(rtb.REBALANCE_STEP_DAYS),
            momentum_weight_60=0.55,
            universe_tickers=list(etf_shared.ETF_LIST),
        )
    finally:
        etf_shared.GROUP_RISK_OVERRIDE.clear()
        etf_shared.GROUP_RISK_OVERRIDE.update(original_override)
    # factorial_ablation은 워밍업 이후 커브로 CAGR을 계산하므로(초기 시점 미포함),
    # convention-independent 지표(최종자산·MDD·거래수)로 교차 검증한다.
    for key, theirs, ours in (
        ("final_equity", float(result["final_equity"]), float(expected["final"])),
        ("mdd", float(result["mdd"]), float(expected["mdd"])),
        ("n_trades", int(result["n_trades"]), int(expected["trades"])),
    ):
        if abs(theirs - ours) > max(1e-6, abs(ours) * 1e-9):
            raise AssertionError(
                f"factorial_ablation 교차검증 불일치 {label}: {key} {theirs} != {ours}"
            )
    return result


def worst_episode_summary(
    curve: pd.DataFrame,
    trades: pd.DataFrame,
    contrib: pd.DataFrame,
    price: pd.DataFrame,
    index_df: pd.DataFrame,
    dates: list[pd.Timestamp],
) -> tuple[dict[str, Any], pd.DataFrame]:
    equity = equity_with_initial(curve, dates)
    episodes = drawdown_episodes(equity)
    worst = episodes.iloc[0]
    detail = episode_detail(worst, curve, trades, contrib, price, index_df)
    contributions = contrib[
        (pd.to_datetime(contrib["date"]) > pd.Timestamp(worst["peak_date"]))
        & (pd.to_datetime(contrib["date"]) <= pd.Timestamp(worst["trough_date"]))
    ]
    by_ticker = (
        contributions.groupby(["ticker", "group"], as_index=False)["contribution"]
        .sum()
        .sort_values("contribution")
    )
    return detail, by_ticker


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    frozen = p0b.configure_frozen_settings(CAP_FROZEN)
    p0b.rtb.START, p0b.rtb.END = p0b.DEFAULT_START, p0b.DEFAULT_END
    digest_before = p0b.data_cache_digest()
    price, index_df, dates = p0b.load_inputs()
    p0b.verify_applied_settings()
    missing = sorted({str(t) for t in etf_shared.ETF_LIST} - set(price["ticker"].astype(str)))
    if missing:
        raise SystemExit(f"[p0c] 필수 종목 누락: {missing}")

    # 기준 실행 ---------------------------------------------------------------
    curve, trades = p0b.run_legacy(
        price,
        index_df,
        dates,
        aum=AUM,
        slippage=FROZEN_SLIPPAGE,
        fee_multiplier=1.0,
        max_asset_pct=CAP_FROZEN,
    )
    baseline = p0b.performance(AUM, dates, curve)
    expected = {"cagr": 0.2454113778672673, "mdd": -0.5121244554166072, "sharpe": 1.0391357658469746}
    deltas = {key: abs(baseline[key] - expected[key]) for key in expected}
    if any(value > 1e-9 for value in deltas.values()):
        raise SystemExit(f"[p0c] P0-B 기준 재현 실패: baseline={baseline}, deltas={deltas}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    closes = close_matrix(price)
    contrib = contributions(curve, trades, closes)
    equity = equity_with_initial(curve, dates)
    episodes = drawdown_episodes(equity)

    top = episodes.head(TOP_DRAWDOWNS).copy()
    detail_rows = [
        episode_detail(row, curve, trades, contrib, price, index_df) for _, row in top.iterrows()
    ]
    drawdowns = pd.DataFrame(detail_rows)
    drawdowns.insert(0, "rank", range(1, len(drawdowns) + 1))
    drawdowns.to_csv(OUTPUT_DIR / "drawdowns.csv", index=False, encoding="utf-8-sig")
    episodes.to_csv(OUTPUT_DIR / "drawdown_episodes_all.csv", index=False, encoding="utf-8-sig")

    contrib_rows = []
    for rank, row in enumerate(top.itertuples(), start=1):
        window = contrib[
            (pd.to_datetime(contrib["date"]) > pd.Timestamp(row.peak_date))
            & (pd.to_datetime(contrib["date"]) <= pd.Timestamp(row.trough_date))
        ]
        by_ticker = window.groupby(["ticker", "group"], as_index=False)["contribution"].sum()
        for item in by_ticker.itertuples():
            contrib_rows.append(
                {
                    "rank": rank,
                    "peak_date": row.peak_date,
                    "trough_date": row.trough_date,
                    "ticker": item.ticker,
                    "group": item.group,
                    "contribution": float(item.contribution),
                }
            )
    pd.DataFrame(contrib_rows).to_csv(
        OUTPUT_DIR / "drawdown_contributions.csv", index=False, encoding="utf-8-sig"
    )

    # B. Regime ---------------------------------------------------------------
    labels = regime_labels(index_df, dates)
    regime_df, regime_asset = regime_stats(equity, curve, trades, contrib, labels)
    regime_df.to_csv(OUTPUT_DIR / "regime_performance.csv", index=False, encoding="utf-8-sig")
    regime_asset.to_csv(
        OUTPUT_DIR / "regime_asset_contribution.csv", index=False, encoding="utf-8-sig"
    )

    # C. 위험 필터 counterfactual -------------------------------------------
    variants = [variant_row("frozen", curve, trades, dates)]
    variant_curves = {"frozen": (curve, trades)}
    for label, kwargs in (
        ("no_kospi_filter", {"use_market_filter": False}),
        ("no_group_override", {"group_override": ()}),
    ):
        v_curve, v_trades = run_variant(
            price, index_df, dates, cap=CAP_FROZEN, label=label, **kwargs
        )
        variants.append(variant_row(label, v_curve, v_trades, dates))
        variant_curves[label] = (v_curve, v_trades)
    for label, use_filter, override in (
        ("frozen", True, None),
        ("no_kospi_filter", False, None),
        ("no_group_override", True, ()),
    ):
        row = next(item for item in variants if item["variant"] == label)
        crosscheck_with_factorial(
            label,
            price,
            index_df,
            dates,
            row,
            use_market_filter=use_filter,
            group_override=override,
        )
        row["factorial_crosscheck"] = True
    ablation = pd.DataFrame(variants)
    ablation.to_csv(OUTPUT_DIR / "risk_filter_ablation.csv", index=False, encoding="utf-8-sig")

    # D. 집중도 50 vs 85 ------------------------------------------------------
    cap50_curve, cap50_trades = p0b.run_legacy(
        price,
        index_df,
        dates,
        aum=AUM,
        slippage=FROZEN_SLIPPAGE,
        fee_multiplier=1.0,
        max_asset_pct=CAP_COMPARE,
    )
    cap50_contrib = contributions(cap50_curve, cap50_trades, closes)
    worst85, worst85_ticker = worst_episode_summary(
        curve, trades, contrib, price, index_df, dates
    )
    worst50, worst50_ticker = worst_episode_summary(
        cap50_curve, cap50_trades, cap50_contrib, price, index_df, dates
    )
    concentration_rows = []
    for cap, detail, ticker_df, curve_cap, trades_cap in (
        (CAP_FROZEN, worst85, worst85_ticker, curve, trades),
        (CAP_COMPARE, worst50, worst50_ticker, cap50_curve, cap50_trades),
    ):
        stats = p0b.performance(AUM, dates, curve_cap)
        row = {
            "cap": cap,
            "cagr": stats["cagr"],
            "mdd": stats["mdd"],
            "sharpe": stats["sharpe"],
            **{key: detail[key] for key in (
                "peak_date", "trough_date", "recovery_date", "mdd", "trough_days",
                "recovery_days", "avg_cash_weight", "max_cash_weight", "avg_positions",
                "avg_top1_weight", "max_top1_weight", "total_contribution",
                "realized_fifo_pnl", "unrealized_and_cost_remainder", "trades",
            )},
            "worst_ticker": (
                ticker_df.iloc[0]["ticker"] if not ticker_df.empty else None
            ),
            "worst_ticker_contribution": (
                float(ticker_df.iloc[0]["contribution"]) if not ticker_df.empty else None
            ),
        }
        concentration_rows.append(row)
    concentration = pd.DataFrame(concentration_rows)
    concentration.to_csv(
        OUTPUT_DIR / "concentration_comparison.csv", index=False, encoding="utf-8-sig"
    )

    if p0b.data_cache_digest() != digest_before:
        raise SystemExit("[p0c] 실행 중 data_cache가 변경되었습니다.")

    metadata = {
        "phase": "P0-C",
        "period": {"start": p0b.DEFAULT_START, "end": p0b.DEFAULT_END},
        "trading_days": len(dates),
        "universe": len(etf_shared.ETF_LIST),
        "max_asset_pct": CAP_FROZEN,
        "compare_cap": CAP_COMPARE,
        "price_basis": "price",
        "slippage": FROZEN_SLIPPAGE,
        "spread_pct": frozen["spread_pct"],
        "freeze_sha256": frozen["freeze_sha256"],
        "oos_start_date": frozen["oos_start_date"],
        "git_commit": p0b.git_commit(),
        "data_cache_digest": digest_before,
        "data_cache_unchanged": True,
        "baseline": baseline,
        "baseline_matches_p0b": True,
        "regime_definition": {
            "risk": "KOSPI index risk_on (as-of)",
            "trend": f"index {TREND_WINDOW}d return: up>{TREND_THRESHOLD}, down<-{TREND_THRESHOLD}",
            "vol": f"{VOL_WINDOW}d realized vol >= trailing {VOL_MEDIAN_WINDOW}d median",
        },
        "artifacts": sorted(path.name for path in OUTPUT_DIR.glob("*")),
    }
    summary = {
        "baseline": baseline,
        "top_drawdowns": drawdowns.to_dict(orient="records"),
        "ablation": ablation.to_dict(orient="records"),
        "concentration": concentration.to_dict(orient="records"),
        "regime_risk": regime_df[regime_df["axis"] == "risk"].to_dict(orient="records"),
    }
    (OUTPUT_DIR / "p0c_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (OUTPUT_DIR / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"[p0c] 완료: {OUTPUT_DIR}")
    print(f"[p0c] baseline CAGR={baseline['cagr']:.4%} MDD={baseline['mdd']:.2%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
