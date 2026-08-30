"""Trailing Stop 간극 측정 드라이버.

run_etf_backtest.run_single_mode()를 직접 호출해 3가지 케이스를 동일 베이스 설정으로
측정하고, 케이스별 performance/trades/equity_curve를 outputs_trailing/ 에 저장한다.
Trailing 파라미터는 run_etf_strategy() 내부에서 호출 시점에 env로 읽히므로
케이스마다 os.environ을 세팅한다.
"""
import os
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run_etf_backtest as m  # noqa: E402

# 공통 베이스 설정 (케이스 간 동일하게 유지)
os.environ["ETF_USE_CACHE"] = "1"
os.environ["ETF_REFRESH_CACHE"] = "0"
os.environ["ETF_ENABLE_BENCHMARK"] = "1"
os.environ["ETF_BACKTEST_MODE"] = "single"

m.START = "20150101"
m.END = "20250804"
m.RUN_MODE = "single"

CASES = {
    "baseline": {
        "ETF_EXIT_CHECK_DAYS": "0",
        "ETF_TRAILING_STOP_PCT": "0",
        "ETF_PORTFOLIO_TRAILING_STOP_PCT": "0",
    },
    "caseA_7pct": {
        "ETF_EXIT_CHECK_DAYS": "20",
        "ETF_TRAILING_STOP_PCT": "0.07",
        "ETF_PORTFOLIO_TRAILING_STOP_PCT": "0",
    },
    "caseB_10pct": {
        "ETF_EXIT_CHECK_DAYS": "20",
        "ETF_TRAILING_STOP_PCT": "0.10",
        "ETF_PORTFOLIO_TRAILING_STOP_PCT": "0",
    },
}

OUT_DIR = ROOT / "outputs_trailing"
OUT_DIR.mkdir(exist_ok=True)

BUY_FEE = 0.00015
SELL_FEE = 0.00015

results = {}
for name, env in CASES.items():
    for k, v in env.items():
        os.environ[k] = v
    print(f"\n########## CASE: {name} ##########")
    print(f"env: {env}")
    result, trades = m.run_single_mode()
    strategy_stats, benchmark_stats, period = m.summarize_single(result, trades)
    payload = {
        "mode": "single",
        "slippage": m.BASE_SLIPPAGE,
        "period": m._to_json_serializable(period),
        "strategy": m._to_json_serializable(strategy_stats),
        "benchmark": m._to_json_serializable(benchmark_stats) if benchmark_stats is not None else None,
        "config": m._to_json_serializable(m._build_performance_config()),
    }
    (OUT_DIR / f"{name}_performance.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result.to_csv(OUT_DIR / f"{name}_equity_curve.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUT_DIR / f"{name}_trades.csv", index=False, encoding="utf-8-sig")

    cum_fee = (strategy_stats.get("buy_value", 0) + strategy_stats.get("sell_value", 0)) * (BUY_FEE + SELL_FEE) / 2
    # 위는 양쪽 수수료 합산 근사; 정확히는 buy*BUY_FEE + sell*SELL_FEE
    cum_fee = strategy_stats.get("buy_value", 0) * BUY_FEE + strategy_stats.get("sell_value", 0) * SELL_FEE
    results[name] = {
        "cagr": strategy_stats["cagr"],
        "mdd": strategy_stats["mdd"],
        "sharpe": strategy_stats["sharpe"],
        "trade_count": strategy_stats["trade_count"],
        "avg_closed_holding_days": strategy_stats.get("avg_closed_holding_days"),
        "avg_open_holding_days": strategy_stats.get("avg_open_holding_days"),
        "buy_value": strategy_stats.get("buy_value", 0),
        "sell_value": strategy_stats.get("sell_value", 0),
        "cumulative_fee": cum_fee,
        "final": strategy_stats["final"],
        "total_return": strategy_stats["total_return"],
        "volatility": strategy_stats["volatility"],
        "calmar": strategy_stats["calmar"],
        "ulcer_index": strategy_stats["ulcer_index"],
    }
    print(f"[saved] {name} -> outputs_trailing/{name}_performance.json")

# 요약 표 출력
print("\n\n========== TRAILING STOP 간극 요약 ==========")
hdr = f"{'case':<12}{'CAGR':>9}{'MDD':>9}{'Sharpe':>8}{'trades':>8}{'avgHold':>9}{'cumFee':>14}"
print(hdr)
base = results["baseline"]
for name in CASES:
    r = results[name]
    print(
        f"{name:<12}{r['cagr']*100:>8.2f}%{r['mdd']*100:>8.2f}%{r['sharpe']:>8.3f}"
        f"{r['trade_count']:>8}{r['avg_closed_holding_days']:>8.1f}d{r['cumulative_fee']:>14,.0f}"
    )
print("\n--- baseline 대비 차이 ---")
for name in ["caseA_7pct", "caseB_10pct"]:
    r = results[name]
    d_cagr = (r["cagr"] - base["cagr"]) * 100
    d_mdd = (r["mdd"] - base["mdd"]) * 100
    d_sharpe = r["sharpe"] - base["sharpe"]
    d_fee = r["cumulative_fee"] - base["cumulative_fee"]
    d_trades = r["trade_count"] - base["trade_count"]
    print(
        f"{name}: CAGR {d_cagr:+.2f}pp, MDD {d_mdd:+.2f}pp, Sharpe {d_sharpe:+.3f}, "
        f"trades {d_trades:+d}, cumFee {d_fee:+,.0f} KRW"
    )

(OUT_DIR / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n[saved] outputs_trailing/summary.json")
