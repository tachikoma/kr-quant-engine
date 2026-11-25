"""P1-A-2: 위험 관리 후보 강건성 검증(연구용).

P1-A-1의 후보(Baseline/A/B2/B3)를 다음 축에서 재검증한다.
- 2026년 제외(2016~2025)
- 연도별·3년·5년 롤링(중첩 구간, 경계 수익률 포함)
- 위험 이벤트 의존성(fwd 5/20/60, 기준 대비 자산 변화, 재진입/휩쏘, 비용)
- 리밸런싱 위상 민감도(안전하게 변경 불가 시 미검증 기록)
- 비용 강건성(base 5bp/1x, stress 50bp/3x)

새 엔진/프레임워크 없이 P1-A-1 러너와 P0-B/P0-C 유틸을 재사용한다.

실행::

    uv run python scripts/p1a2_overlay_robustness.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import p0b_execution_stress as p0b
import p0c_regime_drawdown as p0c
import p1a1_risk_overlay as p1a1

import run_etf_backtest as rtb

OUTPUT_DIR = ROOT / "outputs_p1a2"
AUM = float(rtb.INITIAL_CASH)
CAP = 0.85
BASE_SLIPPAGE = 0.0005
STRESS_SLIPPAGE = 0.005
STRESS_FEE_MULTIPLIER = 3.0
EXCLUDE_2026_END = pd.Timestamp("2025-12-31")
CANDIDATES: dict[str, dict[str, Any]] = {
    "baseline": {},
    "A_daily_risk_off": {"daily_risk_off_exit": True},
    "B2_pstop_15": {"exit_check_days": 1, "portfolio_trailing_stop_pct": 0.15},
    "B3_pstop_20": {"exit_check_days": 1, "portfolio_trailing_stop_pct": 0.20},
}
EXIT_REASON = {
    "A_daily_risk_off": "ETF_DAILY_RISK_OFF",
    "B2_pstop_15": "ETF_PORTFOLIO_TRAILING_STOP",
    "B3_pstop_20": "ETF_PORTFOLIO_TRAILING_STOP",
}
COST_CASES = (
    ("base", BASE_SLIPPAGE, 1.0),
    ("stress_50bp_3x", STRESS_SLIPPAGE, STRESS_FEE_MULTIPLIER),
)


# --------------------------------------------------------------------------- #
# 실행/지표
# --------------------------------------------------------------------------- #
def run_all(
    price: pd.DataFrame,
    index_df: pd.DataFrame,
    dates: list[pd.Timestamp],
    label_suffix: str,
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for cost_case, slippage, multiplier in COST_CASES:
        for name, kwargs in CANDIDATES.items():
            curve, trades = p1a1.run_scenario(
                price,
                index_df,
                dates,
                slippage=slippage,
                fee_multiplier=multiplier,
                **kwargs,
            )
            results[f"{name}|{cost_case}|{label_suffix}"] = {
                "scenario": name,
                "cost_case": cost_case,
                "window": label_suffix,
                "curve": curve,
                "trades": trades,
                "slippage": slippage,
                "fee_multiplier": multiplier,
                "dates": dates,
            }
            print(
                f"[p1a2] {label_suffix:9s} {cost_case:13s} {name:18s} "
                f"trades={len(trades)} final={float(curve['equity'].iloc[-1]):,.0f}"
            )
    return results


def metrics_for(
    results: dict[str, dict[str, Any]],
    price: pd.DataFrame,
    labels: pd.DataFrame,
    oos_start: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rows, drawdowns, events = [], [], []
    for item in results.values():
        row, dd, ev = p1a1.scenario_metrics(
            item["scenario"],
            item["cost_case"],
            item["curve"],
            item["trades"],
            item["dates"],
            price,
            labels,
            item["slippage"],
            item["fee_multiplier"],
            EXIT_REASON.get(item["scenario"]),
            oos_start,
        )
        row["window"] = item["window"]
        rows.append(row)
        drawdowns.append(dd)
        if not ev.empty:
            ev = ev.assign(window=item["window"])
            events.append(ev)
    return (
        pd.DataFrame(rows),
        pd.concat(drawdowns, ignore_index=True) if drawdowns else pd.DataFrame(),
        pd.concat(events, ignore_index=True) if events else pd.DataFrame(),
    )


# --------------------------------------------------------------------------- #
# B. 롤링 구간
# --------------------------------------------------------------------------- #
def rolling_windows(equity: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    years = sorted({pd.Timestamp(d).year for d in equity["date"]})
    for year in years:
        start = pd.Timestamp(f"{year}-01-01")
        end = pd.Timestamp(f"{year}-12-31")
        stats = p1a1.slice_stats(equity, start, end)
        rows.append({"window_type": "year", "start": start, "end": end, **stats})
    for length, label in ((3, "3y"), (5, "5y")):
        for year in years:
            start = pd.Timestamp(f"{year}-01-01")
            end = pd.Timestamp(f"{year + length - 1}-12-31")
            if end > pd.Timestamp(equity["date"].max()):
                continue
            stats = p1a1.slice_stats(equity, start, end)
            rows.append({"window_type": label, "start": start, "end": end, **stats})
    return pd.DataFrame(rows)


def rolling_analysis(
    results: dict[str, dict[str, Any]], dates: list[pd.Timestamp]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    base = results["baseline|base|full"]
    base_equity = p0c.equity_with_initial(base["curve"], dates)
    base_windows = rolling_windows(base_equity).rename(
        columns={"cagr": "base_cagr", "mdd": "base_mdd", "sharpe": "base_sharpe"}
    )
    rows: list[dict[str, Any]] = []
    for name in ("A_daily_risk_off", "B2_pstop_15", "B3_pstop_20"):
        equity = p0c.equity_with_initial(results[f"{name}|base|full"]["curve"], dates)
        windows = rolling_windows(equity)
        merged = windows.merge(
            base_windows, on=["window_type", "start", "end"], how="inner"
        )
        merged["scenario"] = name
        merged["mdd_delta"] = merged["mdd"] - merged["base_mdd"]
        merged["cagr_delta"] = merged["cagr"] - merged["base_cagr"]
        merged["sharpe_delta"] = merged["sharpe"] - merged["base_sharpe"]
        rows.append(merged)
    detail = pd.concat(rows, ignore_index=True)
    summary_rows = []
    for (name, window_type), group in detail.groupby(["scenario", "window_type"]):
        summary_rows.append(
            {
                "scenario": name,
                "window_type": window_type,
                "windows": len(group),
                "mdd_improved_ratio": float((group["mdd_delta"] > 0).mean()),
                "cagr_improved_ratio": float((group["cagr_delta"] > 0).mean()),
                "sharpe_improved_ratio": float((group["sharpe_delta"] > 0).mean()),
                "mdd_delta_median": float(group["mdd_delta"].median()),
                "mdd_delta_worst": float(group["mdd_delta"].min()),
                "cagr_delta_median": float(group["cagr_delta"].median()),
                "cagr_delta_worst": float(group["cagr_delta"].min()),
            }
        )
    return detail, pd.DataFrame(summary_rows)


# --------------------------------------------------------------------------- #
# C. 이벤트 의존성
# --------------------------------------------------------------------------- #
def event_dependency(
    results: dict[str, dict[str, Any]],
    price: pd.DataFrame,
    dates: list[pd.Timestamp],
    labels: pd.DataFrame,
) -> pd.DataFrame:
    base_equity = p0c.equity_with_initial(
        results["baseline|base|full"]["curve"], dates
    ).set_index("date")["equity"]
    closes = p0c.close_matrix(price)
    position = {pd.Timestamp(d): index for index, d in enumerate(dates)}
    rows: list[dict[str, Any]] = []
    for name in ("A_daily_risk_off", "B2_pstop_15", "B3_pstop_20"):
        item = results[f"{name}|base|full"]
        curve, trades = item["curve"], item["trades"]
        equity = p0c.equity_with_initial(curve, dates).set_index("date")["equity"]
        peak = equity.cummax()
        reason = EXIT_REASON[name]
        events = p1a1.exit_events(
            trades, reason, price, dates, labels, risk_based=(name == "A_daily_risk_off")
        )
        if events.empty:
            continue
        for event in events.itertuples():
            exec_date = pd.Timestamp(event.exec_date)
            index = position.get(exec_date, -1)
            ticker = str(event.ticker)
            forward = {}
            for horizon in (5, 20, 60):
                target = index + horizon
                if index >= 0 and target < len(dates):
                    later = dates[target]
                    forward[f"fwd{horizon}"] = (
                        float(closes.at[later, ticker] / closes.at[exec_date, ticker] - 1)
                        if ticker in closes.columns
                        and pd.notna(closes.at[later, ticker])
                        else None
                    )
                else:
                    forward[f"fwd{horizon}"] = None
            decision_index = max(index - 1, 0)
            decision_date = pd.Timestamp(dates[decision_index])
            equity_asof = equity.loc[:decision_date]
            drawdown_at_exit = (
                float(equity_asof.iloc[-1] / peak.loc[:decision_date].iloc[-1] - 1)
                if not equity_asof.empty
                else None
            )
            vs_base = {}
            for horizon in (20, 60):
                target = index + horizon
                if index >= 0 and target < len(dates):
                    later = dates[target]
                    vs_base[f"vs_base_{horizon}d"] = float(
                        (equity.loc[:later].iloc[-1] / equity.loc[:exec_date].iloc[-1] - 1)
                        - (
                            base_equity.loc[:later].iloc[-1]
                            / base_equity.loc[:exec_date].iloc[-1]
                            - 1
                        )
                    )
                else:
                    vs_base[f"vs_base_{horizon}d"] = None
            exit_cost = p0b.cost_breakdown(
                trades[
                    (trades["date"] == exec_date)
                    & (trades["ticker"].astype(str) == ticker)
                    & (trades["side"] == "SELL")
                ],
                BASE_SLIPPAGE,
                1.0,
            )["total_run_rate_cost"]
            reentry = trades[
                (trades["side"] == "BUY")
                & (trades["ticker"].astype(str) == ticker)
                & (trades["date"] > exec_date)
            ]
            reentry_days = (
                int(position.get(pd.Timestamp(reentry.iloc[0]["date"]), index) - index)
                if not reentry.empty
                else None
            )
            rows.append(
                {
                    "scenario": name,
                    "signal_date": pd.Timestamp(event.signal_date),
                    "decision_date": pd.Timestamp(event.decision_date),
                    "exec_date": exec_date,
                    "ticker": ticker,
                    "qty": int(event.qty),
                    "lag_days": event.lag_days,
                    "drawdown_at_exit": drawdown_at_exit,
                    **forward,
                    **vs_base,
                    "reentry_days": reentry_days,
                    "reentry_price_change": (
                        float(reentry.iloc[0]["price"]) / float(event.price) - 1
                        if not reentry.empty
                        else None
                    ),
                    "exit_cost": exit_cost,
                }
            )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# D. 위상 민감도 (안전성 확인)
# --------------------------------------------------------------------------- #
def phase_support_check(
    price: pd.DataFrame, index_df: pd.DataFrame, dates: list[pd.Timestamp]
) -> dict[str, Any]:
    """initial_state로 위상을 바꿀 수 있는지, 가드/워밍업 영향과 함께 기록한다."""
    state = {
        "cash": AUM,
        "holdings": {},
        "holding_cost_basis": {},
        "holding_peak_closes": {},
        "portfolio_peak_equity": None,
        "last_valid_closes": {},
        "rebalance_phase_offset": 0,
    }
    overlay_blocked = False
    try:
        rtb.run_etf_strategy(
            AUM,
            dates[:200],
            index_df,
            use_market_filter=True,
            max_positions=int(p0b.APPLIED["max_positions"]),
            slippage=BASE_SLIPPAGE,
            risk_off_liquidate=True,
            price_data=price,
            max_asset_pct=CAP,
            daily_risk_off_exit=True,
            initial_state=state,
        )
    except ValueError as exc:
        overlay_blocked = "daily_risk_off_exit" in str(exc)
    canonical, canonical_trades = p1a1.run_scenario(
        price, index_df, dates[:260], slippage=BASE_SLIPPAGE, fee_multiplier=1.0
    )
    state_curve, state_trades = rtb.run_etf_strategy(
        AUM,
        dates[:260],
        index_df,
        use_market_filter=True,
        max_positions=int(p0b.APPLIED["max_positions"]),
        slippage=BASE_SLIPPAGE,
        risk_off_liquidate=True,
        price_data=price,
        max_asset_pct=CAP,
        initial_state=state,
    )
    canonical_first = (
        pd.Timestamp(canonical_trades["date"].min()) if not canonical_trades.empty else None
    )
    state_first = (
        pd.Timestamp(state_trades["date"].min()) if not state_trades.empty else None
    )
    return {
        "supported": False,
        "tested_offsets": [0],
        "not_tested_offsets": [5, 10, 15],
        "overlay_guard_blocks_initial_state": overlay_blocked,
        "warmup_days_with_initial_state": 0,
        "probe_offset0_equivalent_within_260d": (
            canonical_first == state_first
            and abs(float(canonical["equity"].iloc[-1]) - float(state_curve["equity"].iloc[-1])) < 1e-6
        ),
        "canonical_first_trade": (
            canonical_first.strftime("%Y-%m-%d") if canonical_first is not None else None
        ),
        "initial_state_first_trade": (
            state_first.strftime("%Y-%m-%d") if state_first is not None else None
        ),
        "canonical_final_equity": float(canonical["equity"].iloc[-1]),
        "initial_state_final_equity": float(state_curve["equity"].iloc[-1]),
        "reason": (
            "위상을 바꾸는 유일한 legacy 경로는 initial_state(rebalance_phase_offset)이며, "
            "이는 상태 재개 경로로 daily_risk_off_exit 가드에서 차단된다(A 사용 불가). "
            "또한 initial_state는 warmup_days=0으로 시작해 baseline/B2/B3도 canonical과 동일한 "
            "실행 경로라 보장되지 않는다(offset 0·260거래일 probe에서는 일치). "
            "4개 후보를 동일 위상에서 비교할 수 없어 위상 민감도는 미검증으로 남긴다. "
            "후속 과제: pending 청산 상태 직렬화 + 워밍업을 보존하는 명시적 phase 파라미터."
        ),
    }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    frozen = p0b.configure_frozen_settings(CAP)
    p0b.rtb.START, p0b.rtb.END = p0b.DEFAULT_START, p0b.DEFAULT_END
    preflight = p0b.preflight_cache(p0b.DEFAULT_START, p0b.DEFAULT_END)
    digest_before = p0b.data_cache_digest()
    price, index_df, dates = p0b.load_inputs()
    p0b.verify_applied_settings()
    missing = sorted({str(t) for t in rtb.ETF_LIST} - set(price["ticker"].astype(str)))
    if missing:
        raise SystemExit(f"[p1a2] 필수 종목 누락: {missing}")
    labels = p0c.regime_labels(index_df, dates)
    oos_start = pd.Timestamp(frozen["oos_start_date"])

    baseline_curve, _ = p1a1.run_scenario(
        price, index_df, dates, slippage=BASE_SLIPPAGE, fee_multiplier=1.0
    )
    baseline = p0b.performance(AUM, dates, baseline_curve)
    expected = {
        "cagr": 0.2454113778672673,
        "mdd": -0.5121244554166072,
        "sharpe": 1.0391357658469746,
    }
    if any(abs(baseline[k] - v) > 1e-9 for k, v in expected.items()):
        raise SystemExit(f"[p1a2] P1-A-1 기준 재현 실패: {baseline}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    full = run_all(price, index_df, dates, "full")
    ex_dates = [d for d in dates if pd.Timestamp(d) <= EXCLUDE_2026_END]
    ex2026 = run_all(price, index_df, ex_dates, "ex2026")

    full_metrics, full_dd, _ = metrics_for(full, price, labels, oos_start)
    ex_metrics, ex_dd, _ = metrics_for(
        ex2026, price, labels, pd.Timestamp("2026-01-01")
    )
    full_metrics.to_csv(OUTPUT_DIR / "scenario_full.csv", index=False, encoding="utf-8-sig")
    ex_metrics.to_csv(OUTPUT_DIR / "scenario_ex2026.csv", index=False, encoding="utf-8-sig")
    full_dd.to_csv(OUTPUT_DIR / "drawdowns_full.csv", index=False, encoding="utf-8-sig")
    ex_dd.to_csv(OUTPUT_DIR / "drawdowns_ex2026.csv", index=False, encoding="utf-8-sig")

    rolling_detail, rolling_summary = rolling_analysis(full, dates)
    rolling_detail.to_csv(OUTPUT_DIR / "rolling_windows.csv", index=False, encoding="utf-8-sig")
    rolling_summary.to_csv(
        OUTPUT_DIR / "rolling_summary.csv", index=False, encoding="utf-8-sig"
    )

    events = event_dependency(full, price, dates, labels)
    events.to_csv(OUTPUT_DIR / "event_dependency.csv", index=False, encoding="utf-8-sig")

    phase = phase_support_check(price, index_df, dates)
    (OUTPUT_DIR / "phase_status.json").write_text(
        json.dumps(phase, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    if p0b.data_cache_digest() != digest_before:
        raise SystemExit("[p1a2] 실행 중 data_cache가 변경되었습니다.")

    metadata = {
        "phase": "P1-A-2",
        "period": {"start": p0b.DEFAULT_START, "end": p0b.DEFAULT_END},
        "exclude_2026_end": EXCLUDE_2026_END.strftime("%Y-%m-%d"),
        "trading_days": len(dates),
        "max_asset_pct": CAP,
        "spread_pct": frozen["spread_pct"],
        "freeze_sha256": frozen["freeze_sha256"],
        "candidates": list(CANDIDATES),
        "cost_cases": {
            "base": [BASE_SLIPPAGE, 1.0],
            "stress": [STRESS_SLIPPAGE, STRESS_FEE_MULTIPLIER],
        },
        "rolling": {"year_step": "1y", "overlap": True, "independent_samples": False},
        "phase_sensitivity": phase,
        "preflight": preflight,
        "git_commit": p0b.git_commit(),
        "data_cache_digest": digest_before,
        "data_cache_unchanged": True,
        "baseline": baseline,
        "baseline_matches_p1a1": True,
        "artifacts": sorted(path.name for path in OUTPUT_DIR.glob("*")),
    }
    summary = {
        "baseline": baseline,
        "full": full_metrics.to_dict(orient="records"),
        "ex2026": ex_metrics.to_dict(orient="records"),
        "rolling_summary": rolling_summary.to_dict(orient="records"),
        "events": events.to_dict(orient="records"),
        "phase": phase,
    }
    (OUTPUT_DIR / "p1a2_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (OUTPUT_DIR / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"[p1a2] 완료: {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
