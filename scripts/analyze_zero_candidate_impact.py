#!/usr/bin/env python3
"""risk_on 상태에서 후보가 0개인 리밸런싱의 실제 포트폴리오 영향을 분석한다."""

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

import run_etf_backtest as rtb
from config_utils import parse_pct_env
from etf_shared import get_strategy_config

OUTPUT_DIR = ROOT / "outputs_etf_only"
HORIZONS = (5, 10, 20, 40)


def _asof_value(series: pd.Series, date: pd.Timestamp) -> float | None:
    values = pd.to_numeric(series.loc[:date], errors="coerce").dropna()
    values = values[values > 0]
    if values.empty:
        return None
    return float(values.iloc[-1])


def _fixed_portfolio_return(
    event: dict,
    end_date: pd.Timestamp,
    price_by_ticker: dict[str, pd.DataFrame],
    distribution_tax_pct: float,
) -> float:
    """사건일 실제 수량과 현금을 고정한 포트폴리오의 순방향 수익률을 계산한다."""
    start_date = pd.Timestamp(event["decision_date"])
    holdings = event["pre_holdings"]
    initial_value = float(event["pre_equity"])
    if initial_value <= 0:
        return np.nan

    end_value = float(event["pre_cash"])
    distribution_cash = 0.0
    for ticker, qty in holdings.items():
        ticker_frame = price_by_ticker.get(str(ticker))
        if ticker_frame is None:
            return np.nan
        end_price = _asof_value(ticker_frame["close"], end_date)
        if end_price is None:
            return np.nan
        end_value += int(qty) * end_price

        if "distribution" in ticker_frame.columns:
            in_horizon = ticker_frame.loc[
                (ticker_frame.index > start_date) & (ticker_frame.index <= end_date),
                "distribution",
            ]
            per_share = pd.to_numeric(in_horizon, errors="coerce").fillna(0.0).sum()
            distribution_cash += int(qty) * float(per_share)

    end_value += distribution_cash * (1 - distribution_tax_pct)
    return end_value / initial_value - 1


def _forward_value(
    series: pd.Series,
    date: pd.Timestamp,
) -> float | None:
    if date not in series.index:
        return None
    value = series.loc[date]
    if isinstance(value, pd.Series):
        value = value.iloc[-1]
    if pd.isna(value):
        return None
    return float(value)


def _uninvested_duration(curve: pd.DataFrame, execution_date: pd.Timestamp) -> int:
    future = curve[curve["date"] >= execution_date].sort_values("date")
    duration = 0
    for holdings in future["holdings"].fillna(""):
        if str(holdings).strip():
            break
        duration += 1
    return duration


def _json_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _diagnostics_frame(events: list[dict]) -> pd.DataFrame:
    rows = []
    for event in events:
        row = dict(event)
        for key in ("ranked_tickers", "targets", "pre_holdings", "post_holdings"):
            row[key] = _json_value(row[key])
        pre_equity = float(row["pre_equity"])
        post_equity = float(row["post_equity"])
        row["pre_cash_ratio"] = float(row["pre_cash"]) / pre_equity if pre_equity else np.nan
        row["post_cash_ratio"] = (
            float(row["post_cash"]) / post_equity if post_equity else np.nan
        )
        rows.append(row)
    return pd.DataFrame(rows)


def _impact_rows(
    events: list[dict],
    common_dates: list[pd.Timestamp],
    index_df: pd.DataFrame,
    price: pd.DataFrame,
    curve: pd.DataFrame,
) -> list[dict]:
    date_positions = {pd.Timestamp(date): i for i, date in enumerate(common_dates)}
    kospi_close = index_df.set_index("date")["close"].sort_index()
    actual_equity = curve.set_index("date")["equity"].sort_index()
    price_by_ticker = {
        str(ticker): frame.set_index("date").sort_index()
        for ticker, frame in price.groupby("ticker")
    }
    distribution_tax_pct = max(
        0.0,
        min(parse_pct_env("ETF_DISTRIBUTION_TAX_PCT", 0.0), 1.0),
    )

    rows = []
    for event in events:
        if not event["risk_on"] or int(event["n_candidates"]) != 0:
            continue

        decision_date = pd.Timestamp(event["decision_date"])
        position = date_positions[decision_date]
        pre_equity = float(event["pre_equity"])
        row = {
            "decision_date": decision_date,
            "execution_date": pd.Timestamp(event["execution_date"]),
            "pre_holdings": _json_value(event["pre_holdings"]),
            "post_holdings": _json_value(event["post_holdings"]),
            "pre_cash": float(event["pre_cash"]),
            "pre_market_value": float(event["pre_market_value"]),
            "pre_equity": pre_equity,
            "pre_cash_ratio": float(event["pre_cash"]) / pre_equity if pre_equity else np.nan,
            "held_unchanged": bool(event["held_unchanged"]),
            "uninvested": bool(event["uninvested"]),
            "uninvested_duration_days": _uninvested_duration(
                curve, pd.Timestamp(event["execution_date"])
            ),
            "allow_empty_target_sell": bool(event["allow_empty_target_sell"]),
            "empty_target_protected": bool(event["empty_target_protected"]),
        }

        kospi_start = _forward_value(kospi_close, decision_date)
        for horizon in HORIZONS:
            end_position = position + horizon
            if end_position >= len(common_dates):
                row[f"horizon_date_{horizon}d"] = pd.NaT
                row[f"kospi_return_{horizon}d"] = np.nan
                row[f"fixed_return_{horizon}d"] = np.nan
                row[f"actual_return_{horizon}d"] = np.nan
                continue

            end_date = pd.Timestamp(common_dates[end_position])
            row[f"horizon_date_{horizon}d"] = end_date
            kospi_end = _forward_value(kospi_close, end_date)
            row[f"kospi_return_{horizon}d"] = (
                kospi_end / kospi_start - 1
                if kospi_start is not None and kospi_end is not None
                else np.nan
            )
            row[f"fixed_return_{horizon}d"] = _fixed_portfolio_return(
                event,
                end_date,
                price_by_ticker,
                distribution_tax_pct,
            )
            actual_end = _forward_value(actual_equity, end_date)
            row[f"actual_return_{horizon}d"] = (
                actual_end / pre_equity - 1
                if pre_equity > 0 and actual_end is not None
                else np.nan
            )
        rows.append(row)
    return rows


