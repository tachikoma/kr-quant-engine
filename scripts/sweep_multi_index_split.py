#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


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


def _parse_grid(grid: str) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for item in grid.split(","):
        text = item.strip()
        if not text:
            continue
        if ":" not in text:
            raise ValueError(f"잘못된 grid 항목: {text} (예: 120:20)")
        ma_s, slope_s = text.split(":", 1)
        result.append((int(ma_s), int(slope_s)))
    if not result:
        raise ValueError("grid가 비어 있습니다.")
    return result


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v:.2%}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="split 게이팅(국내=KOSPI, 미국=US) 파라미터 스윕 실행기"
    )
    parser.add_argument("--start", default="20160105", help="백테스트 시작일 (YYYYMMDD)")
    parser.add_argument("--end", default="20260630", help="백테스트 종료일 (YYYYMMDD)")
    parser.add_argument("--proxies", default="SPY,QQQ", help="미국 프록시 심볼 목록 (쉼표 구분)")
    parser.add_argument(
        "--grid",
        default="100:20,120:10,120:20,140:20",
        help="US MA/SLOPE 조합 목록 (쉼표 구분, 예: 100:20,120:10)",
    )
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
    baseline_dir = compare_root / "phase1_baseline"
    sweep_root = compare_root / "sweep_split"
    sweep_root.mkdir(parents=True, exist_ok=True)

    if not args.skip_sync:
        _run(["uv", "sync"], cwd=project_root)

    baseline_perf = baseline_dir / "performance.json"
    if not (args.reuse_baseline and baseline_perf.exists()):
        base_env = os.environ.copy()
        base_env.update(
            {
                "ETF_BACKTEST_MODE": "single",
                "ENABLE_MULTI_INDEX_RISK": "0",
            }
        )
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
            env=base_env,
        )
        _copy_result_files(baseline_dir, outputs_dir)

    baseline_payload = _load_json(baseline_perf)
    base_sharpe = _get_metric(baseline_payload, "sharpe")
    base_sortino = _get_metric(baseline_payload, "sortino")
    base_cagr = _get_metric(baseline_payload, "cagr")
    base_mdd = _get_metric(baseline_payload, "mdd")

    proxies = [p.strip().upper() for p in args.proxies.split(",") if p.strip()]
    grid = _parse_grid(args.grid)

    summary_rows: list[dict[str, object]] = []

    for proxy in proxies:
        for ma_days, slope_days in grid:
            tag = f"split_{proxy}_ma{ma_days}_s{slope_days}"
            scenario_dir = sweep_root / tag

            env = os.environ.copy()
            env.update(
                {
                    "ETF_BACKTEST_MODE": "single",
                    "ENABLE_MULTI_INDEX_RISK": "1",
                    "MULTI_INDEX_GATING_MODE": "split",
                    "US_RISK_PROXY": proxy,
                    "US_MARKET_MA_DAYS": str(ma_days),
                    "US_MARKET_SLOPE_DAYS": str(slope_days),
                }
            )

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

            delta_sharpe = None if (base_sharpe is None or sharpe is None) else (sharpe - base_sharpe)
            delta_sortino = None if (base_sortino is None or sortino is None) else (sortino - base_sortino)
            delta_cagr = None if (base_cagr is None or cagr is None) else (cagr - base_cagr)
            delta_mdd = None if (base_mdd is None or mdd is None) else (abs(base_mdd) - abs(mdd))

            summary_rows.append(
                {
                    "scenario": tag,
                    "proxy": proxy,
                    "us_ma_days": ma_days,
                    "us_slope_days": slope_days,
                    "cagr": cagr,
                    "mdd": mdd,
                    "sharpe": sharpe,
                    "sortino": sortino,
                    "delta_cagr": delta_cagr,
                    "delta_mdd": delta_mdd,
                    "delta_sharpe": delta_sharpe,
                    "delta_sortino": delta_sortino,
                }
            )

    summary_rows.sort(key=lambda r: (r.get("delta_sharpe") is None, -(r.get("delta_sharpe") or -999)))

    summary_csv = sweep_root / "split_sweep_summary.csv"
    with summary_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "scenario",
                "proxy",
                "us_ma_days",
                "us_slope_days",
                "cagr",
                "mdd",
                "sharpe",
                "sortino",
                "delta_cagr",
                "delta_mdd",
                "delta_sharpe",
                "delta_sortino",
            ],
        )
        writer.writeheader()
        writer.writerows(summary_rows)

    print("\n=== Split Sweep Top Results (by delta_sharpe) ===")
    for row in summary_rows[:5]:
        print(
            f"- {row['scenario']}: "
            f"delta_sharpe={row['delta_sharpe']:.4f}, "
            f"delta_sortino={row['delta_sortino']:.4f}, "
            f"delta_cagr={_fmt_pct(row['delta_cagr'])}, "
            f"delta_mdd={_fmt_pct(row['delta_mdd'])}"
        )

    print(f"\n요약 저장: {summary_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
