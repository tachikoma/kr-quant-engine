"""Pure OHLCV capacity diagnostics for the Phase 3A execution scenario.

This module deliberately does not model an order book or claim historical
executable fills.  Every decision is an ``OHLCV capacity scenario``.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, replace
from enum import Enum
from numbers import Integral
from typing import Any

DEFAULT_MAX_PARTICIPATION = 0.05
DEFAULT_MAX_CARRY_DAYS = 1


class Side(str, Enum):
    """Supported order directions."""

    BUY = "BUY"
    SELL = "SELL"


class DecisionType(str, Enum):
    """Capacity result and diagnostic taxonomy."""

    FULL = "FULL"
    PARTIAL_CARRY = "PARTIAL_CARRY"
    PARTIAL_CANCEL = "PARTIAL_CANCEL"
    ZERO_VOLUME = "ZERO_VOLUME"
    NO_CAPACITY = "NO_CAPACITY"
    MISSING_OPEN = "MISSING_OPEN"
    SUSPENDED = "SUSPENDED"
    POSSIBLE_LIMIT_LOCK = "POSSIBLE_LIMIT_LOCK"
    CARRY_EXPIRED = "CARRY_EXPIRED"
    CARRY_CANCELLED = "CARRY_CANCELLED"
    INVALID_BAR = "INVALID_BAR"


@dataclass(frozen=True)
class OHLCVBar:
    """One next-trading-date OHLCV bar used by the capacity scenario."""

    date: str
    ticker: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    value: float | None = None


@dataclass(frozen=True)
class OrderRequest:
    """A requested order or a remaining carried remainder."""

    date: str
    ticker: str
    side: Side | str
    requested_qty: int
    participation_rate: float = DEFAULT_MAX_PARTICIPATION
    carry_age: int = 0
    max_carry_days: int = DEFAULT_MAX_CARRY_DAYS
    explicit_suspension: bool = False


@dataclass(frozen=True)
class CarryState:
    """Pure state for one remaining quantity carried to a later date."""

    remaining_qty: int
    age: int = 1
    max_carry_days: int = DEFAULT_MAX_CARRY_DAYS
    status: DecisionType | str = DecisionType.PARTIAL_CARRY

    def __post_init__(self) -> None:
        for name, value in (
            ("remaining_qty", self.remaining_qty),
            ("age", self.age),
            ("max_carry_days", self.max_carry_days),
        ):
            if isinstance(value, bool) or not isinstance(value, Integral):
                raise TypeError(f"{name} must be an integer, not bool or float")
        if self.max_carry_days != DEFAULT_MAX_CARRY_DAYS:
            raise ValueError("max_carry_days is fixed at exactly one following date")
        if not isinstance(self.remaining_qty, int) or self.remaining_qty <= 0:
            raise ValueError("remaining_qty must be a positive integer")
        if self.age != DEFAULT_MAX_CARRY_DAYS:
            raise ValueError("carry age must be exactly one")
        status = self.status.value if isinstance(self.status, DecisionType) else str(self.status)
        if status != DecisionType.PARTIAL_CARRY.value:
            raise ValueError("carry status must be PARTIAL_CARRY")
        object.__setattr__(self, "status", status)

    def next_date(self) -> CarryState | None:
        """Return the one eligible next-date state, or ``None`` when expired."""
        return None


@dataclass(frozen=True)
class ExecutionDecision:
    """Deterministic OHLCV capacity result; not an executable-fill assertion."""

    date: str
    ticker: str
    side: str
    decision: DecisionType
    requested_qty: int
    filled_qty: int
    remaining_qty: int
    capacity_qty: int
    bar_volume: float | None
    bar_value: float | None
    close_volume_notional_estimate: float | None
    participation_rate: float
    carry_age: int
    max_carry_days: int
    reason: str
    diagnostic_labels: tuple[str, ...] = ("OHLCV_CAPACITY_SCENARIO",)
    next_carry: CarryState | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe, deterministically shaped mapping."""
        payload = asdict(self)
        payload["decision"] = self.decision.value
        payload["diagnostic_labels"] = list(self.diagnostic_labels)
        if self.next_carry:
            carry = asdict(self.next_carry)
            carry["status"] = self.next_carry.status
            payload["next_carry"] = carry
        else:
            payload["next_carry"] = None
        return payload


