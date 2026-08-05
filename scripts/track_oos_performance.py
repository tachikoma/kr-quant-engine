#!/usr/bin/env python3
"""v2 표본외(OOS) 실전 성과 추적 리포트를 출력한다.

`runtime_state/oos_equity_history.json`에 데일리 러너가 기록한 일별 평가액 이력을
읽어 `strategy_freeze.json`의 v2 OOS 시작일(2026-07-22) 이후 성과를 계산한다.
v1(2026-07-13 동결) 트랙과는 섞지 않는다.
"""

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

from strategy_freeze import load_frozen_strategy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--include-mock",
        action="store_true",
        help="broker 레코드뿐 아니라 mock(드라이런) 평가액도 포함합니다. 기본은 broker만.",
    )
    parser.add_argument(
        "--history-path",
        type=Path,
        default=ROOT / "runtime_state" / "oos_equity_history.json",
    )
    return parser.parse_args()


def calc_stats(equities: list[float], dates: list[str]) -> dict:
    """일별 평가액 시계열로 OOS 성과 지표를 계산한다."""
    if len(equities) < 2:
        return {"trading_days": len(equities)}
    series = pd.Series(equities, dtype=float)
    returns = series.pct_change().dropna()
    initial = float(series.iloc[0])
    final = float(series.iloc[-1])
    start_dt = pd.Timestamp(dates[0])
    end_dt = pd.Timestamp(dates[-1])
    years = max((end_dt - start_dt).days / 365.25, 1 / 365.25)
    volatility = float(returns.std(ddof=0) * np.sqrt(252)) if not returns.empty else 0.0
    return {
        "trading_days": len(equities),
        "initial_equity": initial,
        "final_equity": final,
        "total_return": final / initial - 1,
        "cagr": (final / initial) ** (1 / years) - 1,
        "mdd": float((series / series.cummax() - 1).min()),
        "volatility": volatility,
        "sharpe": float(returns.mean() * 252 / volatility) if volatility else None,
    }


def main() -> int:
    args = parse_args()
    if not args.history_path.exists():
        print(
            json.dumps(
                {"error": f"평가 이력이 없습니다: {args.history_path}"},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    history = json.loads(args.history_path.read_text(encoding="utf-8"))
    if not isinstance(history, dict) or not history:
        print(json.dumps({"error": "평가 이력이 비어 있습니다."}, ensure_ascii=False, indent=2))
        return 1

    frozen = load_frozen_strategy()
    oos_start = pd.Timestamp(frozen["oos_start_date"])
    freeze_date = frozen["freeze_date"]

    records = []
    for date_str, rec in sorted(history.items()):
        ts = pd.Timestamp(date_str)
        if ts < oos_start:
            continue
        if rec.get("current_equity") is None:
            continue
        if rec.get("source") != "broker" and not args.include_mock:
            continue
        records.append(rec)

    if not records:
        print(
            json.dumps(
                {
                    "freeze_date": freeze_date,
                    "oos_start_date": str(oos_start.date()),
                    "error": "OOS 시작일 이후 broker 평가액 레코드가 없습니다.",
                    "hint": "드라이런/모의계좌 평가액을 보려면 --include-mock 를 사용하세요.",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    dates = [rec["date"] for rec in records]
    equities = [float(rec["current_equity"]) for rec in records]
    stats = calc_stats(equities, dates)

    report = {
        "track": "v2",
        "freeze_date": freeze_date,
        "oos_start_date": str(oos_start.date()),
        "source_filter": "broker" if not args.include_mock else "broker+mock",
        "start": dates[0],
        "end": dates[-1],
        **{key: (None if value is None else float(value)) for key, value in stats.items()},
        "daily_records": [
            {
                "date": rec["date"],
                "status": rec["status"],
                "source": rec["source"],
                "current_equity": rec["current_equity"],
                "peak_equity": rec["peak_equity"],
                "current_drawdown": rec["current_drawdown"],
                "risk_on": rec.get("risk_on"),
                "positions": [
                    {"ticker": p["ticker"], "weight": p["weight"]}
                    for p in rec.get("positions") or []
                ],
            }
            for rec in records
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
