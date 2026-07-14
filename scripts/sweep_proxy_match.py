#!/usr/bin/env python3
"""프록시-지수 매칭 실험: SPY→S&P, QQQ→Nasdaq 매칭이 성과를 개선하는지 검증."""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
from pathlib import Path


DOMESTIC = [
    "069500",
    "091160",
    "102110",
    "0101N0",
    "463250",
    "161510",
    "091170",
    "367760",
]
COMMODITY = ["411060"]

# Pure index trackers only (no covered calls)
SP500_FOREIGN = ["143850", "360200", "360750"]  # S&P500 trackers
NASDAQ_FOREIGN = ["133690"]  # Nasdaq100 tracker
COVERED_CALL = ["472150", "486290", "498400"]  # Covered calls (always included)

# Base: domestic + covered calls + commodity (constant across all scenarios)
BASE_UNIVERSE = DOMESTIC + COVERED_CALL + COMMODITY

# S&P500 universe: base + S&P500 foreign
SP_UNIVERSE = BASE_UNIVERSE + SP500_FOREIGN

# Nasdaq universe: base + Nasdaq foreign
NASDAQ_UNIVERSE = BASE_UNIVERSE + NASDAQ_FOREIGN

# Both: base + both foreign groups
BOTH_UNIVERSE = BASE_UNIVERSE + SP500_FOREIGN + NASDAQ_FOREIGN

SP_UNIVERSE_STR = ",".join(SP_UNIVERSE)
NASDAQ_UNIVERSE_STR = ",".join(NASDAQ_UNIVERSE)
BOTH_UNIVERSE_STR = ",".join(BOTH_UNIVERSE)


def _run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    print(f"[RUN] {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(cwd), env=env, check=True)


def _copy_result_files(dst_dir: Path, src_dir: Path) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)
    files = ["performance.json", "etf_equity_curve.csv", "etf_trades.csv"]
    for name in files:
        src = src_dir / name
        if not src.exists():
            raise FileNotFoundError(f"결과 파일이 없습니다: {src}")
        shutil.copy2(src, dst_dir / name)


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _get_metric(payload: dict, key: str) -> float | None:
    strategy = payload.get("strategy", {})
    value = strategy.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v:.2%}"


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except Exception:
        return None


