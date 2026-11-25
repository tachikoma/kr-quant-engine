#!/usr/bin/env python3
"""P1-B-1: 리밸런싱 위상·워밍업 override 합성 fixture.

신규 연구용 파라미터만 검증한다.
- ``rebalance_phase_offset`` 기본값 0은 기존 동작과 완전히 동일
- 위상은 ``(i - warmup + phase) % REBALANCE_STEP_DAYS == 0`` 규칙대로 결정일을 이동
- 마지막 거래일 경계(결정일 n-2, 체결일 n-1) 처리
- 위험 관리 검사 주기(``exit_check_days``)는 위상과 독립
- 미지원 조합(strict/capacity/initial_state)은 ValueError로 차단
- 회계 항등식(equity = cash + market_value, 현금 흐름) 유지

실행::

    uv run python scripts/test_p1b1_phase.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import etf_shared  # noqa: F401 - 엔진 설정 로드 순서 유지
import run_etf_backtest as rtb

DOMESTIC = "900001"
FOREIGN = "900002"
INITIAL_CASH = 1_000_000.0
N_DAYS = 320
NATURAL_WARMUP = max(120, rtb.MARKET_MA_DAYS + rtb.MARKET_SLOPE_DAYS)
STEP = int(rtb.REBALANCE_STEP_DAYS)


def build_price_fixture() -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-01", periods=N_DAYS)
    index = np.arange(N_DAYS, dtype=float)
    # 후반부 급락 구간을 넣어 portfolio trailing stop이 실제로 발동하게 한다.
    shock = np.zeros(N_DAYS)
    shock[210:245] = -0.012
    frames = []
    for offset, ticker in enumerate((DOMESTIC, FOREIGN)):
        daily = 0.0018 + 0.0004 * np.sin(index / 9.0 + offset) + shock
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
    return pd.concat(frames, ignore_index=True)


def build_index_fixture() -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-01", periods=N_DAYS)
    close = 2_500.0 * np.cumprod(np.full(N_DAYS, 1.0003))
    frame = pd.DataFrame({"date": dates, "close": close})
    frame["market_ma"] = frame["close"].rolling(120).mean()
    frame["market_ma_slope"] = frame["market_ma"] - frame["market_ma"].shift(20)
    frame["risk_on"] = True
    return frame


def run(
    price: pd.DataFrame,
    index_df: pd.DataFrame,
    *,
    phase: int = 0,
    omit_phase: bool = False,
    exit_check_days: int | None = None,
    portfolio_stop: float | None = None,
    **extra,
):
    dates = [pd.Timestamp(day) for day in index_df["date"]]
    events: list[dict] = []
    phase_kwargs = {} if omit_phase else {"rebalance_phase_offset": phase}
    curve, trades = rtb.run_etf_strategy(
        INITIAL_CASH,
        dates,
        index_df,
        use_market_filter=True,
        max_positions=2,
        slippage=0.0005,
        risk_off_liquidate=True,
        price_data=price,
        max_asset_pct=0.85,
        rebalance_observer=events.append,
        exit_check_days=exit_check_days,
        trailing_stop_pct=0.0 if portfolio_stop is not None else None,
        portfolio_trailing_stop_pct=portfolio_stop,
        **phase_kwargs,
        **extra,
    )
    return curve, trades, events, dates


def decision_indices(events: list[dict], dates: list[pd.Timestamp]) -> list[int]:
    position = {pd.Timestamp(day): index for index, day in enumerate(dates)}
    return [position[pd.Timestamp(event["decision_date"])] for event in events]


def expected_indices(warmup: int, phase: int) -> list[int]:
    return [
        i for i in range(warmup, N_DAYS - 1) if (i - warmup + phase) % STEP == 0
    ]


def check_default_identity(price, index_df) -> None:
    # 한쪽은 엔진 인자를 실제로 생략해 기본값 경로를 비교한다.
    base_curve, base_trades, base_events, _ = run(price, index_df, omit_phase=True)
    explicit_curve, explicit_trades, explicit_events, _ = run(price, index_df, phase=0)
    if not base_curve["equity"].equals(explicit_curve["equity"]):
        raise AssertionError("phase 0·자연 워밍업이 기본 동작과 다릅니다")
    if not base_trades.equals(explicit_trades):
        raise AssertionError("phase 0 체결 내역이 기본 동작과 다릅니다")
    if len(base_events) != len(explicit_events):
        raise AssertionError("phase 0 결정일 수가 다릅니다")


def check_phase_schedule(price, index_df) -> None:
    _, _, _, dates = run(price, index_df)
    for phase in (0, 5, 10, 15):
        _, _, events, _ = run(price, index_df, phase=phase)
        observed = decision_indices(events, dates)
        expected = expected_indices(NATURAL_WARMUP, phase)
        if observed != expected:
            raise AssertionError(
                f"phase {phase} 일정 불일치: observed={observed[:3]}…{observed[-2:]} "
                f"expected={expected[:3]}…{expected[-2:]}"
            )
        if any(day > N_DAYS - 2 for day in observed):
            raise AssertionError(f"phase {phase}: 결정일이 마지막 거래일을 벗어났습니다")
        for index in observed:
            if index + 1 >= N_DAYS:
                raise AssertionError(f"phase {phase}: 체결일이 존재하지 않습니다")


def check_phase_shifts_dates(price, index_df) -> None:
    _, _, _, dates = run(price, index_df)
    schedules = {
        phase: tuple(decision_indices(run(price, index_df, phase=phase)[2], dates))
        for phase in (0, 5, 10, 15)
    }
    if len(set(schedules.values())) != len(schedules):
        raise AssertionError("위상별 결정일 집합이 서로 다르지 않습니다")
    for phase, schedule in schedules.items():
        residues = {(day - NATURAL_WARMUP + phase) % STEP for day in schedule}
        if residues != {0}:
            raise AssertionError(f"phase {phase}: 결정일이 주기 격자에 없습니다 {sorted(residues)}")
        if any(day < NATURAL_WARMUP for day in schedule):
            raise AssertionError(f"phase {phase}: 워밍업 이전 결정일이 있습니다")
    flat = [day for schedule in schedules.values() for day in schedule]
    if len(flat) != len(set(flat)):
        raise AssertionError("위상 간 결정일이 중복됩니다")


def check_exit_grid_independent(price, index_df) -> None:
    grids = {}
    # 위상 1은 검사 주기 5의 배수가 아니므로 결합 결함을 잡을 수 있다.
    for phase in (0, 1):
        _, trades, _, dates = run(
            price,
            index_df,
            phase=phase,
            exit_check_days=5,
            portfolio_stop=0.05,
        )
        sells = trades[
            (trades["side"] == "SELL")
            & trades["reason"].astype(str).str.contains("PORTFOLIO", na=False)
        ]
        if sells.empty:
            raise AssertionError(f"phase {phase}: portfolio trailing stop 청산이 없습니다")
        # 청산은 검사일(t) 결정 → 다음 거래일(t+1) 시가 체결이므로 -1 보정한다.
        grid = {
            (dates.index(pd.Timestamp(day)) - 1 - NATURAL_WARMUP) % 5
            for day in sells["date"]
        }
        grids[phase] = grid
    if grids[0] != {0} or grids[1] != {0}:
        raise AssertionError(f"위험 관리 검사 주기가 위상에 종속됩니다: {grids}")


def check_unblocked_combinations(price, index_df) -> None:
    cases = (
        ({"phase": -1}, "phase 음수"),
        ({"phase": STEP}, "phase 범위"),
        ({"phase": True}, "phase bool"),
    )
    for kwargs, label in cases:
        try:
            run(price, index_df, **kwargs)
        except ValueError:
            continue
        raise AssertionError(f"{label}가 차단되지 않았습니다")

    unsupported = (
        ({"phase": 5, "execution_mode": "ohlcv_capacity"}, "capacity 조합"),
        ({"phase": 5, "approval_strict": True}, "strict 조합"),
        ({"phase": 5, "initial_state": {"cash": INITIAL_CASH}}, "initial_state 조합"),
    )
    for kwargs, label in unsupported:
        try:
            run(price, index_df, **kwargs)
        except ValueError:
            continue
        raise AssertionError(f"{label}가 차단되지 않았습니다")


def check_reconciliation(price, index_df) -> None:
    curve, trades, _, _ = run(price, index_df, phase=5)
    identity = np.allclose(
        curve["equity"].to_numpy(dtype=float),
        (curve["cash"] + curve["market_value"]).to_numpy(dtype=float),
        rtol=0.0,
        atol=1e-6,
    )
    flow = float(pd.to_numeric(trades["cash_flow"], errors="coerce").fillna(0).sum())
    distribution = float(curve.get("distribution_cash", pd.Series(dtype=float)).sum())
    cash_ok = abs(float(curve["cash"].iloc[-1]) - (INITIAL_CASH + flow + distribution)) <= 1e-6
    if not identity or not cash_ok:
        raise AssertionError(f"회계 검산 실패 identity={identity} cash_ok={cash_ok}")


def main() -> int:
    price = build_price_fixture()
    index_df = build_index_fixture()
    checks = [
        ("default identity", check_default_identity),
        ("phase schedule", check_phase_schedule),
        ("phase shifts dates", check_phase_shifts_dates),
        ("exit grid independent", check_exit_grid_independent),
        ("unsupported combinations", check_unblocked_combinations),
        ("accounting reconciliation", check_reconciliation),
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
    print("\np1b1 phase checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
