#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
필터된 백테스트 결과와 기존 고정 백테스트(동일 n/pos/reb 기준)의 비교 스크립트

사용법: uv run scripts/compare_filtered_vs_baseline.py

출력:
 - outputs_grid/comparison_filtered_vs_baseline.json
 - 콘솔에 비교 요약 출력
"""
from pathlib import Path
import re
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = ROOT / "outputs_grid"

def format_pct(x):
    try:
        return f"{x:.2%}"
    except Exception:
        return "N/A"

def main():
    import pandas as pd
    import run_etf_backtest as rtb

    filtered_files = sorted(OUT.glob("filtered_curve_*.csv"))
    if not filtered_files:
        print("필터된 curve 파일을 찾을 수 없습니다: outputs_grid/filtered_curve_*.csv")
        sys.exit(1)

    filtered_path = filtered_files[-1]
    m = re.match(r"filtered_curve_n(?P<n>\d+)_pos(?P<pos>\d+)_reb(?P<reb>\d+)\.csv", filtered_path.name)
    if not m:
        print(f"파일명 형식 인식 실패: {filtered_path.name}")
        sys.exit(1)

    n = m.group("n")
    pos = m.group("pos")
    reb = m.group("reb")

    baseline_name = f"curve_n{n}_reb{reb}_pos{pos}.csv"
    baseline_path = OUT / baseline_name
    if not baseline_path.exists():
        print(f"기준 curve 파일이 존재하지 않습니다: {baseline_name}")
        # 가능한 다른 후보(같은 n/pos 다른 파일)를 찾아 간단 알림
        alt = sorted(OUT.glob(f"curve_n{n}_reb{reb}_pos*.csv"))
        if alt:
            baseline_path = alt[-1]
            print(f"대체 기준 파일 사용: {baseline_path.name}")
        else:
            print("비교 불가 - 기준 파일이 필요합니다.")
            sys.exit(1)

    # 로드 및 통계 계산
    df_f = pd.read_csv(filtered_path, parse_dates=["date"]).sort_values("date").reset_index(drop=True)
    df_b = pd.read_csv(baseline_path, parse_dates=["date"]).sort_values("date").reset_index(drop=True)

    stats_f = rtb.calc_stats(df_f, "equity")
    stats_b = rtb.calc_stats(df_b, "equity")

    comparison = {
        "filtered_file": filtered_path.name,
        "baseline_file": baseline_path.name,
        "filtered_stats": {
            "initial": stats_f["initial"],
            "final": stats_f["final"],
            "total_return": stats_f["total_return"],
            "cagr": stats_f["cagr"],
            "mdd": stats_f["mdd"],
            "volatility": stats_f["volatility"],
            "sharpe": stats_f["sharpe"],
        },
        "baseline_stats": {
            "initial": stats_b["initial"],
            "final": stats_b["final"],
            "total_return": stats_b["total_return"],
            "cagr": stats_b["cagr"],
            "mdd": stats_b["mdd"],
            "volatility": stats_b["volatility"],
            "sharpe": stats_b["sharpe"],
        },
    }

    # 델타
    def safe_sub(a, b):
        try:
            return a - b
        except Exception:
            return None

    comparison["delta"] = {
        "total_return": safe_sub(stats_f.get("total_return"), stats_b.get("total_return")),
        "cagr": safe_sub(stats_f.get("cagr"), stats_b.get("cagr")),
        "mdd": safe_sub(stats_f.get("mdd"), stats_b.get("mdd")),
        "sharpe": safe_sub(stats_f.get("sharpe"), stats_b.get("sharpe")),
    }

    out_path = OUT / "comparison_filtered_vs_baseline.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(comparison, f, ensure_ascii=False, indent=2)

    # 콘솔 요약 (한국어)
    print("비교 대상:")
    print(f"  필터된: {filtered_path.name}")
    print(f"  기준   : {baseline_path.name}")
    print("")
    print("요약 통계:")
    print("  항목           | 필터된         | 기준            | 차이(필터-기준)")
    def fmt(x):
        if x is None:
            return "N/A"
        if isinstance(x, (int, float)):
            return f"{x:,.2%}" if abs(x) < 100 else f"{x:,.2f}"
        return str(x)

    rows = [
        ("CAGR", stats_f.get("cagr"), stats_b.get("cagr"), comparison["delta"].get("cagr")),
        ("MDD", stats_f.get("mdd"), stats_b.get("mdd"), comparison["delta"].get("mdd")),
        ("샤프", stats_f.get("sharpe"), stats_b.get("sharpe"), comparison["delta"].get("sharpe")),
        ("총수익률", stats_f.get("total_return"), stats_b.get("total_return"), comparison["delta"].get("total_return")),
    ]

    for name, fval, bval, dval in rows:
        fstr = fmt(fval)
        bstr = fmt(bval)
        dstr = fmt(dval) if dval is not None else "N/A"
        print(f"  {name:<13} | {fstr:<14} | {bstr:<14} | {dstr}")

    print("")
    # 간단 결론
    conclusion = []
    if comparison["delta"]["cagr"] is not None and comparison["delta"]["cagr"] < 0:
        conclusion.append("CAGR 하락")
    if comparison["delta"]["mdd"] is not None and comparison["delta"]["mdd"] < 0:
        conclusion.append("MDD 더 큼")
    if comparison["delta"]["total_return"] is not None and comparison["delta"]["total_return"] < 0:
        conclusion.append("총수익률 하락")

    if conclusion:
        print("결론: 필터된 구성은 기준 대비 다음과 같은 단점이 관찰됩니다: ", ", ".join(conclusion))
    else:
        print("결론: 필터된 구성은 기준보다 나쁘지 않습니다.")

    print(f"결과 저장: {out_path}")


if __name__ == "__main__":
    main()
