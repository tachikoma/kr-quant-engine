#!/usr/bin/env python3
"""포트폴리오 trailing stop 임계값의 전체기간 민감도를 비교한다."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run_etf_backtest as rtb

OUTPUT_PATH = ROOT / "outputs_etf_only" / "portfolio_stop_comparison.csv"


def _thresholds(value: str) -> list[float]:
    try:
        thresholds = [float(item.strip()) for item in value.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("임계값은 쉼표로 구분한 숫자여야 합니다.") from exc
    if not thresholds or any(not 0 <= threshold < 1 for threshold in thresholds):
        raise argparse.ArgumentTypeError("각 임계값은 0 이상 1 미만이어야 합니다.")
    return thresholds


def main() -> None:
    parser = argparse.ArgumentParser(description="포트폴리오 trailing stop 민감도 분석")
    parser.add_argument("--thresholds", type=_thresholds, default=_thresholds("0,0.15,0.20,0.25"))
    parser.add_argument("--check-days", type=int, default=1)
    args = parser.parse_args()
    if args.check_days <= 0:
        parser.error("--check-days는 1 이상이어야 합니다.")

    print("[portfolio-stop] 데이터 1회 로드")
    index_df = rtb.get_index_data()
    common_dates = list(index_df["date"])
    price_data = rtb.load_etf_price()
    risk_off_liquidate = rtb.strategy_cfg.get("liquidate_on_risk_off", True)

    rows = []
    for threshold in args.thresholds:
        print(f"[portfolio-stop] 임계값 {threshold:.0%} 실행")
        curve, trades = rtb.run_etf_strategy(
            rtb.INITIAL_CASH,
            common_dates,
            index_df,
            use_market_filter=True,
            max_positions=rtb.ETF_MAX_POSITIONS,
            slippage=rtb.BASE_SLIPPAGE,
            risk_off_liquidate=risk_off_liquidate,
            price_data=price_data,
            exit_check_days=args.check_days,
            trailing_stop_pct=0.0,
            portfolio_trailing_stop_pct=threshold,
        )
        first_strategy_date = pd.Timestamp(curve["date"].min())
        warmup_dates = [
            pd.Timestamp(date)
            for date in common_dates[1:]
            if pd.Timestamp(date) < first_strategy_date
        ]
        warmup_curve = pd.DataFrame(
            {"date": warmup_dates, "equity": float(rtb.INITIAL_CASH)}
        )
        report_curve = pd.concat([warmup_curve, curve[["date", "equity"]]], ignore_index=True)
        stats = rtb.calc_stats(report_curve, "equity")
        stop_sells = trades.loc[trades["reason"] == "ETF_PORTFOLIO_TRAILING_STOP"]
        rows.append(
            {
                "threshold": threshold,
                "check_days": args.check_days,
                "final": stats["final"],
                "cagr": stats["cagr"],
                "mdd": stats["mdd"],
                "sharpe": stats["sharpe"],
                "sortino": stats["sortino"],
                "calmar": stats["calmar"],
                "current_drawdown": stats["current_drawdown"],
                "trade_count": len(trades),
                "portfolio_stop_events": stop_sells["date"].nunique(),
                "portfolio_stop_sells": len(stop_sells),
                "portfolio_stop_dates": "|".join(
                    sorted(pd.to_datetime(stop_sells["date"]).dt.date.astype(str).unique())
                ),
            }
        )

    comparison = pd.DataFrame(rows)
    comparison.to_csv(OUTPUT_PATH, index=False, encoding="utf-8-sig")
    display = comparison.copy()
    for column in ("cagr", "mdd", "sharpe", "calmar", "current_drawdown"):
        display[column] = display[column].map(lambda value: f"{value:.4f}")
    print("\n", display.to_string(index=False))
    print(f"\n저장: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
