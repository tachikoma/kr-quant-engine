"""P2-A-2a: 동결 ETF 분배금 실데이터 확보와 Total Return E2E 검증.

범위:
- 공식 원천 접근성 조사(운용사/예탁결제원/공공데이터/KRX/KIND/DART).
- 접근 가능한 공식 원천에서 소량의 실제 분배금 이벤트를 정규화.
- 기존 `etf_distributions`/`etf_corporate_actions` 경로에 연결해 회계 정합성만 검증.
- 전체 기간·전체 유니버스 coverage가 확보되지 않았으므로 `total_return_approvable=False` 유지.

이 스크립트는 전략·동결 파라미터·운영 상태를 변경하지 않는다. 산출물은
`outputs_p2a2a/`(gitignored)에만 쓴다.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from etf_corporate_actions import (
    EventType,
    create_distribution_receivable,
    load_corporate_action_ledger,
    process_pending_receivables,
)
from etf_distributions import (
    add_distributions,
    add_total_return_price,
    distribution_cash_for_holdings,
    load_distributions,
)

OUTPUT_DIR = ROOT / "outputs_p2a2a"
BACKTEST_START = date(2016, 1, 5)
BACKTEST_END = date(2026, 9, 28)
CURRENCY = "KRW"
METADATA_SNAPSHOT_DATE = "20260721"  # strategy_freeze.json freeze_date와 동일
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122 Safari/537.36"

PILOT_TICKERS = ("069500", "360750", "411060", "161510", "498400")
KODEX_SOURCE_ID = "kodex_api"
KODEX_BASE = "https://www.samsungfund.com"
# www 호스트가 반복 호출에 429를 반환해 동일 운용사의 모바일 호스트를 사용한다.
KODEX_API_BASE = "https://m.samsungfund.com"
KODEX_PRODUCT_URL = KODEX_API_BASE + "/api/v1/kodex/product.do?ordrColm=NAV&ordrSort=DESC&pageNo={page}"
KODEX_DIVID_URL = KODEX_API_BASE + "/api/v1/kodex/divid-info.do?id={fid}"
KODEX_NOTICE_URL = KODEX_BASE + "/etf/lounge/notice-view.do?no={no}"

# 2026-11-27 KODEX 상품 목록에서 확인한 동결 4종의 내부 펀드 ID.
# divid-info 응답의 fNm을 KRX 종목 약명과 대조해 매핑 오류를 검출한다.
KODEX_FUND_IDS = {
    "069500": "2ETF01",
    "091160": "2ETF07",
    "091170": "2ETF08",
    "498400": "2ETFP4",
}

# 2026-11-27 조사 결과. `--probe` 실행 시 접근성만 재확인한다.
SOURCE_SURVEY: list[dict[str, Any]] = [
    {
        "source_id": "kodex_api",
        "source_name": "삼성자산운용 KODEX 분배금 API",
        "source_type": "issuer_api",
        "access_method": "GET JSON",
        "url": KODEX_BASE + "/api/v1/kodex/divid-info.do",
        "access_result": "accessible",
        "structured": "yes",
        "event_dates": "record/payment",
        "period_hint": "최근 5년(펀드별 최대 20건)",
        "terms_note": "공개 상품 페이지가 사용하는 JSON. robots.txt allow. 인증 불필요.",
        "usable_as_primary": "yes",
        "note": "동결 4종(069500/091160/091170/498400) 이벤트 확보",
    },
    {
        "source_id": "kodex_notice",
        "source_name": "KODEX 분배금 공지(공식 공지사항)",
        "source_type": "issuer_notice",
        "access_method": "GET HTML",
        "url": KODEX_NOTICE_URL.format(no=75493),
        "access_result": "accessible",
        "structured": "html_table",
        "event_dates": "cycle_only",
        "period_hint": "공지 시점별",
        "terms_note": "공개 공지. 기준일/지급일 필드는 본문에 없음(회차 표기만).",
        "usable_as_primary": "no",
        "note": "금액·분배율 교차검증에만 사용(날짜는 API 사용)",
    },
    {
        "source_id": "kodex_excel",
        "source_name": "KODEX 분배금 현황 Excel",
        "source_type": "issuer_download",
        "access_method": "GET XLS",
        "url": KODEX_BASE + "/excel-distribution.do",
        "access_result": "accessible",
        "structured": "binary_xls",
        "event_dates": "latest_only",
        "period_hint": "최신 회차",
        "terms_note": "공개 다운로드. OLE2(.xls)라 표준 파서 부재.",
        "usable_as_primary": "no",
        "note": "이번 검증에서는 파싱하지 않음",
    },
    {
        "source_id": "tiger_site",
        "source_name": "미래에셋 TIGER ETF",
        "source_type": "issuer_site",
        "access_method": "GET",
        "url": "https://www.tigeretf.com",
        "access_result": "http_403",
        "structured": "unknown",
        "event_dates": "unknown",
        "period_hint": "unknown",
        "terms_note": "Akamai/WAF로 이 환경에서 거부(동결 7종 해당).",
        "usable_as_primary": "no",
        "note": "브라우저 수동 확인/운용사 공지 PDF 대안",
    },
    {
        "source_id": "ace_site",
        "source_name": "한국투자 ACE ETF",
        "source_type": "issuer_site",
        "access_method": "GET",
        "url": "https://www.aceetf.co.kr/fund/K55101DN7441",
        "access_result": "page_200_api_blocked",
        "structured": "no",
        "event_dates": "unknown",
        "period_hint": "최근 3년(페이지 표기)",
        "terms_note": "상품 페이지는 열리지만 데이터 API 호스트(api.aceetf.co.kr)가 타임아웃.",
        "usable_as_primary": "no",
        "note": "동결 2종(360200/411060) 해당",
    },
    {
        "source_id": "plus_site",
        "source_name": "한화자산운용 PLUS ETF",
        "source_type": "issuer_site",
        "access_method": "GET",
        "url": "https://www.plusetf.co.kr",
        "access_result": "connection_failed",
        "structured": "unknown",
        "event_dates": "unknown",
        "period_hint": "unknown",
        "terms_note": "DNS는 해석되나 TCP 연결 실패.",
        "usable_as_primary": "no",
        "note": "동결 1종(161510) 해당",
    },
    {
        "source_id": "rise_site",
        "source_name": "KB자산운용 RISE ETF",
        "source_type": "issuer_site",
        "access_method": "GET",
        "url": "https://www.riseetf.co.kr",
        "access_result": "connection_failed",
        "structured": "unknown",
        "event_dates": "unknown",
        "period_hint": "unknown",
        "terms_note": "DNS/TCP 실패.",
        "usable_as_primary": "no",
        "note": "동결 2종(0101N0/367760) 해당",
    },
    {
        "source_id": "seibro",
        "source_name": "한국예탁결제원 SEIBro",
        "source_type": "csd_portal",
        "access_method": "GET",
        "url": "https://seibro.or.kr",
        "access_result": "js_shell_only",
        "structured": "no",
        "event_dates": "unknown",
        "period_hint": "unknown",
        "terms_note": "WebSquare 셸만 반환. 공개 JSON 계약 미확인.",
        "usable_as_primary": "no",
        "note": "브라우저 세션 기반 조회는 수동 대안",
    },
    {
        "source_id": "kofia_freesis",
        "source_name": "금융투자협회 FreeSIS",
        "source_type": "industry_portal",
        "access_method": "GET",
        "url": "http://freesis.kofia.or.kr",
        "access_result": "menu_unresolved",
        "structured": "unknown",
        "event_dates": "unknown",
        "period_hint": "unknown",
        "terms_note": "프레임/서비스ID 기반 메뉴. 분배금 통계 화면을 특정하지 못함.",
        "usable_as_primary": "no",
        "note": "serviceId 탐색 필요(이번 범위 제외)",
    },
    {
        "source_id": "data_go_kr",
        "source_name": "공공데이터포털",
        "source_type": "open_data_portal",
        "access_method": "GET catalog",
        "url": "https://www.data.go.kr/tcs/dss/selectDataSetList.do?keyword=%EB%B6%84%EB%B0%B0%EA%B8%88",
        "access_result": "no_dataset",
        "structured": "n/a",
        "event_dates": "n/a",
        "period_hint": "n/a",
        "terms_note": "키워드 '분배금'/'펀드 분배' 검색 결과에 ETF 분배금 데이터셋 없음.",
        "usable_as_primary": "no",
        "note": "주식배당정보는 법인 대상이며 ETF 분배금 아님",
    },
    {
        "source_id": "krx_mdc",
        "source_name": "KRX 정보데이터시스템",
        "source_type": "exchange",
        "access_method": "GET JSP / POST JSON(로그인)",
        "url": "https://data.krx.co.kr/contents/MDC/STAT/standard/MDCSTAT047.jsp",
        "access_result": "snapshot_only",
        "structured": "yes",
        "event_dates": "record_date_only",
        "period_hint": "현재 스냅샷",
        "terms_note": "개별종목 종합정보에 '분배금 기준일' 필드. 이벤트 이력 API는 확인되지 않음.",
        "usable_as_primary": "no",
        "note": "가격 원천(비수정 종가)으로 사용",
    },
    {
        "source_id": "kind",
        "source_name": "KRX KIND 전자공시",
        "source_type": "disclosure",
        "access_method": "GET",
        "url": "https://kind.krx.co.kr/external/2026/09/11/000441/20260911001119/68659.htm",
        "access_result": "http_403",
        "structured": "html_table",
        "event_dates": "record/payment",
        "period_hint": "공시별",
        "terms_note": "Akamai Access Denied. 공시 문서 자체는 존재하나 이 환경에서 조회 불가.",
        "usable_as_primary": "no",
        "note": "브라우저/수동 다운로드 대안. 'ETF이익금분배 신고(일괄공시)' 형식",
    },
    {
        "source_id": "dart",
        "source_name": "DART OpenAPI",
        "source_type": "disclosure",
        "access_method": "GET API",
        "url": "https://opendart.fss.or.kr",
        "access_result": "no_etf_distributions",
        "structured": "no",
        "event_dates": "n/a",
        "period_hint": "n/a",
        "terms_note": "법인 공시만 제공. 집합투자기구 분배금 정형 데이터 없음(P2-A-1 결과 유지).",
        "usable_as_primary": "no",
        "note": "재조회하지 않음",
    },
    {
        "source_id": "etfon",
        "source_name": "ETF ON(third-party aggregator)",
        "source_type": "aggregator",
        "access_method": "GET",
        "url": "https://etfon.co.kr/dividends.html",
        "access_result": "accessible",
        "structured": "html_table",
        "event_dates": "ex_date",
        "period_hint": "최근 1년 중심",
        "terms_note": "운용사 공시를 재가공한 비공식 집계. 승인 근거로 사용하지 않음.",
        "usable_as_primary": "no",
        "note": "교차검증에도 사용하지 않음(공식 문서 아님)",
    },
]


# --------------------------------------------------------------------------- #
# 기본 유틸
# --------------------------------------------------------------------------- #
def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_env_file(path: Path) -> None:
    """pykrx 로그인 환경변수를 .env에서 읽는다(비밀값은 출력하지 않는다)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def http_get(url: str, *, referer: str | None = None, timeout: int = 30) -> tuple[bytes, int]:
    import urllib.error
    import urllib.request

    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"}
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(), int(response.status)
    except urllib.error.HTTPError as exc:
        return exc.read(), int(exc.code)


