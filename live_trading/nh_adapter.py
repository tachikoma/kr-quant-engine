"""
NH투자증권 PLUG REST API 어댑터 (독립 구현).

외부 repo(trading-bot-kis, nhplug-sdk 등)는 읽기 참조만 하고
런타임 의존 없이 독립적으로 동작한다. 형태만 BrokerProtocol을 준수.

공식 포탈: https://www.nhplug.com
- Live: https://api.nhplug.com:8443
- Mock: https://moapi.nhplug.com:8443  (토큰 발급은 live만)
- 문서 원천: https://www.nhplug.com/llms.txt, https://www.nhplug.com/openapi-docs/krstock/openapi.json
- SDK 참조: PLUG-OpenAPI/nhplug-sdk (pip install nhplug) - 읽기 전용

인증: POST /oauth2/token (form-encoded) -> Bearer 24h -> ~/.nhplug/token-YYYYMMDD.json 캐시
요청: POST JSON {"Input_0": {...}} -> {"Output_0": ..., "rsp_cd": "00000", "rsp_msg": ...}
성공 판정: rsp_cd 단독이 아니라 Output_* 존재 + rsp_msg 확인 (복수 성공코드)
레이트리밋: 5/s, 초과 429 IGW42902 -> 백오프 (SDK 기본 4/s 스로틀)

환경변수:
  NHPLUG_APP_KEY, NHPLUG_APP_SECRET (필수)
  NHPLUG_BASE_URL (기본 https://api.nhplug.com:8443)
  NHPLUG_AUTH_URL (기본 NHPLUG_BASE_URL과 동일, mock도 live로 토큰 발급)
  NHPLUG_ACCT_NO (필수, 종합매매 계좌 명시 — 자동 구분 불가)
  NHPLUG_TOKEN_CACHE_DIR (기본 ~/.nhplug)
  NHPLUG_RATE_LIMIT (기본 4, 초당 요청)
  NHPLUG_TIMEOUT (기본 10)
  LIVE_ORDER_ENABLED (0이면 주문 차단, etf_daily_runner와 동일)

BrokerProtocol 6개 필수 메서드를 구현하며,
get_bid_ask_prices / get_buyable_info 는 optional로 제공 (runner hasattr 분기).
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

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 실전 API 베이스 (demo 모의에서 quote API를 moapi 대신 직접 라우팅하는 데 사용)
_REAL_BASE_URL = "https://api.nhplug.com:8443"

# 주문 유형 매핑: PLUG nmn_pr_tp_cd (01=보통, 05=시장가 등)
_ORDER_TYPE_TO_NMN: dict[str, str] = {
    "LIMIT": "01",
    "MARKET": "05",
}

# 주문 성공 rsp_cd (복수 성공코드 — 00000 외 00048 등)
_SUCCESS_RSP_CODES = {"00000", "00048"}


class NhApiError(Exception):
    """NH PLUG API 오류 (rsp_cd가 성공코드가 아닌 경우). runner가 exc.msg_cd를 읽는다."""

    def __init__(self, msg_cd: str, msg: str):
        self.msg_cd = msg_cd
        self.msg = msg
        super().__init__(f"[{msg_cd}] {msg}")


def _canon_no(v: Any) -> str:
    """주문 관련 번호의 유일한 정식 표기. ASCII 숫자만 인정하고 앞자리 0을 제거한다.

    충돌 검사, 조회 비교, 취소 요청이 모두 이 값을 쓴다. 인정하지 않는 표기(부호, 전각/비ASCII 숫자,
    밑줄, 공백뿐, 너무 긴 값, 숫자가 아닌 값)는 "" (유효하지 않음)이며 신원 증거로 쓰지 않는다.
    """
    if isinstance(v, bool) or v is None:
        return ""
    if isinstance(v, int):
        t = str(v) if v >= 0 else ""
        return t if t and len(t) <= 18 else ""
    if not isinstance(v, str):
        return ""
    t = v.strip()
    if not t or len(t) > 18 or not (t.isascii() and t.isdigit()):
        return ""
    return t.lstrip("0") or "0"


_MISSING = object()


def _strict_qty(v: Any) -> int | None:
    """수량 파싱. 음이 아닌 정수만 인정하고 그 외(없음/소수/음수/문자/불리언)는 None(알 수 없음).

    소수를 잘라서 받아들이지 않는다 (0.9 -> 0 이 체결/취소 확정으로 이어지는 것을 막는다).
    """
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, float):
        return int(v) if v == v and v not in (float("inf"), float("-inf")) and v.is_integer() and v >= 0 else None
    if isinstance(v, str):
        t = v.replace(",", "").strip()
        if len(t) > 18:  # 수량으로 말이 안 되는 길이(파이썬 정수 변환 한도 보호)
            return None
        return int(t) if t.isascii() and t.isdigit() else None
    return None


def _field_qty(row: dict[str, Any], key: str) -> Any:
    """행에 키가 없으면 _MISSING, 있으면 엄격 파싱 결과(int | None=잘못된 값)."""
    if key not in row:
        return _MISSING
    return _strict_qty(row[key])


class NhAdapter:
    """NH PLUG 어댑터. BrokerProtocol을 duck-typing으로 만족."""

    def __init__(self) -> None:
        # autotrade-basic와 동일 우선순위: NHPLUG_BASE_URL > BROKER_MODE/ENV_MODE > 기본값
        _raw_base = os.environ.get("NHPLUG_BASE_URL", "").strip()
        if _raw_base:
            self.base_url = _raw_base.rstrip("/")
        else:
            try:
                from config_utils import get_mode

                _mode = get_mode(default="real").lower()
            except Exception:
                _mode = (
                    os.environ.get("MODE") or os.environ.get("BROKER_MODE")
                    or os.environ.get("ENV_MODE") or "real"
                ).lower()
            _is_demo = _mode in {"demo", "mock", "paper", "test"}
            self.base_url = (
                "https://moapi.nhplug.com:8443" if _is_demo else "https://api.nhplug.com:8443"
            )
        # 토큰 발급은 항상 live 도메인 (NH 특이사항)
        _raw_auth = os.environ.get("NHPLUG_AUTH_URL", "").strip()
        self.auth_url = (_raw_auth.rstrip("/") if _raw_auth else "https://api.nhplug.com:8443")
        self.app_key = os.environ.get("NHPLUG_APP_KEY", "").strip()
        self.app_secret = os.environ.get("NHPLUG_APP_SECRET", "").strip()
        self.acct_no = os.environ.get("NHPLUG_ACCT_NO", "").strip().replace("-", "")
        if self.acct_no and not (self.acct_no.isdigit() and 8 <= len(self.acct_no) <= 13):
            logger.warning(
                "NHPLUG_ACCT_NO 형식 오류: %r (숫자 8~13자리, 하이픈 허용)", self.acct_no
            )
        self.timeout = float(os.environ.get("NHPLUG_TIMEOUT", "10"))
        self.rate_limit = float(os.environ.get("NHPLUG_RATE_LIMIT", "4"))
        self.http_max_retries = int(os.environ.get("NHPLUG_HTTP_MAX_RETRIES", "4"))
        self.http_retry_delay = float(os.environ.get("NHPLUG_HTTP_RETRY_DELAY", "1.0"))

        # 스로틀
        self._throttle_lock = threading.Lock()
        self._last_request_ts = 0.0

        # 세션
        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

        self.access_token = ""
        self._token_file: str | None = None
        self._iem_names: dict[str, str] = {}
        self._last_balance_rows: list[dict[str, Any]] = []
        self._last_balance_prices: dict[str, float] | None = None

        self._setup_token_file()
        self._issue_token_if_needed()

        # A안: 다계좌 보유 시 자동 선별 불가 — 명시 필수
        if not self.acct_no:
            raise RuntimeError(
                "NHPLUG_ACCT_NO 미설정: NH는 종합매매/선물옵션/해외파생 자동 구분이 불가합니다. "
                "ETF 거래용 종합매매 계좌를 NHPLUG_ACCT_NO에 명시하세요. "
                "demo/real 계좌가 다르므로 nh-demo/nh-real Environment에 각각 설정. "
                "GitHub Actions는 Settings → Environments → nh-demo/nh-real → vars.NHPLUG_ACCT_NO(또는 secrets)에 설정."
            )

    # ------------------------------------------------------------------
    # 토큰 관리 (24h, 파일 캐시, 401시에만 재발급)
    # ------------------------------------------------------------------

    def _setup_token_file(self) -> None:
        cache_dir = os.environ.get("NHPLUG_TOKEN_CACHE_DIR", "").strip()
        if not cache_dir:
            cache_dir = str(Path.home() / ".nhplug")
        os.makedirs(cache_dir, exist_ok=True)
        # 기존 .nhplug/token-* 형태와 호환, 날짜별 파일
        self._token_file = os.path.join(cache_dir, f"token-{datetime.today().strftime('%Y%m%d')}.json")

    def _read_cached_token(self) -> str | None:
        if not self._token_file or not os.path.exists(self._token_file):
            return None
        try:
            with open(self._token_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            valid_date = data.get("valid_date", "")
            token = data.get("token") or data.get("access_token", "")
            if not token or not valid_date:
                return None
            # valid_date: "YYYY-MM-DD HH:MM:SS"
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if valid_date > now_str:
                return str(token)
            return None
        except Exception:
            return None

    def _save_token(self, token: str, expires_in: int) -> None:
        if not self._token_file:
            return
        valid_date = (datetime.now() + timedelta(seconds=expires_in)).strftime("%Y-%m-%d %H:%M:%S")
        with open(self._token_file, "w", encoding="utf-8") as f:
            json.dump(
                {"token": token, "valid_date": valid_date, "issued_at": datetime.now().isoformat()},
                f,
            )

    def _issue_token(self) -> tuple[str, int]:
        if not self.app_key or not self.app_secret:
            raise RuntimeError("NHPLUG_APP_KEY and NHPLUG_APP_SECRET are required")
        url = f"{self.auth_url}/oauth2/token"
        # form-encoded (x-www-form-urlencoded)
        payload = {
            "appkey": self.app_key,
            "appsecretkey": self.app_secret,
            "grant_type": "client_credentials",
            "scope": "oob",
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        for attempt in range(self.http_max_retries + 1):
            self._throttle_request()
            try:
                resp = self._session.post(url, data=payload, headers=headers, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt < self.http_max_retries:
                    delay = min(self.http_retry_delay * (2**attempt), 10.0)
                    logger.debug("[NH][재시도] 네트워크 오류 %s -> %.1fs 대기", type(e).__name__, delay)
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"NH token request failed (network): {url}") from e

            with self._throttle_lock:
                self._last_request_ts = time.monotonic()

            logger.debug("[NH] POST %s status=%s body=%s", url, resp.status_code, resp.text[:500])

            if resp.status_code == 429 and attempt < self.http_max_retries:
                time.sleep(self.http_retry_delay)
                continue
            if resp.status_code in (500, 502, 503) and attempt < self.http_max_retries:
                time.sleep(min(self.http_retry_delay * (2**attempt), 10.0))
                continue
            if resp.status_code in (400, 401, 403):
                raise RuntimeError(f"NH token issuance failed HTTP {resp.status_code}: {resp.text[:500]}")

            resp.raise_for_status()
            try:
                data = resp.json()
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid JSON from NH token endpoint: {resp.text[:500]}") from exc

            token = data.get("access_token") or data.get("accessToken") or data.get("token")
            if not token:
                raise RuntimeError(f"Cannot find access_token in NH response: {data}")
            expires_in = int(data.get("expires_in", 86400))
            return str(token), expires_in

        raise RuntimeError(f"NH token issuance failed without response: {url}")

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
                os.remove(self._token_file)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 공통 HTTP (Input_0 래핑, rsp_cd 체크, 스로틀)
    # ------------------------------------------------------------------

    def _throttle_request(self) -> None:
        if self.rate_limit <= 0:
            return
        min_interval = 1.0 / self.rate_limit
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

    def _is_rate_limited(self, data: dict[str, Any]) -> bool:
        # NH 429: IGW42902 등, rsp_cd 429 계열 또는 메시지
        msg = str(data.get("rsp_msg", "") or data.get("message", ""))
        code = str(data.get("rsp_cd", ""))
        return "IGW42902" in msg or code == "429" or "초과" in msg

    def _is_auth_expired(self, data: dict[str, Any], status_code: int) -> bool:
        if status_code == 401:
            return True
        msg = str(data.get("rsp_msg", "") or data.get("message", ""))
        code = str(data.get("rsp_cd", ""))
        return "IGW40043" in msg or "IGW40043" in code

    @staticmethod
    def _is_igw40023(data: dict[str, Any]) -> bool:
        """모의투자에서 제공하지 않는 API (currentPrice 등)."""
        msg = str(data.get("rsp_msg", "") or data.get("message", ""))
        code = str(data.get("rsp_cd", ""))
        return "IGW40023" in msg or "IGW40023" in code

    def _is_demo_moapi(self) -> bool:
        """demo(모의) 모드인지 — base_url이 moapi 도메인인지로 판단."""
        return "moapi" in self.base_url

    def _call_quote(self, path: str, input_0: dict[str, Any]) -> dict[str, Any]:
        """quote 엔드포인트 호출.

        demo(moapi)에서 quote API(/krstock/quote/v1/*)는 IGW40023(모의투자 미지원)을
        항상 반환하므로, moapi를 먼저 호출하지 않고 동일 토큰으로 실전 API
        (api.nhplug.com)에 직접 라우팅한다. 실전 API가 실패하면(IGW40023/401/네트워크)
        그대로 반환/예외를 던져 호출부(runner의 pykrx fallback)가 이어받도록 한다.
        """
        if self._is_demo_moapi() and path.startswith("/krstock/quote/v1/"):
            logger.info("[NH] demo quote direct to real API: %s", path)
            return self._call(path, input_0, base_url=_REAL_BASE_URL)
        return self._call(path, input_0)

    def _call(
        self, path: str, input_0: dict[str, Any], base_url: str | None = None,
        retry_network: bool = True,
    ) -> dict[str, Any]:
        """POST {base_url}{path} with {"Input_0": ...} envelope."""
        base_url = base_url or self.base_url
        url = f"{base_url}{path}"
        body = {"Input_0": input_0}
        auth_retried = False

        for attempt in range(self.http_max_retries + 1):
            self._throttle_request()
            try:
                resp = self._session.post(url, headers=self._headers(), json=body, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                if retry_network and attempt < self.http_max_retries:
                    delay = min(self.http_retry_delay * (2**attempt), 10.0)
                    logger.debug("[NH][재시도] 네트워크 %s -> %.1fs", type(e).__name__, delay)
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"NH request failed (network): {url}") from e

            with self._throttle_lock:
                self._last_request_ts = time.monotonic()

            logger.debug("[NH] POST %s status=%s req=%s", path, resp.status_code, input_0)
            logger.debug("[NH] response: %s", resp.text[:1000])

            if resp.status_code == 429 and attempt < self.http_max_retries:
                time.sleep(self.http_retry_delay)
                continue

            # 401은 토큰 재발급 1회
            if resp.status_code == 401 and not auth_retried:
                auth_retried = True
                logger.debug("[NH] 401 감지, 토큰 재발급 후 재시도")
                self.invalidate_token()
                self._ensure_token_valid()
                continue

            if resp.status_code in (500, 502, 503) and retry_network and attempt < self.http_max_retries:
                time.sleep(min(self.http_retry_delay * (2**attempt), 10.0))
                continue

            # 200이 아니어도 JSON 바디에 rsp_cd가 있을 수 있음
            try:
                data = resp.json()
            except json.JSONDecodeError:
                resp.raise_for_status()
                raise

            # body-level rate limit / auth
            if self._is_rate_limited(data) and attempt < self.http_max_retries:
                logger.debug("[NH] rate-limit 감지, %.1fs 대기 후 재시도", self.http_retry_delay)
                time.sleep(self.http_retry_delay)
                continue
            if self._is_auth_expired(data, resp.status_code) and not auth_retried:
                auth_retried = True
                self.invalidate_token()
                self._ensure_token_valid()
                continue

            # 성공 판정: Output_* 존재 여부로 판단 (rsp_cd 단독 체크 금지)
            # 에러는 rsp_cd가 00000이 아니면서 Output이 비어있는 경우
            # 여기서는 그대로 반환, 호출부에서 Output_* 확인
            return data

        raise RuntimeError(f"NH request failed without response: {url}")

    def _discover_account_no(self) -> str:
        data = self._call("/n2/acctinfo", {})
        # Output_0이 배열인 특이 케이스
        out = data.get("Output_0", [])
        if isinstance(out, dict):
            out = [out]
        if not isinstance(out, list) or not out:
            raise RuntimeError(f"NH acctinfo Output_0 empty: {data}")

        # 실거래(base_url이 api.*)면 acct_type 01, mock이면 03
        is_mock = "moapi" in self.base_url
        want_type = "03" if is_mock else "01"
        for row in out:
            if str(row.get("acct_type", "")).strip() == want_type:
                acct = str(row.get("acct_no", "")).strip()
                if acct:
                    logger.info("NH acct_no 자동 선택: %s (type=%s)", acct, want_type)
                    return acct
        # fallback: 첫 번째
        acct = str(out[0].get("acct_no", "")).strip()
        if not acct:
            raise RuntimeError(f"NH acct_no not found in acctinfo: {data}")
        logger.warning("NH acct_no fallback 첫 계좌 사용: %s", acct)
        return acct

    # ------------------------------------------------------------------
    # 계좌 조회
    # ------------------------------------------------------------------

    def get_cash(self) -> float:
        """예수금(주문가능 현금). Output_0에서 예수금 필드 파싱."""
        # 2026-08-30 모의 실연동 확정: dca/drn_pbl_amt/orr_pbl_amt1 등이 예수금 (1685원)
        data = self._call("/krstock/inquiry/v1/balance", {"act_no": self.acct_no})
        out0 = data.get("Output_0", {})
        if isinstance(out0, list):
            out0 = out0[0] if out0 else {}
        for key in ("dca", "drn_pbl_amt", "orr_pbl_amt1", "nxt_dd_dca", "ord_psbl_amt", "dnca"):
            if key in out0:
                try:
                    return float(str(out0[key]).replace(",", "").strip() or 0)
                except ValueError:
                    continue
        logger.warning("NH get_cash Output_0에서 예수금 필드 미발견: keys=%s", list(out0.keys()))
        return 0.0

    def _fetch_balance(self) -> None:
        """balance 조회 후 Output_1 행과 now_pr 가격 맵을 캐시한다."""
        data = self._call("/krstock/inquiry/v1/balance", {"act_no": self.acct_no})
        out1 = data.get("Output_1", [])
        if isinstance(out1, dict):
            out1 = [out1]
        self._last_balance_rows = out1 if isinstance(out1, list) else []
        self._last_balance_prices = {}
        for row in self._last_balance_rows:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("iem_cd", "") or row.get("pdno", "") or row.get("stk_cd", "")).strip()
            if not ticker:
                continue
            if ticker.startswith("A") and len(ticker) == 7 and ticker[1:].isdigit():
                ticker = ticker[1:]
            now_pr = row.get("now_pr") or row.get("prpr") or row.get("stck_prpr") or row.get("cur_prc")
            try:
                p = float(str(now_pr).replace(",", "").strip())
                if p > 0:
                    self._last_balance_prices[ticker] = p
            except (ValueError, TypeError, AttributeError):
                continue

    def get_holdings(self) -> dict[str, int]:
        """보유 종목 ticker -> 수량. Output_1 배열."""
        # 2026-08-30 확정: iem_cd + itg_bnc_qty/rsdl_qty (실연동: 005935 1주)
        self._fetch_balance()
        holdings: dict[str, int] = {}
        for row in self._last_balance_rows:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("iem_cd", "") or row.get("pdno", "") or row.get("stk_cd", "")).strip()
            qty_str = str(
                row.get("itg_bnc_qty", "") or row.get("rsdl_qty", "") or row.get("hldg_qty", "") or "0"
            ).strip()
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

    def get_held_prices(self) -> dict[str, float]:
        """보유종목 현재가. balance Output_1[].now_pr (브로커 권위)."""
        if self._last_balance_prices is None:
            self._fetch_balance()
        return dict(self._last_balance_prices or {})

    def get_buyable_info(self, ticker: str, price: int) -> dict[str, str]:
        """매수 가능 정보. runner의 hasattr 분기 호환용."""
        # NH는 /krstock/inquiry/v1/buyableQuantity 등으로 추정
        # TODO: openapi.json에서 정확한 operationId 확인 필요 (buyableQuantity vs buyableAmount)
        try:
            data = self._call(
                "/krstock/inquiry/v1/buyableQuantity",
                {"act_no": self.acct_no, "iem_cd": ticker, "ord_uv": str(price)},
            )
            out0 = data.get("Output_0", {})
            if isinstance(out0, list):
                out0 = out0[0] if out0 else {}
            return {str(k): str(v) for k, v in out0.items()} if isinstance(out0, dict) else {}
        except Exception:
            return {}

    def get_available_cash(self, ticker: str = "", price: int = 0) -> float:
        """KIS 호환 alias."""
        if ticker and price:
            info = self.get_buyable_info(ticker, price)
            for k in (
                "csh_orr_pbl_amt",
                "orr_pbl_amt1",
                "max_pbl_amt",
                "dca",
                "ord_psbl_amt",
                "ord_psbl_cash",
                "buy_psbl_amt",
            ):
                if k in info:
                    try:
                        return float(str(info[k]).replace(",", ""))
                    except ValueError:
                        continue
        return self.get_cash()

    # ------------------------------------------------------------------
    # 시세 조회
    # ------------------------------------------------------------------

    def get_prices(self, tickers: list[str]) -> dict[str, float]:
        """현재가 조회. /krstock/quote/v1/currentPrice 루프."""
        prices: dict[str, float] = {}
        igw40023_logged = False
        for ticker in tickers:
            t = str(ticker).strip()
            if not t:
                continue
            # NH는 market_cd 필수일 수 있음: KRX/UNT
            for market_cd in ("KRX", "UNT", ""):
                try:
                    payload: dict[str, Any] = {"iem_cd": t}
                    if market_cd:
                        payload["market_cd"] = market_cd
                    data = self._call_quote("/krstock/quote/v1/currentPrice", payload)
                    # 모의투자 미지원 API: 모든 market_cd 동일하게 실패하므로 1회 경고 후 중단
                    if self._is_igw40023(data):
                        if not igw40023_logged:
                            logger.warning(
                                "[NH] currentPrice IGW40023 — 모의투자에서 제공하지 않는 API입니다 "
                                "(demo는 runner의 pykrx 어제종가 fallback 사용)"
                            )
                            igw40023_logged = True
                        break
                    out0 = data.get("Output_0", {})
                    if isinstance(out0, list):
                        out0 = out0[0] if out0 else {}
                    # iem_nm 캐시 (pykrx 실패 시 fallback용)
                    iem_nm = str(out0.get("iem_nm", "")).strip()
                    if iem_nm and t not in self._iem_names:
                        self._iem_names[t] = iem_nm
                    # 후보 필드
                    for key in ("prpr", "stck_prpr", "cur_prc", "price", "now_prc"):
                        if key in out0:
                            try:
                                p = float(str(out0[key]).replace(",", "").strip())
                                if p > 0:
                                    prices[t] = p
                                    break
                            except ValueError:
                                continue
                    if t in prices:
                        break
                except Exception:
                    continue
        return prices

    def get_ticker_name(self, ticker: str) -> str | None:
        """pykrx 실패 시 fallback용 — currentPrice에서 캐시된 iem_nm 반환."""
        return self._iem_names.get(str(ticker).strip())

    def get_bid_ask_prices(self, tickers: list[str]) -> dict[str, dict[str, float]]:
        """매수/매도 기준가. 2026-08-30 확정: askp/bidp(askp1/bidp1) → stck_prpr fallback."""
        out: dict[str, dict[str, float]] = {}
        igw40023_logged = False
        for ticker in tickers:
            t = str(ticker).strip()
            if not t:
                continue
            for market_cd in ("KRX", "UNT", ""):
                try:
                    payload: dict[str, Any] = {"iem_cd": t}
                    if market_cd:
                        payload["market_cd"] = market_cd
                    data = self._call_quote("/krstock/quote/v1/currentPrice", payload)
                    # 모의투자 미지원 API: 모든 market_cd 동일하게 실패하므로 1회 경고 후 중단
                    if self._is_igw40023(data):
                        if not igw40023_logged:
                            logger.warning(
                                "[NH] currentPrice IGW40023 — 모의투자에서 제공하지 않는 API입니다 "
                                "(demo는 runner의 pykrx 어제종가 fallback 사용)"
                            )
                            igw40023_logged = True
                        break
                    out0 = data.get("Output_0", {})
                    if isinstance(out0, list):
                        out0 = out0[0] if out0 else {}
                    if not isinstance(out0, dict) or not out0:
                        continue
                    # iem_nm 캐시도 함께 갱신
                    iem_nm = str(out0.get("iem_nm", "")).strip()
                    if iem_nm and t not in self._iem_names:
                        self._iem_names[t] = iem_nm

                    def _to_float(v: Any) -> float:
                        try:
                            f = float(str(v).replace(",", "").strip())
                            return f if f > 0 else 0.0
                        except (ValueError, TypeError, AttributeError):
                            return 0.0

                    # 매수가는 ask(매도호가), 매도가는 bid(매수호가)
                    buy_price = (
                        _to_float(out0.get("askp1"))
                        or _to_float(out0.get("askp"))
                        or _to_float(out0.get("stck_prpr"))
                    )
                    sell_price = (
                        _to_float(out0.get("bidp1"))
                        or _to_float(out0.get("bidp"))
                        or _to_float(out0.get("stck_prpr"))
                    )
                    quote: dict[str, float] = {}
                    if buy_price > 0:
                        quote["buy_price"] = buy_price
                    if sell_price > 0:
                        quote["sell_price"] = sell_price
                    if quote:
                        out[t] = quote
                        break
                except Exception:
                    continue
        return out

    # ------------------------------------------------------------------
    # 주문
    # ------------------------------------------------------------------

    def _check_live_enabled(self) -> None:
        if os.environ.get("LIVE_ORDER_ENABLED", "0").strip() != "1":
            raise RuntimeError(
                "LIVE_ORDER_ENABLED != 1: NH 실주문 차단. "
                "모의/검증은 NHPLUG_BASE_URL=https://moapi.nhplug.com:8443 로 테스트하세요."
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

        nmn_cd = _ORDER_TYPE_TO_NMN.get(order_type.upper(), "01")
        # 시장가는 가격 0 또는 미전송
        is_market = nmn_cd == "05"

        if side_u == "BUY":
            path = "/krstock/order/v1/cashBuy"
        else:
            path = "/krstock/order/v1/cashSell"

        payload: dict[str, Any] = {
            "act_no": self.acct_no,
            "iem_cd": ticker,
            # trading-bot-kis shape: orr_qty/orr_pr as int + required NH PLUG cashBuy fields
            "orr_qty": int(qty),
            "nmn_pr_tp_cd": nmn_cd,
            "orr_cnd_dit_cd": "00",
            "ssl_nmn_pr_dit_cd": "00",
            "rmt_mkt_cd": "KRX",
            "sor_mkt_sli_yn": "N",
            # backward-compatible aliases
            "ord_qty": str(int(qty)),
        }
        if not is_market and price is not None:
            payload["orr_pr"] = int(price)
            payload["ord_uv"] = str(int(price))
        # dry_run 기본 차단: 실제 주문은 위 _check_live_enabled로 게이트
        data = self._call(path, payload, retry_network=False)
        # order_id 추출 후보
        out0 = data.get("Output_0", {})
        if isinstance(out0, list):
            out0 = out0[0] if out0 else {}
        order_id = ""
        for cand in (
            out0.get("ord_no"),
            out0.get("order_no"),
            out0.get("odno"),
            out0.get("mkt_orr_no"),
            out0.get("anw_cld_mkt_orr_no1"),
            data.get("ord_no"),
        ):
            # 공백뿐인 후보가 유효한 다음 후보(예: mkt_orr_no)를 가리지 않게 한다.
            if cand is not None and not isinstance(cand, bool) and str(cand).strip():
                order_id = str(cand).strip()
                break
        # 상태 조회는 itg_orr_no, 취소는 mkt_orr_no를 쓴다. 두 번호가 같은지는 모의서버로 확인 중이므로
        # (주문번호는 비밀이 아님) 응답에 있는 번호를 로그로 남긴다.
        logger.info(
            "[NH] 주문응답 번호 mkt_orr_no=%s itg_orr_no=%s",
            out0.get("mkt_orr_no") if isinstance(out0, dict) else None,
            out0.get("itg_orr_no") if isinstance(out0, dict) else None,
        )
        # 취소는 mkt_orr_no, 상태 조회는 itg_orr_no. 두 번호 체계가 같다고 가정하지 않고 주문별로 기억한다.
        # order_id 가 비어 있어도 응답의 itg/mkt 번호는 충돌 검사와 burn 에 반영한다(저장은 하지 않음).
        is_d = isinstance(out0, dict)
        if order_id or (is_d and (out0.get("itg_orr_no") is not None or out0.get("mkt_orr_no") is not None)):
            self._register_order(
                order_id,
                {
                    "mkt": _canon_no(out0.get("mkt_orr_no")) if is_d else "",
                    "itg": _canon_no(out0.get("itg_orr_no")) if is_d else "",
                    "ticker": str(ticker),
                    "side": side_u,
                },
            )
        # 실패 응답(예: rsp_cd=14250 잔고부족, Output_0 없음)을 성공으로 오판하지 않도록
        # order_id가 없고 rsp_cd가 실패 코드면 예외를 던져 runner가 submitted=False 처리
        if not order_id:
            rsp_cd = str(data.get("rsp_cd", "") or "")
            rsp_msg = str(data.get("rsp_msg", "") or "")
            if rsp_cd and rsp_cd not in _SUCCESS_RSP_CODES:
                raise NhApiError(rsp_cd, rsp_msg)
        return {"order_id": str(order_id), "response": data}

    def _order_registry(self) -> dict[str, dict[str, str]]:
        reg = self.__dict__.get("_order_ids")
        if reg is None:
            reg = {}
            self.__dict__["_order_ids"] = reg
        return reg

    def _burned_numbers(self) -> dict[str, set[str]]:
        b = self.__dict__.get("_burned")
        if b is None:
            b = {"key": set(), "itg": set(), "mkt": set()}
            self.__dict__["_burned"] = b
        return b

    @staticmethod
    def _numbers_of(key: str, rec: dict[str, str]) -> dict[str, str]:
        return {
            "key": key,
            "itg": rec.get("itg", ""),
            "mkt": rec.get("mkt", ""),
        }

    def _register_order(self, order_id: str, rec: dict[str, str]) -> None:
        """주문 신원 등록. 번호가 한 번이라도 겹치면 그 번호는 영구히 쓸 수 없게(burned) 한다.

        주문번호(key)·조회번호(itg)·시장번호(mkt) 각각에서, 이미 등록된 주문이나 burned 번호와 같은(정규화 후)
        번호가 나오면 관련된 모든 주문을 '충돌'로 표시하고 그 주문들의 모든 번호를 burned 로 만든다.
        충돌한 주문은 상태 조회가 항상 미확정이고 취소도 보내지 않는다(사람이 증권사 내역으로 대조).
        충돌 표시는 신원 정보를 지우지 않으므로 이후 등록도 같은 번호면 걸린다.
        """
        reg = self._order_registry()
        burned = self._burned_numbers()
        key = _canon_no(order_id)
        mine = self._numbers_of(key, rec)
        if not key:
            # 주문번호를 정식 표기로 쓸 수 없으면 이 주문은 저장하지 않는다(상태 조회 미확정, 취소 거부).
            # 그래도 이 주문이 가진 유효한 itg/mkt 번호는 다른 주문과 겹치는지 검사하고 항상 burn 한다.
            logger.warning("[NH] 주문번호 %r 를 신원으로 쓸 수 없어 등록하지 않습니다", order_id)
            mine.pop("key")
            for ns, val in mine.items():
                if not val:
                    continue
                for k, v in reg.items():
                    if self._numbers_of(k, v)[ns] == val:
                        v["conflict"] = "1"
                        for ns2, val2 in self._numbers_of(k, v).items():
                            if val2:
                                burned[ns2].add(val2)
                burned[ns].add(val)
            return
        clashed: list[str] = []
        hit = False
        for ns, val in mine.items():
            if not val:
                continue
            if val in burned[ns]:
                hit = True
            for k, v in reg.items():
                theirs = self._numbers_of(k, v)[ns]
                if theirs and theirs == val:
                    hit = True
                    clashed.append(k)
        if hit:
            for k in set(clashed):
                reg[k]["conflict"] = "1"
                for ns2, val2 in self._numbers_of(k, reg[k]).items():
                    if val2:
                        burned[ns2].add(val2)
            for ns, val in mine.items():
                if val:
                    burned[ns].add(val)
            if key not in reg:
                reg[key] = {**rec, "conflict": "1"}
            logger.warning(
                "[NH][주문번호 충돌] 신원을 확정할 수 없어 관련 주문의 확인/취소를 막습니다: %s",
                sorted(set(clashed + [key])),
            )
            return
        reg[key] = rec

    @staticmethod
    def _unresolved(order_id: str, data: Any) -> dict[str, Any]:
        return {
            "order_id": order_id,
            "filled_qty": 0,
            "order_qty": 0,
            "remaining_qty": 0,
            "is_filled": False,
            "is_found": False,
            "response": data,
        }

    def get_order_status(self, order_id: str, today: str | None = None) -> dict[str, Any]:
        if not order_id:
            raise ValueError("order_id is required")
        # 모의서버 실응답(2026-11-06 캡처) 기준 필드:
        #   itg_orr_no(조회용 주문번호) / orr_qty / tot_cns_qty / ny_cns_qty(미체결) / can_qty(취소)
        #   orr_rjt_rsn_cd_nm(거부사유) / iem_cd / sby_dit_cd_nm("현금매수"/"현금매도")
        # 주문 응답의 번호(mkt_orr_no 등)와 조회 행의 itg_orr_no가 같은 번호 체계인지는 검증되지 않았다.
        # 그래서 이 어댑터가 낸 주문만, 주문 응답이 itg_orr_no를 준 경우에만, 종목·매매구분까지 맞을 때만
        # 확인한다. 그 외에는 "찾지 못함"(미확정)으로 답한다.
        rec = self._order_registry().get(_canon_no(order_id))
        if not rec or rec.get("conflict") or not rec.get("itg"):
            return self._unresolved(
                order_id, {"error": "itg_orr_no unknown for this order — identity unresolved"}
            )
        payload: dict[str, Any] = {"act_no": self.acct_no}
        if today:
            payload["orr_dt"] = today
        try:
            data = self._call("/krstock/inquiry/v1/dailyOrderExecution", payload)
        except Exception as e:  # noqa: BLE001
            return self._unresolved(order_id, {"error": str(e)})

        # Output_0 또는 Output_1에서 주문 행 탐색 (실응답은 Output_0 배열)
        rows: list[dict[str, Any]] = []
        for key in ("Output_0", "Output_1"):
            v = data.get(key, [])
            if isinstance(v, dict):
                rows.append(v)
            elif isinstance(v, list):
                rows.extend([r for r in v if isinstance(r, dict)])

        want = rec["itg"]
        matches = [r for r in rows if _canon_no(r.get("itg_orr_no")) == want]
        # 같은 번호의 행이 둘 이상이면 내용이 모두 같을 때만 쓴다. 서로 다르면 어느 쪽이 맞는지 알 수 없다.
        if len(matches) > 1 and any(m != matches[0] for m in matches[1:]):
            return self._unresolved(order_id, data)
        target: dict[str, Any] = matches[0] if matches else {}
        # 주문번호가 일치하지 않는 행은 절대 대체 사용하지 않는다 (타 주문의 잔량을 오인 → 중복 주문).
        if not target:
            return self._unresolved(order_id, data)

        # 번호가 같아도 종목/매매구분이 다르면 다른 주문이다 (번호 체계 충돌 방어).
        side_nm = str(target.get("sby_dit_cd_nm", "") or "")
        side_ok = ("매수" in side_nm) if rec.get("side") == "BUY" else ("매도" in side_nm)
        if str(target.get("iem_cd", "") or "").strip() != rec.get("ticker") or not side_ok:
            return self._unresolved(order_id, data)

        order_qty = _field_qty(target, "orr_qty")
        filled_qty = _field_qty(target, "tot_cns_qty")
        can_f = _field_qty(target, "can_qty")
        ny_f = _field_qty(target, "ny_cns_qty")
        reject_reason = str(target.get("orr_rjt_rsn_cd_nm", "") or "").strip()
        # 주문/체결수량은 반드시 정상 정수여야 한다. 값이 있는데 잘못된 필드(None 포함)는 확정하지 않는다.
        # 필드 자체가 없는 can_qty/ny_cns_qty만 유도(0 / 주문-체결-취소)를 허용한다.
        if (
            not isinstance(order_qty, int) or order_qty <= 0
            or not isinstance(filled_qty, int)
            or (can_f is None) or (ny_f is None)
        ):
            return self._unresolved(order_id, data)
        cancelled_qty = 0 if can_f is _MISSING else can_f
        ny_qty = None if ny_f is _MISSING else ny_f

        # 수량 회계: 체결 + 취소 + 미체결 == 주문 이어야 "확정"으로 본다.
        # 설명되지 않는 수량(거부/누락/알 수 없음)은 아직 열려 있는 것으로 취급해 취소 확정과 재주문을 막는다.
        known_open = ny_qty if ny_qty is not None else 0
        accounted = filled_qty + cancelled_qty + known_open
        if accounted > order_qty:
            remaining_qty = max(known_open, 1)  # 모순: 완료/취소 확정 불가
        elif accounted < order_qty:
            remaining_qty = known_open + (order_qty - accounted)
        else:
            remaining_qty = known_open
        if reject_reason and filled_qty < order_qty:
            remaining_qty = max(remaining_qty, order_qty - filled_qty)  # 거부는 취소 확정이 아니다
        if filled_qty < order_qty and cancelled_qty == 0 and remaining_qty == 0:
            remaining_qty = order_qty - filled_qty  # 취소 증거(can_qty) 없이 0이 될 수 없다
        is_filled = (
            filled_qty >= order_qty and remaining_qty == 0 and cancelled_qty == 0
        )

        return {
            "order_id": order_id,
            "filled_qty": filled_qty,
            "order_qty": order_qty,
            "remaining_qty": remaining_qty,
            "cancelled_qty": cancelled_qty,
            "reject_reason": reject_reason,
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
        # 취소 키는 원시장주문번호 org_mkt_orr_no(숫자). 전량=all_pat_dit_cd "1", 일부="2" + cor_qty.
        # (이전의 orgn_ord_no/cncl_qty는 NH 명세에 없는 이름이었다.)
        rec = self._order_registry().get(_canon_no(order_id))
        if not rec or rec.get("conflict") or not rec.get("mkt"):
            # 시장주문번호(mkt_orr_no)를 이 어댑터가 확인한 주문만 취소한다. 다른 번호 체계의 값을
            # org_mkt_orr_no 로 보내면 같은 종목의 다른 주문을 취소할 수 있다.
            raise RuntimeError(
                f"NH 취소 거부: 주문 {order_id!r}의 시장주문번호(mkt_orr_no)를 확인할 수 없음 "
                "(미등록/번호 충돌/응답에 없음) — 증권사 내역으로 수동 확인하세요"
            )
        try:
            org_no: int | str = int(rec["mkt"])
        except ValueError as e:
            raise ValueError(f"NH 취소는 숫자 주문번호가 필요합니다: {order_id!r}") from e
        part = qty is not None and int(qty) > 0
        payload: dict[str, Any] = {
            "act_no": self.acct_no,
            "org_mkt_orr_no": org_no,
            "all_pat_dit_cd": "2" if part else "1",
            "iem_cd": ticker,
            "cor_qty": int(qty) if part else 0,
        }

        data = self._call("/krstock/order/v1/cancel", payload)
        # 실패 응답을 "요청됨"으로 오인하지 않는다 (취소 확정은 상태 재조회로만 한다).
        rsp_cd = str(data.get("rsp_cd", "") or "")
        if rsp_cd and rsp_cd not in _SUCCESS_RSP_CODES:
            raise NhApiError(rsp_cd, str(data.get("rsp_msg", "") or ""))
        out0 = data.get("Output_0", {})
        if isinstance(out0, list):
            out0 = out0[0] if out0 else {}
        cancel_id = out0.get("ord_no", "") or data.get("ord_no", "")
        return {"order_id": order_id, "cancel_order_id": str(cancel_id), "response": data}

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> NhAdapter:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
