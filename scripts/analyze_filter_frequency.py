#!/usr/bin/env python3
"""필터 빈도 분석 스크립트

리밸런싱 시점마다 risk_on 상태와 필터 통과 종목 수를 기록하여,
risk_on=True + 후보 0개 빈도를 분석한다.

출력:
  - 콘솔 요약 통계
  - outputs_etf_only/filter_frequency.csv (상세 내역)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_dotenv(dotenv_path: Path | str | None = None) -> None:
    if dotenv_path is None:
        dotenv_path = ROOT / ".env"
    p = Path(dotenv_path)
    if not p.exists():
        return
    try:
        with p.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.lower().startswith("export "):
                    line = line[7:].lstrip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


_load_dotenv()

import pandas as pd

import run_etf_backtest as rtb
from etf_shared import (
    MARKET_MA_DAYS,
    MARKET_SLOPE_DAYS,
)


OUTPUT_DIR = Path("outputs_etf_only")


def _count_filters(snapshot: pd.DataFrame) -> dict[str, int]:
    """스냅샷에 필터 단계별 통과 종목 수를 반환한다."""
    df = snapshot.copy()
    counts: dict[str, int] = {}
    counts["total"] = len(df)

    if "liquidity_ok" in df.columns:
        df = df[df["liquidity_ok"]].copy()
    counts["after_liquidity"] = len(df)

    if "listing_ok" in df.columns:
        df = df[df["listing_ok"]].copy()
    counts["after_listing"] = len(df)

    if "deviation_ok" in df.columns:
        df = df[df["deviation_ok"]].copy()
    counts["after_deviation"] = len(df)

    if "ret_60" in df.columns and "ret_120" in df.columns and "trend_ok" in df.columns:
        mask = df["ret_60"].notna() & df["ret_120"].notna() & df["trend_ok"]
        df = df[mask].copy()
    counts["after_trend_return"] = len(df)

    return counts


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="필터 빈도 분석")
    parser.add_argument("--start", default=None, help="시작일 (YYYYMMDD)")
    parser.add_argument("--end", default=None, help="종료일 (YYYYMMDD)")
    parser.add_argument("--step", type=int, default=None, help="리밸런싱 간격 (거래일)")
    args = parser.parse_args()

    if args.start:
        rtb.START = args.start
    if args.end:
        rtb.END = args.end

    # 리밸런싱 간격 결정
    rebalance_step = args.step if args.step is not None else int(
        os.environ.get("REBALANCE_STEP_DAYS", str(rtb.REBALANCE_STEP_DAYS))
    )

    print(f"백테스트 기간: {rtb.START} ~ {rtb.END}")
    print(f"리밸런싱 간격: {rebalance_step}거래일")

    # 데이터 로드
    print("\n[1/3] ETF 가격 데이터 로딩...")
    price = rtb.load_etf_price()
    price_by_date = {dt: day.set_index("ticker") for dt, day in price.groupby("date")}

    print("\n[2/3] KOSPI 지수 데이터 로딩...")
    index_df = rtb.get_index_data()
    common_dates = list(index_df["date"])

    # 리밸런싱 시점만 추출
    warmup_days = max(120, MARKET_MA_DAYS + MARKET_SLOPE_DAYS)

    rebalance_dates = [
        dt
        for i, dt in enumerate(common_dates[:-1])
        if i >= warmup_days and (i - warmup_days) % rebalance_step == 0
    ]

    print(f"\n[3/3] {len(rebalance_dates)}개 리밸런싱 시점 분석...")

    # 분석 결과 수집
    rows: list[dict] = []
    for dt in rebalance_dates:
        today = price_by_date.get(dt, pd.DataFrame())
        if today.empty:
            continue

        snapshot = today.reset_index()
        kospi_risk_on = rtb.is_risk_on(index_df, dt)
        counts = _count_filters(snapshot)
        n_candidates = counts["after_trend_return"]

        # 필터 단계별 탈락 수 계산
        n_after_liquidity = counts["after_liquidity"]
        n_after_listing = counts["after_listing"]
        n_after_deviation = counts["after_deviation"]
        n_after_trend = counts["after_trend_return"]

        dropped_liquidity = counts["total"] - n_after_liquidity
        dropped_listing = n_after_liquidity - n_after_listing
        dropped_deviation = n_after_listing - n_after_deviation
        dropped_trend = n_after_deviation - n_after_trend

        rows.append(
            {
                "date": dt,
                "risk_on": kospi_risk_on,
                "total": counts["total"],
                "after_liquidity": n_after_liquidity,
                "after_listing": n_after_listing,
                "after_deviation": n_after_deviation,
                "after_trend_return": n_after_trend,
                "n_candidates": n_candidates,
                "zero_candidates": n_candidates == 0,
                "dropped_liquidity": dropped_liquidity,
                "dropped_listing": dropped_listing,
                "dropped_deviation": dropped_deviation,
                "dropped_trend": dropped_trend,
            }
        )

    if not rows:
        print("분석 대상 리밸런싱 시점이 없습니다.")
        return

    df = pd.DataFrame(rows)
    total = len(df)

    # ── 요약 통계 ──
    n_risk_on = int(df["risk_on"].sum())
    n_risk_off = total - n_risk_on

    risk_on_df = df[df["risk_on"]]
    risk_off_df = df[~df["risk_on"]]

    n_on_zero = int(risk_on_df["zero_candidates"].sum())
    n_off_zero = int(risk_off_df["zero_candidates"].sum())

    print("\n" + "=" * 60)
    print("=== 필터 빈도 분석 ===")
    print(f"백테스트 기간: {df['date'].min().date()} ~ {df['date'].max().date()}")
    print(f"총 리밸런싱: {total}회")
    print()

    print(f"risk_on=True:  {n_risk_on}회 ({n_risk_on / total:.1%})")
    print(f"  후보 0개:    {n_on_zero}회 ({n_on_zero / n_risk_on:.1%})" if n_risk_on else "  후보 0개: N/A")
    print(f"  후보 1개↑:   {n_risk_on - n_on_zero}회 ({(n_risk_on - n_on_zero) / n_risk_on:.1%})" if n_risk_on else "")
    print()

    print(f"risk_off=True: {n_risk_off}회 ({n_risk_off / total:.1%})")
    print(f"  후보 0개:    {n_off_zero}회 ({n_off_zero / n_risk_off:.1%})" if n_risk_off else "  후보 0개: N/A")
    print(f"  후보 1개↑:   {n_risk_off - n_off_zero}회 ({(n_risk_off - n_off_zero) / n_risk_off:.1%})" if n_risk_off else "")

    # ── risk_on + 0개 시점 필터 단계별 기여도 ──
    zero_when_on = risk_on_df[risk_on_df["zero_candidates"]]
    if not zero_when_on.empty:
        print()
        print("─" * 60)
        print("[risk_on + 후보 0개] 시점 필터 단계별 탈락 기여:")
        total_dropped_liq = int(zero_when_on["dropped_liquidity"].sum())
        total_dropped_lit = int(zero_when_on["dropped_listing"].sum())
        total_dropped_dev = int(zero_when_on["dropped_deviation"].sum())
        total_dropped_trd = int(zero_when_on["dropped_trend"].sum())
        total_all = total_dropped_liq + total_dropped_lit + total_dropped_dev + total_dropped_trd
        if total_all > 0:
            print(f"  liquidity:    {total_dropped_liq:>4d} ({total_dropped_liq / total_all:.1%})")
            print(f"  listing:      {total_dropped_lit:>4d} ({total_dropped_lit / total_all:.1%})")
            print(f"  deviation:    {total_dropped_dev:>4d} ({total_dropped_dev / total_all:.1%})")
            print(f"  trend/return: {total_dropped_trd:>4d} ({total_dropped_trd / total_all:.1%})")

        print()
        print(f"상세 일시 ({len(zero_when_on)}개):")
        for _, row in zero_when_on.iterrows():
            print(
                f"  {row['date'].date()}  "
                f"risk_on={row['risk_on']}  "
                f"candidate=0  "
                f"(liq={row['dropped_liquidity']}, "
                f"lit={row['dropped_listing']}, "
                f"dev={row['dropped_deviation']}, "
                f"trend={row['dropped_trend']})"
            )

    # ── CSV 저장 ──
    OUTPUT_DIR.mkdir(exist_ok=True)
    csv_path = OUTPUT_DIR / "filter_frequency.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\n상세 결과 저장: {csv_path}")


if __name__ == "__main__":
    main()