def _print_summary(impact: pd.DataFrame) -> None:
    print("\n=== 후보 0개 포트폴리오 영향 분석 ===")
    print("\n[1] 포트폴리오 영향 요약")
    print(f"총 risk_on + 후보 0개: {len(impact)}회")
    if impact.empty:
        return

    uninvested = int(impact["uninvested"].sum())
    unchanged = int(impact["held_unchanged"].sum())
    print(f"  무포지션 상태: {uninvested}회 ({uninvested / len(impact):.1%})")
    print(f"  보유 유지 상태: {unchanged}회 ({unchanged / len(impact):.1%})")
    print(
        "  현금 비중 평균/중앙값: "
        f"{impact['pre_cash_ratio'].mean():.1%} / {impact['pre_cash_ratio'].median():.1%}"
    )
    durations = impact.loc[impact["uninvested"], "uninvested_duration_days"]
    if durations.empty:
        print("  무포지션 지속: 해당 없음")
    else:
        print(f"  무포지션 지속: 평균 {durations.mean():.1f}일, 최대 {durations.max()}일")

    print("\n[2] 이후 수익률 (event study)")
    print(f"{'구간':<13}{'평균':>10}{'중앙값':>10}{'양의비율':>10}{'최소':>10}{'최대':>10}")
    labels = (("KOSPI", "kospi"), ("고정", "fixed"), ("전략", "actual"))
    for horizon in HORIZONS:
        for label, prefix in labels:
            values = impact[f"{prefix}_return_{horizon}d"].dropna()
            if values.empty:
                print(f"{f'{horizon}일 {label}':<13}{'N/A':>10}")
                continue
            positive_ratio = (values > 0).mean()
            print(
                f"{f'{horizon}일 {label}':<13}"
                f"{values.mean():>10.2%}{values.median():>10.2%}"
                f"{positive_ratio:>10.1%}{values.min():>10.2%}{values.max():>10.2%}"
            )

    print("\n[3] 사건별 상세")
    for _, row in impact.iterrows():
        holdings = json.loads(row["pre_holdings"])
        holdings_text = ", ".join(f"{ticker}×{qty}" for ticker, qty in holdings.items()) or "없음"
        print(
            f"{row['decision_date'].date()}: 보유={holdings_text}, "
            f"현금={row['pre_cash']:,.0f}원 ({row['pre_cash_ratio']:.1%}), "
            f"이후5일 고정={row['fixed_return_5d']:.2%}, "
            f"KOSPI={row['kospi_return_5d']:.2%}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="risk_on + 후보 0개 영향 분석")
    parser.add_argument("--start", default=None, help="시작일 (YYYYMMDD)")
    parser.add_argument("--end", default=None, help="종료일 (YYYYMMDD)")
    parser.add_argument("--step", type=int, default=None, help="리밸런싱 간격 (거래일)")
    args = parser.parse_args()

    if args.start:
        rtb.START = args.start
    if args.end:
        rtb.END = args.end
    if args.step is not None:
        if args.step <= 0:
            parser.error("--step은 1 이상이어야 합니다.")
        rtb.REBALANCE_STEP_DAYS = args.step

    print(f"백테스트 기간: {rtb.START} ~ {rtb.END}")
    print(f"리밸런싱 간격: {rtb.REBALANCE_STEP_DAYS}거래일")
    print("\n[1/3] ETF 가격 데이터 로딩...")
    price = rtb.load_etf_price()
    print("\n[2/3] KOSPI 지수 데이터 로딩...")
    index_df = rtb.get_index_data()
    common_dates = [pd.Timestamp(date) for date in index_df["date"]]

    print("\n[3/3] 리밸런싱 이벤트 및 포트폴리오 상태 기록...")
    events: list[dict] = []
    strategy_cfg = get_strategy_config()
    curve, _ = rtb.run_etf_strategy(
        rtb.INITIAL_CASH,
        common_dates,
        index_df,
        use_market_filter=rtb.USE_MARKET_FILTER,
        max_positions=rtb.ETF_MAX_POSITIONS,
        slippage=rtb.BASE_SLIPPAGE,
        risk_off_liquidate=strategy_cfg.get("liquidate_on_risk_off", True),
        price_data=price,
        rebalance_observer=events.append,
    )

    diagnostics = _diagnostics_frame(events)
    impact = pd.DataFrame(_impact_rows(events, common_dates, index_df, price, curve))
    _print_summary(impact)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    diagnostics_path = OUTPUT_DIR / "rebalance_diagnostics.csv"
    impact_path = OUTPUT_DIR / "zero_candidate_impact.csv"
    diagnostics.to_csv(diagnostics_path, index=False, encoding="utf-8-sig")
    impact.to_csv(impact_path, index=False, encoding="utf-8-sig")
    print(f"\n전체 리밸런싱 저장: {diagnostics_path}")
    print(f"후보 0개 사건 저장: {impact_path}")


if __name__ == "__main__":
    main()