def fetch_json(
    url: str, *, referer: str | None = None, attempts: int = 4
) -> tuple[Any, str]:
    """공개 JSON을 읽는다. 429는 지수 backoff로만 재시도한다."""
    import time

    last_status = 0
    for attempt in range(attempts):
        payload, status = http_get(url, referer=referer)
        last_status = status
        if status == 200:
            return json.loads(payload.decode("utf-8")), sha256_bytes(payload)
        if status != 429:
            break
        time.sleep(1.5 * (2**attempt))
    raise RuntimeError(f"HTTP {last_status}: {url}")


def parse_yyyymmdd(value: Any) -> date | None:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{8}", text):
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


def _to_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return parse_yyyymmdd(text)


def _normalize_name(value: str) -> str:
    return re.sub(r"\s+", "", value).upper()


def parse_float(value: Any) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def freeze_universe() -> list[str]:
    payload = json.loads((ROOT / "strategy_freeze.json").read_text(encoding="utf-8"))
    return [str(ticker) for ticker in payload["universe"]]


def pykrx_version() -> str:
    try:
        from pykrx._version import __version__  # type: ignore[attr-defined]
    except ImportError:
        return "unknown"
    return str(__version__)


# --------------------------------------------------------------------------- #
# KIS 조회 전용(주문 없음) — 비수정/수정 종가 대조
# --------------------------------------------------------------------------- #
KIS_RAW_DIR = OUTPUT_DIR / "raw" / "kis"
KIS_ERRORS: tuple[type[BaseException], ...] = (RuntimeError, OSError, ValueError)
try:  # 조회 오류도 종목별로 격리한다.
    from live_trading.kis._kis_api_client import KisApiError

    KIS_ERRORS = (*KIS_ERRORS, KisApiError)
except ImportError:
    pass


def kis_api():
    """KIS 조회 전용 클라이언트. 주문·잔고 API는 호출하지 않는다."""
    if not (os.environ.get("KIS_APP_KEY") and os.environ.get("KIS_APP_SECRET")):
        return None
    try:
        from live_trading.kis._kis_api_client import KisApiClient
        from live_trading.kis._kis_auth_manager import KisAuthManager
    except ImportError:
        return None
    auth = KisAuthManager()
    auth.init()
    mode = (
        os.environ.get("MODE")
        or os.environ.get("BROKER_MODE")
        or os.environ.get("ENV_MODE")
        or "real"
    ).lower()
    return KisApiClient(auth, "demo" if mode in {"demo", "virtual", "moapi"} else "real")


def is_rate_limit_error(exc: BaseException) -> bool:
    """KIS rate limit(EGW00201/EGW00215, HTTP 429)을 문자열이 아니라 코드로 판정한다."""
    msg_cd = str(getattr(exc, "msg_cd", "") or "")
    http_status = int(getattr(exc, "http_status", 0) or 0)
    text = str(exc)
    return (
        http_status == 429
        or msg_cd in {"EGW00201", "EGW00215"}
        or "EGW00201" in text
        or "EGW00215" in text
        or "Rate-limit" in text
    )


def _kis_date(value: Any) -> date:
    text = str(value).strip()
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def kis_daily_series(
    ticker: str,
    start: date,
    end: date,
    *,
    org_adj_prc: str,
    api: Any | None = None,
) -> pd.DataFrame:
    """KIS 기간별 시세를 100건 단위로 나눠 조회하고 원본을 보존한다.

    `org_adj_prc`는 KIS 요청 파라미터 그대로다("0" 또는 "1"). 문서 라벨과 실제
    동작이 반대로 관측되어(0=분배조정 계열, 1=원주가) 라벨로 해석하지 않는다.
    파일명은 기존 캐시 재사용을 위해 0→raw, 1→adj로 고정한다.
    """
    if org_adj_prc not in {"0", "1"}:
        raise ValueError(f"invalid org_adj_prc: {org_adj_prc}")
    KIS_RAW_DIR.mkdir(parents=True, exist_ok=True)
    label = "raw" if org_adj_prc == "0" else "adj"
    cache_path = KIS_RAW_DIR / f"{ticker}_{label}_{start.isoformat()}_{end.isoformat()}.csv"
    if cache_path.exists():
        return pd.read_csv(cache_path, parse_dates=["date"])
    api = api or kis_api()
    if api is None:
        raise RuntimeError("KIS credentials unavailable")
    rows: list[dict[str, Any]] = []
    cursor = end
    complete = False
    while cursor >= start:
        chunk_start = max(start, cursor - timedelta(days=120))
        body = api._get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            "FHKST03010100",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": ticker,
                "FID_INPUT_DATE_1": chunk_start.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": cursor.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": org_adj_prc,
            },
        )
        output = body.get("output2") or []
        if not output:
            break
        rows.extend(output)
        earliest = min(str(item.get("stck_bsop_date")) for item in output)
        earliest_date = parse_yyyymmdd(earliest)
        if earliest_date is None or earliest_date >= cursor:
            break
        if earliest_date <= start:
            complete = True
            break
        cursor = earliest_date - timedelta(days=1)
    else:
        complete = True
    if not rows:
        raise RuntimeError(f"KIS empty series: {ticker}")
    if not complete:
        raise RuntimeError(f"KIS incomplete series: {ticker} {start}..{end}")
    frame = pd.DataFrame(
        {
            "date": [_kis_date(item["stck_bsop_date"]) for item in rows],
            "close": [float(str(item["stck_clpr"]).replace(",", "")) for item in rows],
            "volume": [float(str(item.get("acml_vol", 0)).replace(",", "")) for item in rows],
            "trading_value": [
                float(str(item.get("acml_tr_pbmn", 0)).replace(",", "")) for item in rows
            ],
        }
    )
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
    frame.to_csv(cache_path, index=False)
    return frame


def decide_price_basis(
    equal_flag0: int,
    equal_flag1: int,
    total: int,
    ex_mean: float | None,
    non_ex_mean: float | None,
) -> tuple[str, str]:
    """캐시 일치도와 배당락 수익률 차이로 가격 기준과 조정 동작을 판정한다."""
    if total > 0 and equal_flag1 == total and equal_flag0 == 0:
        basis = "raw"
    elif total > 0 and equal_flag0 == total and equal_flag1 == 0:
        basis = "adjusted"
    else:
        basis = "mixed_or_unknown"
    behavior = (
        "adjusted_series_removes_ex_date_drop"
        if ex_mean is not None
        and non_ex_mean is not None
        and ex_mean < -0.05
        and abs(non_ex_mean) < 0.01
        else "not_observed"
    )
    return basis, behavior


