#!/usr/bin/env python3
from __future__ import annotations

import argparse
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Phase 1 A/B 백테스트 실행 후 비교 리포트를 출력합니다."
    )
    parser.add_argument("--start", default="20160105", help="백테스트 시작일 (YYYYMMDD)")
    parser.add_argument("--end", default="20260630", help="백테스트 종료일 (YYYYMMDD)")
    parser.add_argument("--us-risk-proxy", default="SPY", help="미국 리스크 프록시 (예: SPY, QQQ)")
    parser.add_argument(
        "--gating-mode",
        choices=["hybrid", "split"],
        default="split",
        help="멀티 인덱스 게이팅 모드 (default: split)",
    )
    parser.add_argument("--us-ma-days", type=int, default=120, help="미국 MA 기간")
    parser.add_argument("--us-slope-days", type=int, default=20, help="미국 MA 기울기 기간")
    parser.add_argument(
        "--skip-sync",
        action="store_true",
        help="uv sync를 건너뜁니다 (의존성이 이미 설치된 경우)",
    )
    parser.add_argument(
        "--skip-backtest",
        action="store_true",
        help="이미 생성된 outputs_compare 결과를 사용해 비교만 수행합니다.",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    outputs_dir = project_root / "outputs_etf_only"
    compare_root = project_root / "outputs_compare"
    baseline_dir = compare_root / "phase1_baseline"
    multi_dir = compare_root / "phase1_multi"

    if not args.skip_backtest:
        if not args.skip_sync:
            _run(["uv", "sync"], cwd=project_root)

        baseline_env = os.environ.copy()
        baseline_env.update(
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
            env=baseline_env,
        )
        _copy_result_files(baseline_dir, outputs_dir)

        multi_env = os.environ.copy()
        multi_env.update(
            {
                "ETF_BACKTEST_MODE": "single",
                "ENABLE_MULTI_INDEX_RISK": "1",
                "MULTI_INDEX_GATING_MODE": args.gating_mode,
                "US_RISK_PROXY": args.us_risk_proxy,
                "US_MARKET_MA_DAYS": str(args.us_ma_days),
                "US_MARKET_SLOPE_DAYS": str(args.us_slope_days),
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
            env=multi_env,
        )
        _copy_result_files(multi_dir, outputs_dir)

    compare_script = project_root / "scripts" / "compare_phase1_results.py"
    compare_cmd = [
        sys.executable,
        str(compare_script),
        "--baseline",
        str(baseline_dir / "performance.json"),
        "--multi",
        str(multi_dir / "performance.json"),
    ]
    _run(compare_cmd, cwd=project_root)

    print("\n완료: Phase 1 A/B 실행 및 비교를 마쳤습니다.")
    print(f"- baseline: {baseline_dir}")
    print(f"- multi:    {multi_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
