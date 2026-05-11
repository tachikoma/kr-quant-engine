#!/usr/bin/env python3
"""
Grid backtest runner

- ETF 후보 풀 크기, 리밸런스 주기, 보유 포지션 수를 그리드로 변경해 백테스트를 실행합니다.
- 소규모 기본 그리드를 실행하도록 설계되어 있으며 `outputs_grid`에 결과를 저장합니다.
"""
from __future__ import annotations

import sys
from pathlib import Path

# 프로젝트 루트를 `sys.path`에 추가하여 같은 레벨의 모듈을 절대 import 할 수 있게 합니다.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import json
import os
import traceback

import pandas as pd

import run_etf_backtest as rtb

try:
    from pykrx import stock
except Exception:
    stock = None


OUTPUT_DIR = Path("outputs_grid")
OUTPUT_DIR.mkdir(exist_ok=True)


def fetch_candidate_etfs(n: int, default: list[str]) -> list[str]:
    """pykrx에서 ETF 티커 목록을 가져와 기본 리스트를 확장합니다.
    실패하면 기본 리스트로 제한합니다.
    """
    if n <= len(default):
        return default[:n]

    if stock is None:
        raise RuntimeError("pykrx 미설치로 ETF 후보 확장 불가")

    all_etfs = stock.get_etf_ticker_list()
    all_etfs = [str(t) for t in all_etfs]
    extras = [t for t in all_etfs if t not in default]
    candidates = default + extras[: max(0, n - len(default))]
    return candidates[:n]


def run_grid(
    etf_sizes: list[int] | None = None,
    rebalance_days: list[int] | None = None,
    max_positions_list: list[int] | None = None,
    slippage: float | None = None,
    use_market_filter: bool = True,
):
    etf_sizes = etf_sizes or [len(rtb.ETF_LIST), 12]
    rebalance_days = rebalance_days or [10, 20]
    max_positions_list = max_positions_list or [1, 2, 3]
    slippage = slippage if slippage is not None else rtb.BASE_SLIPPAGE

    print(f"[실험] etf_sizes={etf_sizes} rebalance_days={rebalance_days} max_positions={max_positions_list}")

    summaries: list[dict] = []

    try:
        if not (os.environ.get("KRX_ID") and os.environ.get("KRX_PW")):
            print("⚠️  KRX_ID/KRX_PW 환경변수 미설정: 인덱스 조회가 실패할 수 있습니다.")

        index_df = rtb.get_index_data()
        common_dates = list(index_df["date"])
    except Exception as e:
        print("인덱스 데이터 조회 실패:", e)
        traceback.print_exc()
        raise SystemExit(1)

    default_list = list(rtb.ETF_LIST)

    for n in etf_sizes:
        try:
            etf_list = fetch_candidate_etfs(n, default_list)
        except Exception as e:
            print(f"[스킵] ETF 후보 확장 실패 n={n}: {e}")
            continue

        # 모듈 전역 변수 덮어쓰기(다음 호출에서 사용됨)
        rtb.ETF_LIST = etf_list

        for reb in rebalance_days:
            rtb.REBALANCE_STEP_DAYS = int(reb)

            for max_pos in max_positions_list:
                print(f"\n[실행] 후보수={n}, 리밸={reb}일, 포지션={max_pos}")
                try:
                    result, trades = rtb.run_etf_strategy(
                        rtb.INITIAL_CASH,
                        common_dates,
                        index_df,
                        use_market_filter=use_market_filter,
                        max_positions=int(max_pos),
                        slippage=float(slippage),
                    )

                    stats = rtb.calc_stats(result, "equity")
                    summary = {
                        "n_candidates": n,
                        "rebalance_step_days": int(reb),
                        "max_positions": int(max_pos),
                        "initial": float(stats["initial"]),
                        "final": float(stats["final"]),
                        "total_return": float(stats["total_return"]),
                        "cagr": float(stats["cagr"]),
                        "mdd": float(stats["mdd"]),
                        "volatility": float(stats["volatility"]),
                        "sharpe": None if pd.isna(stats.get("sharpe")) else float(stats.get("sharpe")),
                    }

                    print(f"[결과] {summary}")
                    summaries.append(summary)

                    # 결과 저장
                    fname = OUTPUT_DIR / f"curve_n{n}_reb{reb}_pos{max_pos}.csv"
                    result.to_csv(fname, index=False, encoding="utf-8-sig")
                    trades.to_csv(OUTPUT_DIR / f"trades_n{n}_reb{reb}_pos{max_pos}.csv", index=False, encoding="utf-8-sig")

                except Exception as e:
                    print(f"[오류] 실행 실패 for n={n},reb={reb},pos={max_pos}: {e}")
                    traceback.print_exc()
                    continue

    summary_df = pd.DataFrame(summaries)
    summary_df.to_csv(OUTPUT_DIR / "grid_summary.csv", index=False, encoding="utf-8-sig")
    with (OUTPUT_DIR / "grid_summary.json").open("w", encoding="utf-8") as f:
        json.dump(summaries, f, ensure_ascii=False, indent=2)

    print(f"\n완료: 결과 저장 경로={OUTPUT_DIR}")
    return summary_df


def _parse_env_list_int(name: str, default: list[int]) -> list[int]:
    val = os.environ.get(name)
    if not val:
        return default
    parts = [p.strip() for p in val.split(",") if p.strip()]
    try:
        return [int(x) for x in parts]
    except Exception:
        print(f"[경고] 환경변수 {name} 값 파싱 실패: {val} → 기본값 사용")
        return default


if __name__ == "__main__":
    # 환경변수로 그리드 파라미터를 받습니다.
    etf_sizes = _parse_env_list_int("ETFSIZES", [len(rtb.ETF_LIST), 12])
    rebalance_days = _parse_env_list_int("REBALANCEDAYS", [10, 20])
    max_positions_list = _parse_env_list_int("MAXPOSITIONS", [1, 2, 3])

    print(f"[런옵션] ETFSIZES={etf_sizes} REBALANCEDAYS={rebalance_days} MAXPOSITIONS={max_positions_list}")

    df = run_grid(
        etf_sizes=etf_sizes,
        rebalance_days=rebalance_days,
        max_positions_list=max_positions_list,
        slippage=rtb.BASE_SLIPPAGE,
    )
    print(df.to_string(index=False))