def price_basis_probe(
    cache_dir: Path,
    events: list[dict[str, Any]],
    ticker: str = "069500",
    start: date = date(2026, 6, 1),
    end: date = BACKTEST_END,
) -> dict[str, Any]:
    """가격 캐시가 원주가인지 분배조정 계열인지 KIS 두 파라미터와 대조한다.

    KIS `FID_ORG_ADJ_PRC` 라벨(0=원주가, 1=수정주가)은 실측과 반대로 관측됐다.
    따라서 라벨이 아니라 (a) 캐시와의 일치, (b) 배당락일 수익률 차이로 판정한다.
    """
    cache = load_price(cache_dir, ticker)
    cache_rows = {
        row.date.date(): float(row.close) for row in cache.itertuples() if start <= row.date.date() <= end
    }
    try:
        flag0 = kis_daily_series(ticker, start, end, org_adj_prc="0")
        flag1 = kis_daily_series(ticker, start, end, org_adj_prc="1")
    except KIS_ERRORS as exc:
        reason = str(exc)[:160]
        return {
            "status": "unavailable",
            "reason": reason,
            "rate_limited": is_rate_limit_error(exc),
        }
    flag0_by_date = dict(zip(flag0["date"].dt.date, flag0["close"], strict=True))
    flag1_by_date = dict(zip(flag1["date"].dt.date, flag1["close"], strict=True))
    common = sorted(set(cache_rows) & set(flag0_by_date) & set(flag1_by_date))
    if len(common) < 2:
        return {"status": "insufficient", "reason": "no overlapping dates"}
    equal_flag0 = sum(1 for day in common if abs(cache_rows[day] - flag0_by_date[day]) < 1e-6)
    equal_flag1 = sum(1 for day in common if abs(cache_rows[day] - flag1_by_date[day]) < 1e-6)

    ex_dates = {
        date.fromisoformat(row["ex_date"])
        for row in events
        if row.get("ex_date") and row.get("ticker") == ticker
    }
    ex_diffs: list[float] = []
    non_ex_diffs: list[float] = []
    for previous, current in pairwise(common):
        previous_flag0 = flag0_by_date[previous]
        if previous_flag0 <= 0:
            continue
        cache_ret = cache_rows[current] / cache_rows[previous] - 1
        flag0_ret = flag0_by_date[current] / previous_flag0 - 1
        diff = (cache_ret - flag0_ret) * 100
        if current in ex_dates:
            ex_diffs.append(diff)
        else:
            non_ex_diffs.append(diff)
    ex_mean = round(float(np.mean(ex_diffs)), 5) if ex_diffs else None
    non_ex_mean = round(float(np.mean(non_ex_diffs)), 5) if non_ex_diffs else None
    basis, behavior = decide_price_basis(
        equal_flag0, equal_flag1, len(common), ex_mean, non_ex_mean
    )
    return {
        "status": "ok",
        "ticker": ticker,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "overlap_days": len(common),
        "cache_matches_flag1": equal_flag1,
        "cache_matches_flag0": equal_flag0,
        "price_basis": basis,
        "ex_date_events": len(ex_diffs),
        "ex_date_mean_return_diff_pct": ex_mean,
        "non_ex_date_mean_return_diff_pct": non_ex_mean,
        "ex_date_behavior": behavior,
        "parameter_semantics": (
            "실측: FID_ORG_ADJ_PRC=1(문서상 수정주가)이 캐시/원주가와 일치하고, "
            "0(문서상 원주가)이 배당락 하락을 제거한 조정 계열"
        ),
        "double_count_risk": "not_detected" if basis == "raw" else "unverified",
        "source": "KIS inquire-daily-itemchartprice (FID_ORG_ADJ_PRC 0/1)",
    }


def env_presence() -> dict[str, bool]:
    return {
        "KRX_ID": bool(os.environ.get("KRX_ID")),
        "KRX_PW": bool(os.environ.get("KRX_PW")),
    }


