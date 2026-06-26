

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
- ENV_MODE (real / demo, 기본값 real)
- KIWOOM_APPKEY
- KIWOOM_SECRETKEY

선택 환경 변수:
- KIWOOM_ACCOUNT_NO
- KIWOOM_TOKEN_ENDPOINT, 기본값: /oauth2/token
- KIWOOM_TOKEN_API_ID, 기본값: au10001
- KIWOOM_CASH_ENDPOINT, 기본값: /api/dostk/acnt
- KIWOOM_CASH_API_ID, 기본값: kt00001
- KIWOOM_HOLDINGS_ENDPOINT, 기본값: /api/dostk/acnt
- KIWOOM_HOLDINGS_API_ID, 기본값: kt00018
- KIWOOM_PRICE_ENDPOINT, 기본값: /api/dostk/mrkcond
- KIWOOM_PRICE_API_ID, 기본값: ka10004
- KIWOOM_ORDER_ENDPOINT, 기본값: /api/dostk/ordr
- KIWOOM_ORDER_STATUS_ENDPOINT, 기본값: /api/dostk/acnt
- KIWOOM_ORDER_CANCEL_ENDPOINT, 기본값: /api/dostk/ordr

키움 REST 엔드포인트 경로와 JSON 응답 구조는 계좌/API 버전에 따라 다를 수 있으므로,
이 어댑터는 설정 가능한 JSON 경로를 지원한다.
- KIWOOM_CASH_PATH, 기본값: ord_alow_amt
- KIWOOM_HOLDINGS_PATH, 기본값: acnt_evlt_remn_indv_tot
- KIWOOM_HOLDINGS_TICKER_KEY, 기본값: stk_cd
- KIWOOM_HOLDINGS_QTY_KEY, 기본값: rmnd_qty
- KIWOOM_PRICE_PATH, 기본값: sel_fpr_bid  (ka10004 매도최우선호가)
- KIWOOM_ORDER_ID_PATH, 기본값: ord_no

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
import time
import threading
from typing import Any

