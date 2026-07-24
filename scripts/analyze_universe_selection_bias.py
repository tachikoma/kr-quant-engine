#!/usr/bin/env python3
"""Static ETF 유니버스의 사후 선택 민감도를 정량화한다.

이 분석은 현재 static 유니버스에서 상장 시점·최근 테마·커버드콜 ETF를
제외한 시나리오와 테마 ETF leave-one-out을 동일한 전략 설정으로 비교한다.
현재 생존 종목만 사용하므로 완전한 point-in-time 유니버스 검증은 아니다.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run_etf_backtest as rtb
from pykrx_utils import load_tax_classification


OUTPUT_DIR = ROOT / "outputs_universe_bias"
BASELINE_SCENARIO = "baseline_current_static"
FULL_PERIOD_START = pd.Timestamp("2016-01-06")
RECENT_THEME_TICKERS = {"0101N0", "463250", "367760"}
COVERED_CALL_TICKERS = {"472150", "486290", "498400"}
EVALUATION_STARTS = {
    "full_period": None,
    "since_2023": pd.Timestamp("2023-01-01"),
    "since_2024": pd.Timestamp("2024-01-01"),
}


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    tickers: tuple[str, ...]


def _normalize_listing_date(value: object) -> pd.Timestamp | None:
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else pd.Timestamp(parsed)


def _classification_metadata(tickers: list[str]) -> pd.DataFrame:
    classification = load_tax_classification()
    if classification is None or classification.empty:
        raise RuntimeError("ETF 분류 캐시가 없어 상장일 기반 분석을 실행할 수 없습니다.")

    required = {"ISU_SRT_CD", "ISU_ABBRV", "LIST_DD"}
    missing = required - set(classification.columns)
    if missing:
        raise RuntimeError(f"ETF 분류 캐시 필수 컬럼 누락: {sorted(missing)}")

    metadata = classification[["ISU_SRT_CD", "ISU_ABBRV", "LIST_DD"]].copy()
    metadata["ticker"] = metadata["ISU_SRT_CD"].astype(str).str.strip()
    metadata = metadata[metadata["ticker"].isin(tickers)].copy()
    metadata["listing_date"] = metadata["LIST_DD"].map(_normalize_listing_date)
    metadata = metadata.rename(columns={"ISU_ABBRV": "name"})
    metadata = metadata[["ticker", "name", "listing_date"]].drop_duplicates("ticker")

    missing_tickers = sorted(set(tickers) - set(metadata["ticker"]))
    if missing_tickers:
        raise RuntimeError(f"ETF 분류 캐시에 없는 static 티커: {missing_tickers}")
    return metadata.sort_values(["listing_date", "ticker"]).reset_index(drop=True)


def _build_scenarios(metadata: pd.DataFrame, baseline: list[str]) -> list[Scenario]:
    listing_dates = dict(zip(metadata["ticker"], metadata["listing_date"], strict=False))

    def included_before(cutoff: str) -> list[str]:
        boundary = pd.Timestamp(cutoff)
        return [
            ticker
            for ticker in baseline
            if listing_dates.get(ticker) is not None and listing_dates[ticker] < boundary
        ]

    def excluding(excluded: set[str]) -> list[str]:
        return [ticker for ticker in baseline if ticker not in excluded]

    scenarios = [
        Scenario(
            BASELINE_SCENARIO,
            "현재 static 유니버스 전체",
            tuple(baseline),
        ),
        Scenario(
            "listed_before_2023",
            "2023-01-01 이전 상장된 현재 생존 종목만 유지",
            tuple(included_before("2023-01-01")),
        ),
        Scenario(
            "listed_before_2020",
            "2020-01-01 이전 상장된 현재 생존 종목만 유지",
            tuple(included_before("2020-01-01")),
        ),
        Scenario(
            "exclude_recent_themes",
            "AI전력·K방산·네트워크인프라 제외",
            tuple(excluding(RECENT_THEME_TICKERS)),
        ),
        Scenario(
            "exclude_covered_call",
            "static 유니버스의 커버드콜 ETF 제외",
            tuple(excluding(COVERED_CALL_TICKERS)),
        ),
        Scenario(
            "legacy_no_recent_or_covered",
            "최근 테마와 커버드콜 ETF 모두 제외",
            tuple(excluding(RECENT_THEME_TICKERS | COVERED_CALL_TICKERS)),
        ),
    ]
    for ticker in sorted(RECENT_THEME_TICKERS):
        scenarios.append(
            Scenario(
                f"leave_one_out_{ticker}",
                f"최근 테마 {ticker} 1종목만 제외",
                tuple(excluding({ticker})),
            )
        )
    return scenarios


def _evaluation_curve(
    curve: pd.DataFrame,
    common_dates: list[pd.Timestamp],
    start: pd.Timestamp | None,
) -> pd.DataFrame:
    evaluation = curve[["date", "equity"]].copy()
    evaluation["date"] = pd.to_datetime(evaluation["date"])
    if start is None:
        calendar_dates = pd.to_datetime(common_dates)
        calendar = pd.DataFrame({"date": calendar_dates[calendar_dates >= FULL_PERIOD_START]})
        evaluation = calendar.merge(evaluation, on="date", how="left")
        evaluation["equity"] = evaluation["equity"].ffill().fillna(float(rtb.INITIAL_CASH))
    else:
        evaluation = evaluation[evaluation["date"] >= start].copy()
    return evaluation.sort_values("date").drop_duplicates("date", keep="last")


def _serialize_number(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def run_analysis() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    baseline = [str(ticker) for ticker in rtb.ETF_LIST]
    metadata = _classification_metadata(baseline)
    scenarios = _build_scenarios(metadata, baseline)

    print("[유니버스 편향] static 전체 가격 데이터 로드")
    index_df = rtb.get_index_data()
    common_dates = list(index_df["date"])
    all_price = rtb.load_etf_price(baseline)

    summary_rows: list[dict] = []
    trade_rows: list[dict] = []
    membership_rows: list[dict] = []
    for scenario in scenarios:
        tickers = list(scenario.tickers)
        if not tickers:
            raise RuntimeError(f"빈 유니버스 시나리오: {scenario.name}")
        print(
            f"[유니버스 편향] {scenario.name}: "
            f"{len(tickers)}종목 백테스트"
        )
        scenario_price = all_price[all_price["ticker"].isin(tickers)].copy()
        curve, trades = rtb.run_etf_strategy(
            rtb.INITIAL_CASH,
            common_dates,
            index_df,
            use_market_filter=rtb.USE_MARKET_FILTER,
            max_positions=rtb.ETF_MAX_POSITIONS,
            slippage=rtb.BASE_SLIPPAGE,
            risk_off_liquidate=rtb.strategy_cfg.get("liquidate_on_risk_off", True),
            price_data=scenario_price,
            max_asset_pct=rtb.strategy_cfg.get("max_asset_pct"),
            target_weight_rebalance=rtb.strategy_cfg.get("target_weight_rebalance"),
            rebalance_band_pct=rtb.strategy_cfg.get("rebalance_band_pct"),
            trim_overweight_positions=rtb.strategy_cfg.get("trim_overweight_positions"),
            exit_check_days=rtb.strategy_cfg.get("exit_check_days"),
            trailing_stop_pct=rtb.strategy_cfg.get("trailing_stop_pct"),
            portfolio_trailing_stop_pct=rtb.strategy_cfg.get(
                "portfolio_trailing_stop_pct"
            ),
            enable_multi_index_risk=False,
            universe_tickers=tickers,
        )
        curve["date"] = pd.to_datetime(curve["date"])
        trades["date"] = pd.to_datetime(trades["date"])

        for ticker in tickers:
            membership_rows.append({"scenario": scenario.name, "ticker": ticker})

        if not trades.empty:
            grouped_trades = trades.groupby("ticker", dropna=False)
            for ticker, ticker_trades in grouped_trades:
                trade_rows.append(
                    {
                        "scenario": scenario.name,
                        "ticker": str(ticker),
                        "trade_count": int(len(ticker_trades)),
                        "buy_count": int((ticker_trades["side"] == "BUY").sum()),
                        "sell_count": int((ticker_trades["side"] == "SELL").sum()),
                        "first_trade_date": str(ticker_trades["date"].min().date()),
                        "last_trade_date": str(ticker_trades["date"].max().date()),
                    }
                )

        for period, start in EVALUATION_STARTS.items():
            evaluation = _evaluation_curve(curve, common_dates, start)
            if len(evaluation) < 2:
                continue
            stats = rtb.calc_stats(evaluation, "equity")
            period_trades = trades if start is None else trades[trades["date"] >= start]
            aligned_curve = curve[curve["date"].isin(evaluation["date"])]
            invested_ratio = (
                aligned_curve["market_value"] / aligned_curve["equity"]
                if not aligned_curve.empty
                else pd.Series(dtype=float)
            )
            summary_rows.append(
                {
                    "scenario": scenario.name,
                    "description": scenario.description,
                    "period": period,
                    "evaluation_start": str(evaluation["date"].iloc[0].date()),
                    "evaluation_end": str(evaluation["date"].iloc[-1].date()),
                    "universe_size": len(tickers),
                    "initial": float(stats["initial"]),
                    "final": float(stats["final"]),
                    "total_return": float(stats["total_return"]),
                    "cagr": float(stats["cagr"]),
                    "mdd": float(stats["mdd"]),
                    "sharpe": _serialize_number(stats["sharpe"]),
                    "sortino": _serialize_number(stats["sortino"]),
                    "avg_invested_ratio": (
                        float(invested_ratio.mean()) if not invested_ratio.empty else None
                    ),
                    "trade_count": int(len(period_trades)),
                    "unique_traded_tickers": int(period_trades["ticker"].nunique()),
                }
            )

    summary = pd.DataFrame(summary_rows)
    baseline_metrics = summary[summary["scenario"] == BASELINE_SCENARIO][
        ["period", "cagr", "mdd", "sharpe", "sortino", "final"]
    ].rename(
        columns={
            "cagr": "baseline_cagr",
            "mdd": "baseline_mdd",
            "sharpe": "baseline_sharpe",
            "sortino": "baseline_sortino",
            "final": "baseline_final",
        }
    )
    summary = summary.merge(baseline_metrics, on="period", how="left")
    for metric in ("cagr", "mdd", "sharpe", "sortino", "final"):
        summary[f"delta_{metric}"] = summary[metric] - summary[f"baseline_{metric}"]
    summary = summary.sort_values(["period", "scenario"]).reset_index(drop=True)

    membership = pd.DataFrame(membership_rows).merge(metadata, on="ticker", how="left")
    membership["listing_date"] = membership["listing_date"].dt.strftime("%Y-%m-%d")
    trades_summary = pd.DataFrame(trade_rows)
    if not trades_summary.empty:
        trades_summary = trades_summary.merge(
            metadata.assign(
                listing_date=metadata["listing_date"].dt.strftime("%Y-%m-%d")
            ),
            on="ticker",
            how="left",
        )

    run_metadata = {
        "baseline_scenario": BASELINE_SCENARIO,
        "baseline_tickers": baseline,
        "recent_theme_tickers": sorted(RECENT_THEME_TICKERS),
        "covered_call_tickers": sorted(COVERED_CALL_TICKERS),
        "scenario_count": len(scenarios),
        "evaluation_periods": {
            name: None if start is None else str(start.date())
            for name, start in EVALUATION_STARTS.items()
        },
        "config": {
            "rebalance_step_days": int(rtb.REBALANCE_STEP_DAYS),
            "max_positions": int(rtb.ETF_MAX_POSITIONS),
            "max_asset_pct": float(rtb.strategy_cfg.get("max_asset_pct", 0.0)),
            "slippage": float(rtb.BASE_SLIPPAGE),
            "enable_multi_index_risk": False,
        },
        "limitations": [
            "현재 생존 static 종목만 사용하므로 상장폐지 ETF를 포함한 완전한 point-in-time 유니버스가 아님",
            "listed_before 시나리오도 현재 생존 종목 중 상장일로만 제한한 민감도 분석임",
            "시나리오를 사후에 정의했으므로 결과는 전부 in-sample 진단으로 취급",
        ],
    }
    return summary, membership, trades_summary, run_metadata


def main() -> None:
    summary, membership, trades_summary, metadata = run_analysis()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summary.to_csv(
        OUTPUT_DIR / "universe_bias_summary.csv", index=False, encoding="utf-8-sig"
    )
    membership.to_csv(
        OUTPUT_DIR / "universe_bias_membership.csv", index=False, encoding="utf-8-sig"
    )
    trades_summary.to_csv(
        OUTPUT_DIR / "universe_bias_trade_summary.csv", index=False, encoding="utf-8-sig"
    )
    (OUTPUT_DIR / "universe_bias_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    display = summary[summary["period"] == "full_period"][
        [
            "scenario",
            "universe_size",
            "cagr",
            "delta_cagr",
            "mdd",
            "delta_mdd",
            "sharpe",
            "trade_count",
        ]
    ]
    print("\n=== 유니버스 선택 편향 민감도: 전체 기간 ===")
    print(display.to_string(index=False, float_format=lambda value: f"{value:.4f}"))
    print(f"\n저장 완료: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