# --------------------------------------------------------------------------- #
# KODEX 수집·정규화
# --------------------------------------------------------------------------- #
def fetch_krx_etf_metadata(universe: list[str]) -> tuple[dict[str, dict[str, Any]], str]:
    """KRX ETF 기본종목에서 ISIN/상장일/약명을 한 번에 조회한다."""
    try:
        from pykrx.website.comm.auth import build_krx_session
    except ImportError as exc:  # pragma: no cover - 환경 의존
        return {}, f"unavailable:{exc}"[:120]
    if not (os.environ.get("KRX_ID") and os.environ.get("KRX_PW")):
        return {}, "skipped_no_credentials"
    session = build_krx_session()
    if session is None:
        return {}, "login_failed"
    response = session.session.post(
        "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd",
        data={
            "bld": "dbms/MDC/STAT/standard/MDCSTAT04601",
            "locale": "ko_KR",
            "trdDd": METADATA_SNAPSHOT_DATE,
            "share": "1",
            "money": "1",
            "csvxls_isNo": "false",
        },
        headers={
            "User-Agent": USER_AGENT,
            "Referer": (
                "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/"
                "index.cmd?menuId=MDC0201030104"
            ),
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=30,
    )
    rows = response.json().get("output", [])
    wanted = set(universe)
    metadata: dict[str, dict[str, Any]] = {}
    for row in rows:
        ticker = str(row.get("ISU_SRT_CD") or "")
        if ticker not in wanted:
            continue
        listed = str(row.get("LIST_DD") or "").replace("/", "-")
        metadata[ticker] = {
            "isin": str(row.get("ISU_CD") or ""),
            "name": str(row.get("ISU_ABBRV") or ""),
            "listed_date": listed,
        }
    digest = sha256_bytes(json.dumps(rows, sort_keys=True).encode("utf-8"))
    return metadata, f"ok:{len(rows)}:{digest}"


def normalize_kodex_events(
    ticker: str,
    *,
    isin: str | None,
    name: str | None,
    payload: dict[str, Any],
    source_url: str,
    source_sha256: str,
    retrieved_at: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """dividList를 정규화하고 날짜/금액 이상과 중복 충돌을 분리한다."""
    errors: list[str] = []
    rows: list[dict[str, Any]] = []
    for item in payload.get("dividList") or []:
        record_date = parse_yyyymmdd(item.get("basicD"))
        payment_date = parse_yyyymmdd(item.get("payD"))
        amount = parse_float(item.get("dividA"))
        taxable = parse_float(item.get("taxDividA"))
        rate = parse_float(item.get("dividY"))
        if record_date is None:
            errors.append(f"{ticker}:invalid_record_date:{item.get('basicD')}")
            continue
        if amount is None or amount <= 0:
            errors.append(f"{ticker}:invalid_amount:{item.get('dividA')}")
            continue
        if payment_date is None:
            errors.append(f"{ticker}:invalid_payment_date:{item.get('payD')}")
            continue
        if payment_date < record_date:
            errors.append(f"{ticker}:payment_before_record:{record_date}")
            continue
        if taxable is not None and (taxable < 0 or taxable > amount):
            errors.append(f"{ticker}:invalid_taxable_base:{record_date}")
            taxable = None
        rows.append(
            {
                "ticker": ticker,
                "isin": isin or "",
                "name": name or "",
                "record_date": record_date.isoformat(),
                "payment_date": payment_date.isoformat() if payment_date else "",
                "amount_per_share": float(amount),
                "taxable_base_per_share": float(taxable) if taxable is not None else np.nan,
                "distribution_rate_pct": float(rate) if rate is not None else np.nan,
                "currency": CURRENCY,
                "source_id": KODEX_SOURCE_ID,
                "source_url": source_url,
                "source_sha256": source_sha256,
                "retrieved_at": retrieved_at,
            }
        )
    return deduplicate_events(rows, errors)


def deduplicate_events(
    rows: list[dict[str, Any]], errors: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """(ticker, record_date) 중복을 제거하고 금액 충돌은 오류로 남긴다."""
    kept: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["ticker"], row["record_date"])
        existing = kept.get(key)
        if existing is None:
            kept[key] = row
            continue
        same = (
            existing["amount_per_share"] == row["amount_per_share"]
            and existing["payment_date"] == row["payment_date"]
            and _same_number(
                existing.get("taxable_base_per_share"), row.get("taxable_base_per_share")
            )
            and _same_number(
                existing.get("distribution_rate_pct"), row.get("distribution_rate_pct")
            )
        )
        if same:
            continue
        errors.append(
            f"{row['ticker']}:conflicting_duplicate:{row['record_date']}"
            f"({existing['amount_per_share']} vs {row['amount_per_share']})"
        )
    return sorted(kept.values(), key=lambda item: (item["ticker"], item["record_date"])), errors


def _same_number(left: Any, right: Any) -> bool:
    left_missing = left is None or (isinstance(left, float) and not np.isfinite(left))
    right_missing = right is None or (isinstance(right, float) and not np.isfinite(right))
    if left_missing or right_missing:
        return left_missing and right_missing
    return abs(float(left) - float(right)) < 1e-9


def build_market_calendar(cache_dir: Path, tickers: list[str]) -> list[date]:
    """가격 캐시에서 거래일 달력을 만든다(없으면 빈 달력)."""
    dates: set[date] = set()
    for ticker in tickers:
        path = cache_dir / f"{ticker}.parquet"
        if not path.exists():
            continue
        frame = pd.read_parquet(path, columns=["date"])
        dates.update(pd.to_datetime(frame["date"]).dt.date.tolist())
    return sorted(dates)


def attach_ex_dates(
    rows: list[dict[str, Any]], calendar: list[date]
) -> tuple[list[dict[str, Any]], list[str]]:
    """기준일 직전 거래일을 분배락일로 부여한다(원천 필드 아님, 파생값).

    국내 ETF는 T+2 결제 때문에 지급기준일(T)의 직전 거래일(T-1)이 분배락일이고,
    이날 NAV가 분배금만큼 조정된다. 원천 API는 기준일만 제공하므로 파생값이다.
    """
    errors: list[str] = []
    if not calendar:
        for row in rows:
            row["ex_date"] = ""
            row["ex_date_basis"] = "unresolved_no_calendar"
        return rows, ["calendar_empty"]
    for row in rows:
        record_date = date.fromisoformat(row["record_date"])
        index = bisect.bisect_left(calendar, record_date)
        if record_date > calendar[-1]:
            # 가격 캘린더가 끝난 뒤의 이벤트는 검증 대상이 아니다.
            row["ex_date"] = ""
            row["ex_date_basis"] = "unresolved_after_calendar"
            continue
        record_is_trading_day = index < len(calendar) and calendar[index] == record_date
        if not record_is_trading_day:
            errors.append(f"{row['ticker']}:record_date_not_trading_day:{record_date}")
        if index == 0:
            row["ex_date"] = ""
            row["ex_date_basis"] = "unresolved_before_calendar"
            continue
        row["ex_date"] = calendar[index - 1].isoformat()
        row["ex_date_basis"] = "derived_previous_trading_day"
    return rows, errors


def _archive_failed(raw_path: Path, payload: bytes) -> None:
    failed_dir = raw_path.parent / "failed"
    failed_dir.mkdir(parents=True, exist_ok=True)
    failed_path = failed_dir / f"{raw_path.stem}_{utc_now().replace(':', '')}.bin"
    failed_path.write_bytes(payload)


def _valid_payload(payload: Any, required_keys: tuple[str, ...]) -> bool:
    return isinstance(payload, dict) and all(key in payload for key in required_keys)


def cached_json(
    url: str,
    raw_path: Path,
    *,
    referer: str | None,
    refresh: bool,
    required_keys: tuple[str, ...] = ("dividList",),
) -> tuple[Any, str, str, str]:
    """원본 응답 바이트를 그대로 보존하고 최초 수집 시점을 함께 돌려준다.

    기대 필드가 없는 응답(빈 `{}`, 오류 객체, 잘린 JSON)은 정상 원본을 대체하지 않는다.
    """
    if raw_path.exists() and not refresh:
        payload = raw_path.read_bytes()
        parsed = json.loads(payload.decode("utf-8"))
        if not _valid_payload(parsed, required_keys):
            raise RuntimeError(f"invalid cached payload: {raw_path.name}")
        collected = datetime.fromtimestamp(raw_path.stat().st_mtime, tz=timezone.utc)
        return (
            parsed,
            sha256_bytes(payload),
            "cache",
            collected.replace(microsecond=0).isoformat(),
        )
    payload, status = http_get(url, referer=referer)
    if status != 200:
        _archive_failed(raw_path, payload)
        raise RuntimeError(f"HTTP {status}: {url}")
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _archive_failed(raw_path, payload)
        raise RuntimeError(f"invalid JSON response: {exc}") from exc
    if not _valid_payload(parsed, required_keys):
        _archive_failed(raw_path, payload)
        raise RuntimeError("unexpected JSON shape")
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(payload)
    return parsed, sha256_bytes(payload), "network", utc_now()


def in_window(record_date: str, start: date, end: date) -> bool:
    value = date.fromisoformat(record_date)
    return start <= value <= end


# --------------------------------------------------------------------------- #
# coverage
# --------------------------------------------------------------------------- #
def schedule_months(payload: dict[str, Any]) -> set[int]:
    info = payload.get("dividInfo") or {}
    months: set[int] = set()
    for group in (info.get("dividPeriod") or {}).values():
        for item in group or []:
            month = int(item.get("mm") or 0)
            if 1 <= month <= 12:
                months.add(month)
    return months


def expected_events(first: date, last: date, months: set[int]) -> int | None:
    if not months:
        return None
    count = 0
    for year in range(first.year, last.year + 1):
        for month in sorted(months):
            candidate = date(year, month, 1)
            if first.replace(day=1) <= candidate <= last:
                count += 1
    return count


def coverage_status(
    *,
    official: bool,
    event_count: int,
    span_complete: bool | None,
    covers_start: bool,
    covers_end: bool,
    conflicts: bool,
) -> str:
    if event_count == 0:
        return "UNAVAILABLE"
    if not official or conflicts:
        return "UNVERIFIED"
    if span_complete and covers_start and covers_end:
        return "VERIFIED"
    return "PARTIAL"


def build_coverage(
    *,
    ticker: str,
    issuer: str,
    name: str,
    listed_date: date | None,
    rows: list[dict[str, Any]],
    months: set[int],
    conflicts: bool,
    source_available: bool,
) -> dict[str, Any]:
    window_rows = [row for row in rows if in_window(row["record_date"], BACKTEST_START, BACKTEST_END)]
    required_start = max(BACKTEST_START, listed_date) if listed_date else BACKTEST_START
    first = date.fromisoformat(window_rows[0]["record_date"]) if window_rows else None
    last = date.fromisoformat(window_rows[-1]["record_date"]) if window_rows else None
    span_expected = expected_events(first, last, months) if first and last else None
    # 전 구간 스케줄 일치만 VERIFIED 근거로 쓴다. 최근 1년 값은 참고 지표다.
    recent_start = date(last.year - 1, last.month, 1) if last else None
    recent_expected = expected_events(recent_start, last, months) if last else None
    recent_actual = (
        sum(1 for row in window_rows if date.fromisoformat(row["record_date"]) >= recent_start)
        if recent_start
        else None
    )
    span_complete = None if span_expected is None else span_expected == len(window_rows)
    covers_start = bool(first and first <= required_start)
    covers_end = bool(last and (last >= BACKTEST_END or _period_end(last, months) >= BACKTEST_END))
    status = coverage_status(
        official=source_available,
        event_count=len(window_rows),
        span_complete=span_complete,
        covers_start=covers_start,
        covers_end=covers_end,
        conflicts=conflicts,
    )
    if not source_available and not window_rows:
        status = "UNAVAILABLE"
    reason = _coverage_reason(status, first, required_start, span_complete, covers_end)
    return {
        "ticker": ticker,
        "issuer": issuer,
        "name": name,
        "listed_date": listed_date.isoformat() if listed_date else "",
        "required_start": required_start.isoformat(),
        "required_end": BACKTEST_END.isoformat(),
        "first_record_date": first.isoformat() if first else "",
        "last_record_date": last.isoformat() if last else "",
        "event_count": len(window_rows),
        "expected_events_in_span": span_expected if span_expected is not None else "",
        "recent_expected_events": recent_expected if recent_expected is not None else "",
        "recent_actual_events": recent_actual if recent_actual is not None else "",
        "schedule_months": ",".join(str(month) for month in sorted(months)),
        "coverage_start": first.isoformat() if first else "",
        "coverage_end": last.isoformat() if last else "",
        "status": status,
        "total_return_approvable": False,
        "reason": reason,
    }


def _period_end(last: date, months: set[int]) -> date:
    """마지막 이벤트 이후 다음 예정월 말을 근사 없이 계산한다."""
    if not months:
        return last
    year, month = last.year, last.month
    for _ in range(12):
        month += 1
        if month > 12:
            year += 1
            month = 1
        if month in months:
            return date(year, month, 1)
    return last


def _coverage_reason(
    status: str, first: date | None, required_start: date, span_complete: bool | None, covers_end: bool
) -> str:
    if status == "UNAVAILABLE":
        return "공식 원천 접근 실패(현재 환경) — 이벤트 없음이 아니라 미확보"
    if status == "UNVERIFIED":
        return "원천/coverage 미검증 또는 중복 금액 충돌"
    if status == "VERIFIED":
        return "공식 원천, 스케줄 대비 이벤트 수 일치, 요구 기간 시작/끝 충족"
    parts = ["공식 원천·스케줄 일치 범위만 부분 확보"]
    if first and first > required_start:
        parts.append(f"요구 시작({required_start}) 이후 {first}부터 이벤트 존재(초기 공백 미검증)")
    if span_complete is False:
        parts.append(
            "전 구간 스케줄 대비 이벤트 수 불일치(운용사 주기 변경 또는 원천 5년 한도)"
        )
    if not covers_end:
        parts.append("요구 종료 시점 미충족")
    return "; ".join(parts)


# --------------------------------------------------------------------------- #
# 가격·E2E
# --------------------------------------------------------------------------- #
def load_price(cache_dir: Path, ticker: str) -> pd.DataFrame:
    frame = pd.read_parquet(cache_dir / f"{ticker}.parquet")
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame[(frame["date"].dt.date >= BACKTEST_START) & (frame["date"].dt.date <= BACKTEST_END)]
    return (
        frame[["date", "close"]]
        .assign(ticker=ticker)
        .drop_duplicates(subset=["date"])
        .sort_values("date")
        .reset_index(drop=True)
    )


def slice_from_prior_trading_day(price: pd.DataFrame, first_ex_date: date) -> pd.DataFrame:
    """첫 배당락일의 전일 종가를 포함하도록 자른다(첫 분배금 TR 반영)."""
    prior = price.index[price["date"].dt.date < first_ex_date]
    start_index = int(prior[-1]) if len(prior) else 0
    return price.iloc[start_index:].reset_index(drop=True)


def reinvesting_ledger_check(
    price: pd.DataFrame, distributions: pd.DataFrame, quantity: int = 100
) -> dict[str, Any]:
    """주식수·현금 원장을 직접 굴려 TR 지수와 독립적으로 대조한다(분배금 재투자)."""
    amounts = {
        pd.Timestamp(row.ex_date): float(row.amount_per_share)
        for row in distributions.itertuples()
    }
    shares = float(quantity)
    cash = 0.0
    first_close = float(price["close"].iloc[0])
    for row in price.itertuples():
        day = pd.Timestamp(row.date)
        if day in amounts:
            cash += shares * amounts[day]
        if cash > 0 and row.close > 0:
            shares += cash / float(row.close)
            cash = 0.0
    final_close = float(price["close"].iloc[-1])
    final_equity = shares * final_close + cash
    return {
        "ledger_type": "reinvesting_share_ledger",
        "start_shares": quantity,
        "final_shares": round(shares, 8),
        "final_equity": round(final_equity, 2),
        "equity_over_start": final_equity / (quantity * first_close),
        "unreinvested_cash": round(cash, 6),
    }


def total_return_e2e(
    price: pd.DataFrame,
    events: list[dict[str, Any]],
    output_dir: Path | None = None,
    *,
    price_basis: str = "unknown",
) -> dict[str, Any]:
    """기존 loader/merge/total-return 경로로 평가액을 검산한다."""
    base_dir = output_dir or OUTPUT_DIR
    csv_path = base_dir / "e2e" / f"{price['ticker'].iloc[0]}_distributions.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "ticker": row["ticker"],
                "ex_date": row["ex_date"],
                "amount_per_share": row["amount_per_share"],
                "payment_date": row["payment_date"],
                "source": row["source_url"],
            }
            for row in events
            if row.get("ex_date")
        ]
    ).to_csv(csv_path, index=False)

    distributions = load_distributions(csv_path, required=True)
    merged = add_distributions(price, distributions)
    enriched = add_total_return_price(merged)

    close = enriched["close"].astype(float)
    dist = enriched["distribution"].astype(float)
    growth = (close + dist) / close.shift(1)
    valid = growth.dropna()
    recomputed = float(valid.prod())
    index_ratio = float(enriched["close_total_return"].iloc[-1] / enriched["close_total_return"].iloc[0])
    price_ratio = float(close.iloc[-1] / close.iloc[0])

    quantity = 100
    dividends = float(dist.sum()) * quantity
    price_pnl = (float(close.iloc[-1]) - float(close.iloc[0])) * quantity
    equity_with_cash = price_pnl + dividends
    daily = close.pct_change()
    shock = float(daily.abs().max())
    anomaly_mask = daily.abs() > 0.15
    anomaly_dates = [
        str(value.date()) for value in enriched.loc[anomaly_mask.fillna(False), "date"].tolist()
    ]

    ledger = reinvesting_ledger_check(enriched[["date", "close"]], distributions, quantity)
    ledger["tr_index_ratio"] = index_ratio
    ledger["abs_diff_vs_tr_index"] = abs(ledger["equity_over_start"] - index_ratio)

    return {
        "ticker": price["ticker"].iloc[0],
        "start": price["date"].iloc[0].date().isoformat(),
        "end": price["date"].iloc[-1].date().isoformat(),
        "trading_days": len(price),
        "event_count": len(distributions),
        "distribution_sum_per_share": float(distributions["amount_per_share"].sum()),
        "price_basis": price_basis,
        "price_return_pct": round((price_ratio - 1) * 100, 4),
        "total_return_pct": round((index_ratio - 1) * 100, 4),
        "distribution_contribution_pct": round(((index_ratio - price_ratio) * 100), 4),
        "tr_identity_abs_diff": float(abs(index_ratio - recomputed)),
        "equity_check": {
            "quantity": quantity,
            "price_pnl": round(price_pnl, 2),
            "cash_dividends": round(dividends, 2),
            "equity_change": round(equity_with_cash, 2),
            "identity_abs_diff": round(abs(equity_with_cash - (price_pnl + dividends)), 6),
            "scope": "loader/회계 함수 단위 검증(엔진 전체 실행 아님)",
            "independent_ledger": ledger,
        },
        "max_abs_daily_return_pct": round(shock * 100, 4),
        "split_suspicion": bool(shock > 0.15),
        "anomaly_detected": sorted(set(anomaly_dates)),
        "anomaly_confirmed": [],
        "anomaly_threshold_pct": 15.0,
        "ex_dates_all_in_price": bool(
            set(pd.to_datetime(distributions["ex_date"])) <= set(price["date"])
        ),
        "window_basis": "첫 확인 이벤트 ex_date의 전일 ~ 백테스트 종료(이후 차기 이벤트 없음)",
        "csv_sha256": sha256_file(csv_path),
        "csv_path": str(csv_path.relative_to(ROOT)) if csv_path.is_relative_to(ROOT) else str(csv_path),
    }