import requests
from requests.adapters import HTTPAdapter


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class KiwoomAdapter:
    """ETF 드라이런/하루 1회 라이브 러너에서 사용하는 소형 어댑터."""

    def __init__(self) -> None:
        env_mode = os.environ.get("ENV_MODE", "real").lower()
        if env_mode == "real":
            self.base_url = "https://api.kiwoom.com"
        else:
            self.base_url = "https://mockapi.kiwoom.com"
        self.app_key = os.environ.get("KIWOOM_APPKEY", "")
        self.secret_key = os.environ.get("KIWOOM_SECRETKEY", "")
        self.account_no = os.environ.get("KIWOOM_ACCOUNT_NO", "")
        self.access_token = ""
        self.timeout = float(os.environ.get("KIWOOM_TIMEOUT", "10"))
        self.http_max_retries = int(os.environ.get("KIWOOM_HTTP_MAX_RETRIES", "4"))
        # Rate limit defaults by ENV_MODE: 실전=0.1s(10/sec, 50% margin), 모의=1.0s(1/sec, 40% margin)
        _env_mode = os.environ.get("ENV_MODE", "real").lower()
        _default_interval = 0.1 if _env_mode == "real" else 1.0
        self.http_min_interval = float(os.environ.get("KIWOOM_HTTP_MIN_INTERVAL", str(_default_interval)))
        # Retry delay unified with throttle interval (same value)
        self.http_retry_delay = float(os.environ.get("KIWOOM_HTTP_RETRY_DELAY", str(self.http_min_interval)))
        self.http_debug_response = os.environ.get("KIWOOM_HTTP_DEBUG_RESPONSE", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "on",
        }
        self.http_debug_body = os.environ.get("KIWOOM_HTTP_DEBUG_BODY", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "on",
        }
        self.http_debug_body_limit = int(os.environ.get("KIWOOM_HTTP_DEBUG_BODY_LIMIT", "800"))
        # 스레드 간 호출 간격 예약/동기화를 위한 락과 마지막 예약 타임스탬프
        self._throttle_lock = threading.Lock()
        self._last_request_ts = 0.0

        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

        self.access_token = self._issue_token()

    def _resolve_url(self, endpoint: str) -> str:
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            return endpoint
        return f"{self.base_url}/{endpoint.lstrip('/')}"

    def _issue_token(self) -> str:
        token_endpoint = "/oauth2/token"
        token_api_id = "au10001"
        if not self.app_key or not self.secret_key:
            raise RuntimeError("KIWOOM_APPKEY and KIWOOM_SECRETKEY are required to issue token")

        token_url = self._resolve_url(token_endpoint)
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.secret_key,
        }
        headers = {
            "Content-Type": "application/json; charset=utf-8",
        }
        if token_api_id:
            headers["api-id"] = token_api_id

        # 토큰 발급도 전체 호출 간 딜레이 규칙을 따르도록 한다.
        self._throttle_request()
        response = self._session.post(token_url, headers=headers, json=payload, timeout=self.timeout)
        with self._throttle_lock:
            self._last_request_ts = time.monotonic()
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

    def _throttle_request(self) -> None:
        """스레드 안전하게 요청 시작 시점 간격을 보장한다.

        예약(예약 타임스탬프)을 사용하여 동시에 여러 스레드가 진입해도
        요청 시작 간격이 최소 `http_min_interval` 이상이 되도록 한다.
        """
        if self.http_min_interval <= 0:
            return
        now = time.monotonic()
        with self._throttle_lock:
            # 다음 요청 시작 시각을 예약한다.
            expected = max(self._last_request_ts + self.http_min_interval, now)
            self._last_request_ts = expected
        wait_sec = expected - now
        if wait_sec > 0:
            time.sleep(wait_sec)

    def _retry_delay(self, response: requests.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After", "").strip()
            if retry_after:
                try:
                    return max(0.0, float(retry_after))
                except ValueError:
                    pass
        return max(0.0, self.http_retry_delay)

    def _post(self, endpoint: str, payload: dict[str, Any], api_id: str | None = None) -> dict[str, Any]:
        url = self._resolve_url(endpoint)

        response: requests.Response | None = None
        for attempt in range(self.http_max_retries + 1):
            self._throttle_request()
            try:
                response = self._session.post(url, headers=self._headers(api_id), json=payload, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt < self.http_max_retries:
                    delay = min(self.http_retry_delay * (2 ** attempt), 10.0)
                    if self.http_debug_response:
                        print(
                            f"[HTTP][재시도] 네트워크 오류 ({type(e).__name__}) "
                            f"-> {delay:.1f}초 대기 후 재시도 (attempt {attempt+1}/{self.http_max_retries})"
                        )
                    time.sleep(delay)
                    # retry delay counts toward throttle interval (no separate timestamp update)
                    continue
                raise RuntimeError(f"HTTP request failed (network error): {url}") from e
            # 실제 요청 시작 시각을 최신 값으로 갱신(락으로 동기화)
            with self._throttle_lock:
                self._last_request_ts = time.monotonic()

            if self.http_debug_response:
                print(
                    f"[HTTP] POST {endpoint} api-id={api_id or ''} status={response.status_code} "
                    f"request={payload}"
                )
                if self.http_debug_body:
                    body_text = response.text[: max(self.http_debug_body_limit, 0)]
                    print(f"[HTTP] response(body): {body_text}")

            if response.status_code == 429 and attempt < self.http_max_retries:
                time.sleep(self._retry_delay(response))
                # retry delay counts toward throttle interval (no separate timestamp update)
                continue

            response.raise_for_status()
            try:
                data = response.json()
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid JSON response from {url}: {response.text[:500]}") from exc

            # 키움은 HTTP 200이어도 return_code로 API 제한(예: 5)을 내려줄 수 있다.
            if self._is_api_rate_limited(data) and attempt < self.http_max_retries:
                wait_sec = max(0.0, self.http_retry_delay)
                if self.http_debug_response:
                    code = data.get("return_code")
                    msg = str(data.get("return_msg", "")).strip()
                    print(
                        f"[HTTP][재시도] API 제한 감지(return_code={code}, return_msg={msg}) "
                        f"-> {wait_sec:.1f}초 대기 후 재시도"
                    )
                time.sleep(wait_sec)
                # retry delay counts toward throttle interval (no separate timestamp update)
                continue

            return data

        if response is not None:
            response.raise_for_status()
        raise RuntimeError(f"HTTP request failed without response: {url}")

    def _is_api_rate_limited(self, data: dict[str, Any]) -> bool:
        """키움 API 본문 기준 요청 제한 응답 여부를 판별한다."""
        code = str(data.get("return_code", "")).strip()
        msg = str(data.get("return_msg", "")).strip()

        if code == "5":
            return True
        if "허용된 요청 개수를 초과" in msg:
            return True
        return False

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
        # +/- 부호 제거 (양수로 정규화)
        if text.startswith("+"):
            text = text[1:]
        if text.startswith("-"):
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

    def _normalize_ticker(self, ticker: str) -> str:
        text = str(ticker).strip()
        if os.environ.get("KIWOOM_NORMALIZE_TICKER", "1").strip().lower() in {"1", "true", "yes", "y", "on"}:
            if text.startswith("A") and len(text) == 7 and text[1:].isdigit():
                return text[1:]
        return text

    def _candidate_price_paths(self) -> list[str]:
        """가격 조회 응답에서 사용할 후보 경로 목록을 반환한다."""
        raw = os.environ.get("KIWOOM_PRICE_PATH_CANDIDATES", "").strip()
        if raw:
            return [p.strip() for p in raw.split(",") if p.strip()]

        primary = "sel_fpr_bid"
        # 계좌/환경별 응답 래핑(output/data) 차이를 흡수하기 위한 기본 후보들
        paths = [
            primary,
            f"output.{primary}",
            f"data.{primary}",
            "sel_fpr_bid",
            "buy_fpr_bid",
            "output.sel_fpr_bid",
            "output.buy_fpr_bid",
            "data.sel_fpr_bid",
            "data.buy_fpr_bid",
        ]

        uniq: list[str] = []
        for p in paths:
            if p and p not in uniq:
                uniq.append(p)
        return uniq

    def _candidate_price_tickers(self, ticker: str) -> list[str]:
        """가격 조회 요청 시도용 티커 후보를 반환한다."""
        text = str(ticker).strip()
        if not text:
            return []

        candidates = [text]
        try_a_prefix = os.environ.get("KIWOOM_PRICE_TRY_A_PREFIX", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "on",
        }
        if try_a_prefix and len(text) == 6 and text.isdigit():
            candidates.append(f"A{text}")
        return candidates

    def _candidate_price_paths_for_side(self, side: str) -> list[str]:
        """매수/매도 기준가 조회용 후보 경로를 반환한다.

        NOTE: 매수 시 지불해야 하는 가격 = 매도최우선호가(sel_fpr_bid = ask),
              매도 시 받을 수 있는 가격 = 매수최우선호가(buy_fpr_bid = bid) 이다.
        """
        side_upper = side.upper()
        if side_upper == "BUY":
            raw = os.environ.get("KIWOOM_PRICE_PATH_BUY_CANDIDATES", "").strip()
            if raw:
                return [p.strip() for p in raw.split(",") if p.strip()]
            primary = "sel_fpr_bid"
            defaults = ["sel_fpr_bid", "buy_fpr_bid"]
        else:
            raw = os.environ.get("KIWOOM_PRICE_PATH_SELL_CANDIDATES", "").strip()
            if raw:
                return [p.strip() for p in raw.split(",") if p.strip()]
            primary = "buy_fpr_bid"
            defaults = ["buy_fpr_bid", "sel_fpr_bid"]

        paths = [
            primary,
            f"output.{primary}",
            f"data.{primary}",
        ]
        for key in defaults:
            paths.extend([key, f"output.{key}", f"data.{key}"])

        uniq: list[str] = []
        for p in paths:
            if p and p not in uniq:
                uniq.append(p)
        return uniq

    def _extract_price_from_response(self, data: dict[str, Any], path_candidates: list[str]) -> tuple[float, str | None]:
        """응답에서 가격을 추출하고 사용된 경로를 함께 반환한다."""
        for path in path_candidates:
            value = self._get_by_path(data, path)
            price = self._to_number(value)
            if price > 0:
                return price, path
        return 0.0, None

    def _raise_on_api_error(self, data: dict[str, Any], context: str) -> None:
        """키움 공통 응답(return_code/return_msg) 에러를 명시적으로 처리한다."""
        code = data.get("return_code")
        if code is None:
            return

        code_text = str(code).strip()
        if code_text in {"0", "0000", "OK", "ok"}:
            return

        message = str(data.get("return_msg", "")).strip()
        raise RuntimeError(f"Kiwoom API error ({context}): return_code={code_text}, return_msg={message}")

    # prefix별 qry_tp / dmst_stex_tp 기본값 (.env.sample 기준)
    _DEFAULT_QRY_TP: dict[str, str] = {}
    _DEFAULT_DMST_STEX_TP: dict[str, str] = {
        "KIWOOM_HOLDINGS": "KRX",
    }

    def _build_account_payload(self, prefix: str) -> dict[str, Any]:
        """계좌 조회 공통 파라미터를 환경변수 기반으로 구성한다."""
        payload: dict[str, Any] = {}

        account_key = os.environ.get(f"{prefix}_ACCOUNT_KEY", "account_no")
        if self.account_no:
            payload[account_key] = self.account_no

        default_qry_tp = self._DEFAULT_QRY_TP.get(prefix, "1")
        qry_tp = (
            os.environ.get(f"{prefix}_QRY_TP")
            or os.environ.get("KIWOOM_ACCOUNT_QRY_TP")
            or os.environ.get("KIWOOM_ORDER_STATUS_QRY_TP")
            or default_qry_tp
        )
        if qry_tp:
            payload["qry_tp"] = str(qry_tp)

        stk_bond_tp = os.environ.get(f"{prefix}_STK_BOND_TP") or os.environ.get("KIWOOM_ACCOUNT_STK_BOND_TP")
        if stk_bond_tp:
            payload["stk_bond_tp"] = str(stk_bond_tp)

        sell_tp = os.environ.get(f"{prefix}_SELL_TP") or os.environ.get("KIWOOM_ACCOUNT_SELL_TP")
        if sell_tp:
            payload["sell_tp"] = str(sell_tp)

        # 환경변수 미설정 시 prefix별 기본값 사용(TR마다 필수 여부가 다름)
        default_dmst = self._DEFAULT_DMST_STEX_TP.get(prefix, "")
        dmst_stex_tp = (
            os.environ.get(f"{prefix}_DMST_STEX_TP")
            or os.environ.get("KIWOOM_DMST_STEX_TP")
            or default_dmst
        )
        if dmst_stex_tp:
            payload["dmst_stex_tp"] = str(dmst_stex_tp)

        raw_extra = os.environ.get(f"{prefix}_PAYLOAD_JSON", "").strip()
        if raw_extra:
            try:
                extra = json.loads(raw_extra)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid JSON in {prefix}_PAYLOAD_JSON: {raw_extra}") from exc
            if not isinstance(extra, dict):
                raise RuntimeError(f"{prefix}_PAYLOAD_JSON must be a JSON object")
            payload.update(extra)

        return payload

    def get_cash(self) -> float:
        """ETF 매수에 사용할 수 있는 주문가능 추정예수금을 반환한다."""
        return self.get_available_cash()

    def get_available_cash(self) -> float:
        """추정예수금(D+2 매도대금 포함)을 반환한다. KIS duck-type 호환."""
        endpoint = "/api/dostk/acnt"
        api_id = "kt00001"
        payload = self._build_account_payload("KIWOOM_CASH")
        payload["qry_tp"] = "3"
        data = self._post(endpoint, payload, api_id)
        self._raise_on_api_error(data, context="get_available_cash")
        path = "ord_alow_amt"
        value = self._get_by_path(data, path)
        if value is None:
            raise RuntimeError(f"Available cash response path not found: path={path}, top_keys={list(data.keys())}")
        return self._to_number(value)

    def get_holdings(self) -> dict[str, int]:
        """보유 종목을 ticker -> 수량 형태로 반환한다."""
        endpoint = "/api/dostk/acnt"
        api_id = "kt00018"
        payload = self._build_account_payload("KIWOOM_HOLDINGS")
        data = self._post(endpoint, payload, api_id)
        self._raise_on_api_error(data, context="get_holdings")

        path = "acnt_evlt_remn_indv_tot"
        rows = self._get_by_path(data, path, [])
        if isinstance(rows, dict):
            rows = rows.get("list") or rows.get("items") or rows.get("rows") or []
        if not isinstance(rows, list):
            raise RuntimeError(f"Holdings response path did not resolve to list: path={path}, value={rows}")

        ticker_key = "stk_cd"
        qty_key = "rmnd_qty"

        holdings: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            ticker = self._normalize_ticker(str(row.get(ticker_key, "")).strip())
            if not ticker:
                continue
            qty = int(self._to_number(row.get(qty_key)))
            if qty > 0:
                holdings[ticker] = qty
        return holdings

    def get_prices(self, tickers: list[str]) -> dict[str, float]:
        """최근 체결/참조 가격을 ticker -> 가격 형태로 반환한다."""
        endpoint = "/api/dostk/mrkcond"
        api_id = "ka10004"
        ticker_payload_key = "stk_cd"
        path_candidates = self._candidate_price_paths()

        prices: dict[str, float] = {}
        for ticker in tickers:
            if not str(ticker).strip():
                continue

            last_error: str | None = None
            last_data: dict[str, Any] | None = None
            resolved_price = 0.0
            resolved_path: str | None = None
            used_request_ticker = str(ticker)

            for request_ticker in self._candidate_price_tickers(str(ticker)):
                payload = {
                    ticker_payload_key: request_ticker,
                }
                try:
                    data = self._post(endpoint, payload, api_id)
                    self._raise_on_api_error(data, context=f"get_prices:{request_ticker}")
                    last_data = data
                except Exception as exc:
                    last_error = str(exc)
                    continue

                price, used_path = self._extract_price_from_response(data, path_candidates)
                if price > 0:
                    resolved_price = price
                    resolved_path = used_path
                    used_request_ticker = request_ticker
                    break

            if resolved_price > 0:
                prices[str(ticker)] = resolved_price
        return prices

    def get_bid_ask_prices(self, tickers: list[str]) -> dict[str, dict[str, float]]:
        """종목별 매수/매도 기준가를 함께 반환한다.

        반환 형태:
        {
          "091160": {"buy_price": 12345.0, "sell_price": 12350.0},
          ...
        }
        """
        endpoint = "/api/dostk/mrkcond"
        api_id = "ka10004"
        ticker_payload_key = "stk_cd"
        buy_path_candidates = self._candidate_price_paths_for_side("BUY")
        sell_path_candidates = self._candidate_price_paths_for_side("SELL")

        out: dict[str, dict[str, float]] = {}
        for ticker in tickers:
            ticker_text = str(ticker).strip()
            if not ticker_text:
                continue

            last_error: str | None = None
            last_data: dict[str, Any] | None = None
            buy_price = 0.0
            sell_price = 0.0
            used_buy_path: str | None = None
            used_sell_path: str | None = None
            used_request_ticker = ticker_text

            for request_ticker in self._candidate_price_tickers(ticker_text):
                payload = {ticker_payload_key: request_ticker}
                try:
                    data = self._post(endpoint, payload, api_id)
                    self._raise_on_api_error(data, context=f"get_bid_ask_prices:{request_ticker}")
                    last_data = data
                except Exception as exc:
                    last_error = str(exc)
                    continue

                sell_price, used_sell_path = self._extract_price_from_response(data, sell_path_candidates)
                buy_price, used_buy_path = self._extract_price_from_response(data, buy_path_candidates)
                used_request_ticker = request_ticker

                if sell_price > 0 or buy_price > 0:
                    break

            quote: dict[str, float] = {}
            if buy_price > 0:
                quote["buy_price"] = buy_price
            if sell_price > 0:
                quote["sell_price"] = sell_price

            if quote:
                out[ticker_text] = quote

        return out

    def place_order(
        self,
        side: str,
        ticker: str,
        qty: int,
        price: float | None = None,
        order_type: str = "MARKET",
    ) -> dict[str, Any]:
        """주문을 전송하고 원본 응답과 order_id를 함께 반환한다."""
        endpoint = "/api/dostk/ordr"

        side_upper = side.upper()
        if side_upper not in {"BUY", "SELL"}:
            raise ValueError(f"Invalid side: {side}")
        if qty <= 0:
            raise ValueError(f"qty must be positive: {qty}")

        api_id = "kt10000" if side_upper == "BUY" else "kt10001"
        market_code = os.environ.get("KIWOOM_DMST_STEX_TP", "KRX")
        dmst_stex_key = "dmst_stex_tp"
        ticker_key = "stk_cd"
        qty_key = "ord_qty"
        price_key = "ord_uv"
        type_key = "trde_tp"
        account_key = "account_no"

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

        data = self._post(endpoint, payload, api_id)
        self._raise_on_api_error(data, context=f"place_order:{side_upper}")
        order_id_path = "ord_no"
        order_id = self._get_by_path(data, order_id_path)
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
          - cnfm_qty: 주문 확인/접수 수량이며 체결수량으로 취급하지 않는다.
        """
        if not order_id:
            raise ValueError("order_id is required")

        endpoint = "/api/dostk/acnt"
        api_id = "kt00007"
        market_code = os.environ.get("KIWOOM_DMST_STEX_TP", "KRX")

        ord_dt = today or ""
        payload: dict[str, Any] = {
            "ord_dt": ord_dt,
            "qry_tp": "1",
            "stk_bond_tp": "1",
            "sell_tp": "0",
            "fr_ord_no": order_id,
            "dmst_stex_tp": market_code,
        }
        if self.account_no:
            account_key = "account_no"
            payload[account_key] = self.account_no

        data = self._post(endpoint, payload, api_id)
        self._raise_on_api_error(data, context="get_order_status")

        list_key = "acnt_ord_cntr_prps_dtl"
        rows = self._get_by_path(data, list_key, [])
        if isinstance(rows, dict):
            rows = rows.get("list") or rows.get("items") or rows.get("rows") or []
        if not isinstance(rows, list):
            rows = []

        order_no_key = "ord_no"
        filled_qty_key = "cntr_qty"
        confirm_qty_key = "cnfm_qty"
        order_qty_key = "ord_qty"
        remaining_qty_key = "ord_remnq"

        # 반드시 주문번호가 일치하는 행만 사용한다.
        # 일치 행이 없으면 다른 주문을 오인하지 않도록 미확인 상태로 반환한다.
        target_row: dict[str, Any] = {}
        normalized_order_id = str(order_id).strip()
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get(order_no_key, "")).strip() == normalized_order_id:
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
                "response": data,
            }

        confirm_qty = int(self._to_number(target_row.get(confirm_qty_key, 0)))
        filled_qty = int(self._to_number(target_row.get(filled_qty_key, 0)))

        order_qty = int(self._to_number(target_row.get(order_qty_key, 0)))
        remaining_qty_raw = target_row.get(remaining_qty_key)
        if remaining_qty_raw is None or str(remaining_qty_raw).strip() == "":
            remaining_qty = max(order_qty - filled_qty, 0)
        else:
            remaining_qty = int(self._to_number(remaining_qty_raw))

        # 주문 확인(cnfm_qty)과 실제 체결(cntr_qty)을 구분한다.
        # 잔량이 0이어도 취소/거부 케이스가 있을 수 있으므로 체결수량까지 확인한다.
        is_filled = order_qty > 0 and remaining_qty == 0 and filled_qty >= order_qty

        return {
            "order_id": order_id,
            "filled_qty": filled_qty,
            "confirmed_qty": confirm_qty,
            "order_qty": order_qty,
            "remaining_qty": remaining_qty,
            "is_filled": is_filled,
            "is_found": True,
            "response": data,
        }

    def cancel_order(self, order_id: str, ticker: str, qty: int | None = None) -> dict[str, Any]:
        """미체결 주문 취소를 전송한다."""
        endpoint = "/api/dostk/ordr"
        if not order_id:
            raise ValueError("order_id is required")
        if not ticker:
            raise ValueError("ticker is required")

        api_id = "kt10003"
        market_code = os.environ.get("KIWOOM_DMST_STEX_TP", "KRX")

        dmst_stex_key = "dmst_stex_tp"
        order_id_key = "orig_ord_no"
        ticker_key = "stk_cd"
        qty_key = "cncl_qty"
        account_key = "account_no"

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

    def close(self) -> None:
        """HTTP 세션을 종료합니다."""
        self._session.close()

    def __enter__(self) -> "KiwoomAdapter":
        return self

    def __exit__(self, *args) -> None:
        self.close()
