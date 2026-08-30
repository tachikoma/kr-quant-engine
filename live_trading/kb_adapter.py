"""
KB증권 REST OPEN API 어댑터 (독립 구현, 스켈레톤).

외부 repo(nkwoo/kbsec-mcp 등)는 읽기 참조만 하고 런타임 의존 없음.
형태만 BrokerProtocol을 준수하며, 구현은 kr-quant-engine이 소유한다.

공식 포탈: https://openapi.kbsec.com
- Base URL (실운영): https://developer.kbsec.com:32484
- 문서: https://openapi.kbsec.com/apidoc_b2c (74개 B2C API)
- 인증: POST /oauth2/token JSON {grant_type, appKey, appSecret} -> Bearer 24h
- 요청: POST /api/v1/{apiId}  {"dataHeader": {ipAddr, macAddr}, "dataBody": {...}}
- 응답: JSON (API별 business code, Token 만료/권한없음/유량제어 등)
- 특이: 매 요청 ipAddr/macAddr 필수, 토큰은 IP/MAC 바인딩 -> 네트워크 변경 시 revoke 필요

환경변수:
  KBSEC_APP_KEY, KBSEC_APP_SECRET (필수)
  KBSEC_BASE_URL (기본 https://developer.kbsec.com:32484)
  KBSEC_TOKEN_CACHE_DIR (기본 runtime_state/.kbsec_token_cache)
  KBSEC_IP_ADDR, KBSEC_MAC_ADDR (주문/조회 시 dataHeader에 사용, 기본 127.0.0.1 / 00-00-00-00-00-00)
  KBSEC_TIMEOUT (기본 10)
  LIVE_ORDER_ENABLED (0이면 주문 차단)

참조 스펙 (kbsec-mcp): spec/source/kbsec-openapi.postman_collection.json
주요 API ID (예시, apidoc_b2c에서 확정 필요):
  계좌: SSQM0004 예수금, SSQM1801 보유주식, SSQM2932 잔고
  시세: IVU10070 호가, SIQM4900 종목정보, IVS11560 차트
  주문: SSAM1802 현금매수, SSAM1801 현금매도, SSAM1805 정정, SSAM1806 취소
  체결: SSQM2341 체결/미체결
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)


class KbAdapter:
    """KB REST OPEN API 어댑터. BrokerProtocol duck-typing 준수."""

    def __init__(self) -> None:
        self.base_url = os.environ.get("KBSEC_BASE_URL", "https://developer.kbsec.com:32484").rstrip("/")
        self.app_key = os.environ.get("KBSEC_APP_KEY", "").strip()
        self.app_secret = os.environ.get("KBSEC_APP_SECRET", "").strip()
        self.timeout = float(os.environ.get("KBSEC_TIMEOUT", "10"))
        self.http_max_retries = int(os.environ.get("KBSEC_HTTP_MAX_RETRIES", "4"))
        self.http_retry_delay = float(os.environ.get("KBSEC_HTTP_RETRY_DELAY", "1.0"))

        # dataHeader용 IP/MAC (KB 특이사항)
        self.ip_addr = os.environ.get("KBSEC_IP_ADDR", "127.0.0.1").strip() or "127.0.0.1"
        self.mac_addr = os.environ.get(
            "KBSEC_MAC_ADDR", "00-00-00-00-00-00"
        ).strip() or "00-00-00-00-00-00"

        self._throttle_lock = threading.Lock()
        self._last_request_ts = 0.0

        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

        self.access_token = ""
        self._token_file: str | None = None
        self._setup_token_file()
        self._issue_token_if_needed()

    # ------------------------------------------------------------------
    # 토큰 (24h, 파일 캐시, IP/MAC 바인딩 주의)
    # ------------------------------------------------------------------

    def _setup_token_file(self) -> None:
        cache_dir = os.environ.get("KBSEC_TOKEN_CACHE_DIR", "").strip()
        if not cache_dir:
            cache_dir = str(Path(__file__).resolve().parents[1] / "runtime_state" / ".kbsec_token_cache")
        os.makedirs(cache_dir, exist_ok=True)
        self._token_file = os.path.join(cache_dir, f"KBSEC{datetime.today().strftime('%Y%m%d')}.json")  # noqa: DTZ002

    def _read_cached_token(self) -> str | None:
        if not self._token_file or not os.path.exists(self._token_file):
            return None
        try:
            with open(self._token_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            token = data.get("token") or data.get("access_token", "")
            valid_date = data.get("valid_date", "")
            if not token or not valid_date:
                return None
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # noqa: DTZ005
            if valid_date > now_str:
                # IP/MAC이 바뀌면 캐시 무효 (KB 특이)
                if data.get("ip_addr") != self.ip_addr or data.get("mac_addr") != self.mac_addr:
                    return None
                return str(token)
            return None
        except Exception:  # noqa: BLE001
            return None

    def _save_token(self, token: str, expires_in: int) -> None:
        if not self._token_file:
            return
        valid_date = (datetime.now() + timedelta(seconds=expires_in)).strftime(  # noqa: DTZ005
            "%Y-%m-%d %H:%M:%S"
        )
        with open(self._token_file, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "token": token,
                    "valid_date": valid_date,
                    "issued_at": datetime.now().isoformat(),  # noqa: DTZ005
                    "ip_addr": self.ip_addr,
                    "mac_addr": self.mac_addr,
                },
                f,
            )

    def _issue_token(self) -> tuple[str, int]:
        if not self.app_key or not self.app_secret:
            raise RuntimeError("KBSEC_APP_KEY and KBSEC_APP_SECRET are required")
        url = f"{self.base_url}/oauth2/token"
        payload = {
            "grant_type": "client_credentials",
            "appKey": self.app_key,
            "appSecret": self.app_secret,
        }
        headers = {"Content-Type": "application/json; charset=UTF-8"}
        for attempt in range(self.http_max_retries + 1):
            self._throttle_request()
            try:
                resp = self._session.post(url, headers=headers, json=payload, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt < self.http_max_retries:
                    delay = min(self.http_retry_delay * (2**attempt), 10.0)
                    logger.debug("[KB][재시도] 네트워크 %s -> %.1fs", type(e).__name__, delay)
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"KB token request failed: {url}") from e

            with self._throttle_lock:
                self._last_request_ts = time.monotonic()
            logger.debug("[KB] POST %s status=%s body=%s", url, resp.status_code, resp.text[:500])
            if resp.status_code == 429 and attempt < self.http_max_retries:
                time.sleep(self.http_retry_delay)
                continue
            if resp.status_code in (500, 502, 503) and attempt < self.http_max_retries:
                time.sleep(min(self.http_retry_delay * (2**attempt), 10.0))
                continue
            if resp.status_code in (400, 401, 403):
                raise RuntimeError(f"KB token issuance failed HTTP {resp.status_code}: {resp.text[:500]}")
            resp.raise_for_status()
            try:
                data = resp.json()
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid JSON from KB token endpoint: {resp.text[:500]}") from exc
            token = data.get("access_token") or data.get("accessToken") or data.get("token")
            if not token:
                raise RuntimeError(f"Cannot find access_token in KB response: {data}")
            expires_in = int(data.get("expires_in", 86400))
            return str(token), expires_in
        raise RuntimeError(f"KB token issuance failed: {url}")

    def _issue_token_if_needed(self) -> None:
        cached = self._read_cached_token()
        if cached:
            self.access_token = cached
            return
        token, expires_in = self._issue_token()
        self._save_token(token, expires_in)
        self.access_token = token

    def _ensure_token_valid(self) -> None:
        if not self.access_token:
            self._issue_token_if_needed()

    def invalidate_token(self) -> None:
        self.access_token = ""
        if self._token_file and os.path.exists(self._token_file):
            try:
                os.remove(self._token_file)  # noqa: PTH107
            except Exception:  # noqa: BLE001, S110
                pass

    # ------------------------------------------------------------------
    # 공통 HTTP (dataHeader/dataBody 래핑)
    # ------------------------------------------------------------------

    def _throttle_request(self) -> None:
        # KB는 유량제어 per-API, 기본 0.1s 간격으로 보수적 스로틀
        min_interval = 0.1
        now = time.monotonic()
        with self._throttle_lock:
            expected = max(self._last_request_ts + min_interval, now)
            self._last_request_ts = expected
        wait = expected - now
        if wait > 0:
            time.sleep(wait)

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json; charset=UTF-8",
            "Authorization": f"Bearer {self.access_token}",
        }

    def _call(self, api_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST /api/v1/{apiId} with dataHeader/dataBody envelope."""
        url = f"{self.base_url}/api/v1/{api_id.lower()}"
        envelope = {
            "dataHeader": {"ipAddr": self.ip_addr, "macAddr": self.mac_addr},
            "dataBody": body,
        }
        auth_retried = False
        for attempt in range(self.http_max_retries + 1):
            self._throttle_request()
            try:
                resp = self._session.post(url, headers=self._headers(), json=envelope, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt < self.http_max_retries:
                    delay = min(self.http_retry_delay * (2**attempt), 10.0)
                    logger.debug("[KB][재시도] 네트워크 %s -> %.1fs", type(e).__name__, delay)
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"KB request failed: {url}") from e

            with self._throttle_lock:
                self._last_request_ts = time.monotonic()
            logger.debug("[KB] POST %s status=%s req=%s", api_id, resp.status_code, body)
            logger.debug("[KB] response: %s", resp.text[:1000])

            if resp.status_code == 429 and attempt < self.http_max_retries:
                time.sleep(self.http_retry_delay)
                continue
            if resp.status_code == 401 and not auth_retried:
                auth_retried = True
                logger.debug("[KB] 401 감지, 토큰 재발급 후 재시도")
                self.invalidate_token()
                self._ensure_token_valid()
                continue
            if resp.status_code in (500, 502, 503) and attempt < self.http_max_retries:
                time.sleep(min(self.http_retry_delay * (2**attempt), 10.0))
                continue

            try:
                data = resp.json()
            except json.JSONDecodeError:
                resp.raise_for_status()
                raise

            # KB 오류는 apidoc_b2c 기준 business code로 판단 (여기선 그대로 반환)
            return data
        raise RuntimeError(f"KB request failed without response: {url}")

    # ------------------------------------------------------------------
    # BrokerProtocol 필수 6개 (스켈레톤)
    # ------------------------------------------------------------------

    def get_cash(self) -> float:
        """예수금. SSQM0004 등에서 조회 (스켈레톤: 필드 매핑 TODO)."""
        # TODO: apidoc_b2c SSQM0004 / SSQM2932 응답 필드 확정 후 파싱
        data = self._call("SSQM0004", {})
        # 후보 탐색
        for key in ("ord_psbl_amt", "dnca", "evlu_amt", "nxdy_excc_amt"):
            if key in data:
                try:
                    return float(str(data[key]).replace(",", ""))
                except ValueError:
                    continue
            # dataBody 래핑된 경우
            body = data.get("dataBody", {}) if isinstance(data.get("dataBody"), dict) else {}
            if key in body:
                try:
                    return float(str(body[key]).replace(",", ""))
                except ValueError:
                    continue
        logger.warning("KB get_cash 필드 미발견: keys=%s", list(data.keys()))
        return 0.0

    def get_holdings(self) -> dict[str, int]:
        data = self._call("SSQM1801", {})
        # 응답이 dataBody에 리스트로 오는 경우 가정
        rows = data.get("dataBody", data)
        if isinstance(rows, dict):
            # 단일 객체 또는 {list: [...]} 형태
            for k in ("list", "items", "rows", "output"):
                if k in rows and isinstance(rows[k], list):
                    rows = rows[k]
                    break
            else:
                rows = [rows] if "pdno" in rows or "stk_cd" in rows else []
        if not isinstance(rows, list):
            return {}
        holdings: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("pdno", "") or row.get("stk_cd", "") or row.get("iem_cd", "")).strip()
            qty_str = str(row.get("hldg_qty", "") or row.get("rmnd_qty", "") or row.get("qty", "0")).strip()
            if not ticker:
                continue
            try:
                qty = int(float(qty_str.replace(",", "")))
            except ValueError:
                qty = 0
            if qty > 0:
                if ticker.startswith("A") and len(ticker) == 7 and ticker[1:].isdigit():
                    ticker = ticker[1:]
                holdings[ticker] = qty
        return holdings

    def get_prices(self, tickers: list[str]) -> dict[str, float]:
        """현재가. SIQM4900/IVU* 등 (스켈레톤: 시세 API 확정 필요)."""
        prices: dict[str, float] = {}
        for t in tickers:
            ticker = str(t).strip()
            if not ticker:
                continue
            try:
                # TODO: apidoc_b2c에서 정확한 시세 API ID 확정 (IVU10070 등)
                data = self._call("SIQM4900", {"is_no": ticker})
                body = data.get("dataBody", data) if isinstance(data.get("dataBody"), dict) else data
                if isinstance(body, dict):
                    for key in ("prpr", "stck_prpr", "cur_prc", "price", "now_prc"):
                        if key in body:
                            try:
                                p = float(str(body[key]).replace(",", "").strip())
                                if p > 0:
                                    prices[ticker] = p
                                    break
                            except ValueError:
                                continue
            except Exception:  # noqa: BLE001, S112
                continue
        return prices

    def get_bid_ask_prices(self, tickers: list[str]) -> dict[str, dict[str, float]]:
        prices = self.get_prices(tickers)
        return {t: {"buy_price": p, "sell_price": p} for t, p in prices.items()}

    def _check_live_enabled(self) -> None:
        if os.environ.get("LIVE_ORDER_ENABLED", "0").strip() != "1":
            raise RuntimeError(
                "LIVE_ORDER_ENABLED != 1: KB 실주문 차단. 모의 환경에서 테스트하세요."
            )

    def place_order(
        self,
        side: str,
        ticker: str,
        qty: int,
        price: float | None = None,
        order_type: str = "LIMIT",
    ) -> dict[str, Any]:
        self._check_live_enabled()
        side_u = side.upper()
        if side_u not in {"BUY", "SELL"}:
            raise ValueError(f"Invalid side: {side}")
        if qty <= 0:
            raise ValueError(f"qty must be positive: {qty}")

        # SSAM1802 매수, SSAM1801 매도 (현금)
        api_id = "SSAM1802" if side_u == "BUY" else "SSAM1801"
        is_market = order_type.upper() == "MARKET"
        body: dict[str, Any] = {
            "is_no": ticker,
            "ord_qty": str(int(qty)),
            # TODO: apidoc_b2c에서 주문 구분/가격 필드명 확정 (ord_prc, ord_uv 등)
        }
        if not is_market and price is not None:
            body["ord_prc"] = str(int(price))
        # 시장가면 가격 미전송

        data = self._call(api_id, body)
        # order_id 추출 후보
        body_resp = data.get("dataBody", data) if isinstance(data.get("dataBody"), dict) else data
        order_id = ""
        if isinstance(body_resp, dict):
            order_id = str(body_resp.get("ord_no", "") or body_resp.get("order_no", "") or "")
        return {"order_id": order_id, "response": data}

    def get_order_status(self, order_id: str, today: str | None = None) -> dict[str, Any]:
        if not order_id:
            raise ValueError("order_id is required")
        try:
            data = self._call("SSQM2341", {"ord_no": str(order_id)})
        except Exception as e:  # noqa: BLE001
            return {
                "order_id": order_id,
                "filled_qty": 0,
                "order_qty": 0,
                "remaining_qty": 0,
                "is_filled": False,
                "is_found": False,
                "response": {"error": str(e)},
            }
        body = data.get("dataBody", data) if isinstance(data.get("dataBody"), dict) else data
        # 리스트/단일 객체 모두 대응
        rows: list[dict[str, Any]] = []
        if isinstance(body, list):
            rows = [r for r in body if isinstance(r, dict)]
        elif isinstance(body, dict):
            # {list: [...]} 또는 단일 행
            found_list = False
            for k in ("list", "items", "rows", "output"):
                if k in body and isinstance(body[k], list):
                    rows = [r for r in body[k] if isinstance(r, dict)]
                    found_list = True
                    break
            if not found_list:
                rows = [body] if "ord_no" in body else []

        target: dict[str, Any] = {}
        for r in rows:
            if str(r.get("ord_no", "")).strip() == str(order_id).strip():
                target = r
                break
        if not target and len(rows) == 1:
            target = rows[0]
        if not target:
            return {
                "order_id": order_id,
                "filled_qty": 0,
                "order_qty": 0,
                "remaining_qty": 0,
                "is_filled": False,
                "is_found": False,
                "response": data,
            }

        def _int(v: Any) -> int:
            try:
                return int(float(str(v).replace(",", "").strip() or 0))
            except ValueError:
                return 0

        order_qty = _int(target.get("ord_qty", 0))
        filled_qty = _int(target.get("ccld_qty", target.get("tot_ccld_qty", 0)))
        remaining_qty = _int(target.get("rmn_qty", target.get("ord_remnq", max(order_qty - filled_qty, 0))))
        is_filled = order_qty > 0 and remaining_qty == 0 and filled_qty >= order_qty
        return {
            "order_id": order_id,
            "filled_qty": filled_qty,
            "order_qty": order_qty,
            "remaining_qty": remaining_qty,
            "is_filled": is_filled,
            "is_found": True,
            "response": data,
        }

    def cancel_order(self, order_id: str, ticker: str, qty: int | None = None) -> dict[str, Any]:
        self._check_live_enabled()
        if not order_id:
            raise ValueError("order_id is required")
        if not ticker:
            raise ValueError("ticker is required")
        body: dict[str, Any] = {"orgn_ord_no": str(order_id), "is_no": ticker}
        if qty is not None and int(qty) > 0:
            body["cncl_qty"] = str(int(qty))
        data = self._call("SSAM1806", body)
        body_resp = data.get("dataBody", data) if isinstance(data.get("dataBody"), dict) else data
        cancel_id = ""
        if isinstance(body_resp, dict):
            cancel_id = str(body_resp.get("ord_no", "") or "")
        return {"order_id": order_id, "cancel_order_id": cancel_id, "response": data}

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> KbAdapter:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
