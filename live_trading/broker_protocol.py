"""
Broker protocol (thin) for ETF daily runner.

독립 소유 프로토콜: trading-bot-kis 등 외부 repo를 런타임에 참조하지 않는다.
형태만 동일하게 유지하고 구현은 각 어댑터가 독립적으로 소유한다.

Runner는 duck-typing + hasattr로 동작하므로 이 프로토콜은 타입 힌트/문서화
목적이며 런타임 강제(ABC)는 하지 않는다. 기존 KIS/KIWOOM 어댑터는 수정 없이
호환되며, 신규 NH/KB 어댑터는 이 시그니처를 준수한다.

환경변수나 배포 경로에 외부 repo 의존을 두지 않는다.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class BrokerProtocol(Protocol):
    """ETF 러너가 기대하는 브로커 인터페이스 (최소 6개 필수)."""

    def get_cash(self) -> float:
        """주문 가능 현금(예수금)을 반환한다."""
        ...

    def get_holdings(self) -> dict[str, int]:
        """보유 종목 ticker -> 수량."""
        ...

    def get_prices(self, tickers: list[str]) -> dict[str, float]:
        """ticker -> 현재가(원)."""
        ...

    def place_order(
        self,
        side: str,
        ticker: str,
        qty: int,
        price: float | None = None,
        order_type: str = "LIMIT",
    ) -> dict[str, Any]:
        """주문 전송. 반환은 반드시 {"order_id": str, "response": ...} 포함."""
        ...

    def get_order_status(
        self, order_id: str, today: str | None = None
    ) -> dict[str, Any]:
        """주문 체결 상태. {filled_qty, remaining_qty, is_filled, is_found} 포함."""
        ...

    def cancel_order(
        self, order_id: str, ticker: str, qty: int | None = None
    ) -> dict[str, Any]:
        """미체결 취소. 반환은 {"order_id": str, "cancel_order_id": str} 권장."""
        ...


# Optional methods (runner가 hasattr로 분기, 구현은 선택):
# - get_bid_ask_prices(tickers: list[str]) -> dict[str, dict[str, float]]
#     {"091160": {"buy_price": ..., "sell_price": ...}}
# - get_buyable_info(ticker: str, price: int) -> dict[str, str]
# - get_available_cash(ticker: str = "", price: int = 0) -> float
