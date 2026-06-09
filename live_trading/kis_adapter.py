"""
ETF 드라이런/라이브 실행용 KIS REST 어댑터.

KiwoomAdapter와 동일한 7개 메서드 인터페이스를 제공:
- get_cash(), get_holdings(), get_prices(), get_bid_ask_prices()
- place_order(), get_order_status(), cancel_order()

환경변수:
  ENV_MODE=real|demo
  KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO, KIS_ACCOUNT_PROD_CD
"""

import os
from datetime import datetime
from typing import Any, Optional

from live_trading.kis import KisApiClient, KisAuthManager, KisApiError

_ORDER_TYPE_MAP = {
    "MARKET": "01",
    "LIMIT": "00",
}


class KisAdapter:
    """ETF 러너에서 사용하는 KIS 어댑터. KiwoomAdapter와 동일한 인터페이스."""

    def __init__(self) -> None:
        self._auth = KisAuthManager()
        self._auth.init()
        env_mode = os.environ.get("ENV_MODE", "real").lower()
        self._api = KisApiClient(self._auth, env_mode)

        self._cano: str = ""
        self._acnt_prdt_cd: str = "01"
        self._parse_account()
        self._check_env_mismatch()

        # place_order 시 저장해두는 메타정보 (cancel_order에서 사용)
        self._order_meta: dict[str, dict[str, str]] = {}

    def _parse_account(self) -> None:
        account = self._auth.account
        parts = account.split("-")
        self._cano = parts[0].strip() if parts else ""
        if len(parts) > 1:
            self._acnt_prdt_cd = parts[1].strip()
        else:
            self._acnt_prdt_cd = self._auth.product_code or "01"

    def _check_env_mismatch(self) -> None:
        """ENV_MODE와 KIS 앱키 환경이 일치하는지 시세조회로 사전 확인."""
        try:
            self._api.get_buyable_cash(
                self._cano, self._acnt_prdt_cd, "069500", 10000
            )
        except KisApiError as e:
            if "EGW02007" in str(e):
                env_mode = os.environ.get("ENV_MODE", "real")
                raise RuntimeError(
                    f"KIS 앱키가 ENV_MODE({env_mode})와 일치하지 않습니다. "
                    "ENV_MODE를 확인하거나 올바른 앱키로 변경하세요."
                ) from e

    # ------------------------------------------------------------------
    # 계좌 조회
    # ------------------------------------------------------------------

    def get_cash(self) -> float:
        """ETF 매수에 사용할 수 있는 예수금을 반환한다."""
        _, output2 = self._api.get_balance(self._cano, self._acnt_prdt_cd)
        if not output2:
            return 0.0
        nxdy_excc_amt = output2[0].get("nxdy_excc_amt", "0")
        return float(nxdy_excc_amt)

    def get_available_cash(self) -> float:
        """KIS inquire-psbl-order API가 직접 계산한 실제 주문가능금액을 반환한다."""
        output = self._api.get_buyable_cash(self._cano, self._acnt_prdt_cd, "069500", 1)
        if not output:
            return 0.0
        return float(output.get("nrcvb_buy_amt", "0"))

    def get_holdings(self) -> dict[str, int]:
        """보유 종목을 ticker -> 수량 형태로 반환한다."""
        output1, _ = self._api.get_balance(self._cano, self._acnt_prdt_cd)
        holdings: dict[str, int] = {}
        for row in output1:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("pdno", "")).strip()
            qty_str = str(row.get("hldg_qty", "0")).strip()
            if not ticker:
                continue
            try:
                qty = int(qty_str)
            except ValueError:
                qty = 0
            if qty > 0:
                holdings[ticker] = qty
        return holdings

    # ------------------------------------------------------------------
    # 시세 조회
    # ------------------------------------------------------------------

    def get_prices(self, tickers: list[str]) -> dict[str, float]:
        """최근 체결/참조 가격을 ticker -> 가격 형태로 반환한다."""
        prices: dict[str, float] = {}
        for ticker in tickers:
            ticker_str = str(ticker).strip()
            if not ticker_str:
                continue
            try:
                output = self._api.get_price(ticker_str)
                price_str = output.get("stck_prpr", "0")
                price = float(price_str)
                if price > 0:
                    prices[ticker_str] = price
            except KisApiError:
                continue
        return prices

    def get_bid_ask_prices(self, tickers: list[str]) -> dict[str, dict[str, float]]:
        """종목별 매수/매도 기준가를 함께 반환한다.

        반환 형태:
          {"091160": {"buy_price": 12345.0, "sell_price": 12350.0}, ...}
        """
        out: dict[str, dict[str, float]] = {}
        for ticker in tickers:
            ticker_str = str(ticker).strip()
            if not ticker_str:
                continue
            try:
                output1, _ = self._api.get_asking_price(ticker_str)
                ask_price_str = output1.get("askp1", "0")  # 매도호가 = 매수 시 가격
                bid_price_str = output1.get("bidp1", "0")  # 매수호가 = 매도 시 가격
                buy_price = float(ask_price_str)
                sell_price = float(bid_price_str)
                quote: dict[str, float] = {}
                if buy_price > 0:
                    quote["buy_price"] = buy_price
                if sell_price > 0:
                    quote["sell_price"] = sell_price
                if quote:
                    out[ticker_str] = quote
            except KisApiError:
                continue
        return out

    # ------------------------------------------------------------------
    # 주문
    # ------------------------------------------------------------------

    def place_order(
        self,
        side: str,
        ticker: str,
        qty: int,
        price: float | None = None,
        order_type: str = "MARKET",
    ) -> dict[str, Any]:
        """주문을 전송하고 원본 응답과 order_id를 함께 반환한다."""
        side_upper = side.upper()
        if side_upper not in {"BUY", "SELL"}:
            raise ValueError(f"Invalid side: {side}")
        if qty <= 0:
            raise ValueError(f"qty must be positive: {qty}")

        kis_order_type = _ORDER_TYPE_MAP.get(order_type.upper(), "01")
        kis_price = int(price) if price is not None else 0

        output = self._api.order_cash(
            cano=self._cano,
            acnt_prdt_cd=self._acnt_prdt_cd,
            side="buy" if side_upper == "BUY" else "sell",
            symbol=ticker,
            qty=qty,
            price=kis_price,
            order_type=kis_order_type,
        )

        order_id = output.get("ODNO", "")
        self._order_meta[order_id] = {
            "krx_fwdg_ord_orgno": output.get("KRX_FWDG_ORD_ORGNO", ""),
        }

        return {
            "order_id": str(order_id),
            "response": output,
        }

    def get_order_status(
        self, order_id: str, today: str | None = None
    ) -> dict[str, Any]:
        """특정 주문의 체결 상태를 조회한다."""
        if not order_id:
            raise ValueError("order_id is required")

        today_dt = today or datetime.today().strftime("%Y%m%d")

        try:
            output1, _ = self._api.get_order_fills(
                cano=self._cano,
                acnt_prdt_cd=self._acnt_prdt_cd,
                start_dt=today_dt,
                end_dt=today_dt,
                order_no=order_id,
            )
        except KisApiError as e:
            return {
                "order_id": order_id,
                "filled_qty": 0,
                "order_qty": 0,
                "remaining_qty": 0,
                "is_filled": False,
                "is_found": False,
                "response": {"error": str(e)},
            }

        target_row: dict[str, Any] = {}
        normalized_order_id = str(order_id).strip()
        for row in output1:
            if not isinstance(row, dict):
                continue
            if str(row.get("odno", "")).strip() == normalized_order_id:
                target_row = row
                break

        if not target_row:
            return {
                "order_id": order_id,
                "filled_qty": 0,
                "order_qty": 0,
                "remaining_qty": 0,
                "is_filled": False,
                "is_found": False,
                "response": output1,
            }

        order_qty = int(target_row.get("ord_qty", 0))
        filled_qty = int(target_row.get("ccld_qty", 0))
        remaining_qty = int(target_row.get("rmn_qty", 0))

        is_filled = remaining_qty == 0 or filled_qty >= order_qty

        return {
            "order_id": order_id,
            "filled_qty": filled_qty,
            "order_qty": order_qty,
            "remaining_qty": remaining_qty,
            "is_filled": is_filled,
            "is_found": True,
            "response": output1,
        }

    def cancel_order(
        self, order_id: str, ticker: str, qty: int | None = None
    ) -> dict[str, Any]:
        """미체결 주문 취소를 전송한다."""
        if not order_id:
            raise ValueError("order_id is required")
        if not ticker:
            raise ValueError("ticker is required")

        meta = self._order_meta.get(order_id, {})
        krx_fwdg_ord_orgno = meta.get("krx_fwdg_ord_orgno", "")

        cancel_qty = int(qty) if qty is not None else 0

        output = self._api.cancel_order(
            cano=self._cano,
            acnt_prdt_cd=self._acnt_prdt_cd,
            krx_fwdg_ord_orgno=krx_fwdg_ord_orgno,
            orgn_odno=order_id,
            ord_dvsn="00",
            ord_qty=cancel_qty,
            ord_unpr=0,
        )

        cancel_order_id = output.get("ODNO", "")
        return {
            "order_id": order_id,
            "cancel_order_id": str(cancel_order_id),
            "response": output,
        }
