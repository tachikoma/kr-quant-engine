#!/usr/bin/env python3
"""US_RISK_PROXY SPY vs QQQ 신호/포트폴리오/레짐 비교 분석

WHY QQQ가 SPY보다 좋은 결과를 내는지 세 가지 관점에서 분석한다.

  Part A: 신호 패턴 비교 (KOSPI/SPY/QQQ risk_on 시그널, 독립 다운로드)
  Part B: 포트폴리오 구성 비교 (SPY/QQQ 게이팅 백테스트 후 거래 빈도)
  Part C: 레짐 조건별 수익률 (KOSPI/US 레짐별 전략 수익률)

출력: outputs_compare/proxy_analysis/{signal_comparison,portfolio_comparison,regime_returns}.csv
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from etf_shared import ETF_TICKER_GROUPS
from _proxy_utils import (
    KOSPI_YF_TICKER,
    align_signal_to_dates,
    download_index,
    load_dotenv,
    pick_equity_column,
)

OUT_DIR_DEFAULT = ROOT / "outputs_compare" / "proxy_analysis"


def _regime_stats(risk_on: pd.Series) -> dict[str, float]:
    total = len(risk_on)
    on = int(risk_on.sum())
    off = total - on
    states = risk_on.astype(int).to_numpy()
    durations: list[tuple[int, int]] = []
    if total:
        prev = int(states[0])
        cur = 1
        for v in states[1:]:
            v = int(v)
            if v == prev:
                cur += 1
            else:
                durations.append((prev, cur))
                prev = v
                cur = 1
        durations.append((prev, cur))
    on_durs = [d for s, d in durations if s == 1]
    off_durs = [d for s, d in durations if s == 0]

    def avg(xs: list[int]) -> float:
        return (sum(xs) / len(xs)) if xs else 0.0

    return {
        "total_days": total,
        "risk_on_days": on,
        "risk_off_days": off,
        "risk_on_pct": (on / total) if total else 0.0,
        "avg_risk_on_duration": avg(on_durs),
        "avg_risk_off_duration": avg(off_durs),
        "max_risk_on_duration": max(on_durs) if on_durs else 0,
        "max_risk_off_duration": max(off_durs) if off_durs else 0,
        "num_off_to_on_transitions": len(off_durs),
        "avg_recovery_days": avg(off_durs),
    }


def _fmt_pct(v: float) -> str:
    return f"{v:.2%}"


def _part_a(start: str, end: str, out_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    print("\n=== Part A: 신호 패턴 비교 (KOSPI / SPY / QQQ) ===")
    kospi = download_index(KOSPI_YF_TICKER, start, end)
    spy = download_index("SPY", start, end)
    qqq = download_index("QQQ", start, end)

    # Merge on common dates (inner join)
    combined = kospi[["date", "risk_on"]].rename(columns={"risk_on": "kospi_on"})
    combined = combined.merge(
        spy[["date", "risk_on"]].rename(columns={"risk_on": "spy_on"}), on="date", how="inner"
    )
    combined = combined.merge(
        qqq[["date", "risk_on"]].rename(columns={"risk_on": "qqq_on"}), on="date", how="inner"
    )

    kospi_off = ~combined["kospi_on"]
    spy_pct_kospi_off = float(combined.loc[kospi_off, "spy_on"].mean()) if kospi_off.any() else 0.0
    qqq_pct_kospi_off = float(combined.loc[kospi_off, "qqq_on"].mean()) if kospi_off.any() else 0.0

    rows: list[dict] = []
    for label, df, cond_pct in [
        ("KOSPI", kospi, None),
        ("SPY", spy, spy_pct_kospi_off),
        ("QQQ", qqq, qqq_pct_kospi_off),
    ]:
        st = _regime_stats(df["risk_on"])
        rows.append(
            {
                "proxy": label,
                "total_days": st["total_days"],
                "risk_on_days": st["risk_on_days"],
                "risk_off_days": st["risk_off_days"],
                "risk_on_pct": round(st["risk_on_pct"], 6),
                "avg_risk_on_duration": round(st["avg_risk_on_duration"], 3),
                "avg_risk_off_duration": round(st["avg_risk_off_duration"], 3),
                "max_risk_on_duration": st["max_risk_on_duration"],
                "max_risk_off_duration": st["max_risk_off_duration"],
                "num_off_to_on_transitions": st["num_off_to_on_transitions"],
                "avg_recovery_days": round(st["avg_recovery_days"], 3),
                "risk_on_pct_during_kospi_off": ("" if cond_pct is None else round(cond_pct, 6)),
            }
        )

    csv_path = out_dir / "signal_comparison.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = pd.DataFrame(rows)
        writer.to_csv(f, index=False)

    print(f"\n기간: {start} ~ {end}")
    print(f"{'proxy':<7}{'risk_on%':>10}{'avg_on':>9}{'avg_off':>9}{'max_off':>9}{'recover':>9}{'on%@KOSPIoff':>16}")
    for r in rows:
        cond = r["risk_on_pct_during_kospi_off"]
        cond_s = "N/A" if cond == "" else _fmt_pct(cond)
        print(
            f"{r['proxy']:<7}{_fmt_pct(r['risk_on_pct']):>10}"
            f"{r['avg_risk_on_duration']:>9.1f}{r['avg_risk_off_duration']:>9.1f}"
            f"{r['max_risk_off_duration']:>9}{r['avg_recovery_days']:>9.1f}{cond_s:>16}"
        )
    print(f"\n저장: {csv_path}")

    return kospi, spy, qqq


def _run_backtest(project_root: Path, env: dict[str, str], start: str, end: str) -> None:
    print(f"[RUN] uv run python run_etf_backtest.py --start {start} --end {end}")
    subprocess.run(
        ["uv", "run", "python", "run_etf_backtest.py", "--start", start, "--end", end],
        cwd=str(project_root),
        env=env,
        check=True,
    )


def _copy_results(src_dir: Path, dst_dir: Path, prefix: str) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)
    for name in ["etf_equity_curve.csv", "etf_trades.csv"]:
        src = src_dir / name
        if not src.exists():
            raise FileNotFoundError(f"결과 파일이 없습니다: {src}")
        shutil.copy2(src, dst_dir / f"{prefix}_{name}")


def _trade_counts(trades: pd.DataFrame) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for ticker, grp in trades.groupby("ticker"):
        name = grp["name"].iloc[0] if "name" in grp.columns and len(grp) else ""
        buy = int((grp["side"] == "BUY").sum())
        sell = int((grp["side"] == "SELL").sum())
        result[str(ticker)] = {"name": str(name), "buy": buy, "sell": sell, "total": buy + sell}
    return result


def _part_b(start: str, end: str, project_root: Path, out_dir: Path) -> None:
    print("\n=== Part B: 포트폴리오 구성 비교 (SPY vs QQQ 백테스트) ===")
    outputs_dir = project_root / "outputs_etf_only"
    for proxy in ["SPY", "QQQ"]:
        env = os.environ.copy()
        env.update(
            {
                "ETF_BACKTEST_MODE": "single",
                "ENABLE_MULTI_INDEX_RISK": "1",
                "MULTI_INDEX_GATING_MODE": "hybrid",
                "US_RISK_PROXY": proxy,
            }
        )
        _run_backtest(project_root, env, start, end)
        _copy_results(outputs_dir, out_dir, proxy.lower())

    spy_trades = pd.read_csv(out_dir / "spy_etf_trades.csv")
    qqq_trades = pd.read_csv(out_dir / "qqq_etf_trades.csv")
    spy_counts = _trade_counts(spy_trades)
    qqq_counts = _trade_counts(qqq_trades)

    all_tickers = sorted(set(spy_counts) | set(qqq_counts))
    rows: list[dict] = []
    for t in all_tickers:
        sc = spy_counts.get(t, {"name": "", "buy": 0, "sell": 0, "total": 0})
        qc = qqq_counts.get(t, {"name": "", "buy": 0, "sell": 0, "total": 0})
        rows.append(
            {
                "ticker": t,
                "name": sc["name"] or qc["name"],
                "group": ETF_TICKER_GROUPS.get(t, "domestic_equity"),
                "spy_buy": sc["buy"],
                "spy_sell": sc["sell"],
                "spy_total": sc["total"],
                "qqq_buy": qc["buy"],
                "qqq_sell": qc["sell"],
                "qqq_total": qc["total"],
                "delta_total": qc["total"] - sc["total"],
            }
        )

    csv_path = out_dir / "portfolio_comparison.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        pd.DataFrame(rows).to_csv(f, index=False)

    print(f"\n{'ticker':<8}{'group':<18}{'SPY':>6}{'QQQ':>6}{'Δ':>6}")
    for r in rows:
        print(f"{r['ticker']:<8}{r['group']:<18}{r['spy_total']:>6}{r['qqq_total']:>6}{r['delta_total']:>6}")
    print(f"\n저장: {csv_path}")


def _load_kospi_signal(start: str, end: str) -> pd.DataFrame:
    return download_index(KOSPI_YF_TICKER, start, end)


def _regime_returns(equity_df: pd.DataFrame, kospi_df: pd.DataFrame, us_df: pd.DataFrame, label: str) -> dict:
    eq = equity_df.copy()
    eq["date"] = pd.to_datetime(eq["date"])
    equity_col = pick_equity_column(eq)
    eq = eq[["date", equity_col]].rename(columns={equity_col: "equity"})

    # Align US signal to equity dates (forward-fill)
    us_aligned = align_signal_to_dates(us_df, eq["date"])
    kospi_aligned = align_signal_to_dates(kospi_df, eq["date"])

    eq["kospi_on"] = kospi_aligned.values
    eq["us_on"] = us_aligned.values
    eq["daily_ret"] = eq["equity"].pct_change().fillna(0.0)

    kospi_on = eq["kospi_on"]
    kospi_off = ~eq["kospi_on"]
    us_on = eq["us_on"]

    def comp(mask: pd.Series) -> float:
        r = eq.loc[mask, "daily_ret"]
        return float((1.0 + r).prod() - 1.0) if len(r) else 0.0

    return {
        "scenario": label,
        "kospi_on_return": round(comp(kospi_on), 6),
        "kospi_off_us_on_return": round(comp(kospi_off & us_on), 6),
        "kospi_off_us_off_return": round(comp(kospi_off & ~us_on), 6),
        "kospi_on_days": int(kospi_on.sum()),
        "kospi_off_us_on_days": int((kospi_off & us_on).sum()),
        "kospi_off_us_off_days": int((kospi_off & ~us_on).sum()),
    }


def _part_c(
    start: str,
    end: str,
    out_dir: Path,
    kospi: pd.DataFrame,
    spy: pd.DataFrame,
    qqq: pd.DataFrame,
) -> None:
    print("\n=== Part C: 레짐 조건별 수익률 ===")
    spy_eq = pd.read_csv(out_dir / "spy_etf_equity_curve.csv")
    qqq_eq = pd.read_csv(out_dir / "qqq_etf_equity_curve.csv")

    rows = [
        _regime_returns(spy_eq, kospi, spy, "SPY"),
        _regime_returns(qqq_eq, kospi, qqq, "QQQ"),
    ]

    csv_path = out_dir / "regime_returns.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        pd.DataFrame(rows).to_csv(f, index=False)

    print(
        f"\n{'scenario':<8}{'KOSPI_on':>12}{'KOSPIoff+USon':>16}{'KOSPIoff+USoff':>17}"
        f"{'on_d':>7}{'off_on_d':>9}{'off_off_d':>10}"
    )
    for r in rows:
        print(
            f"{r['scenario']:<8}{_fmt_pct(r['kospi_on_return']):>12}"
            f"{_fmt_pct(r['kospi_off_us_on_return']):>16}{_fmt_pct(r['kospi_off_us_off_return']):>17}"
            f"{r['kospi_on_days']:>7}{r['kospi_off_us_on_days']:>9}{r['kospi_off_us_off_days']:>10}"
        )
    print(f"\n저장: {csv_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="US_RISK_PROXY SPY vs QQQ 신호/포트폴리오/레짐 비교 분석"
    )
    parser.add_argument("--start", default="20160105", help="백테스트 시작일 (YYYYMMDD)")
    parser.add_argument("--end", default="20260630", help="백테스트 종료일 (YYYYMMDD)")
    parser.add_argument("--skip-sync", action="store_true", help="uv sync를 생략합니다.")
    parser.add_argument(
        "--skip-backtest",
        action="store_true",
        help="Part B/C 백테스트를 생략하고 기존 결과를 재사용합니다.",
    )
    args = parser.parse_args()

    load_dotenv()
    project_root = ROOT
    out_dir = project_root / "outputs_compare" / "proxy_analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_sync:
        subprocess.run(["uv", "sync"], cwd=str(project_root), check=True)

    kospi, spy, qqq = _part_a(args.start, args.end, out_dir)

    if not args.skip_backtest:
        _part_b(args.start, args.end, project_root, out_dir)
    else:
        for prefix in ["spy", "qqq"]:
            for name in ["etf_equity_curve.csv", "etf_trades.csv"]:
                path = out_dir / f"{prefix}_{name}"
                if not path.exists():
                    raise FileNotFoundError(f"기존 결과가 없습니다: {path}")

    _part_c(args.start, args.end, out_dir, kospi, spy, qqq)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
