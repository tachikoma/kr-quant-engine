#!/usr/bin/env python3
"""Point-in-time 유니버스 백테스트 — 생존 편향 제거 검증.

현재 static `ETF_LIST`(16종목)는 오늘의 정보로 확정된 후보군이므로, 과거 성과에
유니버스 선택 편향이 포함됩니다. 이 스크립트는 `data_cache/pit_prices/`의
전체 1,370종목 가격과 `pit_universe_snapshots.parquet`의 시점별 membership을
사용해, 각 리밸런싱 시점에 실제 존재했던 종목만 후보로 허용하는 PIT 백테스트를
실행합니다.

사용: `uv run scripts/pit_backtest.py`
출력: `outputs_pit/pit_equity_curve.csv`, `pit_trades.csv`, `pit_summary.json`
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run_etf_backtest as rtb
from etf_distributions import add_distributions, load_distributions
from etf_shared import (
    ETF_LIST,
    add_deviation_flag,
    add_liquidity_flag,
    add_listing_flag,
    add_price_basis_columns,
)
from pit_universe import (
    add_pit_membership_flag,
    build_pit_ticker_groups,
    validate_pit_preflight,
)

OUTPUT_DIR = ROOT / "outputs_pit"
PIT_PANEL = ROOT / "data_cache" / "pit_universe" / "pit_universe_snapshots.parquet"
PIT_PRICE_DIR = ROOT / "data_cache" / "pit_prices"
START_DATE = pd.Timestamp("2016-08-01")
END_DATE = pd.Timestamp("2026-07-21")


def load_all_pit_prices(price_dir: Path) -> pd.DataFrame:
    """data_cache/pit_prices의 모든 종목 parquet를 하나의 DataFrame으로 병합한다."""
    files = sorted(price_dir.glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"PIT 가격 캐시가 없습니다: {price_dir}")
    frames = []
    for path in files:
        df = pd.read_parquet(path)
        if "date" in df.columns and "close" in df.columns:
            frames.append(df[["date", "ticker", "open", "high", "low", "close", "volume", "trading_value", "nav"]])
    if not frames:
        raise RuntimeError("PIT 가격 캐시가 비어 있습니다.")
    price = pd.concat(frames, ignore_index=True)
    price["date"] = pd.to_datetime(price["date"])
    price["ticker"] = price["ticker"].astype(str)
    return price.sort_values(["ticker", "date"]).reset_index(drop=True)


def build_pit_price_data(panel: pd.DataFrame) -> pd.DataFrame:
    """PIT 유니버스용 전처리 가격 데이터를 구축한다.

    static `load_etf_price`와 동일한 변환(분배금·유동성·상장·괴리율·수익률 기준·
    모멘텀/추세)을 적용하고, PIT membership 플래그를 추가한다.
    """
    price = load_all_pit_prices(PIT_PRICE_DIR)

    distributions = load_distributions(required=False)
    price = add_distributions(price, distributions)
    price = add_liquidity_flag(price)
    price = add_listing_flag(price)  # 상장일 미지정 종목은 listing_ok=True 허용
    price = add_deviation_flag(price)
    price = add_price_basis_columns(price)

    grouped = price.groupby("ticker")
    price["ret_60"] = grouped["close_adj"].pct_change(60)
    price["ret_120"] = grouped["close_adj"].pct_change(120)
    price["ma20"] = grouped["close_adj"].transform(lambda x: x.rolling(20).mean())
    price["ma60"] = grouped["close_adj"].transform(lambda x: x.rolling(60).mean())
    price["trend_ok"] = (price["close_adj"] > price["ma20"]) & (price["ma20"] > price["ma60"])

    price = add_pit_membership_flag(price, panel)
    return price


def main() -> int:
    if not PIT_PANEL.exists():
        raise FileNotFoundError(f"PIT 패널이 없습니다: {PIT_PANEL}")
    panel = pd.read_parquet(PIT_PANEL)

    print("[pit_backtest] PIT 가격 데이터 구축 (전체 1,370종목)")
    pit_price = build_pit_price_data(panel)

    index_df = rtb.get_index_data()
    common_dates = [
        d for d in pd.to_datetime(index_df["date"])
        if START_DATE <= d <= END_DATE
    ]

    # PIT 유니버스: 전체 티커 (membership 플래그가 as-of로 후보를 제한)
    all_tickers = sorted(pit_price["ticker"].unique())
    ticker_groups = build_pit_ticker_groups(
        current_classification_path=ROOT / "data_cache" / "etf_tax_classification.parquet",
        restored_classification_path=ROOT
        / "data_cache"
        / "pit_universe"
        / "pit_classification_restored.parquet",
    )
    print(f"[pit_backtest] 그룹 매핑: {len(ticker_groups)}개 (static {len(ETF_LIST)}개)")

    preflight = validate_pit_preflight(
        panel=panel,
        price=pit_price,
        trading_dates=common_dates,
        decision_dates=common_dates,
        ticker_groups=ticker_groups,
    )
    print(f"[pit_backtest] strict preflight 통과: {preflight['decision_date_count']}개 거래일")

    us_index_df = (
        rtb.get_us_index_data()
        if os.environ.get("ENABLE_MULTI_INDEX_RISK") == "1"
        else None
    )

    print(f"[pit_backtest] PIT 백테스트 실행 (티커 {len(all_tickers)}개, {common_dates[0].date()}~{common_dates[-1].date()})")
    curve, trades = rtb.run_etf_strategy(
        rtb.INITIAL_CASH,
        common_dates,
        index_df,
        use_market_filter=True,
        max_positions=int(os.environ.get("ETF_MAX_POSITIONS", "2")),
        slippage=rtb.BASE_SLIPPAGE,
        risk_off_liquidate=bool(rtb.strategy_cfg.get("liquidate_on_risk_off", True)),
        price_data=pit_price,
        us_index_df=us_index_df,
        enable_multi_index_risk=os.environ.get("ENABLE_MULTI_INDEX_RISK") == "1",
        universe_tickers=all_tickers,
        ticker_groups=ticker_groups,
    )
    curve = curve.sort_values("date").reset_index(drop=True)
    stats = rtb.calc_stats(curve, "equity")

    # 동일 구간 static 유니버스 결과 재현 (비교용)
    print("[pit_backtest] static 유니버스 동일 구간 재현")
    static_price = rtb.load_etf_price(ETF_LIST)
    static_curve, _ = rtb.run_etf_strategy(
        rtb.INITIAL_CASH,
        common_dates,
        index_df,
        use_market_filter=True,
        max_positions=int(os.environ.get("ETF_MAX_POSITIONS", "2")),
        slippage=rtb.BASE_SLIPPAGE,
        risk_off_liquidate=bool(rtb.strategy_cfg.get("liquidate_on_risk_off", True)),
        price_data=static_price,
        us_index_df=us_index_df,
        enable_multi_index_risk=os.environ.get("ENABLE_MULTI_INDEX_RISK") == "1",
        universe_tickers=ETF_LIST,
    )
    static_curve = static_curve.sort_values("date").reset_index(drop=True)
    static_stats = rtb.calc_stats(static_curve, "equity")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    curve.to_csv(OUTPUT_DIR / "pit_equity_curve.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUTPUT_DIR / "pit_trades.csv", index=False, encoding="utf-8-sig")
    static_curve.to_csv(OUTPUT_DIR / "static_equity_curve.csv", index=False, encoding="utf-8-sig")

    summary = {
        "universe": {
            "pit_tickers": len(all_tickers),
            "static_tickers": len(ETF_LIST),
            "start": str(START_DATE.date()),
            "end": str(END_DATE.date()),
        },
        "pit": {k: (None if pd.isna(v) else v) for k, v in stats.items()},
        "static": {k: (None if pd.isna(v) else v) for k, v in static_stats.items()},
        "difference": {
            "cagr_delta": float(stats["cagr"] - static_stats["cagr"]),
            "mdd_delta": float(stats["mdd"] - static_stats["mdd"]),
        },
    }
    (OUTPUT_DIR / "pit_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print()
    print("=== PIT vs static (동일 구간 2016-08-01~2026-07-21) ===")
    for label, s in [("PIT", stats), ("static", static_stats)]:
        print(
            f"{label:8s} CAGR={float(s['cagr']):6.2%}  MDD={float(s['mdd']):7.2%}  "
            f"Sharpe={float(s['sharpe']):.2f}  최종={float(curve['equity'].iloc[-1] if label=='PIT' else static_curve['equity'].iloc[-1]):,.0f}"
        )
    print(f"\n[pit_backtest] 저장 완료: {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