def entitlement_case(events: list[dict[str, Any]], price: pd.DataFrame, quantity: int = 100) -> dict[str, Any]:
    """배당락일 매수 제외·매도 포함·지급 지연을 기존 함수로 확인한다."""
    if not events:
        return {"status": "no_events"}
    event = events[-1]
    gross = distribution_cash_for_holdings({event["ticker"]: quantity}, {event["ticker"]: event["amount_per_share"]})
    taxable = event.get("taxable_base_per_share")
    taxable_base = float(taxable) if taxable is not None and np.isfinite(taxable) else float(event["amount_per_share"])
    gross_taxed = distribution_cash_for_holdings(
        {event["ticker"]: quantity}, {event["ticker"]: event["amount_per_share"]}, tax_pct=0.154
    )
    base_taxed = gross * (1 - 0.154 * (taxable_base / float(event["amount_per_share"])))
    lag = (date.fromisoformat(event["payment_date"]) - date.fromisoformat(event["ex_date"])).days
    return {
        "status": "ok",
        "ticker": event["ticker"],
        "event_ex_date": event["ex_date"],
        "quantity": quantity,
        "gross_cash": round(gross, 2),
        "gross_taxed_15_4pct": round(gross_taxed, 2),
        "taxable_base_15_4pct": round(base_taxed, 2),
        "tax_overstatement_if_gross_basis": round(base_taxed - gross_taxed, 2),
        "payment_lag_days": lag,
        "buy_on_ex_date_entitled": False,
        "sell_on_ex_date_entitled": True,
        "cash_credited_in_engine_on": "ex_date (payment_date 아님)",
        "ex_date_precedes_payment": lag >= 0,
        "price_rows_after_ex_date": (
            price["date"].dt.date > date.fromisoformat(event["ex_date"])
        ).sum(),
    }


def receivable_case(events: list[dict[str, Any]], quantity: int = 100) -> dict[str, Any]:
    """ledger 이벤트 → receivable → 지급 시 현금 반영을 검증한다."""
    if not events:
        return {"status": "no_events"}
    event = events[-1]
    row = {
        "event_id": f"P2A2A-{event['ticker']}-{event['record_date']}",
        "ticker": event["ticker"],
        "event_type": EventType.CASH_DISTRIBUTION.value,
        "event_date": event["ex_date"],
        "record_date": event["record_date"],
        "ex_date": event["ex_date"],
        "payment_date": event["payment_date"],
        "cash_amount": str(event["amount_per_share"]),
        "currency": CURRENCY,
        "source_document_id": "kodex_api",
        "source_url": event["source_url"],
        "source_sha256": event["source_sha256"],
        "notes": "P2-A-2a E2E",
    }
    action = _ledger_action(row)
    receivable = create_distribution_receivable(
        action, quantity, held_on_record_date=True, held_on_ex_date=True
    )
    not_entitled = create_distribution_receivable(
        action, quantity, held_on_record_date=True, held_on_ex_date=False
    )
    paid_before = process_pending_receivables([receivable], date.fromisoformat(event["payment_date"]).replace(day=1))
    paid_after = process_pending_receivables([receivable], event["payment_date"])
    return {
        "status": "ok",
        "event_id": row["event_id"],
        "receivable_amount": float(receivable.amount) if receivable else None,
        "expected_amount": round(quantity * event["amount_per_share"], 4),
        "not_entitled_is_none": not_entitled is None,
        "paid_before_payment_date": float(paid_before[1]),
        "paid_on_payment_date": float(paid_after[1]),
        "post_sale_still_payable": bool(paid_after[1] > 0),
        "payment_lag_days": (
            date.fromisoformat(event["payment_date"]) - date.fromisoformat(event["ex_date"])
        ).days,
    }


def _ledger_action(row: dict[str, Any]):
    from decimal import Decimal

    from etf_corporate_actions import CorporateAction

    return CorporateAction(
        event_id=row["event_id"],
        ticker=row["ticker"],
        event_type=EventType(row["event_type"]),
        event_date=date.fromisoformat(row["event_date"]),
        record_date=date.fromisoformat(row["record_date"]),
        ex_date=date.fromisoformat(row["ex_date"]),
        payment_date=date.fromisoformat(row["payment_date"]),
        cash_amount=Decimal(str(row["cash_amount"])),
        currency=row["currency"],
        source_document_id=row["source_document_id"],
        source_url=row["source_url"],
        source_sha256=row["source_sha256"],
        notes=row["notes"],
    )