def _as_finite(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _finite_number(value: Any) -> bool:
    return _as_finite(value) is not None


def _normalize_side(side: Side | str) -> str:
    value = side.value if isinstance(side, Side) else str(side).strip().upper()
    if value not in {Side.BUY.value, Side.SELL.value}:
        raise ValueError("side must be BUY or SELL")
    return value


def _validate_request(request: OrderRequest) -> tuple[str, float]:
    if not isinstance(request, OrderRequest):
        raise TypeError("request must be an OrderRequest")
    if not isinstance(request.date, str) or not request.date.strip():
        raise ValueError("request date must be a non-empty string")
    if not isinstance(request.ticker, str) or not request.ticker.strip():
        raise ValueError("request ticker must be a non-empty string")
    if isinstance(request.requested_qty, bool) or not isinstance(request.requested_qty, int):
        raise TypeError("requested_qty must be an integer")
    if request.requested_qty < 0:
        raise ValueError("requested_qty must be non-negative")
    try:
        participation_rate = float(request.participation_rate)
    except (TypeError, ValueError) as exc:
        raise ValueError("participation_rate must be finite and between 0 and 1") from exc
    if not math.isfinite(participation_rate) or not 0 <= participation_rate <= 1:
        raise ValueError("participation_rate must be finite and between 0 and 1")
    if isinstance(request.carry_age, bool) or not isinstance(request.carry_age, int):
        raise TypeError("carry_age must be an integer")
    if request.carry_age < 0:
        raise ValueError("carry_age must be non-negative")
    if isinstance(request.max_carry_days, bool) or not isinstance(request.max_carry_days, int):
        raise TypeError("max_carry_days must be an integer")
    if request.max_carry_days != DEFAULT_MAX_CARRY_DAYS:
        raise ValueError("max_carry_days is fixed at exactly one following date")
    if not isinstance(request.explicit_suspension, bool):
        raise TypeError("explicit_suspension must be a bool")
    return _normalize_side(request.side), participation_rate


def _validate_bar(request: OrderRequest, bar: OHLCVBar) -> None:
    if not isinstance(bar.date, str) or not bar.date.strip():
        raise ValueError("bar date must be a non-empty string")
    if not isinstance(bar.ticker, str) or not bar.ticker.strip():
        raise ValueError("bar ticker must be a non-empty string")
    if bar.date != request.date or bar.ticker != request.ticker:
        raise ValueError("request and bar date/ticker must match exactly")
    if bar.open is not None and (_as_finite(bar.open) is None or float(bar.open) <= 0):
        raise ValueError("bar open must be positive finite or None for missing open")
    high = _as_finite(bar.high)
    low = _as_finite(bar.low)
    close = _as_finite(bar.close)
    if high is None or low is None or close is None or min(high, low, close) <= 0:
        raise ValueError("bar high, low, and close must be positive finite values")
    if high < low:
        raise ValueError("bar high must be greater than or equal to low")
    if bar.open is not None:
        open_price = float(bar.open)
        if not low <= open_price <= high:
            raise ValueError("bar open must lie within [low, high]")
    if not low <= close <= high:
        raise ValueError("bar close must lie within [low, high]")
    volume = _as_finite(bar.volume)
    if volume is None or volume < 0:
        raise ValueError("bar volume must be finite and non-negative")
    if bar.value is not None:
        value = _as_finite(bar.value)
        if value is None or value < 0:
            raise ValueError("authoritative bar value must be finite and non-negative")


def _decision(
    request: OrderRequest,
    side: str,
    decision: DecisionType,
    *,
    filled_qty: int = 0,
    capacity_qty: int = 0,
    bar_volume: float | None = None,
    bar_value: float | None = None,
    close_volume_notional_estimate: float | None = None,
    reason: str,
    next_carry: CarryState | None = None,
    labels: tuple[str, ...] = ("OHLCV_CAPACITY_SCENARIO",),
) -> ExecutionDecision:
    remaining = request.requested_qty - filled_qty
    return ExecutionDecision(
        date=request.date,
        ticker=request.ticker,
        side=side,
        decision=decision,
        requested_qty=request.requested_qty,
        filled_qty=filled_qty,
        remaining_qty=remaining,
        capacity_qty=capacity_qty,
        bar_volume=bar_volume,
        bar_value=bar_value,
        close_volume_notional_estimate=close_volume_notional_estimate,
        participation_rate=request.participation_rate,
        carry_age=request.carry_age,
        max_carry_days=request.max_carry_days,
        reason=reason,
        diagnostic_labels=labels,
        next_carry=next_carry,
    )


def decide_execution(request: OrderRequest, bar: OHLCVBar) -> ExecutionDecision:
    """Decide a conservative OHLCV capacity scenario for one trading date."""
    side, participation_rate = _validate_request(request)
    request = replace(request, participation_rate=participation_rate)
    if not isinstance(bar, OHLCVBar):
        raise TypeError("bar must be an OHLCVBar")
    _validate_bar(request, bar)
    raw_volume = _as_finite(bar.volume)
    close_price = _as_finite(bar.close)
    assert raw_volume is not None and close_price is not None
    raw_value = _as_finite(bar.value) if bar.value is not None else None
    close_volume_notional_estimate = close_price * raw_volume

    def make_decision(decision: DecisionType, **kwargs: Any) -> ExecutionDecision:
        kwargs.setdefault("bar_volume", raw_volume)
        kwargs.setdefault("bar_value", raw_value)
        return _decision(
            request,
            side,
            decision,
            close_volume_notional_estimate=close_volume_notional_estimate,
            **kwargs,
        )

    def diagnostic_decision(
        status: DecisionType,
        reason: str,
        extra_labels: tuple[str, ...] = (),
    ) -> ExecutionDecision:
        if request.carry_age == DEFAULT_MAX_CARRY_DAYS:
            return make_decision(
                DecisionType.CARRY_CANCELLED,
                reason=f"final carry attempt cancelled: {reason}",
                labels=(
                    "OHLCV_CAPACITY_SCENARIO",
                    "FINAL_CARRY_CANCEL",
                    status.value,
                    *extra_labels,
                ),
            )
        return make_decision(
            status,
            reason=reason,
            labels=("OHLCV_CAPACITY_SCENARIO", *extra_labels),
        )

    if request.explicit_suspension:
        return diagnostic_decision(
            DecisionType.SUSPENDED,
            "explicit suspension has priority over OHLCV capacity checks",
        )
    if request.carry_age > request.max_carry_days:
        return make_decision(
            DecisionType.CARRY_EXPIRED,
            bar_volume=raw_volume,
            bar_value=raw_value,
            reason="carried remainder exceeded its eligible carry age",
        )
    open_price = _as_finite(bar.open)
    if open_price is None or open_price <= 0:
        return diagnostic_decision(
            DecisionType.MISSING_OPEN,
            "missing next-bar open; no fallback price used",
        )
    high_price = _as_finite(bar.high)
    low_price = _as_finite(bar.low)
    assert high_price is not None and low_price is not None
    prices = (open_price, high_price, low_price, close_price)
    if raw_volume == 0 and len(set(prices)) == 1:
        return diagnostic_decision(
            DecisionType.POSSIBLE_LIMIT_LOCK,
            "equal OHLC and zero volume; possible lock heuristic, not confirmed",
            ("POSSIBLE_LIMIT_LOCK_HEURISTIC",),
        )
    if raw_volume == 0:
        return diagnostic_decision(
            DecisionType.ZERO_VOLUME,
            "positive-price bar has zero volume",
        )

    capacity_qty = math.floor(raw_volume * request.participation_rate)
    if request.requested_qty == 0:
        return make_decision(
            DecisionType.FULL,
            capacity_qty=capacity_qty,
            bar_volume=raw_volume,
            bar_value=raw_value,
            reason="zero requested quantity requires no capacity",
        )
    if capacity_qty <= 0:
        decision = (
            DecisionType.PARTIAL_CANCEL
            if request.carry_age >= request.max_carry_days
            else DecisionType.NO_CAPACITY
        )
        if decision is DecisionType.PARTIAL_CANCEL:
            return make_decision(
                decision,
                capacity_qty=capacity_qty,
                bar_volume=raw_volume,
                bar_value=raw_value,
                reason="final carry attempt has no available capacity; remainder cancelled",
            )
        return diagnostic_decision(
            DecisionType.NO_CAPACITY,
            "participation cap produces no available shares",
        )

    filled_qty = min(request.requested_qty, capacity_qty)
    if filled_qty >= request.requested_qty:
        return make_decision(
            DecisionType.FULL,
            filled_qty=request.requested_qty,
            capacity_qty=capacity_qty,
            bar_volume=raw_volume,
            bar_value=raw_value,
            reason="requested quantity is within the OHLCV participation capacity",
        )
    if request.carry_age < request.max_carry_days:
        next_carry = CarryState(
            request.requested_qty - filled_qty,
            request.carry_age + 1,
            request.max_carry_days,
        )
        decision = DecisionType.PARTIAL_CARRY
        reason = "partial capacity; remainder is eligible on exactly one later date"
    else:
        next_carry = None
        decision = DecisionType.PARTIAL_CANCEL
        reason = "partial capacity on the final eligible carry date; remainder cancelled"
    return make_decision(
        decision,
        filled_qty=filled_qty,
        capacity_qty=capacity_qty,
        bar_volume=raw_volume,
        bar_value=raw_value,
        reason=reason,
        next_carry=next_carry,
    )


def serialize_decision(decision: ExecutionDecision) -> str:
    """Serialize a decision with stable key ordering for diagnostics/tests."""
    return json.dumps(decision.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
