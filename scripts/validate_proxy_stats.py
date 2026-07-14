#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TRADING_DAYS = 252

from _proxy_utils import (
    KOSPI_YF_TICKER,
    align_signal_to_dates,
    download_index,
    pick_equity_column,
)


def _load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Missing file: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _load_equity(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Missing file: {path}")
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"])
    return df


def _daily_returns(equity: np.ndarray) -> np.ndarray:
    return equity[1:] / equity[:-1] - 1.0


def _cagr_from_returns(rets: np.ndarray, years: float) -> float:
    if len(rets) == 0 or years <= 0:
        return 0.0
    growth = float(np.prod(1.0 + rets))
    if growth <= 0:
        return 0.0
    return growth ** (1.0 / years) - 1.0


def _sharpe_from_returns(rets: np.ndarray, rf: float = 0.0) -> float:
    if len(rets) < 2:
        return 0.0
    excess = rets - rf
    sd = float(np.std(excess, ddof=1))
    if sd == 0:
        return 0.0
    return float(np.mean(excess) / sd * np.sqrt(TRADING_DAYS))


def _sortino_from_returns(rets: np.ndarray, rf: float = 0.0) -> float:
    if len(rets) < 2:
        return 0.0
    excess = rets - rf
    downside = excess[excess < 0]
    dd = float(np.std(downside, ddof=1)) if len(downside) > 1 else 0.0
    if dd == 0:
        return 0.0
    return float(np.mean(excess) / dd * np.sqrt(TRADING_DAYS))


def bootstrap_ci(data: np.ndarray, n_bootstrap: int = 10000, ci: float = 0.95) -> tuple[float, float]:
    n = len(data)
    means = np.array(
        [float(np.mean(np.random.choice(data, size=n, replace=True))) for _ in range(n_bootstrap)]
    )
    alpha = (1 - ci) / 2
    lo, hi = (float(x) for x in np.percentile(means, [alpha * 100, (1 - alpha) * 100]))
    return lo, hi


def bootstrap_stat_diff(
    spy_rets: np.ndarray,
    qqq_rets: np.ndarray,
    stat_fn,
    n_bootstrap: int = 1000,
    ci: float = 0.95,
) -> tuple[float, float, float]:
    n = len(spy_rets)
    diffs = np.empty(n_bootstrap)
    for i in range(n_bootstrap):
        idx = np.random.choice(n, size=n, replace=True)
        diffs[i] = stat_fn(qqq_rets[idx]) - stat_fn(spy_rets[idx])
    alpha = (1 - ci) / 2
    lo, hi = tuple(float(x) for x in np.percentile(diffs, [alpha * 100, (1 - alpha) * 100]))
    point = stat_fn(qqq_rets) - stat_fn(spy_rets)
    return point, lo, hi


def _drawdown_series(equity: np.ndarray) -> np.ndarray:
    running_max = np.maximum.accumulate(equity)
    return equity / running_max - 1.0


def _worst_drawdown_episode(
    equity: np.ndarray, dates: pd.Series
) -> dict:
    dd = _drawdown_series(equity)
    trough_idx = int(np.argmin(dd))
    peak_idx = trough_idx
    for i in range(trough_idx, -1, -1):
        if dd[i] >= 0:
            peak_idx = i
            break
    recovery_idx = None
    for i in range(trough_idx + 1, len(dd)):
        if dd[i] >= 0:
            recovery_idx = i
            break
    end_idx = recovery_idx if recovery_idx is not None else len(dd) - 1
    duration = end_idx - peak_idx
    return {
        "peak_date": str(dates.iloc[peak_idx].date()),
        "trough_date": str(dates.iloc[trough_idx].date()),
        "recovery_date": (str(dates.iloc[recovery_idx].date()) if recovery_idx is not None else "not recovered"),
        "mdd": float(dd[trough_idx]),
        "duration_days": int(duration),
    }


def _fmt_pct(v: float | None) -> str:
    return "N/A" if v is None else f"{v:.2%}"


def _fmt_num(v: float | None) -> str:
    return "N/A" if v is None else f"{v:.4f}"


def _sig_flag(lo: float, hi: float) -> str:
    return "SIGNIFICANT" if lo > 0 or hi < 0 else "not significant"


def part_a_bootstrap(
    spy_rets: np.ndarray,
    qqq_rets: np.ndarray,
    years: float,
    n_bootstrap: int,
) -> pd.DataFrame:
    cagr_point, cagr_lo, cagr_hi = bootstrap_stat_diff(
        spy_rets, qqq_rets, lambda r: _cagr_from_returns(r, years), n_bootstrap
    )
    sharpe_point, sharpe_lo, sharpe_hi = bootstrap_stat_diff(
        spy_rets, qqq_rets, _sharpe_from_returns, n_bootstrap
    )
    sortino_point, sortino_lo, sortino_hi = bootstrap_stat_diff(
        spy_rets, qqq_rets, _sortino_from_returns, n_bootstrap
    )
    rows = [
        ("cagr_diff", cagr_point, cagr_lo, cagr_hi, _sig_flag(cagr_lo, cagr_hi)),
        ("sharpe_diff", sharpe_point, sharpe_lo, sharpe_hi, _sig_flag(sharpe_lo, sharpe_hi)),
        ("sortino_diff", sortino_point, sortino_lo, sortino_hi, _sig_flag(sortino_lo, sortino_hi)),
    ]
    df = pd.DataFrame(rows, columns=["metric", "point_estimate", "ci_low", "ci_high", "significance"])
    print("=== Part A: Bootstrap Confidence Intervals (QQQ - SPY) ===")
    print(f"{'metric':<14} {'point':>12} {'95% CI low':>12} {'95% CI high':>12} {'5% sig':>16}")
    print("-" * 70)
    for _, r in df.iterrows():
        if r["metric"] == "cagr_diff":
            print(f"{r['metric']:<14} {_fmt_pct(r['point_estimate']):>12} {_fmt_pct(r['ci_low']):>12} {_fmt_pct(r['ci_high']):>12} {r['significance']:>16}")
        else:
            print(f"{r['metric']:<14} {_fmt_num(r['point_estimate']):>12} {_fmt_num(r['ci_low']):>12} {_fmt_num(r['ci_high']):>12} {r['significance']:>16}")
    print()
    return df


def part_b_regime(spy_df: pd.DataFrame, qqq_df: pd.DataFrame) -> pd.DataFrame | None:
    print("=== Part B: Regime-Conditional Returns ===")
    try:
        import yfinance  # noqa: F401
    except Exception:
        print("yfinance not available; skipping.")
        print()
        return None

    start = str(spy_df["date"].min().date()).replace("-", "")
    end = str(spy_df["date"].max().date()).replace("-", "")

    try:
        kospi = download_index(KOSPI_YF_TICKER, start, end)
        spy_idx = download_index("SPY", start, end)
        qqq_idx = download_index("QQQ", start, end)
    except Exception as e:
        print(f"Failed to download index data: {e}; skipping.")
        print()
        return None

    # Build merged dataframe on equity dates (Korean trading dates)
    eq = pd.merge(
        spy_df[["date", "equity_spy"]],
        qqq_df[["date", "equity_qqq"]],
        on="date", how="inner",
    ).sort_values("date")

    # Align US/KOSPI signals forward to equity dates (left join semantics)
    kospi_aligned = align_signal_to_dates(kospi, eq["date"])
    spy_aligned = align_signal_to_dates(spy_idx, eq["date"])
    qqq_aligned = align_signal_to_dates(qqq_idx, eq["date"])

    merged = eq.copy()
    merged["kospi_on"] = kospi_aligned.values
    merged["spy_on"] = spy_aligned.values
    merged["qqq_on"] = qqq_aligned.values
    merged["ret_spy"] = merged["equity_spy"].pct_change()
    merged["ret_qqq"] = merged["equity_qqq"].pct_change()

    # Define regimes
    def _regime(row):
        if row["kospi_on"]:
            return "kospi_risk_on"
        if row["spy_on"] and row["qqq_on"]:
            return "risk_off_both_on"
        if row["spy_on"]:
            return "risk_off_spy_on"
        if row["qqq_on"]:
            return "risk_off_qqq_on"
        return "risk_off_both_off"

    merged["regime"] = merged.apply(_regime, axis=1)

    rows = []
    for regime, grp in merged.groupby("regime"):
        rs = grp["ret_spy"].dropna()
        rq = grp["ret_qqq"].dropna()
        rows.append(
            {
                "regime": regime,
                "n_days": len(grp),
                "avg_ret_spy": float(rs.mean()) if len(rs) else 0.0,
                "avg_ret_qqq": float(rq.mean()) if len(rq) else 0.0,
                "avg_ret_diff": float(rq.mean() - rs.mean()) if len(rs) and len(rq) else 0.0,
            }
        )

    df = pd.DataFrame(rows)
    print(f"{'regime':<20} {'n_days':>8} {'avg_spy':>12} {'avg_qqq':>12} {'diff(q-s)':>12}")
    print("-" * 68)
    for _, r in df.iterrows():
        print(f"{r['regime']:<20} {int(r['n_days']):>8} {_fmt_num(r['avg_ret_spy']):>12} {_fmt_num(r['avg_ret_qqq']):>12} {_fmt_num(r['avg_ret_diff']):>12}")
    if not df.empty:
        best = df.loc[df["avg_ret_diff"].idxmax()]
        print(f"\nQQQ advantage driven by: {best['regime']} (avg daily diff {_fmt_num(best['avg_ret_diff'])})")
    print()
    return df


def part_c_drawdown(spy_df: pd.DataFrame, qqq_df: pd.DataFrame) -> pd.DataFrame:
    print("=== Part C: Drawdown Comparison ===")
    spy_eq = spy_df["equity_spy"].to_numpy()
    qqq_eq = qqq_df["equity_qqq"].to_numpy()
    spy_ep = _worst_drawdown_episode(spy_eq, spy_df["date"])
    qqq_ep = _worst_drawdown_episode(qqq_eq, qqq_df["date"])
    rows = [
        ("SPY", spy_ep["mdd"], spy_ep["peak_date"], spy_ep["trough_date"], spy_ep["recovery_date"], spy_ep["duration_days"]),
        ("QQQ", qqq_ep["mdd"], qqq_ep["peak_date"], qqq_ep["trough_date"], qqq_ep["recovery_date"], qqq_ep["duration_days"]),
    ]
    df = pd.DataFrame(rows, columns=["proxy", "worst_mdd", "peak_date", "trough_date", "recovery_date", "duration_days"])
    print(f"{'proxy':<6} {'worst_mdd':>12} {'peak':>12} {'trough':>12} {'recovery':>16} {'duration':>10}")
    print("-" * 70)
    for _, r in df.iterrows():
        print(f"{r['proxy']:<6} {_fmt_pct(r['worst_mdd']):>12} {str(r['peak_date']):>12} {str(r['trough_date']):>12} {str(r['recovery_date']):>16} {int(r['duration_days']):>10}")
    print()
    return df


def part_d_decision(boot_df: pd.DataFrame) -> str:
    print("=== Part D: Summary Decision ===")
    sig_metrics = [m for m in ("sharpe_diff", "sortino_diff") if m in set(boot_df["metric"])]
    significant = all(
        boot_df.loc[boot_df["metric"] == m, "significance"].iloc[0] == "SIGNIFICANT"
        for m in sig_metrics
    )
    cagr_row = boot_df[boot_df["metric"] == "cagr_diff"]
    cagr_positive = bool(cagr_row["point_estimate"].iloc[0] > 0) if not cagr_row.empty else False
    passed = significant and cagr_positive
    decision = "PASS" if passed else "HOLD"
    print(f"Risk-adjusted metrics significant: {significant}")
    print(f"CAGR improvement positive: {cagr_positive}")
    print(f"Decision: {decision} ({'QQQ significantly better' if passed else 'difference not distinguishable from noise'})")
    print()
    if passed:
        print("Recommendation: Use QQQ as the US risk proxy. The improvement in risk-adjusted")
        print("returns is statistically significant at the 5% level and CAGR is higher.")
    else:
        print("Recommendation: HOLD on switching. The apparent QQQ advantage is within bootstrap")
        print("noise; keep SPY (or decide on non-statistical grounds).")
    print()
    return decision


def main() -> int:
    parser = argparse.ArgumentParser(description="Statistical validation for SPY vs QQQ proxy comparison")
    parser.add_argument(
        "--spy-equity",
        default="outputs_compare/proxy_analysis/spy_etf_equity_curve.csv",
        help="SPY backtest equity curve CSV (file path)",
    )
    parser.add_argument(
        "--qqq-equity",
        default="outputs_compare/proxy_analysis/qqq_etf_equity_curve.csv",
        help="QQQ backtest equity curve CSV (file path)",
    )
    parser.add_argument("--start", default=None, help="Start date filter YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date filter YYYY-MM-DD")
    parser.add_argument("--n-bootstrap", type=int, default=1000, help="Bootstrap iterations (default 1000)")
    parser.add_argument("--output", default="outputs_compare/proxy_match/stat_validation.csv", help="Output CSV path")
    args = parser.parse_args()

    spy_path = Path(args.spy_equity)
    qqq_path = Path(args.qqq_equity)

    try:
        spy_curve = _load_equity(spy_path)
        qqq_curve = _load_equity(qqq_path)
    except Exception as e:
        print(f"[ERROR] {e}")
        print("Run the proxy signal analysis / sweep backtests first so that")
        print("the SPY/QQQ equity curve CSVs exist at the given paths.")
        return 2

    if args.start:
        spy_curve = spy_curve[spy_curve["date"] >= pd.to_datetime(args.start)]
        qqq_curve = qqq_curve[qqq_curve["date"] >= pd.to_datetime(args.start)]
    if args.end:
        spy_curve = spy_curve[spy_curve["date"] <= pd.to_datetime(args.end)]
        qqq_curve = qqq_curve[qqq_curve["date"] <= pd.to_datetime(args.end)]

    spy_col = pick_equity_column(spy_curve)
    qqq_col = pick_equity_column(qqq_curve)
    spy_df = spy_curve[["date", spy_col]].rename(columns={spy_col: "equity_spy"})
    qqq_df = qqq_curve[["date", qqq_col]].rename(columns={qqq_col: "equity_qqq"})

    merged = pd.merge(spy_df, qqq_df, on="date", how="inner").sort_values("date").reset_index(drop=True)
    if merged.empty:
        print("[ERROR] No overlapping dates between SPY and QQQ equity curves.")
        return 2

    years = max((merged["date"].iloc[-1] - merged["date"].iloc[0]).days / 365.25, 1e-9)
    spy_rets = _daily_returns(merged["equity_spy"].to_numpy())
    qqq_rets = _daily_returns(merged["equity_qqq"].to_numpy())

    boot_df = part_a_bootstrap(spy_rets, qqq_rets, years, args.n_bootstrap)

    regime_df = part_b_regime(spy_df, qqq_df)

    dd_df = part_c_drawdown(spy_df, qqq_df)

    decision = part_d_decision(boot_df)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    summary = boot_df.copy()
    summary["decision"] = decision
    summary.to_csv(out_path, index=False)
    if regime_df is not None:
        regime_df.to_csv(out_path.parent / "regime_returns.csv", index=False)
    dd_df.to_csv(out_path.parent / "drawdown_comparison.csv", index=False)
    print(f"Saved validation output: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