def write_ledger(events: list[dict[str, Any]], coverage_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """실데이터 이벤트를 기존 ledger 스키마로 변환하고 로드 결과를 확인한다."""
    ledger_path = OUTPUT_DIR / "distribution_ledger.csv"
    manifest_path = OUTPUT_DIR / "distribution_manifest.json"
    columns = [
        "event_id",
        "ticker",
        "event_type",
        "event_date",
        "record_date",
        "ex_date",
        "payment_date",
        "settlement_date",
        "ratio_num",
        "ratio_den",
        "cash_amount",
        "currency",
        "source_document_id",
        "source_document",
        "source_url",
        "source_sha256",
        "notes",
    ]
    if not events:
        return {
            "ledger_path": None,
            "manifest_path": None,
            "ledger_sha256": None,
            "event_count": 0,
            "tickers": [],
            "status": "SKIPPED",
            "approval_valid": False,
            "blockers": [
                {
                    "code": "NO_EVENTS",
                    "message": "분배금 이벤트가 없어 ledger 변환을 생략했습니다",
                }
            ],
        }
    rows = []
    for row in events:
        if not row.get("ex_date") or not row.get("payment_date"):
            continue
        rows.append(
            {
                "event_id": f"P2A2A-{row['ticker']}-{row['record_date']}",
                "ticker": row["ticker"],
                "event_type": EventType.CASH_DISTRIBUTION.value,
                "event_date": row["ex_date"],
                "record_date": row["record_date"],
                "ex_date": row["ex_date"],
                "payment_date": row["payment_date"],
                "settlement_date": "",
                "ratio_num": "",
                "ratio_den": "",
                "cash_amount": f"{row['amount_per_share']:.4f}".rstrip("0").rstrip("."),
                "currency": CURRENCY,
                "source_document_id": f"kodex_api_{row['ticker']}",
                "source_document": f"kodex_api_{row['ticker']}",
                "source_url": row["source_url"],
                "source_sha256": row["source_sha256"],
                "notes": "P2-A-2a normalized event",
            }
        )
    pd.DataFrame(rows, columns=columns).to_csv(ledger_path, index=False)
    file_sha = sha256_file(ledger_path)
    documents = [
        {
            "document_id": f"kodex_api_{ticker}",
            "source_url": source_url,
            "sha256": source_sha,
        }
        for ticker, source_url, source_sha in sorted(
            {(row["ticker"], row["source_url"], row["source_sha256"]) for row in events}
        )
    ]
    covered = sorted({row["ticker"] for row in events})
    starts = [date.fromisoformat(row["ex_date"]) for row in events]
    ends = [date.fromisoformat(row["payment_date"]) for row in events]
    manifest = {
        "manifest_version": 1,
        "ledger_file": ledger_path.name,
        "ledger_sha256": "0" * 64,
        "verification_period": {
            "start": min(starts).isoformat(),
            "end": max(ends).isoformat(),
        },
        "verification_tickers": covered,
        "source_documents": documents,
        "coverage_status": "PARTIAL",
        "notes": "KODEX 공식 API에서 확보한 부분 기간 이벤트. 전 기간/전 유니버스 coverage 아님.",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    # loader의 정규화 결과로 canonical hash를 다시 계산해 manifest에 고정한다.
    from etf_corporate_actions import canonical_ledger_sha256

    provisional = load_corporate_action_ledger(ledger_path, manifest_path)
    ledger_sha = canonical_ledger_sha256(provisional.events)
    manifest["ledger_sha256"] = ledger_sha
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    ledger = load_corporate_action_ledger(ledger_path, manifest_path)
    report = ledger.approval_report()
    return {
        "ledger_path": str(ledger_path.relative_to(ROOT)),
        "manifest_path": str(manifest_path.relative_to(ROOT)),
        "ledger_sha256": ledger_sha,
        "ledger_file_sha256": file_sha,
        "event_count": len(rows),
        "tickers": covered,
        "status": report.status,
        "approval_valid": report.approval_valid,
        "blockers": [
            {"code": blocker.code, "message": blocker.message} for blocker in report.blockers
        ],
    }


def krx_field_evidence() -> dict[str, Any]:
    """KRX ETF 시세 화면의 필드 정의를 확인한다(수정주가 파라미터 존재 여부)."""
    url = "https://data.krx.co.kr/contents/MDC/STAT/standard/MDCSTAT045.jsp"
    try:
        payload, status = http_get(url, timeout=20)
    except OSError as exc:
        return {"status": "unavailable", "reason": str(exc)[:120], "url": url}
    if status != 200 or not payload:
        return {"status": "unavailable", "http_status": status, "url": url}
    html = payload.decode("utf-8", errors="replace")
    return {
        "status": "ok",
        "url": url,
        "http_status": status,
        "sha256": sha256_bytes(payload),
        "close_field": "TDD_CLSPRC(종가)" if 'name="TDD_CLSPRC"' in html else "unknown",
        "has_adjustment_parameter": "adjStkPrc" in html,
        "note": (
            "ETF 시세 화면에는 수정주가 선택 파라미터가 없고, 종가 필드는 TDD_CLSPRC다. "
            "실제 수정 여부는 KIS 비수정/수정 종가 대조로 판정한다."
        ),
    }


# --------------------------------------------------------------------------- #
# 교차검증
# --------------------------------------------------------------------------- #
def annual_sum_checks(
    ticker: str, rows: list[dict[str, Any]], annual_trend: dict[str, Any]
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    totals: dict[str, float] = {}
    for row in rows:
        year = row["record_date"][:4]
        totals[year] = totals.get(year, 0.0) + float(row["amount_per_share"])
    last_year = max((row["record_date"][:4] for row in rows), default="")
    for item in (annual_trend or {}).get("money") or []:
        year = str(item.get("basicD"))
        expected = parse_float(item.get("divid"))
        actual = totals.get(year)
        if expected is None or actual is None:
            continue
        same = abs(expected - actual) < 1e-6
        status = "pass" if same else ("window_incomplete" if year == last_year else "fail")
        checks.append(
            {
                "check_id": f"annual_sum:{ticker}:{year}",
                "ticker": ticker,
                "check_type": "annual_sum_vs_api_trend",
                "source": KODEX_SOURCE_ID,
                "expected": expected,
                "actual": round(actual, 4),
                "abs_diff": round(abs(expected - actual), 4),
                "tolerance": 0.0,
                "status": status,
                "note": (
                    "같은 운용사 API의 연간 합계(별도 엔드포인트). "
                    "연도 미종료 시 미공시 이벤트가 포함될 수 있음"
                ),
            }
        )
    return checks


def notice_checks(
    notice_no: int, rows: list[dict[str, Any]], cycle_month: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    url = KODEX_NOTICE_URL.format(no=notice_no)
    payload, status = http_get(url, referer=KODEX_BASE + "/etf/lounge/notice.do")
    meta = {"url": url, "http_status": status, "sha256": sha256_bytes(payload)}
    if status != 200:
        return [], {**meta, "status": "unavailable"}
    table = parse_notice_table(payload.decode("utf-8", errors="replace"))
    if not table:
        return [], {**meta, "status": "parse_failed"}
    checks: list[dict[str, Any]] = []
    for ticker in sorted({row["ticker"] for row in rows}):
        notice_amount = table.get(ticker)
        matches = [
            row for row in rows if row["ticker"] == ticker and row["record_date"][:7] == cycle_month
        ]
        if notice_amount is None or not matches:
            continue
        api_amount = float(matches[0]["amount_per_share"])
        checks.append(
            {
                "check_id": f"notice:{ticker}:{cycle_month}",
                "ticker": ticker,
                "check_type": "notice_vs_api_amount",
                "source": "kodex_notice",
                "expected": api_amount,
                "actual": notice_amount,
                "abs_diff": round(abs(api_amount - notice_amount), 6),
                "tolerance": 0.0,
                "status": "pass" if abs(api_amount - notice_amount) < 1e-6 else "fail",
                "note": "공식 공지 HTML 표 vs API 이벤트(같은 운용사, 다른 산출물)",
            }
        )
    return checks, {**meta, "status": "ok", "rows": len(table)}


def parse_notice_table(html: str) -> dict[str, float]:
    """공지 HTML의 첫 표에서 종목코드→분배금(좌당)을 읽는다."""
    result: dict[str, float] = {}
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL):
        cells = [
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", cell)).strip()
            for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.DOTALL)
        ]
        if len(cells) < 4 or not re.fullmatch(r"[0-9A-Z]{6}", cells[0]):
            continue
        amount = parse_float(cells[3].replace(",", ""))
        if amount is not None:
            result[cells[0]] = amount
    return result


# --------------------------------------------------------------------------- #
# 실행
# --------------------------------------------------------------------------- #
def run(
    cache_dir: Path,
    *,
    notice_no: int = 75493,
    cycle_month: str = "2026-04",
    refresh_raw: bool = False,
) -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for stale in OUTPUT_DIR.glob("*.csv"):
        stale.unlink()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    retrieved_at = utc_now()

    universe = freeze_universe()
    metadata, metadata_status = fetch_krx_etf_metadata(universe)

    all_events: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    fetch_origins: dict[str, str] = {}
    fetch_status: dict[str, dict[str, Any]] = {}
    parse_errors: list[str] = []
    per_ticker_payload: dict[str, dict[str, Any]] = {}
    blocked_reason: str | None = None

    for ticker in universe:
        meta = metadata.get(ticker, {})
        fund_id = KODEX_FUND_IDS.get(ticker)
        if fund_id is None:
            fetch_status[ticker] = {
                "status": "not_available",
                "reason": "공식 원천 접근 실패(이 환경) — 이벤트 없음이 아니라 미확보",
            }
            coverage_rows.append(
                build_coverage(
                    ticker=ticker,
                    issuer=_issuer(ticker),
                    name=str(meta.get("name") or ""),
                    listed_date=_to_date(meta.get("listed_date")),
                    rows=[],
                    months=set(),
                    conflicts=False,
                    source_available=False,
                )
            )
            continue
        if blocked_reason is not None:
            fetch_status[ticker] = {
                "status": "skipped_blocked",
                "reason": blocked_reason,
            }
            coverage_rows.append(
                build_coverage(
                    ticker=ticker,
                    issuer=_issuer(ticker),
                    name=str(meta.get("name") or ""),
                    listed_date=_to_date(meta.get("listed_date")),
                    rows=[],
                    months=set(),
                    conflicts=False,
                    source_available=False,
                )
            )
            continue
        url = KODEX_DIVID_URL.format(fid=fund_id)
        try:
            payload, digest, origin, collected_at = cached_json(
                url,
                OUTPUT_DIR / "raw" / f"kodex_{ticker}.json",
                referer=KODEX_BASE + "/etf/product/distribution.do",
                refresh=refresh_raw,
            )
        except (RuntimeError, OSError, ValueError) as exc:
            reason = str(exc)[:200]
            fetch_status[ticker] = {"status": "failed", "reason": reason}
            parse_errors.append(f"{ticker}:fetch_failed:{reason}")
            coverage_rows.append(
                build_coverage(
                    ticker=ticker,
                    issuer=_issuer(ticker),
                    name=str(meta.get("name") or ""),
                    listed_date=_to_date(meta.get("listed_date")),
                    rows=[],
                    months=set(),
                    conflicts=False,
                    source_available=False,
                )
            )
            # 429/403은 더 호출하지 않고 남은 종목을 미확보로 둔다.
            if "429" in reason or "403" in reason:
                blocked_reason = f"rate_limited_or_blocked: {reason}"
            continue
        fetch_origins[ticker] = origin
        fetch_status[ticker] = {
            "status": "ok",
            "origin": origin,
            "first_collected_at": collected_at,
            "sha256": digest,
        }
        if not payload or not payload.get("dividList"):
            parse_errors.append(f"{ticker}:empty_payload")
            coverage_rows.append(
                build_coverage(
                    ticker=ticker,
                    issuer=_issuer(ticker),
                    name=str(meta.get("name") or ""),
                    listed_date=_to_date(meta.get("listed_date")),
                    rows=[],
                    months=set(),
                    conflicts=False,
                    source_available=False,
                )
            )
            continue
        per_ticker_payload[ticker] = payload
        fund_name = str((payload.get("dividInfo") or {}).get("fNm") or "")
        krx_name = str(meta.get("name") or "")
        if fund_name and krx_name and _normalize_name(fund_name) != _normalize_name(krx_name):
            parse_errors.append(f"{ticker}:fund_name_mismatch:{fund_name}!={krx_name}")
        rows, errors = normalize_kodex_events(
            ticker,
            isin=str(meta.get("isin") or ""),
            name=fund_name or krx_name,
            payload=payload,
            source_url=url,
            source_sha256=digest,
            retrieved_at=collected_at,
        )
        parse_errors.extend(errors)
        all_events.extend(rows)
        coverage_rows.append(
            build_coverage(
                ticker=ticker,
                issuer=_issuer(ticker),
                name=fund_name or krx_name,
                listed_date=_to_date(meta.get("listed_date")),
                rows=rows,
                months=schedule_months(payload),
                conflicts=any("conflicting_duplicate" in error for error in errors),
                source_available=True,
            )
        )

    calendar = build_market_calendar(cache_dir, universe)
    all_events, calendar_errors = attach_ex_dates(all_events, calendar)
    parse_errors.extend(calendar_errors)

    event_frame = pd.DataFrame(all_events)
    if not event_frame.empty:
        event_frame = event_frame.assign(
            in_backtest_window=event_frame["record_date"].map(
                lambda value: in_window(value, BACKTEST_START, BACKTEST_END)
            )
        )
        event_frame = event_frame.sort_values(["ticker", "record_date"]).reset_index(drop=True)
    event_frame.to_csv(OUTPUT_DIR / "distribution_events.csv", index=False)

    coverage_frame = pd.DataFrame(coverage_rows)
    coverage_frame.to_csv(OUTPUT_DIR / "distribution_coverage.csv", index=False)

    source_frame = pd.DataFrame(SOURCE_SURVEY)
    source_frame["checked_at"] = "2026-11-27"
    source_frame.to_csv(OUTPUT_DIR / "distribution_source_inventory.csv", index=False)

    # 교차검증: 연간 합계 + 공지 문서 + KRX 수정주가 파라미터
    checks: list[dict[str, Any]] = []
    for ticker, payload in per_ticker_payload.items():
        rows = [row for row in all_events if row["ticker"] == ticker]
        checks.extend(annual_sum_checks(ticker, rows, payload.get("annualTrend") or {}))
    if blocked_reason is None:
        notice_rows, notice_meta = notice_checks(notice_no, all_events, cycle_month)
        checks.extend(notice_rows)
    else:
        notice_rows, notice_meta = [], {
            "status": "skipped_blocked",
            "reason": blocked_reason,
        }
    field_evidence = krx_field_evidence()
    price_basis = price_basis_probe(cache_dir, all_events)
    pd.DataFrame(
        checks,
        columns=[
            "check_id",
            "ticker",
            "check_type",
            "source",
            "expected",
            "actual",
            "abs_diff",
            "tolerance",
            "status",
            "note",
        ],
    ).to_csv(OUTPUT_DIR / "distribution_crosscheck.csv", index=False)

    # E2E는 요구 기간 내 이벤트가 있는 종목만 수행한다.
    # 캐시가 원주가로 확인되면 분배금 가산이 정상이고, 조정 계열이면 이중 계상이다.
    e2e: dict[str, Any] = {}
    cache_basis = price_basis.get("price_basis", "unknown")
    basis_confirmed = (
        cache_basis == "raw"
        and price_basis.get("ex_date_behavior") == "adjusted_series_removes_ex_date_drop"
    )
    kis_blocked_error: str | None = None
    for ticker in sorted({row["ticker"] for row in all_events}):
        events = [
            row
            for row in all_events
            if row["ticker"] == ticker and row.get("ex_date") and in_window(row["record_date"], BACKTEST_START, BACKTEST_END)
        ]
        price_path = cache_dir / f"{ticker}.parquet"
        if not events or not price_path.exists():
            continue
        cache_price = load_price(cache_dir, ticker)
        window_start = date.fromisoformat(min(row["ex_date"] for row in events))
        price_slice = slice_from_prior_trading_day(cache_price, window_start)
        window_events = [
            row
            for row in events
            if window_start.isoformat()
            <= row["ex_date"]
            <= price_slice["date"].iloc[-1].date().isoformat()
        ]
        if not window_events or len(price_slice) < 2:
            continue

        if price_basis.get("rate_limited"):
            kis_blocked_error = str(price_basis.get("reason") or "kis_rate_limited")
        result = total_return_e2e(price_slice, window_events, price_basis=f"cache_{cache_basis}")
        result["total_return_valid"] = basis_confirmed
        result["price_source_error"] = ""

        adjusted_check: dict[str, Any] = {"status": "unavailable"}
        confirmation_dates: list[str] = []
        if kis_blocked_error is not None:
            adjusted_check = {"status": "skipped_blocked", "reason": kis_blocked_error}
        try:
            if kis_blocked_error is not None:
                raise RuntimeError(kis_blocked_error)
            adjusted = kis_daily_series(
                ticker,
                price_slice["date"].iloc[0].date(),
                BACKTEST_END,
                org_adj_prc="0",
            )
            adjusted = (
                adjusted[["date", "close"]]
                .assign(date=pd.to_datetime(adjusted["date"]), ticker=ticker)
                .sort_values("date")
                .reset_index(drop=True)
            )
            if len(adjusted) >= 2:
                aligned = (
                    adjusted.set_index("date")["close"]
                    .reindex(price_slice["date"])
                    .astype(float)
                )
                all_ok = bool(not aligned.isna().any())
                start_ok = all_ok and not pd.isna(aligned.iloc[0])
                end_ok = all_ok and not pd.isna(aligned.iloc[-1])
                adjusted_return = (
                    float(aligned.iloc[-1] / aligned.iloc[0]) - 1
                ) * 100 if start_ok and end_ok else float("nan")
                diff = abs(adjusted_return - result["total_return_pct"])
                adjusted_slice = price_slice[["date", "ticker"]].assign(
                    close=aligned.to_numpy()
                )
                adjusted_check = {
                    "status": (
                        "pass"
                        if diff < 1.0 and all_ok
                        else ("incomplete" if not all_ok else "review")
                    ),
                    "kis_flag0_price_return_pct": round(adjusted_return, 4),
                    "abs_diff_vs_tr_pct_points": round(diff, 4),
                    "window_aligned": all_ok,
                    "note": (
                        "분배조정 계열(flag0)은 분배금이 이미 반영되어 있어 가산하지 않고 "
                        "TR과 비교한다. 별도 원천의 독립 검산"
                    ),
                }
                if all_ok:
                    double_count = total_return_e2e(
                        adjusted_slice,
                        window_events,
                        OUTPUT_DIR / "double_count_illustration",
                        price_basis="kis_flag0_adjusted",
                    )
                    result["double_count_illustration"] = {
                        "total_return_pct": double_count["total_return_pct"],
                        "note": (
                            "조정 계열에 분배금을 다시 더한 값(이중 계상). "
                            "승인 근거로 사용하지 않음"
                        ),
                    }
            raw_flag = kis_daily_series(
                ticker,
                price_slice["date"].iloc[0].date(),
                BACKTEST_END,
                org_adj_prc="1",
            )
            raw_flag = raw_flag[["date", "close"]].assign(date=pd.to_datetime(raw_flag["date"]))
            raw_flag = raw_flag.sort_values("date").reset_index(drop=True)
            raw_flag["ret"] = raw_flag["close"].pct_change()
            confirmation_dates = [
                str(value.date())
                for value in raw_flag.loc[raw_flag["ret"].abs().gt(0.15).fillna(False), "date"]
            ]
        except KIS_ERRORS as exc:
            reason = str(exc)[:160]
            if adjusted_check.get("status") != "skipped_blocked":
                adjusted_check = {"status": "unavailable", "reason": reason}
            if is_rate_limit_error(exc):
                kis_blocked_error = f"kis_rate_limited: {reason}"

        result["adjusted_price_check"] = adjusted_check
        result["anomaly_confirmed"] = sorted(
            set(result["anomaly_detected"]) & set(confirmation_dates)
        )
        e2e[ticker] = {
            "total_return": result,
            "entitlement": entitlement_case(window_events, price_slice),
            "receivable": receivable_case(window_events),
        }

    ledger = write_ledger(
        [row for row in all_events if in_window(row["record_date"], BACKTEST_START, BACKTEST_END)],
        coverage_rows,
    )

    status_counts = coverage_frame["status"].value_counts().to_dict() if not coverage_frame.empty else {}
    result = {
        "generated_at": retrieved_at,
        "run_id": run_id,
        "backtest_window": {"start": BACKTEST_START.isoformat(), "end": BACKTEST_END.isoformat()},
        "universe": universe,
        "pilot_tickers": list(PILOT_TICKERS),
        "sources": source_frame.to_dict(orient="records"),
        "metadata": {
            "status": metadata_status,
            "matched_frozen": sorted(metadata),
            "isin_coverage": sum(1 for item in metadata.values() if item.get("isin")),
            "fund_id_source": "recorded_2026-11-27",
        },
        "fetch_origin": fetch_origins,
        "events": {
            "total": len(event_frame),
            "in_window": event_frame["in_backtest_window"].sum() if not event_frame.empty else 0,
            "by_ticker": event_frame.groupby("ticker").size().to_dict() if not event_frame.empty else {},
            "parse_errors": parse_errors,
        },
        "coverage": {
            "counts": status_counts,
            "tickers": coverage_frame.to_dict(orient="records"),
        },
        "crosscheck": {
            "checks": checks,
            "notice": notice_meta,
            "krx_field": field_evidence,
            "price_basis": price_basis,
        },
        "fetch_status": fetch_status,
        "ex_date_provenance": {
            "rule": "분배락일 = 지급기준일 직전 거래일(국내 T+2 결제)",
            "calendar_source": "가격 캐시 거래일(동결 16종 union)",
            "calendar_start": calendar[0].isoformat() if calendar else "",
            "calendar_end": calendar[-1].isoformat() if calendar else "",
            "calendar_days": len(calendar),
            "official_calendar_verified": False,
            "official_ex_date_verified": False,
            "note": "거래소 공식 캘린더/운용사 배당락일 공시로 검증하지 않은 파생값",
        },
        "e2e": e2e,
        "ledger": ledger,
        "total_return_approvable": False,
        "blockers": [
            {
                "code": "DISTRIBUTION-COVERAGE",
                "status": "open",
                "message": "공식 원천 확보 4/16종, 2016-2021 구간 및 TIGER/ACE/PLUS/RISE 미확보",
            },
            {
                "code": "EX-DATE-DERIVED",
                "status": "open",
                "message": "배당락일은 원천 필드가 아니라 기준일 직전 거래일로 파생(공식 검증 없음)",
            },
            {
                "code": "PRICE-BASIS",
                "status": "watch",
                "message": (
                    "캐시는 원주가로 확인됨(이중 계상 없음). 다만 조정 계열에 분배금을 더하면 "
                    "이중 계상되므로 엔진/CSV 입력의 가격 기준을 고정해야 함"
                ),
            },
            {
                "code": "PAYMENT-TIMING",
                "status": "open",
                "message": "백테스트는 배당락일에 현금 반영, 실제 지급일과 지연 차이 존재",
            },
            {
                "code": "TAX-BASE",
                "status": "open",
                "message": "과세표준액은 확보했으나 세율은 계좌 유형 의존 → 임의 확정 불가",
            },
            {
                "code": "INDEPENDENT-SOURCE",
                "status": "open",
                "message": "교차검증은 같은 운용사 산출물/공지로 한정, 독립 원천 아님",
            },
        ],
        "limits": [
            "KODEX API는 펀드별 최근 5년만 제공",
            "TIGER(403)/ACE(API 타임아웃)/PLUS·RISE(연결 실패)는 이 환경에서 미확보",
            "배당락일 파생 규칙은 KRX/운용사 관행에 기반하며 원천 문서로 확인하지 않음",
            "세후 수치는 15.4% 가정 시나리오이며 계좌·상품 유형에 따라 달라짐",
        ],
        "env_presence": env_presence(),
        "pykrx_version": pykrx_version(),
    }
    (OUTPUT_DIR / "pilot_validation.json").write_text(
        json.dumps(sanitize_json(result), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )

    provenance = {
        "run_id": run_id,
        "generated_at": retrieved_at,
        "git_commit": git_commit(),
        "code_sha256": {
            "scripts/p2a2a_etf_distributions.py": sha256_file(Path(__file__)),
            "strategy_freeze.json": sha256_file(ROOT / "strategy_freeze.json"),
        },
        "price_cache": {
            ticker: {
                "sha256": sha256_file(cache_dir / f"{ticker}.parquet"),
                "bytes": (cache_dir / f"{ticker}.parquet").stat().st_size,
            }
            for ticker in universe
            if (cache_dir / f"{ticker}.parquet").exists()
        },
        "raw_responses": {
            ticker: {
                "first_collected_at": item.get("first_collected_at"),
                "sha256": item.get("sha256"),
                "origin": item.get("origin"),
            }
            for ticker, item in fetch_status.items()
            if item.get("status") == "ok"
        },
        "kis_raw": {
            path.name: {"sha256": sha256_file(path), "bytes": path.stat().st_size}
            for path in sorted(KIS_RAW_DIR.glob("*.csv"))
        },
        "sources": [
            {
                "source_id": item["source_id"],
                "url": item["url"],
                "access_result": item["access_result"],
            }
            for item in SOURCE_SURVEY
            if item["source_id"] in {"kodex_api", "kodex_notice", "krx_mdc", "kind"}
        ],
        "pykrx_version": pykrx_version(),
        "env_presence": env_presence(),
        "artifacts": {},
    }
    for name in [
        "distribution_source_inventory.csv",
        "distribution_events.csv",
        "distribution_coverage.csv",
        "distribution_crosscheck.csv",
        "pilot_validation.json",
        "distribution_ledger.csv",
        "distribution_manifest.json",
    ]:
        path = OUTPUT_DIR / name
        if path.exists():
            provenance["artifacts"][name] = {
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
    (OUTPUT_DIR / "provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def sanitize_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): sanitize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json(item) for item in value]
    if isinstance(value, (np.floating, float)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (pd.Timestamp, date, datetime)):
        return value.isoformat()
    return value


def git_commit() -> str:
    import subprocess

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "unknown"


def _issuer(ticker: str) -> str:
    if ticker in {"069500", "091160", "091170", "498400"}:
        return "삼성자산운용(KODEX)"
    if ticker in {"360200", "411060"}:
        return "한국투자(ACE)"
    if ticker in {"161510"}:
        return "한화자산운용(PLUS)"
    if ticker in {"0101N0", "367760"}:
        return "KB자산운용(RISE)"
    return "미래에셋(TIGER)"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(os.environ.get("ETF_PRICE_CACHE_DIR", ROOT / "data_cache")),
        help="가격 캐시 디렉터리(읽기 전용)",
    )
    parser.add_argument("--notice-no", type=int, default=75493)
    parser.add_argument("--cycle-month", default="2026-04")
    parser.add_argument(
        "--refresh-raw",
        action="store_true",
        help="저장된 원본 응답을 무시하고 다시 조회한다",
    )
    args = parser.parse_args()

    load_env_file(Path(os.environ.get("ETF_ENV_FILE", ROOT / ".env")))
    result = run(
        args.cache_dir,
        notice_no=args.notice_no,
        cycle_month=args.cycle_month,
        refresh_raw=args.refresh_raw,
    )
    counts = result["coverage"]["counts"]
    print(
        f"[p2a2a] 완료: {OUTPUT_DIR} "
        f"(events={result['events']['total']}, coverage={counts}, "
        f"ledger={result['ledger']['status']}, approvable=False)"
    )


if __name__ == "__main__":
    main()
