#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Missing file: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v:.2%}"


def _fmt_num(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v:.4f}"


def _get_metric(payload: dict, key: str) -> float | None:
    strategy = payload.get("strategy", {})
    value = strategy.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _safe_div(numer: float, denom: float) -> float | None:
    if denom == 0:
        return None
    return numer / abs(denom)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare Phase 1 baseline vs multi-index results")
    parser.add_argument(
        "--baseline",
        default="outputs_compare/phase1_baseline/performance.json",
        help="Baseline performance.json path",
    )
    parser.add_argument(
        "--multi",
        default="outputs_compare/phase1_multi/performance.json",
        help="Multi-index performance.json path",
    )
    parser.add_argument(
        "--mdd-worsen-limit",
        type=float,
        default=0.01,
        help="Allowed MDD worsening in absolute points (default: 0.01 = 1%%p)",
    )
    parser.add_argument(
        "--risk-improve-threshold",
        type=float,
        default=0.10,
        help="Required risk-adjusted improvement ratio (default: 0.10 = 10%%)",
    )
    args = parser.parse_args()

    baseline_path = Path(args.baseline)
    multi_path = Path(args.multi)

    try:
        baseline = _load_json(baseline_path)
        multi = _load_json(multi_path)
    except Exception as e:
        print(f"[ERROR] {e}")
        print("Run A/B backtests first, then copy performance.json files into outputs_compare.")
        return 2

    metrics = ["cagr", "mdd", "sharpe", "sortino", "volatility", "total_return"]

    rows: list[tuple[str, float | None, float | None, float | None]] = []
    for key in metrics:
        b = _get_metric(baseline, key)
        m = _get_metric(multi, key)
        d = (None if (b is None or m is None) else (m - b))
        rows.append((key, b, m, d))

    print("=== Phase 1 A/B Comparison ===")
    print(f"baseline: {baseline_path}")
    print(f"multi:    {multi_path}")
    print()
    print(f"{'metric':<12} {'baseline':>14} {'multi':>14} {'delta(m-b)':>14}")
    print("-" * 58)

    for key, b, m, d in rows:
        if key in {"cagr", "mdd", "volatility", "total_return"}:
            print(f"{key:<12} {_fmt_pct(b):>14} {_fmt_pct(m):>14} {_fmt_pct(d):>14}")
        else:
            print(f"{key:<12} {_fmt_num(b):>14} {_fmt_num(m):>14} {_fmt_num(d):>14}")

    b_sharpe = _get_metric(baseline, "sharpe")
    m_sharpe = _get_metric(multi, "sharpe")
    b_sortino = _get_metric(baseline, "sortino")
    m_sortino = _get_metric(multi, "sortino")
    b_mdd = _get_metric(baseline, "mdd")
    m_mdd = _get_metric(multi, "mdd")

    sharpe_improve = None if (b_sharpe is None or m_sharpe is None) else _safe_div(m_sharpe - b_sharpe, b_sharpe)
    sortino_improve = None if (b_sortino is None or m_sortino is None) else _safe_div(m_sortino - b_sortino, b_sortino)

    # mdd is usually negative; larger absolute drawdown is worse.
    mdd_worsened = None
    if b_mdd is not None and m_mdd is not None:
        mdd_worsened = abs(m_mdd) - abs(b_mdd)

    print()
    print("=== Gate Check ===")
    print(f"Sharpe improve:  {_fmt_pct(sharpe_improve)}")
    print(f"Sortino improve: {_fmt_pct(sortino_improve)}")
    print(f"MDD worsened:    {_fmt_pct(mdd_worsened)} (limit={args.mdd_worsen_limit:.2%})")

    risk_ok = False
    if sharpe_improve is not None and sortino_improve is not None:
        risk_ok = (
            sharpe_improve >= args.risk_improve_threshold
            or sortino_improve >= args.risk_improve_threshold
        )

    mdd_ok = (mdd_worsened is not None) and (mdd_worsened <= args.mdd_worsen_limit)

    passed = risk_ok and mdd_ok
    print(f"Decision: {'PASS (Go Phase 2)' if passed else 'HOLD (Keep Phase 1 tuning)'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
