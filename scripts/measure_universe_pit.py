#!/usr/bin/env python3
"""Auto vs Static + PIT 생존편향 간극 측정 드라이버.

세 케이스를 동일 조건(2015-01-01~2025-08-04, trailing OFF, price 기준,
slippage 5bp, rebalance 20일)으로 측정한다.

  Case S : static universe (ETF_LIST 16종, pit_membership 필터 없음) — baseline
  Case A : auto universe 비-PIT (현재 KRX 목록, pit_membership 없음) — 생존편향 포함
  Case P : auto PIT (같은 auto 유니버스 + pit_membership_flag 적용) — survivorship-bias-free

PIT 데이터(data_cache/pit_prices, data_cache/pit_universe/pit_universe_snapshots.parquet)
가 없으면 Case P는 건너뛴다(SKIP). 나머지 케이스는 data_cache의 개별 티커 parquet을
재사용하므로 네트워크 최소화(인덱스 1회만 가능).

출력: outputs_universe_pit/{case}_equity_curve.csv, {case}_trades.csv, summary.json
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd

# ── 조건 고정 (run_etf_backtest import 전에 설정) ──────────────────────
os.environ["ETF_RETURN_BASIS"] = "price"
os.environ["REBALANCE_STEP_DAYS"] = "20"
os.environ["ETF_USE_CACHE"] = "1"
os.environ["ETF_TRAILING_STOP_PCT"] = "0"
os.environ["ETF_PORTFOLIO_TRAILING_STOP_PCT"] = "0"
os.environ["ETF_EXIT_CHECK_DAYS"] = "0"
os.environ["ETF_MAX_POSITIONS"] = "2"
os.environ["ETF_UNIVERSE_MODE"] = "static"  # 유니버스는 직접 주입
os.environ.setdefault("ETF_UNIVERSE_EXCLUDE_KEYWORDS", "커버드콜")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run_etf_backtest as rtb  # noqa: E402
from etf_distributions import add_distributions, load_distributions  # noqa: E402
from etf_shared import (  # noqa: E402
    ETF_LIST,
    ETF_TICKER_GROUPS,
    add_deviation_flag,
    add_liquidity_flag,
    add_listing_flag,
    add_price_basis_columns,
)
from etf_universe import build_universe, config_from_env  # noqa: E402

OUTPUT_DIR = ROOT / "outputs_universe_pit"
START_DATE = pd.Timestamp("2015-01-01")
END_DATE = pd.Timestamp("2025-08-04")
SLIPPAGE = 0.0005  # 5bp

PIT_PANEL = ROOT / "data_cache" / "pit_universe" / "pit_universe_snapshots.parquet"
PIT_PRICE_DIR = ROOT / "data_cache" / "pit_prices"


def build_price_from_cache(tickers: list[str]) -> pd.DataFrame:
    """data_cache/{ticker}.parquet을 병합하고 load_etf_price와 동일 전처리를 적용한다."""
    frames = []
    for ticker in tickers:
        path = ROOT / "data_cache" / f"{ticker}.parquet"
        if not path.exists():
            print(f"[경고] 캐시 없음: {ticker} (건너뜀)")
            continue
        df = pd.read_parquet(path)
        keep = [c for c in ["date", "ticker", "open", "high", "low", "close", "volume", "trading_value", "nav"] if c in df.columns]
        frames.append(df[keep])
    if not frames:
        raise RuntimeError("캐시 가격 데이터가 없습니다.")
    price = pd.concat(frames, ignore_index=True)
    price["date"] = pd.to_datetime(price["date"])
    price["ticker"] = price["ticker"].astype(str)

    distributions = load_distributions(required=False)
    price = add_distributions(price, distributions)
    price = add_liquidity_flag(price)
    price = add_listing_flag(price)  # listing_dates 미지정 → 전종목 listing_ok=True
    price = add_deviation_flag(price)
    price = add_price_basis_columns(price)

    grouped = price.groupby("ticker")
    price["ret_60"] = grouped["close_adj"].pct_change(60)
    price["ret_120"] = grouped["close_adj"].pct_change(120)
    price["ma20"] = grouped["close_adj"].transform(lambda x: x.rolling(20).mean())
    price["ma60"] = grouped["close_adj"].transform(lambda x: x.rolling(60).mean())
    price["trend_ok"] = (price["close_adj"] > price["ma20"]) & (price["ma20"] > price["ma60"])
    return price.sort_values(["ticker", "date"]).reset_index(drop=True)


def run_case(label: str, tickers: list[str], ticker_groups: dict[str, str], price_data: pd.DataFrame) -> dict:
    index_df = rtb.get_index_data()
    common_dates = [d for d in pd.to_datetime(index_df["date"]) if START_DATE <= d <= END_DATE]
    print(f"[{label}] 거래일 {len(common_dates)}개 ({common_dates[0].date()}~{common_dates[-1].date()}), 유니버스 {len(tickers)}종")

    candidates: list[int] = []

    def _observer(ev: dict) -> None:
        candidates.append(int(ev.get("n_candidates", 0)))

    curve, trades = rtb.run_etf_strategy(
        rtb.INITIAL_CASH,
        common_dates,
        index_df,
        use_market_filter=True,
        max_positions=int(os.environ.get("ETF_MAX_POSITIONS", "2")),
        slippage=SLIPPAGE,
        risk_off_liquidate=bool(rtb.strategy_cfg.get("liquidate_on_risk_off", True)),
        price_data=price_data,
        us_index_df=None,
        enable_multi_index_risk=False,
        universe_tickers=tickers,
        ticker_groups=ticker_groups,
        rebalance_observer=_observer,
    )
    curve = curve.sort_values("date").reset_index(drop=True)
    stats = rtb.calc_stats(curve, "equity")
    trading = rtb.calc_trading_stats(
        trades,
        curve["equity"],
        curve["date"],
        rebalance_decisions=curve.get("rebalance_decision"),
    )
    avg_candidates = float(pd.Series(candidates).mean()) if candidates else float("nan")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    curve.to_csv(OUTPUT_DIR / f"{label}_equity_curve.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUTPUT_DIR / f"{label}_trades.csv", index=False, encoding="utf-8-sig")

    return {
        "label": label,
        "universe_size": len(tickers),
        "avg_candidates": avg_candidates,
        "cagr": float(stats["cagr"]),
        "mdd": float(stats["mdd"]),
        "sharpe": float(stats["sharpe"]) if pd.notna(stats["sharpe"]) else None,
        "total_return": float(stats["total_return"]),
        "final": float(stats["final"]),
        "trade_count": int(trading["trade_count"]),
        "avg_holding_days": (
            float(trading["avg_closed_holding_days"])
            if pd.notna(trading["avg_closed_holding_days"])
            else None
        ),
        "unique_traded_tickers": int(trades["ticker"].nunique()) if not trades.empty else 0,
        "start": str(START_DATE.date()),
        "end": str(END_DATE.date()),
    }


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results: dict[str, object] = {"period": {"start": str(START_DATE.date()), "end": str(END_DATE.date())}}

    # ── Case S: static ──────────────────────────────────────────────
    print("\n=== Case S: static universe ===")
    s_price = build_price_from_cache(list(ETF_LIST))
    results["S"] = run_case("S", list(ETF_LIST), dict(ETF_TICKER_GROUPS), s_price)

    # ── Case A: auto 비-PIT ─────────────────────────────────────────
    print("\n=== Case A: auto universe (non-PIT) ===")
    cls = pd.read_parquet(ROOT / "data_cache" / "etf_tax_classification.parquet")
    auto = build_universe(cls, config_from_env())
    a_price = build_price_from_cache(auto.tickers)
    results["A"] = run_case("A", auto.tickers, auto.ticker_groups, a_price)

    # ── Case P: auto PIT ───────────────────────────────────────────
    # 진짜 PIT는 data_cache/pit_prices(전체 1,370종 OHLCV) + pit_universe_snapshots.parquet
    # (시점별 membership) 이 둘 다 필요하다. 둘 중 하나라도 없으면 생존편향 제거 백테스트가
    # 불가능하므로 건너뛴다(SKIP). 이 드라이버는 데이터만 있으면 즉시 실행되도록 작성됨.
    print("\n=== Case P: auto PIT (survivorship-bias-free) ===")
    pit_available = PIT_PANEL.exists() and PIT_PRICE_DIR.exists() and any(PIT_PRICE_DIR.glob("*.parquet"))
    if not pit_available:
        print(f"[Case P] SKIP — PIT 데이터 부재 (panel={PIT_PANEL.exists()}, prices={PIT_PRICE_DIR.exists()})")
        results["P"] = {
            "label": "P",
            "status": "skipped",
            "reason": "PIT 데이터 없음: data_cache/pit_prices(전체 1,370종) 및 pit_universe_snapshots.parquet 필요. "
                      "생존편향 제거 백테스트 불가 → (A-P) 간극은 이번 실행에서 산출 불가.",
        }
    else:
        from pit_universe import add_pit_membership_flag, build_pit_ticker_groups

        panel = pd.read_parquet(PIT_PANEL)
        # PIT 전용 가격 로더 (pit_backtest.load_all_pit_prices 패턴 재사용)
        files = sorted(PIT_PRICE_DIR.glob("*.parquet"))
        frames = [
            pd.read_parquet(p)[["date", "ticker", "open", "high", "low", "close", "volume", "trading_value", "nav"]]
            for p in files
        ]
        pit_price = pd.concat(frames, ignore_index=True)
        pit_price["date"] = pd.to_datetime(pit_price["date"])
        pit_price["ticker"] = pit_price["ticker"].astype(str)
        distributions = load_distributions(required=False)
        pit_price = add_distributions(pit_price, distributions)
        pit_price = add_liquidity_flag(pit_price)
        pit_price = add_listing_flag(pit_price)
        pit_price = add_deviation_flag(pit_price)
        pit_price = add_price_basis_columns(pit_price)
        grouped = pit_price.groupby("ticker")
        pit_price["ret_60"] = grouped["close_adj"].pct_change(60)
        pit_price["ret_120"] = grouped["close_adj"].pct_change(120)
        pit_price["ma20"] = grouped["close_adj"].transform(lambda x: x.rolling(20).mean())
        pit_price["ma60"] = grouped["close_adj"].transform(lambda x: x.rolling(60).mean())
        pit_price["trend_ok"] = (pit_price["close_adj"] > pit_price["ma20"]) & (pit_price["ma20"] > pit_price["ma60"])
        pit_price = add_pit_membership_flag(pit_price, panel)
        all_tickers = sorted(pit_price["ticker"].unique())
        pit_groups = build_pit_ticker_groups(
            current_classification_path=ROOT / "data_cache" / "etf_tax_classification.parquet",
            restored_classification_path=ROOT / "data_cache" / "pit_universe" / "pit_classification_restored.parquet",
        )
        results["P"] = run_case("P", all_tickers, pit_groups, pit_price)

    # ── 간극 해석 ───────────────────────────────────────────────────
    if isinstance(results.get("S"), dict) and isinstance(results.get("A"), dict):
        s = results["S"]
        a = results["A"]
        results["gap_A_minus_S"] = {
            "cagr_delta": a["cagr"] - s["cagr"],
            "mdd_delta": a["mdd"] - s["mdd"],
        }
    if isinstance(results.get("P"), dict) and isinstance(results.get("A"), dict) and results["P"].get("status") != "skipped":
        a = results["A"]
        p = results["P"]
        results["gap_A_minus_P"] = {
            "cagr_delta": a["cagr"] - p["cagr"],
            "mdd_delta": a["mdd"] - p["mdd"],
        }

    (OUTPUT_DIR / "summary.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ── 콘솔 표 ─────────────────────────────────────────────────────
    print("\n=== 결과 요약 ===")
    hdr = f"{'Case':5s} {'Univ':>5s} {'AvgCand':>8s} {'CAGR':>8s} {'MDD':>8s} {'Sharpe':>7s} {'Trades':>7s} {'HoldD':>7s}"
    print(hdr)
    for key in ("S", "A", "P"):
        r = results.get(key)
        if not isinstance(r, dict) or r.get("status") == "skipped":
            print(f"{key:5s}  SKIPPED ({r.get('reason', '') if isinstance(r, dict) else ''})")
            continue
        print(
            f"{key:5s} {r['universe_size']:5d} {r['avg_candidates']:8.1f} "
            f"{r['cagr']:7.2%} {r['mdd']:7.2%} {r['sharpe'] if r['sharpe'] is not None else float('nan'):7.2f} "
            f"{r['trade_count']:7d} {r['avg_holding_days'] if r['avg_holding_days'] is not None else float('nan'):7.1f}"
        )
    print(f"\n[저장] {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
