

"""
ETF 드라이런/라이브 실행용 키움 REST 어댑터.

지원 기능:
- get_cash()
- get_holdings()
- get_prices()
- place_order()
- get_order_status()
- cancel_order()

필수 환경 변수(보통 `.env`에 설정):
- KIWOOM_BASE_URL
- KIWOOM_APPKEY
- KIWOOM_SECRETKEY

선택 환경 변수:
- KIWOOM_ACCOUNT_NO
- KIWOOM_ACCESS_TOKEN
- KIWOOM_TOKEN_URL
- KIWOOM_CASH_ENDPOINT
- KIWOOM_HOLDINGS_ENDPOINT
- KIWOOM_PRICE_ENDPOINT
- KIWOOM_ORDER_ENDPOINT
- KIWOOM_ORDER_STATUS_ENDPOINT
- KIWOOM_ORDER_CANCEL_ENDPOINT

키움 REST 엔드포인트 경로와 JSON 응답 구조는 계좌/API 버전에 따라 다를 수 있으므로,
이 어댑터는 설정 가능한 JSON 경로를 지원한다.
- KIWOOM_CASH_PATH, 기본값: output.deposit
- KIWOOM_HOLDINGS_PATH, 기본값: output
- KIWOOM_HOLDINGS_TICKER_KEY, 기본값: ticker
- KIWOOM_HOLDINGS_QTY_KEY, 기본값: quantity
- KIWOOM_PRICE_PATH, 기본값: output.price
- KIWOOM_ORDER_ID_PATH, 기본값: output.order_id
- KIWOOM_ORDER_FILLED_QTY_PATH, 기본값: output.filled_qty
- KIWOOM_ORDER_QTY_PATH, 기본값: output.order_qty

주문/취소 기본 스펙(키움 REST 문서):
- 매수 api-id: kt10000
- 매도 api-id: kt10001
- 취소 api-id: kt10003
- 주문 바디: dmst_stex_tp, stk_cd, ord_qty, ord_uv(선택), trde_tp, cond_uv(선택)
- 취소 바디: dmst_stex_tp, orig_ord_no, stk_cd, cncl_qty
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(dotenv_path: str | Path | None = None) -> None:
    if dotenv_path is None:
        dotenv_path = PROJECT_ROOT / ".env"
    path = Path(dotenv_path)
    if not path.exists():
        return

    with path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


load_dotenv()


class KiwoomAdapter:
    """ETF 드라이런/하루 1회 라이브 러너에서 사용하는 소형 어댑터."""

    def __init__(self) -> None:
        self.base_url = os.environ.get("KIWOOM_BASE_URL", "").rstrip("/")
        self.app_key = os.environ.get("KIWOOM_APPKEY", "")
        self.secret_key = os.environ.get("KIWOOM_SECRETKEY", "")
        self.account_no = os.environ.get("KIWOOM_ACCOUNT_NO", "")
        self.access_token = os.environ.get("KIWOOM_ACCESS_TOKEN", "")
        self.timeout = float(os.environ.get("KIWOOM_TIMEOUT", "10"))

        if not self.base_url:
            raise RuntimeError("KIWOOM_BASE_URL is required")

        if not self.access_token:
            self.access_token = self._issue_token()

    def _issue_token(self) -> str:
        token_url = os.environ.get("KIWOOM_TOKEN_URL")
        if not token_url:
            raise RuntimeError("KIWOOM_ACCESS_TOKEN or KIWOOM_TOKEN_URL is required")
        if not self.app_key or not self.secret_key:
            raise RuntimeError("KIWOOM_APPKEY and KIWOOM_SECRETKEY are required to issue token")

        payload = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.secret_key,
        }
        response = requests.post(token_url, json=payload, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()

        token = data.get("access_token") or data.get("token") or data.get("accessToken")
        if not token:
            raise RuntimeError(f"Cannot find access token in response: {data}")
        return str(token)

    def _headers(self, api_id: str | None = None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "secretkey": self.secret_key,
        }
        if api_id:
            headers["api-id"] = api_id
        return headers

    def _post(self, endpoint: str, payload: dict[str, Any], api_id: str | None = None) -> dict[str, Any]:
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            url = endpoint
        else:
            url = f"{self.base_url}/{endpoint.lstrip('/')}"

        response = requests.post(url, headers=self._headers(api_id), json=payload, timeout=self.timeout)
        response.raise_for_status()
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON response from {url}: {response.text[:500]}") from exc

    def _get_by_path(self, data: Any, path: str, default: Any = None) -> Any:
        current = data
        for part in path.split("."):
            if part == "":
                continue
            if isinstance(current, dict):
                current = current.get(part, default)
            elif isinstance(current, list):
                try:
                    current = current[int(part)]
                except (ValueError, IndexError):
                    return default
            else:
                return default
        return current

    def _to_number(self, value: Any) -> float:
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().replace(",", "")
        if text.startswith("+"):
            text = text[1:]
        if text == "":
            return 0.0
        return float(text)

    def _resolve_trde_tp(self, order_type: str) -> str:
        """문자열 주문유형을 키움 매매구분 코드로 변환한다."""
        text = str(order_type).strip().upper()
        if text.isdigit():
            return text

        mapping = {
            "LIMIT": "0",
            "MARKET": "3",
            "CONDITIONAL_LIMIT": "5",
            "BEST": "6",
            "PRIORITY": "7",
            "LIMIT_IOC": "10",
            "MARKET_IOC": "13",
            "BEST_IOC": "16",
            "LIMIT_FOK": "20",
            "MARKET_FOK": "23",
            "BEST_FOK": "26",
        }
        return mapping.get(text, "3")

    def get_cash(self) -> float:
        """ETF 매수에 사용할 수 있는 예수금을 반환한다."""
        endpoint = os.environ.get("KIWOOM_CASH_ENDPOINT")
        if not endpoint:
            raise RuntimeError("KIWOOM_CASH_ENDPOINT is required")

        api_id = os.environ.get("KIWOOM_CASH_API_ID")
        payload: dict[str, Any] = {}
        if self.account_no:
            payload["account_no"] = self.account_no
        data = self._post(endpoint, payload, api_id)
        path = os.environ.get("KIWOOM_CASH_PATH", "output.deposit")
        value = self._get_by_path(data, path)
        return self._to_number(value)

    def get_holdings(self) -> dict[str, int]:
        """보유 종목을 ticker -> 수량 형태로 반환한다."""
        endpoint = os.environ.get("KIWOOM_HOLDINGS_ENDPOINT")
        if not endpoint:
            raise RuntimeError("KIWOOM_HOLDINGS_ENDPOINT is required")

        api_id = os.environ.get("KIWOOM_HOLDINGS_API_ID")
        payload: dict[str, Any] = {}
        if self.account_no:
            payload["account_no"] = self.account_no
        data = self._post(endpoint, payload, api_id)

        path = os.environ.get("KIWOOM_HOLDINGS_PATH", "output")
        rows = self._get_by_path(data, path, [])
        if isinstance(rows, dict):
            rows = rows.get("list") or rows.get("items") or rows.get("rows") or []
        if not isinstance(rows, list):
            raise RuntimeError(f"Holdings response path did not resolve to list: path={path}, value={rows}")

        ticker_key = os.environ.get("KIWOOM_HOLDINGS_TICKER_KEY", "ticker")
        qty_key = os.environ.get("KIWOOM_HOLDINGS_QTY_KEY", "quantity")

        holdings: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get(ticker_key, "")).strip()
            if not ticker:
                continue
            qty = int(self._to_number(row.get(qty_key)))
            if qty > 0:
                holdings[ticker] = qty
        return holdings

    def get_prices(self, tickers: list[str]) -> dict[str, float]:
        """최근 체결/참조 가격을 ticker -> 가격 형태로 반환한다."""
        endpoint = os.environ.get("KIWOOM_PRICE_ENDPOINT")
        if not endpoint:
            raise RuntimeError("KIWOOM_PRICE_ENDPOINT is required")

        api_id = os.environ.get("KIWOOM_PRICE_API_ID")
        price_path = os.environ.get("KIWOOM_PRICE_PATH", "output.price")
        ticker_payload_key = os.environ.get("KIWOOM_PRICE_TICKER_KEY", "ticker")

        prices: dict[str, float] = {}
        for ticker in tickers:
            payload = {
                ticker_payload_key: ticker,
            }
            data = self._post(endpoint, payload, api_id)
            value = self._get_by_path(data, price_path)
            price = self._to_number(value)
            if price > 0:
                prices[ticker] = price
        return prices

    def place_order(
        self,
        side: str,
        ticker: str,
        qty: int,
        price: float | None = None,
        order_type: str = "MARKET",
    ) -> dict[str, Any]:
        """주문을 전송하고 원본 응답과 order_id를 함께 반환한다."""
        endpoint = os.environ.get("KIWOOM_ORDER_ENDPOINT", "/api/dostk/ordr")

        side_upper = side.upper()
        if side_upper not in {"BUY", "SELL"}:
            raise ValueError(f"Invalid side: {side}")
        if qty <= 0:
            raise ValueError(f"qty must be positive: {qty}")

        default_api_id = "kt10000" if side_upper == "BUY" else "kt10001"
        side_api_id_key = "KIWOOM_ORDER_BUY_API_ID" if side_upper == "BUY" else "KIWOOM_ORDER_SELL_API_ID"
        api_id = os.environ.get(side_api_id_key) or os.environ.get("KIWOOM_ORDER_API_ID") or default_api_id

        market_code = os.environ.get("KIWOOM_DMST_STEX_TP", "KRX")
        dmst_stex_key = os.environ.get("KIWOOM_ORDER_EXCHANGE_KEY", "dmst_stex_tp")
        ticker_key = os.environ.get("KIWOOM_ORDER_TICKER_KEY", "stk_cd")
        qty_key = os.environ.get("KIWOOM_ORDER_QTY_KEY", "ord_qty")
        price_key = os.environ.get("KIWOOM_ORDER_PRICE_KEY", "ord_uv")
        type_key = os.environ.get("KIWOOM_ORDER_TYPE_KEY", "trde_tp")
        cond_price_key = os.environ.get("KIWOOM_ORDER_COND_PRICE_KEY", "cond_uv")
        account_key = os.environ.get("KIWOOM_ORDER_ACCOUNT_KEY", "account_no")
        cond_price = os.environ.get("KIWOOM_ORDER_COND_UV")

        payload: dict[str, Any] = {
            dmst_stex_key: market_code,
            ticker_key: ticker,
            qty_key: str(int(qty)),
            type_key: self._resolve_trde_tp(order_type),
        }
        if self.account_no:
            payload[account_key] = self.account_no
        if price is not None:
            payload[price_key] = str(price)
        if cond_price:
            payload[cond_price_key] = str(cond_price)

        data = self._post(endpoint, payload, api_id)
        order_id_path = os.environ.get("KIWOOM_ORDER_ID_PATH", "ord_no")
        order_id = self._get_by_path(data, order_id_path)
        if not order_id:
            order_id = (
                data.get("ord_no")
                or data.get("orderId")
                or data.get("order_id")
                or data.get("odno")
                or data.get("id")
                or self._get_by_path(data, "output.ord_no")
                or self._get_by_path(data, "output.odno")
            )

        return {
            "order_id": "" if order_id is None else str(order_id),
            "response": data,
        }

    def get_order_status(self, order_id: str, today: str | None = None) -> dict[str, Any]:
        """계좌별주문체결내역상세(kt00007)로 특정 주문의 체결 상태를 조회한다.

        kt00007 스펙:
        - URL: /api/dostk/acnt
        - 요청 필수: qry_tp, stk_bond_tp, sell_tp, dmst_stex_tp
        - 응답 리스트 키: acnt_ord_cntr_prps_dtl
          - ord_no: 주문번호, cntr_qty: 체결수량, ord_qty: 주문수량, ord_remnq: 주문잔량
        """
        if not order_id:
            raise ValueError("order_id is required")

        endpoint = os.environ.get("KIWOOM_ORDER_STATUS_ENDPOINT", "/api/dostk/acnt")
        api_id = os.environ.get("KIWOOM_ORDER_STATUS_API_ID", "kt00007")
        market_code = os.environ.get("KIWOOM_DMST_STEX_TP", "KRX")

        ord_dt = today or ""
        payload: dict[str, Any] = {
            "ord_dt": ord_dt,
            "qry_tp": os.environ.get("KIWOOM_ORDER_STATUS_QRY_TP", "1"),
            "stk_bond_tp": "1",
            "sell_tp": "0",
            "fr_ord_no": order_id,
            "dmst_stex_tp": market_code,
        }
        if self.account_no:
            account_key = os.environ.get("KIWOOM_ORDER_STATUS_ACCOUNT_KEY", "account_no")
            payload[account_key] = self.account_no

        data = self._post(endpoint, payload, api_id)

        list_key = os.environ.get("KIWOOM_ORDER_STATUS_LIST_KEY", "acnt_ord_cntr_prps_dtl")
        rows = data.get(list_key) or []
        if not isinstance(rows, list):
            rows = []

        # fr_ord_no로 시작하는 첫 번째 레코드 중 ord_no가 일치하는 행을 찾는다.
        # 일치하는 행이 없으면 첫 번째 행을 사용한다.
        target_row: dict[str, Any] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("ord_no", "")).strip() == str(order_id).strip():
                target_row = row
                break
        if not target_row and rows:
            target_row = rows[0] if isinstance(rows[0], dict) else {}

        filled_qty = int(self._to_number(target_row.get("cntr_qty", 0)))
        order_qty = int(self._to_number(target_row.get("ord_qty", 0)))
        remaining_qty = int(self._to_number(target_row.get("ord_remnq", 0)))

        # ord_remnq == 0 이거나 cntr_qty >= ord_qty면 전량체결로 간주
        if order_qty > 0:
            is_filled = remaining_qty == 0 or filled_qty >= order_qty
        else:
            is_filled = False

        return {
            "order_id": order_id,
            "filled_qty": filled_qty,
            "order_qty": order_qty,
            "remaining_qty": remaining_qty,
            "is_filled": is_filled,
            "response": data,
        }

    def cancel_order(self, order_id: str, ticker: str, qty: int | None = None) -> dict[str, Any]:
        """미체결 주문 취소를 전송한다."""
        endpoint = os.environ.get("KIWOOM_ORDER_CANCEL_ENDPOINT", "/api/dostk/ordr")
        if not order_id:
            raise ValueError("order_id is required")
        if not ticker:
            raise ValueError("ticker is required")

        api_id = os.environ.get("KIWOOM_ORDER_CANCEL_API_ID", "kt10003")
        market_code = os.environ.get("KIWOOM_DMST_STEX_TP", "KRX")

        dmst_stex_key = os.environ.get("KIWOOM_ORDER_CANCEL_EXCHANGE_KEY", "dmst_stex_tp")
        order_id_key = os.environ.get("KIWOOM_ORDER_CANCEL_ID_KEY", "orig_ord_no")
        ticker_key = os.environ.get("KIWOOM_ORDER_CANCEL_TICKER_KEY", "stk_cd")
        qty_key = os.environ.get("KIWOOM_ORDER_CANCEL_QTY_KEY", "cncl_qty")
        account_key = os.environ.get("KIWOOM_ORDER_CANCEL_ACCOUNT_KEY", "account_no")

        payload: dict[str, Any] = {
            dmst_stex_key: market_code,
            order_id_key: order_id,
            ticker_key: ticker,
        }
        if qty is None:
            payload[qty_key] = "0"
        elif int(qty) > 0:
            payload[qty_key] = str(int(qty))
        else:
            payload[qty_key] = "0"
        if self.account_no:
            payload[account_key] = self.account_no

        data = self._post(endpoint, payload, api_id)
        cancel_order_id = (
            data.get("ord_no")
            or self._get_by_path(data, "ord_no")
            or self._get_by_path(data, "output.ord_no")
            or ""
        )
        return {
            "order_id": order_id,
            "cancel_order_id": str(cancel_order_id),
            "response": data,
        }