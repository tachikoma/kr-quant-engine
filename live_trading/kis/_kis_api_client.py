"""
KIS 국내주식 REST API 클라이언트

third_party/open-trading-api/.../domestic_stock_functions.py 의 핵심 함수를 직접 구현.
pandas.DataFrame 대신 dict / list[dict] 를 반환합니다.

모의투자(demo) 모드에서는 TR_ID 앞자리가 T/J/C 인 경우 V로 자동 변환합니다.
"""

import json
import threading
import time
from typing import Any, List, Optional, Tuple

import requests

from ._kis_auth_manager import KisAuthManager


class KisApiError(Exception):
    """KIS API 오류 (HTTP 오류 또는 rt_cd != "0")."""

    def __init__(self, msg_cd: str, msg: str, http_status: int = 200):
        self.msg_cd = msg_cd
        self.msg = msg
        self.http_status = http_status
        super().__init__(f"[{msg_cd}] {msg} (HTTP {http_status})")


class KisApiClient:
    """KIS 국내주식 REST API 클라이언트."""

    # 모의투자 throttle (실전보다 느림)
    _SMART_SLEEP_REAL = 0.05
    _SMART_SLEEP_DEMO = 0.9

    def __init__(self, auth: KisAuthManager, env_mode: str):
        """
        Args:
            auth: KisAuthManager 인스턴스
            env_mode: "real" 또는 "demo"
        """
        self._auth = auth
        self._env_mode = env_mode
        self._is_demo = (env_mode == "demo")
        self._sleep_sec = self._SMART_SLEEP_DEMO if self._is_demo else self._SMART_SLEEP_REAL
        # 재시도 및 스로틀
        self._max_retries = 3
        self._retry_delay = 0.5
        self._min_interval = self._SMART_SLEEP_DEMO if self._is_demo else 0.1
        self._last_request_ts = 0.0
        self._throttle_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 내부 HTTP 헬퍼
    # ------------------------------------------------------------------

    def _tr_id(self, tr_id_real: str, tr_id_demo: Optional[str] = None) -> str:
        """모드에 따라 적절한 TR_ID를 반환합니다.

        tr_id_demo 가 None 이면 실전/모의 공용 TR_ID로 간주합니다.
        tr_id_demo 가 제공되지 않고 is_demo 이면 T/J/C 첫 글자를 V로 자동 변환합니다.
        """
        if not self._is_demo:
            return tr_id_real
        if tr_id_demo is not None:
            return tr_id_demo
        # 자동 변환: T/J/C → V
        if tr_id_real and tr_id_real[0] in ("T", "J", "C"):
            return "V" + tr_id_real[1:]
        return tr_id_real

    def _build_headers(self, tr_id: str, tr_cont: str = "") -> dict:
        headers = self._auth.get_base_headers()
        headers["tr_id"] = tr_id
        headers["custtype"] = "P"
        headers["tr_cont"] = tr_cont
        return headers

    def _throttle(self) -> None:
        """스레드 안전 최소 호출 간격 보장."""
        if self._min_interval <= 0:
            return
        now = time.monotonic()
        with self._throttle_lock:
            expected = max(self._last_request_ts + self._min_interval, now)
            self._last_request_ts = expected
        wait = expected - now
        if wait > 0:
            time.sleep(wait)

    def _get(self, path: str, tr_id: str, params: dict, tr_cont: str = "") -> dict:
        """GET 요청 수행 (재시도 + 스로틀 포함). 응답 dict 반환."""
        url = f"{self._auth.base_url}{path}"
        for attempt in range(self._max_retries + 1):
            self._throttle()
            headers = self._build_headers(tr_id, tr_cont)
            try:
                res = requests.get(url, headers=headers, params=params, timeout=15)
                return self._handle_response(res)
            except KisApiError as e:
                if attempt < self._max_retries and (
                    e.http_status >= 500 or e.msg_cd == "EGW00201"
                ):
                    time.sleep(self._retry_delay)
                    continue
                raise
            except requests.RequestException as e:
                if attempt < self._max_retries:
                    time.sleep(self._retry_delay)
                    continue
                raise KisApiError("NETWORK_ERROR", str(e), http_status=0) from e

    def _post(self, path: str, tr_id: str, payload: dict) -> dict:
        """POST 요청 수행 (재시도 + 스로틀 포함). 응답 dict 반환."""
        url = f"{self._auth.base_url}{path}"
        for attempt in range(self._max_retries + 1):
            self._throttle()
            headers = self._build_headers(tr_id, "")
            try:
                res = requests.post(url, headers=headers, data=json.dumps(payload), timeout=15)
                return self._handle_response(res)
            except KisApiError as e:
                if attempt < self._max_retries and (
                    e.http_status >= 500 or e.msg_cd == "EGW00201"
                ):
                    time.sleep(self._retry_delay)
                    continue
                raise
            except requests.RequestException as e:
                if attempt < self._max_retries:
                    time.sleep(self._retry_delay)
                    continue
                raise KisApiError("NETWORK_ERROR", str(e), http_status=0) from e

    def _handle_response(self, res: requests.Response) -> dict:
        """응답을 처리하고 dict 를 반환합니다. 오류 시 KisApiError 를 raise 합니다."""
        if res.status_code != 200:
            raise KisApiError(
                str(res.status_code),
                res.text[:500],
                http_status=res.status_code,
            )
        try:
            body = res.json()
        except Exception as e:
            raise KisApiError("PARSE_ERROR", f"JSON 파싱 실패: {e}", http_status=200) from e

        rt_cd = body.get("rt_cd", "")
        if rt_cd != "0":
            raise KisApiError(
                body.get("msg_cd", "UNKNOWN"),
                body.get("msg1", ""),
                http_status=res.status_code,
            )

        # 연속 조회용 헤더를 body에 포함
        body["_tr_cont"] = res.headers.get("tr_cont", "")
        body["_tr_id"] = res.headers.get("tr_id", "")
        return body

    def _smart_sleep(self) -> None:
        time.sleep(self._sleep_sec)

    # ------------------------------------------------------------------
    # 시세 조회
    # ------------------------------------------------------------------

    def get_price(self, symbol: str) -> dict:
        """현재가 조회 (inquire-price). output dict 반환."""
        body = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            "FHKST01010100",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        )
        return body.get("output") or {}

    def get_daily_price(self, symbol: str, period: str = "D") -> List[dict]:
        """일별 시세 조회 (inquire-daily-price). output list[dict] 반환."""
        body = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-price",
            "FHKST01010400",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_PERIOD_DIV_CODE": period,
                "FID_ORG_ADJ_PRC": "0",
            },
        )
        return body.get("output") or []

    def get_period_price(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        period: str = "D",
    ) -> Tuple[dict, List[dict]]:
        """기간별 시세 조회 (inquire-daily-itemchartprice).
        (output1 dict, output2 list[dict]) 반환."""
        body = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            "FHKST03010100",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": start_date,
                "FID_INPUT_DATE_2": end_date,
                "FID_PERIOD_DIV_CODE": period,
                "FID_ORG_ADJ_PRC": "0",
            },
        )
        return body.get("output1") or {}, body.get("output2") or []

    def get_asking_price(self, symbol: str) -> Tuple[dict, dict]:
        """호가 조회 (inquire-asking-price-exp-ccn).
        (output1 dict, output2 dict) 반환."""
        body = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            "FHKST01010200",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        )
        return body.get("output1") or {}, body.get("output2") or {}

    # ------------------------------------------------------------------
    # 계좌 조회
    # ------------------------------------------------------------------

    def get_balance(
        self,
        cano: str,
        acnt_prdt_cd: str,
        max_pages: int = 10,
    ) -> Tuple[List[dict], List[dict]]:
        """잔고 조회 (inquire-balance). 연속 조회 포함.
        (output1 list, output2 list) 반환."""
        tr_id = self._tr_id("TTTC8434R", "VTTC8434R")
        params: dict = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "01",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        out1: List[dict] = []
        out2: List[dict] = []
        tr_cont = ""

        for _ in range(max_pages):
            body = self._get(
                "/uapi/domestic-stock/v1/trading/inquire-balance",
                tr_id,
                params,
                tr_cont=tr_cont,
            )
            out1.extend(body.get("output1") or [])
            o2 = body.get("output2")
            if isinstance(o2, list):
                out2.extend(o2)
            elif isinstance(o2, dict) and o2:
                out2.append(o2)

            if body.get("_tr_cont") in ("M", "F"):
                params["CTX_AREA_FK100"] = body.get("ctx_area_fk100", "")
                params["CTX_AREA_NK100"] = body.get("ctx_area_nk100", "")
                tr_cont = "N"
                self._smart_sleep()
            else:
                break

        return out1, out2

    def get_buyable_cash(
        self,
        cano: str,
        acnt_prdt_cd: str,
        symbol: str = "",
        price: str = "",
    ) -> dict:
        """매수가능금액 조회 (inquire-psbl-order). output dict 반환.
        symbol/price를 공란으로 조회 시 매수수량 없이 매수금액만 조회된다."""
        tr_id = self._tr_id("TTTC8908R", "VTTC8908R")
        params = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "PDNO": symbol,
            "ORD_UNPR": price,
            "ORD_DVSN": "01",
            "CMA_EVLU_AMT_ICLD_YN": "N",
            "OVRS_ICLD_YN": "N",
        }
        body = self._get(
            "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            tr_id,
            params,
        )
        return body.get("output") or {}

    # ------------------------------------------------------------------
    # 주문
    # ------------------------------------------------------------------

    def order_cash(
        self,
        cano: str,
        acnt_prdt_cd: str,
        side: str,
        symbol: str,
        qty: int,
        price: int,
        order_type: str,
        excg_id: str = "KRX",
        sll_type: str = "",
        cndt_pric: str = "",
    ) -> dict:
        """현금 매수/매도 주문 (order-cash). output dict 반환.

        Args:
            side: "buy" 또는 "sell"
        """
        if side == "buy":
            tr_id = self._tr_id("TTTC0012U", "VTTC0012U")
        else:
            tr_id = self._tr_id("TTTC0011U", "VTTC0011U")

        payload: dict[str, Any] = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "PDNO": symbol,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
            "EXCG_ID_DVSN_CD": excg_id,
            "SLL_TYPE": sll_type,
            "CNDT_PRIC": cndt_pric,
        }
        body = self._post("/uapi/domestic-stock/v1/trading/order-cash", tr_id, payload)
        return body.get("output") or {}

    def cancel_order(
        self,
        cano: str,
        acnt_prdt_cd: str,
        krx_fwdg_ord_orgno: str,
        orgn_odno: str,
        ord_dvsn: str,
        ord_qty: int,
        ord_unpr: int,
        excg_id: str = "KRX",
    ) -> dict:
        """주문 취소 (order-rvsecncl). output dict 반환."""
        tr_id = self._tr_id("TTTC0013U", "VTTC0013U")
        payload: dict[str, Any] = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "KRX_FWDG_ORD_ORGNO": krx_fwdg_ord_orgno,
            "ORGN_ODNO": orgn_odno,
            "ORD_DVSN": ord_dvsn,
            "RVSE_CNCL_DVSN_CD": "02",  # 취소
            "ORD_QTY": str(ord_qty),
            "ORD_UNPR": str(ord_unpr),
            "QTY_ALL_ORD_YN": "Y" if ord_qty == 0 else "N",
            "EXCG_ID_DVSN_CD": excg_id,
        }
        body = self._post(
            "/uapi/domestic-stock/v1/trading/order-rvsecncl", tr_id, payload
        )
        return body.get("output") or {}

    # ------------------------------------------------------------------
    # 체결 조회
    # ------------------------------------------------------------------

    def get_order_fills(
        self,
        cano: str,
        acnt_prdt_cd: str,
        start_dt: str,
        end_dt: str,
        side: str = "00",
        symbol: str = "",
        order_no: str = "",
        pd_dv: str = "inner",
        max_pages: int = 10,
        excg_id: Optional[str] = "KRX",
    ) -> Tuple[List[dict], List[dict]]:
        """일별 주문체결 조회 (inquire-daily-ccld). 연속 조회 포함.
        (output1 list, output2 list) 반환.

        Args:
            pd_dv: "inner" (당일 이내) 또는 다른 값 (이전 기간)
        """
        if self._is_demo:
            tr_id = "VTTC0081R" if pd_dv == "inner" else "VTSC9215R"
        else:
            tr_id = "TTTC0081R" if pd_dv == "inner" else "CTSC9215R"

        params: dict = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "INQR_STRT_DT": start_dt,
            "INQR_END_DT": end_dt,
            "SLL_BUY_DVSN_CD": side,
            "PDNO": symbol,
            "CCLD_DVSN": "00",
            "INQR_DVSN": "00",
            "INQR_DVSN_3": "00",
            "ORD_GNO_BRNO": "",
            "ODNO": order_no,
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        if excg_id is not None:
            params["EXCG_ID_DVSN_CD"] = excg_id

        out1: List[dict] = []
        out2: List[dict] = []
        tr_cont = ""

        for _ in range(max_pages):
            body = self._get(
                "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                tr_id,
                params,
                tr_cont=tr_cont,
            )
            o1 = body.get("output1")
            if isinstance(o1, list):
                out1.extend(o1)
            elif isinstance(o1, dict) and o1:
                out1.append(o1)

            o2 = body.get("output2")
            if isinstance(o2, list):
                out2.extend(o2)
            elif isinstance(o2, dict) and o2:
                out2.append(o2)

            if body.get("_tr_cont") in ("M", "F"):
                params["CTX_AREA_FK100"] = body.get("ctx_area_fk100", "")
                params["CTX_AREA_NK100"] = body.get("ctx_area_nk100", "")
                tr_cont = "N"
                self._smart_sleep()
            else:
                break

        return out1, out2