def _build_scenarios() -> list[dict]:
    return [
        {
            "scenario": "baseline_spy",
            "proxy": "SPY",
            "etf_list": BOTH_UNIVERSE_STR,
            "desc": "Current approach with SPY (full universe)",
        },
        {
            "scenario": "baseline_qqq",
            "proxy": "QQQ",
            "etf_list": BOTH_UNIVERSE_STR,
            "desc": "Current approach with QQQ (full universe)",
        },
        {
            "scenario": "spy_match_sp500",
            "proxy": "SPY",
            "etf_list": SP_UNIVERSE_STR,
            "desc": "SPY signal -> S&P500 trackers available",
        },
        {
            "scenario": "qqq_match_nasdaq",
            "proxy": "QQQ",
            "etf_list": NASDAQ_UNIVERSE_STR,
            "desc": "QQQ signal -> Nasdaq tracker available",
        },
        {
            "scenario": "spy_cross_nasdaq",
            "proxy": "SPY",
            "etf_list": NASDAQ_UNIVERSE_STR,
            "desc": "SPY signal -> Nasdaq tracker (mismatch)",
        },
        {
            "scenario": "qqq_cross_sp500",
            "proxy": "QQQ",
            "etf_list": SP_UNIVERSE_STR,
            "desc": "QQQ signal -> S&P500 trackers (mismatch)",
        },
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="프록시-지수 매칭 실험: SPY→S&P, QQQ→Nasdaq 매칭 성과 검증"
    )
    parser.add_argument("--start", default="20160105", help="백테스트 시작일 (YYYYMMDD)")
    parser.add_argument("--end", default="20260630", help="백테스트 종료일 (YYYYMMDD)")
    parser.add_argument(
        "--skip-sync",
        action="store_true",
        help="uv sync를 생략합니다.",
    )
    parser.add_argument(
        "--reuse-baseline",
        action="store_true",
        help="기존 baseline 파일이 있으면 baseline 재생성을 생략합니다.",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    outputs_dir = project_root / "outputs_etf_only"
    compare_root = project_root / "outputs_compare"
    sweep_root = compare_root / "proxy_match"
    sweep_root.mkdir(parents=True, exist_ok=True)

    if not args.skip_sync:
        _run(["uv", "sync"], cwd=project_root)

    scenarios = _build_scenarios()
    summary_rows: list[dict[str, object]] = []

    for idx, sc in enumerate(scenarios):
        tag = sc["scenario"]
        scenario_dir = sweep_root / tag

        is_baseline = idx == 0
        baseline_perf = sweep_root / "baseline_spy" / "performance.json"
        if is_baseline and args.reuse_baseline and baseline_perf.exists():
            print(f"\n[REUSE] {tag}: 기존 baseline 사용")
        else:
            env = os.environ.copy()
            env.update(
                {
                    "ETF_BACKTEST_MODE": "single",
                    "ENABLE_MULTI_INDEX_RISK": "1",
                    "MULTI_INDEX_GATING_MODE": "hybrid",
                    "US_RISK_PROXY": sc["proxy"],
                }
            )
            if sc["etf_list"] is not None:
                env["ETF_LIST"] = sc["etf_list"]

            _run(
                [
                    "uv",
                    "run",
                    "python",
                    "run_etf_backtest.py",
                    "--start",
                    args.start,
                    "--end",
                    args.end,
                ],
                cwd=project_root,
                env=env,
            )
            _copy_result_files(scenario_dir, outputs_dir)

        payload = _load_json(scenario_dir / "performance.json")
        sharpe = _get_metric(payload, "sharpe")
        sortino = _get_metric(payload, "sortino")
        cagr = _get_metric(payload, "cagr")
        mdd = _get_metric(payload, "mdd")

        summary_rows.append(
            {
                "scenario": tag,
                "proxy": sc["proxy"],
                "etf_list": sc["etf_list"] or "(default)",
                "cagr": cagr,
                "mdd": mdd,
                "sharpe": sharpe,
                "sortino": sortino,
                "delta_cagr": None,
                "delta_mdd": None,
                "delta_sharpe": None,
                "delta_sortino": None,
                "note": sc["desc"],
            }
        )

    base = summary_rows[0]
    base_sharpe = _as_float(base.get("sharpe"))
    base_sortino = _as_float(base.get("sortino"))
    base_cagr = _as_float(base.get("cagr"))
    base_mdd = _as_float(base.get("mdd"))

    for row in summary_rows:
        sharpe = _as_float(row.get("sharpe"))
        sortino = _as_float(row.get("sortino"))
        cagr = _as_float(row.get("cagr"))
        mdd = _as_float(row.get("mdd"))
        row["delta_sharpe"] = (
            None if (base_sharpe is None or sharpe is None) else (sharpe - base_sharpe)
        )
        row["delta_sortino"] = (
            None if (base_sortino is None or sortino is None) else (sortino - base_sortino)
        )
        row["delta_cagr"] = (
            None if (base_cagr is None or cagr is None) else (cagr - base_cagr)
        )
        row["delta_mdd"] = (
            None if (base_mdd is None or mdd is None) else (mdd - base_mdd)
        )

    def _sort_key(r: dict[str, object]) -> tuple[int, float]:
        v = _as_float(r.get("delta_sharpe"))
        if v is None:
            return (1, 0.0)
        return (0, -v)

    summary_rows.sort(key=_sort_key)

    summary_csv = sweep_root / "match_comparison.csv"
    with summary_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "scenario",
                "proxy",
                "etf_list",
                "cagr",
                "mdd",
                "sharpe",
                "sortino",
                "delta_cagr",
                "delta_mdd",
                "delta_sharpe",
                "delta_sortino",
                "note",
            ],
        )
        writer.writeheader()
        writer.writerows(summary_rows)

    print("\n=== Proxy Match Results (by delta_sharpe vs baseline_spy) ===")
    for row in summary_rows:
        ds = _as_float(row.get("delta_sharpe"))
        dso = _as_float(row.get("delta_sortino"))
        dc = _as_float(row.get("delta_cagr"))
        dm = _as_float(row.get("delta_mdd"))
        ds_s = "N/A" if ds is None else f"{ds:.4f}"
        dso_s = "N/A" if dso is None else f"{dso:.4f}"
        print(
            f"- {row['scenario']} [{row['proxy']}]: "
            f"delta_sharpe={ds_s}, "
            f"delta_sortino={dso_s}, "
            f"delta_cagr={_fmt_pct(dc)}, "
            f"delta_mdd={_fmt_pct(dm)}"
        )

    print(f"\n요약 저장: {summary_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
