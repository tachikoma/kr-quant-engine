#!/usr/bin/env python3
"""P1-B-2: daily_risk_off_exit 상태 연속성·fold 경계 합성 fixture.

검증 항목
1. 위험 신호 직후 fold 종료(신호일이 경계)
2. 경계 체결일 시가 누락 → 미체결 청산 의도가 fold를 넘어 재시도
3. 미체결 상태에서 risk_on 복귀
4. 재시도와 정기 리밸런싱 일정 충돌(당일 재매수 금지)
5. fold 마지막 결정의 다음 거래일 체결(경계에서 중복·누락 없음)
6. 중복 매도·당일 재매수 방지

그리고 연속 실행과 분할 실행의 일별 평가액·보유수량·현금·체결·비용·최종 상태가 동일해야 한다.

실행::

    uv run python scripts/test_p1b2_state_continuity.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import etf_shared  # noqa: F401 - 엔진 설정 로드 순서 유지
import run_etf_backtest as rtb

DOMESTIC = "900001"
FOREIGN = "900002"
INITIAL_CASH = 1_000_000.0
N_DAYS = 420
WARMUP = max(120, rtb.MARKET_MA_DAYS + rtb.MARKET_SLOPE_DAYS)
STEP = int(rtb.REBALANCE_STEP_DAYS)
MISSING_OPEN_INDEX = 201


def build_price_fixture(missing_open_index: int | None = MISSING_OPEN_INDEX) -> pd.DataFrame:
    dates = pd.bdate_range("2022-01-03", periods=N_DAYS)
    index = np.arange(N_DAYS, dtype=float)
    shock = np.zeros(N_DAYS)
    shock[230:265] = -0.010
    frames = []
    for offset, ticker in enumerate((DOMESTIC, FOREIGN)):
        daily = 0.0016 + 0.0005 * np.sin(index / 8.0 + offset) + shock
        close = 10_000.0 * np.cumprod(1.0 + daily)
        open_ = np.empty(N_DAYS, dtype=float)
        open_[0] = close[0]
        open_[1:] = close[:-1] * (1.0 + (0.002 if offset == 0 else -0.002))
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
    if missing_open_index is not None:
        missing = price["date"].eq(dates[missing_open_index])
        price.loc[missing, "open"] = np.nan
    return price


def build_index_fixture(risk_off_windows: tuple[tuple[int, int], ...]) -> pd.DataFrame:
    dates = pd.bdate_range("2022-01-03", periods=N_DAYS)
    close = 2_500.0 * np.cumprod(np.full(N_DAYS, 1.0003))
    frame = pd.DataFrame({"date": dates, "close": close})
    frame["market_ma"] = frame["close"].rolling(120).mean()
    frame["market_ma_slope"] = frame["market_ma"] - frame["market_ma"].shift(20)
    frame["risk_on"] = True
    for start, end in risk_off_windows:
        frame.loc[frame.index[start : end + 1], "risk_on"] = False
    return frame


def run(
    price: pd.DataFrame,
    index_df: pd.DataFrame,
    dates_sub: list[pd.Timestamp],
    *,
    state: dict | None = None,
    overlay: bool = True,
):
    return rtb.run_etf_strategy(
        INITIAL_CASH,
        dates_sub,
        index_df,
        use_market_filter=True,
        max_positions=2,
        slippage=0.0005,
        risk_off_liquidate=True,
        price_data=price,
        max_asset_pct=0.85,
        daily_risk_off_exit=overlay,
        initial_state=state,
        return_final_state=True,
    )


def segmented_run(
    price: pd.DataFrame,
    index_df: pd.DataFrame,
    full_dates: list[pd.Timestamp],
    boundaries: list[pd.Timestamp],
    *,
    overlay: bool,
) -> tuple[pd.DataFrame, pd.DataFrame, dict, list[dict]]:
    """fold 경계마다 상태를 이월하며 분할 실행한다(경계일은 다음 세그먼트가 재처리)."""
    index = {pd.Timestamp(day): position for position, day in enumerate(full_dates)}
    starts = [full_dates[0]] + list(boundaries)
    ends = list(boundaries) + [full_dates[-1]]
    curves, trades_list, segments = [], [], []
    state: dict | None = None
    for start, end in zip(starts, ends, strict=True):
        sub = [day for day in full_dates if start <= day <= end]
        if state is not None:
            state = dict(state)
            start_index = index[pd.Timestamp(sub[0])]
            state["rebalance_phase_offset"] = (start_index - WARMUP) % STEP
            state["exit_phase_offset"] = (start_index - WARMUP) % STEP
        curve, trades, state = run(price, index_df, sub, state=state, overlay=overlay)
        curves.append(curve)
        trades_list.append(trades)
        segments.append({"start": str(pd.Timestamp(start).date()), "end": str(pd.Timestamp(end).date())})
    joined_curve = pd.concat(curves, ignore_index=True).drop_duplicates("date")
    joined_curve["date"] = pd.to_datetime(joined_curve["date"])
    joined_trades = (
        pd.concat(trades_list, ignore_index=True) if trades_list else pd.DataFrame()
    )
    return joined_curve, joined_trades, state, segments


def trade_key(trades: pd.DataFrame) -> list[tuple]:
    if trades.empty:
        return []
    columns = [
        "date",
        "ticker",
        "side",
        "qty",
        "price",
        "cash_flow",
        "reason",
        "exit_origin_date",
    ]
    frame = trades[[column for column in columns if column in trades.columns]].copy()
    return sorted(map(tuple, frame.astype(str).values.tolist()))


def assert_continuous_matches_split(
    price: pd.DataFrame,
    index_df: pd.DataFrame,
    boundaries: list[pd.Timestamp],
    *,
    overlay: bool,
    label: str,
) -> tuple[pd.DataFrame, dict]:
    full_dates = [pd.Timestamp(day) for day in index_df["date"]]
    continuous_curve, continuous_trades, continuous_state = run(
        price, index_df, full_dates, overlay=overlay
    )
    split_curve, split_trades, split_state, _ = segmented_run(
        price, index_df, full_dates, boundaries, overlay=overlay
    )
    merged = continuous_curve.merge(
        split_curve, on="date", suffixes=("_full", "_split"), how="outer", indicator=True
    )
    if (merged["_merge"] != "both").any():
        raise AssertionError(f"{label}: 세그먼트 병합에 누락/중복 날짜가 있습니다")
    for column in ("equity", "cash", "market_value"):
        diff = float(np.max(np.abs(merged[f"{column}_full"] - merged[f"{column}_split"])))
        if diff > 1e-6:
            raise AssertionError(f"{label}: {column} 불일치 {diff}")
    if (merged["holdings_full"].astype(str) != merged["holdings_split"].astype(str)).any():
        raise AssertionError(f"{label}: 보유 종목 불일치")
    if trade_key(continuous_trades) != trade_key(split_trades):
        raise AssertionError(f"{label}: 체결 내역 불일치")
    # phase offset은 세그먼트 시작점에서 재계산되는 실행 파라미터이므로 상태 비교에서 제외한다.
    for key in ("cash", "holdings", "holding_cost_basis", "holding_peak_closes", "last_valid_closes"):
        if key not in continuous_state:
            continue
        if continuous_state.get(key) != split_state.get(key):
            raise AssertionError(f"{label}: 최종 상태 {key} 불일치")
    if dict(continuous_state.get("pending_daily_risk_off_exits", {})) != dict(
        split_state.get("pending_daily_risk_off_exits", {})
    ):
        raise AssertionError(f"{label}: pending 청산 상태 불일치")
    if abs(
        float(continuous_state["final_equity"]) - float(split_state["final_equity"])
    ) > 1e-6:
        raise AssertionError(f"{label}: 최종 평가액 불일치")
    total_cost_full = float(pd.to_numeric(continuous_trades["cost"], errors="coerce").fillna(0).sum()) if "cost" in continuous_trades else 0.0
    total_cost_split = float(pd.to_numeric(split_trades["cost"], errors="coerce").fillna(0).sum()) if "cost" in split_trades else 0.0
    if abs(total_cost_full - total_cost_split) > 1e-6:
        raise AssertionError(f"{label}: 거래비용 불일치")
    return continuous_curve, continuous_state, split_state


def check_signal_at_boundary(price, index_df) -> None:
    """위험 신호일이 fold 마지막 날이어도 다음 세그먼트가 동일하게 청산한다."""
    risk_off = ((150, 152), (200, 205), (260, 263))
    index = build_index_fixture(risk_off)
    # 경계를 "신호가 처음 발생한 날"로 둔다(구간 중간이 아님).
    boundary_indices = (150, 200)
    for position in boundary_indices:
        if bool(index["risk_on"].iloc[position]):
            raise AssertionError(f"경계 {position}이 risk_off 신호일이 아닙니다")
    boundaries = [pd.Timestamp(index["date"].iloc[position]) for position in boundary_indices]
    _, state, _ = assert_continuous_matches_split(
        price, index, boundaries, overlay=True, label="signal at boundary"
    )
    # 경계 재처리로 pending이 남아있지 않아야 한다(다음 세그먼트가 실행).
    if any(
        pd.Timestamp(origin) <= boundaries[-1]
        for origin in state["pending_daily_risk_off_exits"].values()
    ):
        raise AssertionError("경계 신호의 pending 청산이 실행되지 않았습니다")


def check_missing_open_across_fold(price, index_df) -> None:
    """경계 체결일 시가 누락 시 미체결 청산 의도가 fold를 넘어 재시도되어야 한다."""
    # 신호일 200 → 실행일 201(시가 누락). 경계를 201로 두어 pending이 fold를 넘는다.
    if MISSING_OPEN_INDEX - 1 != 200:
        raise AssertionError("시가 누락일과 신호일 간격이 1거래일이 아닙니다")
    index = build_index_fixture(((200, 200),))
    boundary = pd.Timestamp(index["date"].iloc[MISSING_OPEN_INDEX])
    assert_continuous_matches_split(
        price, index, [boundary], overlay=True, label="missing open across fold"
    )
    full_dates = [pd.Timestamp(day) for day in index["date"]]
    signal_date = str(pd.Timestamp(full_dates[MISSING_OPEN_INDEX - 1]).date())
    position = {pd.Timestamp(day): position for position, day in enumerate(full_dates)}

    # 1) 경계(시가 누락일)에서 끝난 세그먼트의 상태에 pending과 원 신호일이 남아야 한다.
    first_segment = [day for day in full_dates if day <= boundary]
    _, _, state_after_first = run(
        price, index, first_segment, state=None, overlay=True
    )
    pending_after_first = dict(state_after_first.get("pending_daily_risk_off_exits", {}))
    if not pending_after_first:
        raise AssertionError("경계 시가 누락 후 pending 청산 의도가 상태에 없습니다")
    for ticker, origin in pending_after_first.items():
        if str(origin) != signal_date:
            raise AssertionError(f"pending 원 신호일 불일치: {ticker}={origin} != {signal_date}")

    # 2) 다음 세그먼트가 상태를 이어받아 재시도하고, 원 신호일을 체결 기록에 보존한다.
    second_segment = [day for day in full_dates if day >= boundary]
    resumed = dict(state_after_first)
    start_index = position[pd.Timestamp(second_segment[0])]
    resumed["rebalance_phase_offset"] = (start_index - WARMUP) % STEP
    resumed["exit_phase_offset"] = resumed["rebalance_phase_offset"]
    _, resumed_trades, state_after_second = run(
        price, index, second_segment, state=resumed, overlay=True
    )
    resumed_sells = resumed_trades[
        (resumed_trades["side"] == "SELL")
        & (resumed_trades["reason"].astype(str) == "ETF_DAILY_RISK_OFF")
    ]
    if resumed_sells.empty:
        raise AssertionError("분할 실행에서 청산 재시도가 체결되지 않았습니다")
    for origin in resumed_sells["exit_origin_date"].astype(str):
        if origin != signal_date:
            raise AssertionError(f"분할 실행 청산 원 신호일 불일치: {origin} != {signal_date}")
    if state_after_second.get("pending_daily_risk_off_exits"):
        raise AssertionError("재시도 후에도 pending 청산 의도가 남아 있습니다")

    trades = run(price, index, full_dates, overlay=True)[1]
    sells = trades[
        (trades["side"] == "SELL")
        & (trades["reason"].astype(str) == "ETF_DAILY_RISK_OFF")
    ]
    if sells.empty:
        raise AssertionError("시가 누락 이후 재시도 청산이 없습니다")
    if pd.Timestamp(sells["date"].iloc[0]) <= full_dates[MISSING_OPEN_INDEX]:
        raise AssertionError("시가 누락일에도 청산이 체결되었습니다")


def check_pending_survives_risk_on(price, index_df) -> None:
    """미체결 상태에서 risk_on으로 복귀해도 청산 의도가 유지된다."""
    index = build_index_fixture(((200, 200),))
    assert_continuous_matches_split(
        price, index, [pd.Timestamp(index["date"].iloc[203])], overlay=True, label="pending risk_on"
    )
    trades = run(price, index, [pd.Timestamp(day) for day in index["date"]], overlay=True)[1]
    sells = trades[
        (trades["side"] == "SELL")
        & (trades["reason"].astype(str) == "ETF_DAILY_RISK_OFF")
    ]
    if sells.empty:
        raise AssertionError("risk_on 복귀 후에도 재시도 청산이 없습니다")
    origin = pd.Timestamp(sells["exit_origin_date"].iloc[0])
    if origin != pd.Timestamp(index["date"].iloc[200]):
        raise AssertionError(f"원 신호일이 보존되지 않았습니다: {origin}")


def check_retry_vs_rebalance(price, index_df) -> None:
    """재시도 청산일이 정기 리밸런싱 체결일과 겹쳐도 당일 재매수하지 않는다."""
    # 신호일 199 → 실행일 200(시가 누락) → 재시도 201. 200은 리밸런싱 결정일이므로
    # 201은 정기 리밸런싱 체결일과 정확히 겹친다.
    signal_index = WARMUP + STEP * 3 - 1
    missing_index = signal_index + 1
    retry_index = signal_index + 2
    if (missing_index - WARMUP) % STEP != 0:
        raise AssertionError("재시도일이 리밸런싱 체결일과 겹치지 않습니다")
    local_price = build_price_fixture(missing_open_index=missing_index)
    index = build_index_fixture(((signal_index, signal_index),))
    full_dates = [pd.Timestamp(day) for day in index["date"]]
    continuous = run(local_price, index, full_dates, overlay=True)[1]
    sells = continuous[
        (continuous["side"] == "SELL")
        & (continuous["reason"].astype(str) == "ETF_DAILY_RISK_OFF")
    ]
    if sells.empty:
        raise AssertionError("재시도 청산이 없습니다")
    retry_date = pd.Timestamp(sells["date"].iloc[0])
    if retry_date != full_dates[retry_index]:
        raise AssertionError(f"재시도일이 예상({full_dates[retry_index].date()})과 다릅니다: {retry_date.date()}")
    same_day = continuous[
        (continuous["date"] == retry_date) & (continuous["side"] == "BUY")
    ]
    if not same_day.empty:
        raise AssertionError("청산 재시도 당일 재매수가 발생했습니다")
    duplicates = continuous.groupby(["date", "ticker", "side"]).size()
    if (duplicates > 1).any():
        raise AssertionError("같은 날 동일 종목·방향 중복 주문이 있습니다")
    assert_continuous_matches_split(
        local_price, index, [full_dates[missing_index]], overlay=True, label="retry vs rebalance"
    )


def check_boundary_decision_execution(price, index_df) -> None:
    """경계일 결정이 다음 거래일 체결로 이어지고, 분할 실행에서도 동일하다."""
    index = build_index_fixture(())
    full_dates = [pd.Timestamp(day) for day in index["date"]]
    # 결정 격자(index % 20)에 정확히 걸리는 날을 경계로 선택
    boundary_index = WARMUP + 20 * 4
    boundaries = [full_dates[boundary_index]]
    curve, _, _ = assert_continuous_matches_split(
        price, index, boundaries, overlay=False, label="decision at boundary"
    )
    trades = run(price, index, full_dates, overlay=False)[1]
    if trades.empty:
        raise AssertionError("리밸런싱 체결이 없습니다")
    decision_dates = set(pd.to_datetime(trades["date"]))
    if boundaries[0] not in decision_dates and boundaries[0] not in set(pd.to_datetime(curve["date"])):
        raise AssertionError("경계일이 평가/체결 경로에 없습니다")


def check_final_state_roundtrip(price, index_df) -> None:
    """최종 상태 직렬화·복원이 수량·현금·원가·peak·위상을 보존한다."""
    index = build_index_fixture(((200, 205),))
    full_dates = [pd.Timestamp(day) for day in index["date"]]
    state = run(price, index, full_dates, overlay=True)[2]
    for key in (
        "cash",
        "holdings",
        "holding_cost_basis",
        "holding_peak_closes",
        "last_valid_closes",
        "portfolio_peak_equity",
        "rebalance_phase_offset",
        "exit_phase_offset",
        "pending_daily_risk_off_exits",
    ):
        if key not in state:
            raise AssertionError(f"최종 상태에 {key}가 없습니다")
    resumed = run(
        price,
        index,
        full_dates[-5:],
        state={**state, "rebalance_phase_offset": 0, "exit_phase_offset": 0},
        overlay=True,
    )
    if resumed[0].empty:
        raise AssertionError("상태 복원 실행이 평가곡선을 만들지 못했습니다")


def check_guard_backlog(price, index_df) -> None:
    """overlay 비활성 상태에서 pending 청산 상태를 복원하면 거부해야 한다."""
    index = build_index_fixture(((200, 205),))
    full_dates = [pd.Timestamp(day) for day in index["date"]]
    state = run(price, index, full_dates, overlay=True)[2]
    state = {**state, "pending_daily_risk_off_exits": {DOMESTIC: "2022-10-10"}}
    try:
        run(price, index, full_dates[-5:], state=state, overlay=False)
    except ValueError:
        pass
    else:
        raise AssertionError("overlay 비활성 상태에서 pending 복원이 차단되지 않았습니다")
    for bad_origin in ("not-a-date", None, "", "NaT"):
        bad = {**state, "pending_daily_risk_off_exits": {DOMESTIC: bad_origin}}
        try:
            run(price, index, full_dates[-5:], state=bad, overlay=True)
        except (ValueError, TypeError):
            continue
        raise AssertionError(f"잘못된 원 신호일({bad_origin!r})이 차단되지 않았습니다")
    for bad_type in (20221010, 20221010.0, True):
        bad = {**state, "pending_daily_risk_off_exits": {DOMESTIC: bad_type}}
        try:
            run(price, index, full_dates[-5:], state=bad, overlay=True)
        except TypeError:
            continue
        raise AssertionError(f"날짜가 아닌 타입({bad_type!r})이 차단되지 않았습니다")

    # 정상 문자열 날짜는 기존처럼 복원되어야 한다.
    good = {
        **state,
        "pending_daily_risk_off_exits": {DOMESTIC: str(pd.Timestamp(full_dates[200]).date())},
    }
    run(price, index, full_dates[-5:], state=good, overlay=True)


def main() -> int:
    price = build_price_fixture()
    index_df = build_index_fixture(((150, 152), (200, 205), (260, 263)))
    checks = [
        ("continuous vs split", lambda p, i: assert_continuous_matches_split(
            p, i, [pd.Timestamp(i["date"].iloc[160]), pd.Timestamp(i["date"].iloc[210]), pd.Timestamp(i["date"].iloc[270])],
            overlay=True, label="multi-boundary")),
        ("signal at boundary", check_signal_at_boundary),
        ("missing open across fold", check_missing_open_across_fold),
        ("pending survives risk_on", check_pending_survives_risk_on),
        ("retry vs rebalance", check_retry_vs_rebalance),
        ("boundary decision execution", check_boundary_decision_execution),
        ("final state roundtrip", check_final_state_roundtrip),
        ("guard backlog", check_guard_backlog),
    ]
    failures = 0
    for label, check in checks:
        try:
            check(price, index_df)
            print(f"PASS {label}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {label}: {exc}")
    if failures:
        print(f"\n{failures} checks failed")
        return 1
    print("\np1b2 state continuity checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
