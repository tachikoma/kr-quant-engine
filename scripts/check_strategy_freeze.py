"""동결 전략 변경 여부와 표본외 성과를 점검한다."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if not (value.startswith(('"', "'")) and value.endswith(('"', "'"))):
            value = value.split(" #", 1)[0].strip()
        os.environ.setdefault(key.strip(), value.strip('"').strip("'"))


load_dotenv()

from config_utils import parse_fraction_env, parse_pct_env
from etf_distributions import distributions_file_sha256, distributions_path
from etf_shared import (
    ETF_DEVIATION_THRESHOLD_BY_GROUP,
    ETF_DEVIATION_THRESHOLD_BY_TICKER,
    ETF_LIST,
    ETF_MAX_POSITIONS,
    ETF_SELL_RANK_BUFFER,
    ETF_TICKER_GROUPS,
    GROUP_RISK_OVERRIDE,
    MARKET_MA_DAYS,
    MARKET_SLOPE_DAYS,
    TAXABLE_ETF_TICKERS,
    get_strategy_config,
)
from strategy_freeze import canonical_payload, diff_payloads, load_frozen_strategy


def current_strategy_payload() -> dict:
    cfg = get_strategy_config()
    return {
        "universe": list(ETF_LIST),
        "parameters": {
            "return_basis": cfg.get("return_basis", "price"),
            "distributions_file": str(distributions_path()),
            "distributions_sha256": distributions_file_sha256(),
            "distribution_tax_pct": parse_pct_env("ETF_DISTRIBUTION_TAX_PCT", 0.0),
            "min_listing_days": cfg.get("min_listing_days", 60),
            "max_premium_discount": cfg.get("max_premium_discount", 0.02),
            "deviation_threshold_by_group": dict(ETF_DEVIATION_THRESHOLD_BY_GROUP),
            "deviation_threshold_by_ticker": dict(ETF_DEVIATION_THRESHOLD_BY_TICKER),
            "min_avg_trading_value": cfg.get("min_avg_trading_value", 1_000_000_000),
            "max_asset_pct": parse_fraction_env("MAX_ASSET_PCT", 0.50),
            "target_weight_rebalance": cfg.get("target_weight_rebalance", False),
            "rebalance_band_pct": cfg.get("rebalance_band_pct", 0.05),
            "liquidate_on_risk_off": cfg.get("liquidate_on_risk_off", True),
            "slippage": parse_pct_env("ETF_BASE_SLIPPAGE", 0.0005),
            "spread_pct": parse_pct_env("ETF_SPREAD_PCT", 0.0005),
            "rebalance_step_days": cfg.get("rebalance_step_days", 10),
            "market_ma_days": MARKET_MA_DAYS,
            "market_slope_days": MARKET_SLOPE_DAYS,
            "max_positions": ETF_MAX_POSITIONS,
            "sell_rank_buffer": ETF_SELL_RANK_BUFFER,
            "use_market_filter": True,
        },
        "ticker_groups": dict(ETF_TICKER_GROUPS),
        "group_risk_override": sorted(GROUP_RISK_OVERRIDE),
        "taxable_tickers": sorted(TAXABLE_ETF_TICKERS),
    }


def calc_stats(curve: pd.DataFrame, equity_col: str) -> dict:
    curve = curve.sort_values("date").reset_index(drop=True)
    returns = curve[equity_col].pct_change().dropna()
    initial = float(curve[equity_col].iloc[0])
    final = float(curve[equity_col].iloc[-1])
    years = max((curve["date"].iloc[-1] - curve["date"].iloc[0]).days / 365.25, 1 / 365.25)
    volatility = float(returns.std(ddof=0) * np.sqrt(252)) if not returns.empty else 0.0
    return {
        "initial": initial,
        "final": final,
        "total_return": final / initial - 1,
        "cagr": (final / initial) ** (1 / years) - 1,
        "mdd": float((curve[equity_col] / curve[equity_col].cummax() - 1).min()),
        "volatility": volatility,
        "sharpe": float(returns.mean() * 252 / volatility) if volatility else None,
    }


def calculate_oos_stats(freeze_date: str, start_date: str) -> dict | None:
    curve_path = ROOT / "outputs_etf_only" / "etf_equity_curve.csv"
    if not curve_path.exists():
        return None
    curve = pd.read_csv(curve_path)
    equity_col = "equity_strategy" if "equity_strategy" in curve else "equity"
    curve["date"] = pd.to_datetime(curve["date"])
    evaluation = curve[curve["date"] >= pd.Timestamp(freeze_date)][
        ["date", equity_col]
    ].dropna()
    oos_rows = evaluation[evaluation["date"] >= pd.Timestamp(start_date)]
    if len(evaluation) < 2 or oos_rows.empty:
        return None
    stats = calc_stats(evaluation, equity_col)
    return {
        "baseline_date": str(evaluation["date"].iloc[0].date()),
        "start": str(oos_rows["date"].iloc[0].date()),
        "end": str(evaluation["date"].iloc[-1].date()),
        "trading_days": len(oos_rows),
        **{key: (None if pd.isna(value) else float(value)) for key, value in stats.items()},
    }


def main() -> int:
    frozen = load_frozen_strategy()
    diffs = diff_payloads(canonical_payload(frozen), current_strategy_payload())
    report = {
        "freeze_date": frozen["freeze_date"],
        "oos_start_date": frozen["oos_start_date"],
        "freeze_sha256": frozen["sha256"],
        "strategy_unchanged": not diffs,
        "differences": diffs,
        "oos_performance": calculate_oos_stats(
            frozen["freeze_date"], frozen["oos_start_date"]
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not diffs else 1


if __name__ == "__main__":
    raise SystemExit(main())
