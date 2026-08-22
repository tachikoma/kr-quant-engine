"""Offline regression checks for the Phase 3A OHLCV capacity scenario."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from etf_execution import (
    DEFAULT_MAX_PARTICIPATION,
    CarryState,
    DecisionType,
    OHLCVBar,
    OrderRequest,
    Side,
    decide_execution,
    serialize_decision,
)


def bar(
    *,
    volume: float = 1_000.0,
    open_price: float | None = 10.0,
    high: float = 10.0,
    low: float = 10.0,
    close: float = 10.0,
    value: float | None = None,
) -> OHLCVBar:
    return OHLCVBar("2024-01-02", "000001", open_price, high, low, close, volume, value)


def request(
    *,
    qty: int = 10,
    side: Side | str = Side.BUY,
    age: int = 0,
    max_carry_days: int = 1,
    rate: float = DEFAULT_MAX_PARTICIPATION,
    suspended: bool = False,
) -> OrderRequest:
    return OrderRequest(
        "2024-01-02",
        "000001",
        side,
        qty,
        rate,
        age,
        max_carry_days,
        suspended,
    )


def check_full_buy_sell() -> None:
    for side in (Side.BUY, Side.SELL):
        decision = decide_execution(request(qty=40, side=side), bar())
        assert decision.decision is DecisionType.FULL
        assert decision.filled_qty == 40
        assert decision.remaining_qty == 0
        assert decision.capacity_qty == 50
        assert decision.bar_value is None
        assert decision.close_volume_notional_estimate == 10_000
    explicit = decide_execution(request(qty=40), bar(value=1234.5))
    assert explicit.bar_value == 1234.5
    assert explicit.close_volume_notional_estimate == 10_000


def check_partial_carry_and_cancel() -> None:
    partial = decide_execution(request(qty=120), bar())
    assert partial.decision is DecisionType.PARTIAL_CARRY
    assert partial.filled_qty == 50
    assert partial.remaining_qty == 70
    assert partial.next_carry == CarryState(70, 1, 1)
    assert partial.next_carry is not None and partial.next_carry.next_date() is None

    cancel = decide_execution(request(qty=120, age=1), bar())
    assert cancel.decision is DecisionType.PARTIAL_CANCEL
    assert cancel.filled_qty == 50
    assert cancel.remaining_qty == 70
    expired = decide_execution(request(qty=70, age=2), bar())
    assert expired.decision is DecisionType.CARRY_EXPIRED
    assert expired.filled_qty == 0
    assert expired.remaining_qty == 70


def check_bar_diagnostics() -> None:
    zero_volume = decide_execution(request(), bar(volume=0, high=11.0))
    assert zero_volume.decision is DecisionType.ZERO_VOLUME

    missing_open = decide_execution(request(), bar(open_price=None))
    assert missing_open.decision is DecisionType.MISSING_OPEN

    lock = decide_execution(request(), bar(volume=0))
    assert lock.decision is DecisionType.POSSIBLE_LIMIT_LOCK
    assert "POSSIBLE_LIMIT_LOCK_HEURISTIC" in lock.diagnostic_labels
    assert lock.filled_qty == 0
    assert "not confirmed" in lock.reason

    suspended = decide_execution(request(suspended=True), bar(open_price=None))
    assert suspended.decision is DecisionType.SUSPENDED

    age_zero_diagnostics = (
        decide_execution(request(), bar(volume=0, high=11.0)),
        decide_execution(request(), bar(open_price=None)),
        decide_execution(request(suspended=True), bar()),
        decide_execution(request(), bar(volume=0)),
        decide_execution(request(rate=0.0), bar(volume=1_000.0)),
    )
    for decision in age_zero_diagnostics:
        assert decision.carry_age == 0
        assert decision.filled_qty == 0
        assert decision.remaining_qty == decision.requested_qty
        assert decision.next_carry is None


def check_final_carry_rejections() -> None:
    cases = (
        (decide_execution(request(age=1), bar(volume=0, high=11.0)), DecisionType.ZERO_VOLUME),
        (decide_execution(request(age=1), bar(open_price=None)), DecisionType.MISSING_OPEN),
        (decide_execution(request(age=1, suspended=True), bar()), DecisionType.SUSPENDED),
        (decide_execution(request(age=1), bar(volume=0)), DecisionType.POSSIBLE_LIMIT_LOCK),
    )
    for decision, underlying in cases:
        assert decision.decision is DecisionType.CARRY_CANCELLED
        assert decision.filled_qty == 0
        assert decision.remaining_qty == decision.requested_qty
        assert decision.next_carry is None
        assert "FINAL_CARRY_CANCEL" in decision.diagnostic_labels
        assert underlying.value in decision.diagnostic_labels
        assert "cancelled" in decision.reason


def check_validation() -> None:
    invalid_requests = (
        request(qty=-1),
        request(qty=1.5),  # type: ignore[arg-type]
        request(rate=-0.1),
        request(rate=1.1),
        request(age=-1),
        request(max_carry_days=-1),
        request(side="HOLD"),
    )
    for invalid in invalid_requests:
        try:
            decide_execution(invalid, bar())
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError(f"invalid request was accepted: {invalid}")

    invalid_cases = (
        OrderRequest("", "000001", Side.BUY, 1),
        OrderRequest("2024-01-02", "", Side.BUY, 1),
        OrderRequest(1, "000001", Side.BUY, 1),  # type: ignore[arg-type]
        OrderRequest("2024-01-02", 1, Side.BUY, 1),  # type: ignore[arg-type]
        OrderRequest("2024-01-02", "000001", Side.BUY, 1, explicit_suspension=1),  # type: ignore[arg-type]
        OrderRequest("2024-01-02", "000001", Side.BUY, 1, max_carry_days=0),
        OrderRequest("2024-01-02", "000001", Side.BUY, 1, max_carry_days=2),
    )
    bars = (
        bar(volume=float("nan")),
        bar(value=-1),
        OHLCVBar("", "000001", 10.0, 10.0, 10.0, 10.0, 1000.0),
        OHLCVBar("2024-01-02", "", 10.0, 10.0, 10.0, 10.0, 1000.0),
        OHLCVBar("2024-01-03", "000001", 10.0, 10.0, 10.0, 10.0, 1000.0),
        OHLCVBar("2024-01-02", "000002", 10.0, 10.0, 10.0, 10.0, 1000.0),
        OHLCVBar(1, "000001", 10.0, 10.0, 10.0, 10.0, 1000.0),  # type: ignore[arg-type]
        OHLCVBar("2024-01-02", 1, 10.0, 10.0, 10.0, 10.0, 1000.0),  # type: ignore[arg-type]
        OHLCVBar("2024-01-02", "000001", 10.0, 9.0, 10.0, 10.0, 1000.0),
        OHLCVBar("2024-01-02", "000001", 11.0, 10.0, 9.0, 10.0, 1000.0),
        OHLCVBar("2024-01-02", "000001", 10.0, 10.0, 9.0, 8.0, 1000.0),
    )
    for invalid_request in invalid_cases:
        try:
            decision = decide_execution(invalid_request, bar())
            serialize_decision(decision)
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError(f"invalid request integrity was accepted: {invalid_request}")
    for invalid_bar in bars:
        try:
            decision = decide_execution(request(), invalid_bar)
            serialize_decision(decision)
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError(f"invalid bar integrity was accepted: {invalid_bar}")
    try:
        decide_execution(request(), bar(volume=0, open_price=float("nan")))
    except ValueError:
        pass
    else:
        raise AssertionError("invalid NaN open was accepted")
    invalid_carry_states = (
        lambda: CarryState(True),
        lambda: CarryState(10, age=1.0),  # type: ignore[arg-type]
        lambda: CarryState(10, max_carry_days=1.0),  # type: ignore[arg-type]
        lambda: CarryState(10, age=0),
        lambda: CarryState(10, age=2),
        lambda: CarryState(10, status=DecisionType.FULL),
        lambda: CarryState(0),
        lambda: CarryState(-1),
        lambda: CarryState(10, max_carry_days=2),
    )
    for make_carry in invalid_carry_states:
        try:
            make_carry()
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError("invalid CarryState was accepted")
    valid_carry = CarryState(10, age=1, max_carry_days=1)
    assert valid_carry.remaining_qty == 10
    assert valid_carry.age == 1


def check_deterministic_serialization() -> None:
    decision = decide_execution(request(qty=120, side=Side.SELL), bar())
    first = serialize_decision(decision)
    second = serialize_decision(decision)
    assert first == second
    assert first.index('"decision"') < first.index('"filled_qty"')


def main() -> None:
    check_full_buy_sell()
    check_partial_carry_and_cancel()
    check_bar_diagnostics()
    check_final_carry_rejections()
    check_validation()
    check_deterministic_serialization()
    print("execution realism checks passed")


if __name__ == "__main__":
    main()
