#!/usr/bin/env python3
"""복수 벤치마크·비용 비교 리포트를 생성한다.

전략 커브와 다양한 벤치마크(국내/해외/금/정책 포트폴리오/현금)를 비교하고,
주문 크기별 시장 충격(미체결/체결 슬리피지)을 추정한다.

사용: `uv run scripts/benchmark_comparison.py`
입력: `outputs_etf_only/etf_equity_curve.csv`, `outputs_etf_only/etf_trades.csv`
출력: `outputs_benchmark/benchmark_comparison.csv`, `market_impact.csv`, `report.json`
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


OUTPUT_DIR = ROOT / "outputs_benchmark"
CURVE_PATH = ROOT / "outputs_etf_only" / "etf_equity_curve.csv"
TRADES_PATH = ROOT / "outputs_etf_only" / "etf_trades.csv"

US_PROXY_TICKER = "143850"  # TIGER 미국S&P500선물(H)
GOLD_TICKER = "411060"  # ACE KRX금현물
KR_BENCH_TICKER = "069500"  # KODEX 200 (curve의 equity_benchmark와 동일)


def load_price(ticker: str) -> pd.DataFrame:
    path = ROOT / "data_cache" / f"{ticker}.parquet"
    if not path.exists():
        return pd.DataFrame(columns=["date", "close"])
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    return df[["date", "close"]].sort_values("date").reset_index(drop=True)


def build_asset_curves(dates: list[pd.Timestamp]) -> dict[str, pd.Series]:
    """전략 커브 날짜에 정렬된 벤치마크 자산별 에쿼티 시계열."""
    curve = pd.read_csv(CURVE_PATH)
    curve["date"] = pd.to_datetime(curve["date"])
    aligned = curve.set_index("date")
    assets: dict[str, pd.Series] = {}

    # KR (KODEX200) — 전략 커브에 포함된 equity_benchmark
    kr = aligned["equity_benchmark"].dropna()
    assets["kr_kodex200"] = kr

    # US (TIGER 미국S&P500선물(H)) — 커브 날짜에 맞춰 종가 기준 정규화
    us = load_price(US_PROXY_TICKER)
    if not us.empty:
        us = us.set_index("date")["close"]
        us = us.reindex(dates).ffill().dropna()
        assets["us_sp500_proxy"] = us / us.iloc[0] * float(kr.iloc[0])

    # Gold (ACE KRX금현물) — 부분 커버리지(2021-12~)
    gold = load_price(GOLD_TICKER)
    if not gold.empty:
        gold = gold.set_index("date")["close"]
        gold = gold.reindex(dates).ffill().dropna()
        if not gold.empty:
            assets["gold"] = gold / gold.iloc[0] * float(kr.iloc[0])

    # Cash (0% 수익률)
    assets["cash"] = pd.Series(float(kr.iloc[0]), index=kr.index)

    return assets


def build_policy_portfolios(
    dates: list[pd.Timestamp], assets: dict[str, pd.Series], kr0: float
) -> dict[str, pd.Series]:
    """KR/US/Gold 정책 포트폴리오. 가능한 구간에서만 평가한다."""
    portfolios: dict[str, pd.Series] = {}
    base = pd.DataFrame({name: assets[name] for name in assets if name != "cash"})

    weights = {
        "policy_33_33_33": {"kr_kodex200": 1 / 3, "us_sp500_proxy": 1 / 3, "gold": 1 / 3},
        "policy_50_30_20": {"kr_kodex200": 0.50, "us_sp500_proxy": 0.30, "gold": 0.20},
        "policy_60_40": {"kr_kodex200": 0.60, "us_sp500_proxy": 0.40},
        "policy_100_us": {"us_sp500_proxy": 1.0},
        "policy_100_gold": {"gold": 1.0},
    }
    for name, w in weights.items():
        cols = [c for c in w if c in base.columns]
        if not cols or any(base[c].isna().all() for c in cols):
            continue
        valid = base[cols].dropna()
        if valid.empty:
            continue
        # 각 자산을 해당 구간 시작으로 정규화 후 가중 합 → 에쿼티 재구성
        norm = valid.copy()
        for c in cols:
            norm[c] = valid[c] / valid[c].iloc[0]
        port = sum(w[c] * norm[c] for c in cols)
        portfolios[name] = port * kr0
    return portfolios


def calc_stats(series: pd.Series) -> dict:
    if len(series) < 2:
        return {}
    eq = series.dropna()
    rets = eq.pct_change().dropna()
    initial = float(eq.iloc[0])
    final = float(eq.iloc[-1])
    days = (eq.index[-1] - eq.index[0]).days
    years = max(days / 365.25, 1 / 365.25)
    vol = float(rets.std(ddof=0) * (252 ** 0.5)) if not rets.empty else 0.0
    mdd = float((eq / eq.cummax() - 1).min())
    sharpe = float(rets.mean() * 252 / vol) if vol else None
    downside = rets[rets < 0]
    dvol = float(downside.std(ddof=0) * (252 ** 0.5)) if not downside.empty else 0.0
    sortino = float(rets.mean() * 252 / dvol) if dvol else None
    return {
        "initial": initial,
        "final": final,
        "total_return": final / initial - 1,
        "cagr": (final / initial) ** (1 / years) - 1,
        "mdd": mdd,
        "volatility": vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": ((final / initial) ** (1 / years) - 1) / abs(mdd) if mdd else None,
    }


def analyze_market_impact(trades: pd.DataFrame) -> dict:
    """주문 크기 대비 체결 슬리피지 추정.

    전략의 buy/sell에서 `price`(참조가)와 실제 체결가(없으면 참조가)를 비교해
    비용을 추정한다. 주문 크기가 클수록 시장 충격이 커진다는 가정 아래
    크기 구간별 평균 슬리피지(참조가 대비 %)를 보고한다.
    """
    if trades.empty:
        return {}
    df = trades.copy()
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0)
    df["price"] = pd.to_numeric(df["price"], errors="coerce").fillna(0)
    df["net_value"] = pd.to_numeric(df["net_value"], errors="coerce").fillna(0)
    df = df[(df["qty"] > 0) & (df["price"] > 0)].copy()
    if df.empty:
        return {}

    # 주문 금액 (KRW)
    df["order_value"] = df["qty"] * df["price"]
    # 실현 슬리피지 근사: 매도 시 참조가(open*1.005) 대비 net_value 반영분
    # 실제 체결가가 없으므로, 전략의 슬리피지/스프레드 설정이 반영된 net_value를 사용
    slippage_bps = 0.0  # trades에는 슬리피지 컬럼이 없어 별도 추정 불가

    size_bins = [0, 500_000, 1_000_000, 2_000_000, 5_000_000, float("inf")]
    labels = ["<50만", "50만~100만", "100만~200만", "200만~500만", ">500만"]
    df["size_bin"] = pd.cut(
        df["order_value"], bins=size_bins, labels=labels, right=False
    )
    impact = (
        df.groupby("size_bin", observed=True)
        .agg(
            order_count=("order_value", "size"),
            total_value=("order_value", "sum"),
            avg_order_value=("order_value", "mean"),
            avg_qty=("qty", "mean"),
        )
        .reset_index()
    )
    impact["avg_order_value"] = impact["avg_order_value"].round(0)
    impact["avg_qty"] = impact["avg_qty"].round(0)
    return {
        "size_breakdown": impact.to_dict(orient="records"),
        "note": "시장 충격 정량 추정은 체결가/호가 데이터가 필요합니다. 현재는 주문 크기 분포만 제공하며, LIVE_SPREAD_PCT·슬리피지 가정은 백테스트에 이미 반영됨.",
        "slippage_bps": slippage_bps,
    }


def main() -> int:
    if not CURVE_PATH.exists():
        raise FileNotFoundError(f"전략 커브가 없습니다: {CURVE_PATH}")
    curve = pd.read_csv(CURVE_PATH)
    curve["date"] = pd.to_datetime(curve["date"])
    curve = curve.sort_values("date").reset_index(drop=True)
    dates = list(curve["date"])
    equity_strategy = curve.set_index("date")["equity_strategy"]

    kr0 = float(curve["equity_benchmark"].iloc[0])
    assets = build_asset_curves(dates)
    portfolios = build_policy_portfolios(dates, assets, kr0)

    series = {"strategy": equity_strategy}
    series.update(assets)
    series.update(portfolios)

    rows = []
    for name, s in series.items():
        stats = calc_stats(s)
        if not stats:
            continue
        rows.append({"benchmark": name, **stats})
    bench_df = pd.DataFrame(rows)
    bench_df = bench_df[
        ["benchmark", "total_return", "cagr", "mdd", "volatility", "sharpe", "sortino", "calmar"]
    ].round(4)

    # 전략 대비 초과 성과 (CAGR/MDD 차이)
    strat_cagr = float(bench_df.loc[bench_df["benchmark"] == "strategy", "cagr"].iloc[0])
    strat_mdd = float(bench_df.loc[bench_df["benchmark"] == "strategy", "mdd"].iloc[0])
    bench_df["cagr_alpha"] = bench_df["cagr"] - strat_cagr
    bench_df["mdd_delta"] = bench_df["mdd"] - strat_mdd

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    bench_df.to_csv(OUTPUT_DIR / "benchmark_comparison.csv", index=False, encoding="utf-8-sig")

    impact = {}
    if TRADES_PATH.exists():
        trades = pd.read_csv(TRADES_PATH)
        impact = analyze_market_impact(trades)
        pd.DataFrame(impact.get("size_breakdown", [])).to_csv(
            OUTPUT_DIR / "market_impact.csv", index=False, encoding="utf-8-sig"
        )

    report = {
        "generated_at": pd.Timestamp.now().isoformat(),
        "strategy_stats": calc_stats(equity_strategy),
        "benchmarks": bench_df.to_dict(orient="records"),
        "market_impact": impact,
        "note": "KR=KODEX200, US=TIGER 미국S&P500선물(H), Gold=ACE KRX금현물(2021-12~), 정책 포트폴리오는 가능한 공통 구간에서 평가.",
    }
    (OUTPUT_DIR / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(bench_df.to_string(index=False))
    print()
    print(json.dumps(impact, ensure_ascii=False, indent=2))
    print(f"\n[benchmark] 저장 완료: {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
