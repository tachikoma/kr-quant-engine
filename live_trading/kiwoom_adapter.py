

"""
ETF 드라이런용 최소 키움 REST 어댑터.

이 어댑터는 의도적으로 읽기 전용 기능부터 지원한다.
- get_cash()
- get_holdings()
- get_prices()

현재 주문 전송 기능은 구현되어 있지 않다.

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

키움 REST 엔드포인트 경로와 JSON 응답 구조는 계좌/API 버전에 따라 다를 수 있으므로,
이 어댑터는 설정 가능한 JSON 경로를 지원한다.
- KIWOOM_CASH_PATH, 기본값: output.deposit
- KIWOOM_HOLDINGS_PATH, 기본값: output
- KIWOOM_HOLDINGS_TICKER_KEY, 기본값: ticker
- KIWOOM_HOLDINGS_QTY_KEY, 기본값: quantity
- KIWOOM_PRICE_PATH, 기본값: output.price
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
    """`etf_dry_run.py`에서 사용하는 소형 읽기 전용 어댑터."""

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