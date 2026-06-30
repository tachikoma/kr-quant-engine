"""
KIS API 인증 관리자 (kr_quant_engine 버전)

자격증명은 환경변수(KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO)에서만 로드.
토큰 캐시는 JSON 포맷으로 저장.

ENV_MODE 공통 환경변수 사용:
  - "real" → 실전 (https://openapi.koreainvestment.com:9443)
  - "demo" → 모의 (https://openapivts.koreainvestment.com:29443)
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

_REAL_BASE_URL = "https://openapi.koreainvestment.com:9443"
_DEMO_BASE_URL = "https://openapivts.koreainvestment.com:29443"
_REAL_WS_URL = "ws://ops.koreainvestment.com:21000"
_DEMO_WS_URL = "ws://opsv.koreainvestment.com:31000"

_DEFAULT_USER_AGENT = "python/kr-quant-engine"


def _default_token_dir() -> str:
    """KIS_CONFIG_PATH env → 프로젝트 루트/.kis_token_cache/"""
    env_dir = os.environ.get("KIS_CONFIG_PATH", "").strip()
    if env_dir:
        return env_dir
    return str(Path(__file__).resolve().parents[2] / ".kis_token_cache")


class KisAuthManager:
    """KIS API 인증 및 자격증명 관리 클래스.

    환경변수(ENV_MODE, KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO)에서
    자격증명을 로드합니다. kis_devlp.yaml 폴백은 없습니다.
    """

    def __init__(self, logger=None):
        self._token_dir = _default_token_dir()
        self._logger = logger

        self.app_key: str = ""
        self.app_secret: str = ""
        self.account: str = ""
        self.product_code: str = ""
        self.hts_id: str = ""
        self.base_url: str = ""
        self.base_url_ws: str = ""

        self._svr: str = ""
        self._access_token: Optional[str] = None
        self._token_file: Optional[str] = None
        self._ws_approval_key: Optional[str] = None
        self._base_headers: dict = {
            "Content-Type": "application/json",
            "Accept": "text/plain",
            "charset": "UTF-8",
            "User-Agent": _DEFAULT_USER_AGENT,
        }
        self._session = requests.Session()
        self._auth_timeout = float(os.environ.get("KIS_AUTH_TIMEOUT", "10"))

    # ------------------------------------------------------------------
    # 로깅 헬퍼
    # ------------------------------------------------------------------

    def _log_info(self, msg: str) -> None:
        if self._logger:
            self._logger.info(msg)

    def _log_warning(self, msg: str) -> None:
        if self._logger:
            self._logger.warning(msg)

    def _log_debug(self, msg: str) -> None:
        if self._logger:
            self._logger.debug(msg)

    # ------------------------------------------------------------------
    # 자격증명 로드
    # ------------------------------------------------------------------

    def load_credentials(self) -> None:
        """자격증명만 로드합니다 (토큰 발급 없이)."""
        self._load_credentials()
        self._setup_token_file()

    def init(self) -> None:
        """자격증명 로드 + 토큰 발급을 한 번에 수행합니다."""
        self._load_credentials()
        self._setup_token_file()
        self._issue_token_if_needed()

    def _load_credentials(self) -> None:
        """환경변수에서 자격증명을 로드합니다."""
        env_mode = os.environ.get("ENV_MODE", "real").lower()
        is_real = (env_mode == "real")
        self._svr = "prod" if is_real else "vps"

        self.app_key = os.environ.get("KIS_APP_KEY", "")
        self.app_secret = os.environ.get("KIS_APP_SECRET", "")
        self.account = os.environ.get("KIS_ACCOUNT_NO", "")
        self.product_code = os.environ.get("KIS_ACCOUNT_PROD_CD", "01").strip()
        self.hts_id = os.environ.get("KIS_HTS_ID", "").strip()

        if is_real:
            self.base_url = (
                os.environ.get("KIS_BASE_URL", "").strip() or _REAL_BASE_URL
            )
            self.base_url_ws = (
                os.environ.get("KIS_WS_URL", "").strip() or _REAL_WS_URL
            )
        else:
            self.base_url = (
                os.environ.get("KIS_DEMO_BASE_URL", "").strip() or _DEMO_BASE_URL
            )
            self.base_url_ws = (
                os.environ.get("KIS_DEMO_WS_URL", "").strip() or _DEMO_WS_URL
            )

        self.base_url = self.base_url.rstrip("/")

        self._base_headers["appkey"] = self.app_key
        self._base_headers["appsecret"] = self.app_secret

    # ------------------------------------------------------------------
    # 토큰 파일 관리
    # ------------------------------------------------------------------

    def _setup_token_file(self) -> None:
        """오늘 날짜 기반 토큰 파일 경로를 설정하고, 디렉터리가 없으면 생성합니다."""
        token_filename = f"KIS{datetime.today().strftime('%Y%m%d')}.json"
        self._token_file = os.path.join(self._token_dir, token_filename)
        os.makedirs(self._token_dir, exist_ok=True)

    def read_token(self) -> Optional[str]:
        """로컬 캐시에서 유효한 액세스 토큰을 읽습니다. 없거나 만료 시 None 반환."""
        if not self._token_file:
            return None
        try:
            with open(self._token_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            valid_date = data.get("valid_date", "")
            if not valid_date:
                return None
            now_str = datetime.today().strftime("%Y-%m-%d %H:%M:%S")
            return data["token"] if valid_date > now_str else None
        except Exception:
            return None

    def _save_token(self, token: str, expired_str: str) -> None:
        """토큰을 로컬 파일에 JSON 형식으로 저장합니다."""
        with open(self._token_file, "w", encoding="utf-8") as f:
            json.dump({"token": token, "valid_date": expired_str}, f)

    def invalidate_token(self) -> None:
        """메모리 및 파일 캐시의 토큰을 무효화합니다."""
        self._access_token = None
        self._base_headers.pop("authorization", None)
        if self._token_file and os.path.exists(self._token_file):
            try:
                os.remove(self._token_file)
                self._log_debug("토큰 파일 삭제 완료")
            except Exception as e:
                self._log_warning(f"토큰 파일 삭제 실패: {e}")

    # ------------------------------------------------------------------
    # 토큰 발급
    # ------------------------------------------------------------------

    def _issue_token_if_needed(self) -> None:
        """캐시된 토큰이 유효하면 사용하고, 없으면 새로 발급합니다."""
        saved = self.read_token()
        if saved:
            self._access_token = saved
            self._base_headers["authorization"] = f"Bearer {saved}"
            self._log_debug("캐시된 토큰 로드 완료")
            return
        self.issue_token()

    def issue_token(self) -> None:
        """KIS OAuth2 액세스 토큰을 새로 발급하고 캐시에 저장합니다."""
        url = f"{self.base_url}/oauth2/tokenP"
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        headers = self._base_headers.copy()
        headers.pop("authorization", None)

        try:
            res = self._session.post(url, data=json.dumps(payload), headers=headers, timeout=self._auth_timeout)
        except requests.RequestException as e:
            raise RuntimeError(
                f"KIS 토큰 발급 요청 실패 — KIS 서버({self.base_url}) 연결 불가: {e}"
            ) from e

        if res.status_code != 200:
            raise RuntimeError(f"KIS 토큰 발급 실패: HTTP {res.status_code} - {res.text[:500]}")

        body = res.json()
        token = body.get("access_token")
        expired_str = body.get("access_token_token_expired")
        if not token:
            raise RuntimeError(f"KIS 토큰 발급 응답에 access_token 없음: {body}")

        if self._token_file and expired_str:
            self._save_token(token, expired_str)

        self._access_token = token
        self._base_headers["authorization"] = f"Bearer {token}"
        self._log_info("KIS 액세스 토큰 발급 완료")

    # ------------------------------------------------------------------
    # 외부 접근자
    # ------------------------------------------------------------------

    def get_access_token(self) -> str:
        """현재 유효한 액세스 토큰을 반환합니다. 없으면 빈 문자열."""
        if not self._access_token:
            self._issue_token_if_needed()
        return self._access_token or ""

    def get_base_headers(self) -> dict:
        """API 호출용 기본 헤더의 복사본을 반환합니다."""
        return self._base_headers.copy()

    def get_websocket_approval_key(self) -> Optional[str]:
        """WebSocket 접속키(approval_key)를 새로 발급하고 반환합니다."""
        url = f"{self.base_url}/oauth2/Approval"
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.app_secret,
        }
        headers = self._base_headers.copy()
        headers.pop("authorization", None)

        try:
            res = self._session.post(url, data=json.dumps(payload), headers=headers, timeout=self._auth_timeout)
        except requests.RequestException as e:
            self._log_warning(f"WebSocket 접속키 발급 요청 실패: {e}")
            return None

        if res.status_code == 200:
            approval_key = res.json().get("approval_key")
            self._ws_approval_key = approval_key
            return approval_key
        else:
            self._log_warning(f"WebSocket 접속키 발급 실패: HTTP {res.status_code} - {res.text[:200]}")
            return None

    def close(self) -> None:
        """HTTP 세션을 종료합니다."""
        self._session.close()
