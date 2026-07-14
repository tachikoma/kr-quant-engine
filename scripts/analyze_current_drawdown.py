#!/usr/bin/env python3
"""현재 진행 중인 드로우다운의 종목별 기여도와 리밸런싱 이력을 분석한다."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "outputs_etf_only"
CACHE_DIR = ROOT / "data_cache"


def _load_csv(name: str, **kwargs) -> pd.DataFrame:
    path = OUTPUT_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"필수 파일이 없습니다: {path}")
    return pd.read_csv(path, **kwargs)


def _asof_close(ticker: str, date: pd.Timestamp) -> float:
    path = CACHE_DIR / f"{ticker}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"가격 캐시가 없습니다: {path}")
    frame = pd.read_parquet(path)
    frame["date"] = pd.to_datetime(frame["date"])
    values = pd.to_numeric(
        frame.loc[frame["date"] <= date].sort_values("date")["close"],
        errors="coerce",
    ).dropna()
    if values.empty:
        raise ValueError(f"{ticker}의 {date.date()} 이전 종가가 없습니다.")
    return float(values.iloc[-1])


def _holdings_at(trades: pd.DataFrame, date: pd.Timestamp) -> dict[str, int]:
    eligible = trades.loc[trades["date"] <= date]
    quantities: dict[str, int] = {}
    for row in eligible.itertuples(index=False):
        signed_qty = int(row.qty) if row.side == "BUY" else -int(row.qty)
        quantities[row.ticker] = quantities.get(row.ticker, 0) + signed_qty
    return {ticker: qty for ticker, qty in quantities.items() if qty > 0}


def _parse_tickers(value: object) -> list[str]:
    if pd.isna(value):
        return []
    return [str(ticker) for ticker in json.loads(str(value))]


def _parse_holdings(value: object) -> dict[str, int]:
    if pd.isna(value):
        return {}
    parsed = json.loads(str(value))
    return {str(ticker): int(qty) for ticker, qty in parsed.items()}


def _current_drawdown(curve: pd.DataFrame) -> dict:
    equity = pd.to_numeric(curve["equity_strategy"], errors="raise")
    peak_position = int(equity.idxmax())
    trough_position = int(curve.index[-1])
    peak_equity = float(equity.loc[peak_position])
    trough_equity = float(equity.loc[trough_position])
    return {
        "peak_date": pd.Timestamp(curve.loc[peak_position, "date"]),
        "trough_date": pd.Timestamp(curve.loc[trough_position, "date"]),
        "peak_equity": peak_equity,
        "trough_equity": trough_equity,
        "equity_loss": trough_equity - peak_equity,
        "drawdown": trough_equity / peak_equity - 1,
    }


def _attribution_rows(
    drawdown: dict,
    holdings: dict[str, int],
    names: dict[str, str],
) -> list[dict]:
    rows = []
    total_loss = float(drawdown["equity_loss"])
    for ticker, qty in holdings.items():
        peak_close = _asof_close(ticker, drawdown["peak_date"])
        trough_close = _asof_close(ticker, drawdown["trough_date"])
        peak_value = qty * peak_close
        trough_value = qty * trough_close
        pnl = trough_value - peak_value
        rows.append(
            {
                "ticker": ticker,
                "name": names.get(ticker, ticker),
                "qty": qty,
                "peak_close": peak_close,
                "trough_close": trough_close,
                "price_return": trough_close / peak_close - 1,
                "peak_value": peak_value,
                "trough_value": trough_value,
                "peak_weight": peak_value / drawdown["peak_equity"],
                "pnl": pnl,
                "drawdown_contribution": pnl / drawdown["peak_equity"],
                "loss_share": pnl / total_loss if total_loss else 0.0,
            }
        )
    return sorted(rows, key=lambda row: row["pnl"])


def _rebalance_history(
    diagnostics: pd.DataFrame,
    current_tickers: set[str],
    last_trade_date: pd.Timestamp,
) -> pd.DataFrame:
    rows = []
    relevant = diagnostics.loc[diagnostics["execution_date"] >= last_trade_date]
    for row in relevant.itertuples(index=False):
        ranked = _parse_tickers(row.ranked_tickers)
        targets = _parse_tickers(row.targets)
        pre_holdings = _parse_holdings(row.pre_holdings)
        post_holdings = _parse_holdings(row.post_holdings)
        item = {
            "decision_date": row.decision_date,
            "execution_date": row.execution_date,
            "risk_on": bool(row.risk_on),
            "n_candidates": int(row.n_candidates),
            "n_orders": int(row.n_orders),
            "held_unchanged": bool(row.held_unchanged),
            "pre_holdings_match_current": set(pre_holdings) == current_tickers,
            "post_holdings_match_current": set(post_holdings) == current_tickers,
        }
        for ticker in sorted(current_tickers):
            item[f"rank_{ticker}"] = ranked.index(ticker) + 1 if ticker in ranked else None
            item[f"target_{ticker}"] = ticker in targets
        rows.append(item)
    return pd.DataFrame(rows)


def _next_unexecutable_decision(
    curve: pd.DataFrame,
    diagnostics: pd.DataFrame,
) -> pd.Timestamp | None:
    curve_positions = {date: position for position, date in enumerate(curve["date"])}
    decision_positions = [
        curve_positions[date]
        for date in diagnostics["decision_date"]
        if date in curve_positions
    ]
    if len(decision_positions) < 2:
        return None
    step = int(pd.Series(decision_positions).diff().dropna().median())
    next_position = decision_positions[-1] + step
    if next_position >= len(curve):
        return None
    return pd.Timestamp(curve.iloc[next_position]["date"])


def main() -> None:
    curve = _load_csv("etf_equity_curve.csv", parse_dates=["date"]).sort_values("date")
    curve = curve.reset_index(drop=True)
    trades = _load_csv(
        "etf_trades.csv",
        dtype={"ticker": str},
        parse_dates=["date"],
    ).sort_values("date")
    diagnostics = _load_csv(
        "rebalance_diagnostics.csv",
        parse_dates=["decision_date", "execution_date"],
    ).sort_values("decision_date")

    trades["ticker"] = trades["ticker"].str.zfill(6)
    trades["side"] = trades["side"].str.upper()
    names = trades.drop_duplicates("ticker", keep="last").set_index("ticker")["name"].to_dict()

    drawdown = _current_drawdown(curve)
    holdings = _holdings_at(trades, drawdown["peak_date"])
    window_trades = trades.loc[
        (trades["date"] > drawdown["peak_date"])
        & (trades["date"] <= drawdown["trough_date"])
    ]
    if not window_trades.empty:
        raise ValueError("현재 드로우다운 구간에 거래가 있어 정적 보유 기여도로 분석할 수 없습니다.")

    attribution = pd.DataFrame(_attribution_rows(drawdown, holdings, names))
    last_trade_date = pd.Timestamp(
        trades.loc[trades["date"] <= drawdown["trough_date"], "date"].max()
    )
    history = _rebalance_history(diagnostics, set(holdings), last_trade_date)
    next_decision = _next_unexecutable_decision(curve, diagnostics)

    distribution_cash = pd.to_numeric(
        curve.loc[
            (curve["date"] > drawdown["peak_date"])
            & (curve["date"] <= drawdown["trough_date"]),
            "distribution_cash",
        ],
        errors="coerce",
    ).fillna(0.0).sum()
    attributed_change = float(attribution["pnl"].sum()) + float(distribution_cash)
    residual = float(drawdown["equity_loss"]) - attributed_change

    attribution_path = OUTPUT_DIR / "current_drawdown_attribution.csv"
    history_path = OUTPUT_DIR / "current_holding_rebalance_history.csv"
    summary_path = OUTPUT_DIR / "current_drawdown_summary.json"
    attribution.to_csv(attribution_path, index=False, encoding="utf-8-sig")
    history.to_csv(history_path, index=False, encoding="utf-8-sig")

    rank_columns = [f"rank_{ticker}" for ticker in sorted(holdings)]
    all_holdings_top_ranked = history[rank_columns].le(len(holdings)).all(axis=1)
    summary = {
        "peak_date": drawdown["peak_date"].date().isoformat(),
        "trough_date": drawdown["trough_date"].date().isoformat(),
        "peak_equity": drawdown["peak_equity"],
        "trough_equity": drawdown["trough_equity"],
        "equity_loss": drawdown["equity_loss"],
        "drawdown": drawdown["drawdown"],
        "last_trade_date": last_trade_date.date().isoformat(),
        "rebalance_decisions_since_last_trade": len(history),
        "zero_order_decisions_since_last_trade": int((history["n_orders"] == 0).sum()),
        "zero_candidate_decisions_since_last_trade": int(
            (history["n_candidates"] == 0).sum()
        ),
        "unchanged_decisions_since_last_trade": int(history["held_unchanged"].sum()),
        "all_holdings_top_ranked_decisions": int(all_holdings_top_ranked.sum()),
        "distribution_cash": float(distribution_cash),
        "attribution_residual": residual,
        "next_unexecutable_decision_date": (
            next_decision.date().isoformat() if next_decision is not None else None
        ),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 현재 드로우다운 원인 분석 ===")
    print(
        f"구간: {summary['peak_date']} → {summary['trough_date']} / "
        f"낙폭 {drawdown['drawdown']:.2%} ({drawdown['equity_loss']:,.0f}원)"
    )
    for row in attribution.itertuples(index=False):
        print(
            f"  {row.name}({row.ticker}): 가격 {row.price_return:.2%}, "
            f"손익 {row.pnl:,.0f}원, 전체 손실 기여 {row.loss_share:.1%}"
        )
    print(
        f"마지막 거래 이후 리밸런싱 판단 {len(history)}회 / "
        f"무주문 {(history['n_orders'] == 0).sum()}회"
    )
    print(
        f"후보 0개 판단 {(history['n_candidates'] == 0).sum()}회 / "
        f"전체 보유 종목이 상위 {len(holdings)}위 안에 든 판단 "
        f"{all_holdings_top_ranked.sum()}회"
    )
    if next_decision is not None:
        print(f"다음 예정 판단일은 종료일 {next_decision.date()}로, 다음 거래일 체결 데이터가 없음")
    print(f"저장: {attribution_path}")
    print(f"저장: {history_path}")
    print(f"저장: {summary_path}")


if __name__ == "__main__":
    main()
