"""P1-A-1: 일별 risk_off 청산 오버레이 합성 fixture 검증.

``run_etf_strategy(daily_risk_off_exit=True)``의 신규 동작만 검증한다.
- 기본값 False는 기존 동작과 동일
- risk_off 신호일(t)에 판단하고 **다음 거래일 시가(t+1)** 에 청산
- 위험 정책상 허용되는 그룹(해외·원자재 override)은 유지
- 같은 실행일 중복 매도·재매수 없음
- 시가 누락 시 미체결(상태 보존)
- 현금·평가액 검산

실행::

    uv run python scripts/test_p1a1_risk_overlay.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import p1a1_risk_overlay as runner

import etf_shared
import run_etf_backtest as rtb

DOMESTIC = "900001"
FOREIGN = "900002"
INITIAL_CASH = 1_000_000.0
GAP = 0.003
RISK_OFF_WINDOWS = ((150, 155), (170, 171))
MISSING_OPEN_INDEX = 171


def build_price_fixture(missing_open_index: int = MISSING_OPEN_INDEX) -> pd.DataFrame:
    n = 220
    dates = pd.bdate_range("2024-01-01", periods=n)
    frames = []
    for offset, ticker in enumerate((DOMESTIC, FOREIGN)):
        index = np.arange(n, dtype=float)
        daily = 0.0015 + 0.0004 * np.sin(index / 7.0 + offset)
        close = 10_000.0 * np.cumprod(1.0 + daily)
        open_ = np.empty(n, dtype=float)
        open_[0] = close[0]
        gap = np.where(np.arange(1, n) % 2 == 0, GAP, -GAP)
        open_[1:] = close[:-1] * (1.0 + gap)
        frame = pd.DataFrame(
            {
                "date": dates,
                "ticker": ticker,
                "open": open_,
                "close": close,
                "volume": 1_000_000,
                "trading_value": close * 1_000_000,
            }
        )
        frame["close_adj"] = frame["close"]
        frame["ret_60"] = frame["close"].pct_change(60)
        frame["ret_120"] = frame["close"].pct_change(120)
        frame["ma20"] = frame["close"].rolling(20).mean()
        frame["ma60"] = frame["close"].rolling(60).mean()
        frame["trend_ok"] = (frame["close"] > frame["ma20"]) & (frame["ma20"] > frame["ma60"])
        frame["liquidity_ok"] = True
        frame["listing_ok"] = True
        frame["deviation_ok"] = True
        frame["distribution"] = 0.0
        frames.append(frame)
    price = pd.concat(frames, ignore_index=True)
    missing = price["date"].eq(dates[missing_open_index])
    price.loc[missing, "open"] = np.nan
    return price


def build_index_fixture(dates: pd.DatetimeIndex) -> pd.DataFrame:
    n = len(dates)
    close = 2_500.0 * np.cumprod(np.full(n, 1.0004))
    frame = pd.DataFrame({"date": dates, "close": close})
    frame["market_ma"] = frame["close"].rolling(120).mean()
    frame["market_ma_slope"] = frame["market_ma"] - frame["market_ma"].shift(20)
    frame["risk_on"] = True
    for start, end in RISK_OFF_WINDOWS:
        frame.loc[frame.index[start : end + 1], "risk_on"] = False
    return frame


def run(price: pd.DataFrame, index_df: pd.DataFrame, overlay: bool):
    dates = [pd.Timestamp(d) for d in index_df["date"]]
    return rtb.run_etf_strategy(
        INITIAL_CASH,
        dates,
        index_df,
        use_market_filter=True,
        max_positions=2,
        slippage=0.0005,
        risk_off_liquidate=True,
        price_data=price,
        max_asset_pct=0.85,
        daily_risk_off_exit=overlay,
    )


def reconcile(curve: pd.DataFrame, trades: pd.DataFrame, label: str) -> None:
    identity = np.allclose(
        curve["equity"].to_numpy(dtype=float),
        (curve["cash"] + curve["market_value"]).to_numpy(dtype=float),
        rtol=0.0,
        atol=1e-6,
    )
    flow = float(pd.to_numeric(trades["cash_flow"], errors="coerce").fillna(0).sum())
    distribution = float(curve.get("distribution_cash", pd.Series(dtype=float)).sum())
    final_cash = float(curve["cash"].iloc[-1])
    cash_ok = abs(final_cash - (INITIAL_CASH + flow + distribution)) <= 1e-6
    if not identity or not cash_ok:
        raise AssertionError(f"{label}: 회계 검산 실패 identity={identity} cash_ok={cash_ok}")


def sells(trades: pd.DataFrame, ticker: str) -> pd.DataFrame:
    return trades[(trades["ticker"].astype(str) == ticker) & (trades["side"] == "SELL")]


def check_default_is_unchanged(price, index_df) -> None:
    curve_off, trades_off = run(price, index_df, overlay=False)
    curve_explicit, _ = run(price, index_df, overlay=False)
    if not curve_off["equity"].equals(curve_explicit["equity"]):
        raise AssertionError("기본값 False 실행이 결정적이지 않습니다")
    if not trades_off.empty and (
        trades_off["reason"].astype(str) == "ETF_DAILY_RISK_OFF"
    ).any():
        raise AssertionError("오버레이 비활성인데 ETF_DAILY_RISK_OFF 체결이 있습니다")
    reconcile(curve_off, trades_off, "default_off")


def check_daily_exit_next_open(price, index_df) -> None:
    dates = list(index_df["date"])
    curve, trades = run(price, index_df, overlay=True)
    reconcile(curve, trades, "overlay_on")
    window = trades[
        (trades["date"] > dates[150]) & (trades["date"] <= dates[155])
    ]
    domestic_sells = sells(trades, DOMESTIC)
    domestic_sells = domestic_sells[
        (domestic_sells["date"] >= dates[151]) & (domestic_sells["date"] <= dates[156])
    ]
    if len(domestic_sells) != 1:
        raise AssertionError(f"국내 ETF 청산이 1건이 아닙니다: {len(domestic_sells)}")
    sell = domestic_sells.iloc[0]
    if pd.Timestamp(sell["date"]) != pd.Timestamp(dates[151]):
        raise AssertionError(f"청산일이 t+1이 아닙니다: {sell['date']}")
    if str(sell["reason"]) != "ETF_DAILY_RISK_OFF":
        raise AssertionError(f"청산 사유가 다릅니다: {sell['reason']}")
    expected_price = float(
        price[
            (price["ticker"] == DOMESTIC) & (price["date"] == dates[151])
        ]["open"].iloc[0]
    ) * (1 - rtb.SPREAD_PCT / 2)
    if abs(float(sell["price"]) - expected_price) > 1e-9:
        raise AssertionError(
            f"체결가가 t+1 시가 기준이 아닙니다: {sell['price']} != {expected_price}"
        )
    foreign_sells = sells(trades, FOREIGN)
    foreign_sells = foreign_sells[
        (foreign_sells["date"] >= dates[151]) & (foreign_sells["date"] <= dates[156])
    ]
    if not foreign_sells.empty:
        raise AssertionError("해외 그룹 override가 유지되지 않았습니다(해외 ETF 청산 발생)")
    if (window["side"] == "BUY").any():
        raise AssertionError("위험 청산일에 신규 매수가 발생했습니다")
    # risk_on 복귀 직후(156~157) 즉시 재매수 금지
    rebuy = trades[
        (trades["date"].isin([dates[156], dates[157]])) & (trades["side"] == "BUY")
    ]
    if not rebuy.empty:
        raise AssertionError("risk_on 복귀 직후 재매수가 발생했습니다")
    # 이후 정기 리밸런싱(160→161)에서는 재진입 가능
    if trades[(trades["date"] == dates[161]) & (trades["side"] == "BUY")].empty:
        raise AssertionError("정기 리밸런싱 재진입이 사라졌습니다")


def check_no_duplicate_on_rebalance_day(price, index_df) -> None:
    dates = list(index_df["date"])
    index = index_df.copy()
    index["risk_on"] = True
    index.loc[index.index[180], "risk_on"] = False  # 결정일 180 = 정기 리밸런싱일
    _, trades = run(price, index, overlay=True)
    day = trades[trades["date"] == dates[181]]
    domestic_sells = day[(day["ticker"].astype(str) == DOMESTIC) & (day["side"] == "SELL")]
    if len(domestic_sells) != 1:
        raise AssertionError(f"리밸런싱일 중복 매도: {len(domestic_sells)}건")
    domestic_buys = day[(day["ticker"].astype(str) == DOMESTIC) & (day["side"] == "BUY")]
    if not domestic_buys.empty:
        raise AssertionError("같은 실행일에 청산 후 즉시 재매수되었습니다")


def check_missing_open_keeps_state(price, index_df) -> None:
    dates = list(index_df["date"])
    _, trades = run(price, index_df, overlay=True)
    if not trades[(trades["date"] == dates[171]) & (trades["ticker"] == DOMESTIC)].empty:
        raise AssertionError("시가 누락일에도 청산이 체결되었습니다")
    later = trades[(trades["date"] == dates[172]) & (trades["ticker"] == DOMESTIC)]
    if later.empty or str(later.iloc[0]["side"]) != "SELL":
        raise AssertionError("시가 누락 다음 거래일에 청산이 재시도되지 않았습니다")


def check_retry_no_same_day_rebuy(price, index_df) -> None:
    """미체결 재시도가 리밸런싱일과 겹쳐도 같은 날 재매수하지 않는다."""
    dates = list(index_df["date"])
    retry_price = build_price_fixture(missing_open_index=180)
    index = index_df.copy()
    index["risk_on"] = True
    index.loc[index.index[179], "risk_on"] = False  # 179 신호 → 180 시가 누락
    _, trades = run(retry_price, index, overlay=True)
    day = trades[trades["date"] == dates[181]]
    sells = day[(day["ticker"] == DOMESTIC) & (day["side"] == "SELL")]
    buys = day[(day["ticker"] == DOMESTIC) & (day["side"] == "BUY")]
    if len(sells) != 1:
        raise AssertionError(f"재시도 청산이 1건이 아닙니다: {len(sells)}")
    if not buys.empty:
        raise AssertionError("청산 당일 리밸런싱에서 즉시 재매수되었습니다")


def check_pending_exit_survives_risk_on(price, index_df) -> None:
    """시가 누락으로 미체결된 청산 의도는 신호가 risk_on으로 돌아와도 유지된다."""
    dates = list(index_df["date"])
    index = index_df.copy()
    index["risk_on"] = True
    index.loc[index.index[170], "risk_on"] = False  # 신호일 170, 실행일 171 시가 누락
    _, trades = run(price, index, overlay=True)
    on_171 = trades[(trades["date"] == dates[171]) & (trades["ticker"] == DOMESTIC)]
    if not on_171.empty:
        raise AssertionError("시가 누락일(171)에 청산이 체결되었습니다")
    on_172 = trades[(trades["date"] == dates[172]) & (trades["ticker"] == DOMESTIC)]
    if len(on_172) != 1 or str(on_172.iloc[0]["side"]) != "SELL":
        raise AssertionError("risk_on 복귀 후에도 미체결 청산 의도가 재시도되지 않았습니다")
    if str(on_172.iloc[0]["reason"]) != "ETF_DAILY_RISK_OFF":
        raise AssertionError(f"재시도 사유가 다릅니다: {on_172.iloc[0]['reason']}")
    origin = pd.Timestamp(on_172.iloc[0].get("exit_origin_date"))
    if origin != pd.Timestamp(dates[170]):
        raise AssertionError(f"청산 원 신호일이 기록되지 않았습니다: {origin}")


def check_retry_event_attribution(price, index_df) -> None:
    """장기 재시도 청산도 원 신호일 기준으로 1개 이벤트로 집계된다."""
    dates = pd.DatetimeIndex(sorted(price["date"].unique()))
    labels = pd.DataFrame({"risk": "risk_on"}, index=dates)
    trades = pd.DataFrame(
        [
            {
                "date": dates[50],
                "ticker": DOMESTIC,
                "side": "SELL",
                "reason": "ETF_DAILY_RISK_OFF",
                "qty": 1,
                "price": 100.0,
                "exit_origin_date": dates[38].strftime("%Y-%m-%d"),
            }
        ]
    )
    events = runner.exit_events(
        trades, "ETF_DAILY_RISK_OFF", price, list(dates), labels, risk_based=True
    )
    if len(events) != 1:
        raise AssertionError(f"이벤트 프레임 생성 실패: {len(events)}")
    row = events.iloc[0]
    if pd.Timestamp(row["event_id"]) != pd.Timestamp(dates[38]):
        raise AssertionError(f"이벤트 키가 원 신호일이 아닙니다: {row['event_id']}")
    if int(row["lag_days"]) != 12:
        raise AssertionError(f"지연일 계산이 다릅니다: {row['lag_days']}")


def check_oos_boundary_capture() -> None:
    """OOS 첫 거래일 수익률·낙폭이 직전 평가액 기준으로 포함된다."""
    dates = pd.bdate_range("2024-01-01", periods=5)
    curve = pd.DataFrame({"date": dates, "equity": [1.0, 1.0, 0.9, 0.9, 0.9]})
    stats = runner.slice_stats(curve, dates[2], None)
    if stats["total_return"] is None or abs(stats["total_return"] + 0.1) > 1e-12:
        raise AssertionError(f"OOS 첫날 수익률이 누락되었습니다: {stats}")
    if abs(stats["mdd"] + 0.1) > 1e-12:
        raise AssertionError(f"OOS 첫날 낙폭이 누락되었습니다: {stats}")
    if stats["days"] != 3:
        raise AssertionError(f"OOS 일수 계산이 다릅니다: {stats['days']}")


def check_overlay_support_guards(price, index_df) -> None:
    """legacy 외 조합은 명시적으로 차단된다."""
    dates = [pd.Timestamp(d) for d in index_df["date"]]
    base = {
        "initial_cash": INITIAL_CASH,
        "common_dates": dates,
        "index_df": index_df,
        "price_data": price,
        "daily_risk_off_exit": True,
    }
    cases = (
        ("ohlcv_capacity", {"execution_mode": "ohlcv_capacity"}),
        ("approval_strict", {"approval_strict": True}),
        ("initial_state", {"initial_state": {"cash": INITIAL_CASH, "holdings": {}}}),
    )
    for label, extra in cases:
        try:
            rtb.run_etf_strategy(**base, **extra)
        except ValueError as exc:
            if "daily_risk_off_exit" not in str(exc):
                raise AssertionError(f"{label}: 다른 오류가 발생했습니다: {exc}") from exc
        else:
            raise AssertionError(f"{label}: 차단되지 않았습니다")


def main() -> int:
    price = build_price_fixture()
    index_df = build_index_fixture(pd.DatetimeIndex(sorted(price["date"].unique())))
    original_groups = dict(etf_shared.ETF_TICKER_GROUPS)
    original_override = set(etf_shared.GROUP_RISK_OVERRIDE)
    etf_shared.ETF_TICKER_GROUPS.clear()
    etf_shared.ETF_TICKER_GROUPS.update(
        {DOMESTIC: "domestic_equity", FOREIGN: "foreign_investment"}
    )
    etf_shared.GROUP_RISK_OVERRIDE.clear()
    etf_shared.GROUP_RISK_OVERRIDE.update({"foreign_investment", "commodity"})
    checks = [
        ("default off", lambda: check_default_is_unchanged(price, index_df)),
        ("daily risk_off exit (t+1)", lambda: check_daily_exit_next_open(price, index_df)),
        ("no duplicate on rebalance", lambda: check_no_duplicate_on_rebalance_day(price, index_df)),
        ("missing open state", lambda: check_missing_open_keeps_state(price, index_df)),
        (
            "pending exit survives risk_on",
            lambda: check_pending_exit_survives_risk_on(price, index_df),
        ),
        ("retry no same-day rebuy", lambda: check_retry_no_same_day_rebuy(price, index_df)),
        ("retry event attribution", lambda: check_retry_event_attribution(price, index_df)),
        ("oos boundary capture", check_oos_boundary_capture),
        (
            "overlay support guards",
            lambda: check_overlay_support_guards(price, index_df),
        ),
    ]
    failures = 0
    try:
        for label, check in checks:
            try:
                check()
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {label}: {exc}")
            else:
                print(f"PASS {label}")
    finally:
        etf_shared.ETF_TICKER_GROUPS.clear()
        etf_shared.ETF_TICKER_GROUPS.update(original_groups)
        etf_shared.GROUP_RISK_OVERRIDE.clear()
        etf_shared.GROUP_RISK_OVERRIDE.update(original_override)
    if failures:
        print(f"\nrisk overlay checks FAILED ({failures}건)")
        return 1
    print("\nrisk overlay checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
