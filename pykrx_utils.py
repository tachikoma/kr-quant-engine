"""pykrx 호출 관련 유틸리티

- fd 수준(stdout/stderr) 캡처와 주말 범위 스킵 판별 함수를 제공합니다.
- 원래 `run_etf_backtest.py`에 있던 로직을 공용 모듈로 추출했습니다.
- KRX ETF 분류 데이터 조회/캐싱 함수를 포함합니다.

모든 주석은 한국어로 작성되어 있으며, 함수 이름은 기존 코드와 호환되도록 언더스코어 접두사를 유지합니다.
"""
from __future__ import annotations

import io
import os
import time
import contextlib
import sys
from pathlib import Path
import pandas as pd


def _call_capture_stderr(func, *args, **kwargs):
    """pykrx 호출 시 Python 레벨 출력과 OS fd(1/2) 레벨 출력을 함께 캡처합니다.

    - Python 레벨 출력은 `contextlib.redirect_stdout/redirect_stderr`로 캡처합니다.
    - C/확장 모듈이 직접 쓰는 fd(1/2)는 `os.pipe()` + `os.dup2()`로 임시 리다이렉트해 캡처합니다.

    캡처된 출력은 각각 `[pykrx-stdout]` / `[pykrx-stderr]`로 재로그됩니다.
    """
    buf_out = io.StringIO()
    buf_err = io.StringIO()

    # 파이프 생성 (read, write)
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()

    # 현재 stdout/stderr 백업
    saved_stdout = os.dup(1)
    saved_stderr = os.dup(2)

    res = None
    exc = None
    try:
        # fd-level 리다이렉트: fd(1)->out_w, fd(2)->err_w
        os.dup2(out_w, 1)
        os.dup2(err_w, 2)
        # 로컬 복사 닫기
        os.close(out_w)
        os.close(err_w)

        try:
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                res = func(*args, **kwargs)
        except Exception as e:
            exc = e
    finally:
        # Python-level 스트림 flush
        try:
            sys.stdout.flush()
        except Exception:
            pass
        try:
            sys.stderr.flush()
        except Exception:
            pass

        # 원래 fd 복원
        try:
            os.dup2(saved_stdout, 1)
            os.dup2(saved_stderr, 2)
        except Exception:
            pass
        try:
            os.close(saved_stdout)
        except Exception:
            pass
        try:
            os.close(saved_stderr)
        except Exception:
            pass

        # 파이프에서 읽기
        out_bytes = b""
        err_bytes = b""
        try:
            def _read_all(fd):
                chunks = []
                while True:
                    chunk = os.read(fd, 8192)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks)

            out_bytes = _read_all(out_r)
            err_bytes = _read_all(err_r)
        except Exception:
            pass
        finally:
            try:
                os.close(out_r)
            except Exception:
                pass
            try:
                os.close(err_r)
            except Exception:
                pass

    # 버퍼 결합 및 출력
    py_out = buf_out.getvalue().strip()
    py_err = buf_err.getvalue().strip()
    fd_out = out_bytes.decode(errors="ignore").strip()
    fd_err = err_bytes.decode(errors="ignore").strip()

    combined_out = "\n".join([s for s in (py_out, fd_out) if s])
    combined_err = "\n".join([s for s in (py_err, fd_err) if s])

    if combined_out:
        print(f"[pykrx-stdout] {combined_out}")
    if combined_err:
        print(f"[pykrx-stderr] {combined_err}")

    if exc:
        raise exc
    return res


def _range_has_weekday(start_ymd: str, end_ymd: str) -> bool:
    """주어진 YYYYMMDD 범위에 평일(Mon-Fri)이 하나라도 있는지 확인합니다.

    - 파싱 실패 또는 역구간인 경우 보수적으로 `True`를 반환하여 조회를 허용합니다.
    - 공휴일 판별은 하지 않으므로 주말만 가득한 경우에만 조회를 건너뜁니다.
    """
    try:
        s = pd.to_datetime(start_ymd, errors="coerce")
        e = pd.to_datetime(end_ymd, errors="coerce")
    except Exception:
        return True
    if pd.isna(s) or pd.isna(e) or s > e:
        return True
    try:
        dr = pd.date_range(s, e, freq="D")
        return any(d.weekday() < 5 for d in dr)
    except Exception:
        return True


def get_ticker_name(ticker: str) -> str:
    """종목 코드로 종목명을 조회합니다.

    ENABLE_TICKER_NAME_LOOKUP=1 이고 KRX 인증 정보가 있을 때만 pykrx를 호출합니다.
    비활성화 또는 실패 시 ticker 코드를 그대로 반환합니다.
    """
    # 함수 호출 시점에 읽어야 load_dotenv() 이후 값이 반영됨
    if os.environ.get("ENABLE_TICKER_NAME_LOOKUP", "0") != "1":
        return ticker
    if not (os.environ.get("KRX_ID") and os.environ.get("KRX_PW")):
        return ticker
    try:
        from pykrx import stock as _stock  # lazy import: load_dotenv() 이후에 실행됨

        def _is_valid(val: object) -> bool:
            """pykrx 반환값이 유효한 종목명인지 확인합니다."""
            if val is None:
                return False
            if hasattr(val, "empty") and val.empty:
                return False
            s = str(val).strip()
            return bool(s)

        buf = io.StringIO()
        # 1단계: ETF 전용 API 시도 (일반 주식 API보다 ETF에서 더 정확)
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            name = _stock.get_etf_ticker_name(ticker)
        if _is_valid(name):
            return str(name).strip()

        # 2단계: 일반 주식 API 폴백 (ETF가 아닌 종목 커버)
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2), contextlib.redirect_stderr(buf2):
            name = _stock.get_market_ticker_name(ticker)
        if _is_valid(name):
            return str(name).strip()

        return ticker
    except Exception:
        return ticker


KRX_PASSWORD_CHANGE_URL = "https://data.krx.co.kr"


def check_krx_auth_status() -> str:
    """KRX 세션 인증 상태를 확인한다.

    Returns:
        "ok"                   — 정상 로그인 상태
        "no_credentials"       — KRX_ID/KRX_PW 미설정
        "password_change_needed" — CD010 (비밀번호 변경 필요)
        "auth_failed"          — 기타 인증 실패
    """
    if not (os.environ.get("KRX_ID") and os.environ.get("KRX_PW")):
        return "no_credentials"

    from pykrx.website.comm.auth import build_krx_session, get_auth_session

    session = get_auth_session()
    if session is not None and session.is_valid():
        return "ok"

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        build_krx_session(os.environ["KRX_ID"], os.environ["KRX_PW"])
    output = buf.getvalue()
    if "패스워드 변경 필요" in output or "CD010" in output:
        return "password_change_needed"
    return "auth_failed"


TAX_CACHE_PATH = "data_cache/etf_tax_classification.parquet"


def _empty_etf_ohlcv_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "date",
            "ticker",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "trading_value",
            "nav",
            "base_index",
        ]
    )


def normalize_etf_ohlcv_with_nav(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """ETF OHLCV/NAV 데이터를 공통 스키마로 정규화한다.

    pykrx의 ETF 전용 OHLCV는 NAV/기초지수를 포함할 수 있고, 일반 market OHLCV는
    가격/거래량만 포함한다. 이 함수는 두 반환 포맷을 같은 컬럼으로 맞춘다.
    """
    if df is None or df.empty:
        return _empty_etf_ohlcv_frame()

    data = df.reset_index().rename(
        columns={
            "날짜": "date",
            "시가": "open",
            "고가": "high",
            "저가": "low",
            "종가": "close",
            "거래량": "volume",
            "거래대금": "trading_value",
            "NAV": "nav",
            "기초지수": "base_index",
        }
    )
    data["ticker"] = str(ticker)

    if "date" not in data.columns and "index" in data.columns:
        data = data.rename(columns={"index": "date"})

    required_columns = ["date", "ticker", "open", "close"]
    missing_columns = [col for col in required_columns if col not in data.columns]
    if missing_columns:
        raise ValueError(
            f"{ticker}: missing columns {missing_columns}; actual columns={list(data.columns)}"
        )

    if "high" not in data.columns:
        data["high"] = data["close"]
    if "low" not in data.columns:
        data["low"] = data["close"]
    if "volume" not in data.columns:
        data["volume"] = 0
    if "trading_value" not in data.columns:
        data["trading_value"] = data["close"] * data["volume"]
    if "nav" not in data.columns:
        data["nav"] = pd.NA
    if "base_index" not in data.columns:
        data["base_index"] = pd.NA

    out = data[
        [
            "date",
            "ticker",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "trading_value",
            "nav",
            "base_index",
        ]
    ].copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    for col in ["open", "high", "low", "close", "volume", "trading_value", "nav", "base_index"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


def fetch_etf_ohlcv_with_nav(start: str, end: str, ticker: str) -> pd.DataFrame:
    """ETF OHLCV를 NAV 포함 스키마로 조회한다.

    ETF 전용 API가 실패하면 일반 OHLCV API로 폴백하고 `nav`는 결측으로 둔다.
    따라서 NAV 기반 랭킹을 켜지 않은 기존 실행은 계속 price-only로 동작한다.
    """
    from pykrx import stock as _stock

    fallback_reason = ""
    try:
        raw = _stock.get_etf_ohlcv_by_date(start, end, ticker)
        normalized = normalize_etf_ohlcv_with_nav(raw, ticker)
        if not normalized.empty:
            return normalized
        fallback_reason = "ETF OHLCV가 비어있음"
    except Exception as exc:
        fallback_reason = str(exc)

    try:
        raw = _stock.get_market_ohlcv_by_date(start, end, ticker)
        normalized = normalize_etf_ohlcv_with_nav(raw, ticker)
        if fallback_reason:
            print(f"[pykrx_utils] {ticker} ETF OHLCV/NAV 조회 실패 → market OHLCV 폴백: {fallback_reason}")
        return normalized
    except Exception as exc:
        raise RuntimeError(
            f"{ticker}: ETF/market OHLCV 조회 모두 실패. ETF 오류={fallback_reason}; market 오류={exc}"
        ) from exc


def _fetch_krx_etf_classification() -> pd.DataFrame:
    """pykrx 내부 API를 통해 KRX 전종목 ETF 분류 데이터를 조회한다.
    KRX 로그인(KRX_ID/KRX_PW)이 필요하다.
    """
    from pykrx.website.krx.etx.core import ETF_전종목기본종목

    df = ETF_전종목기본종목().fetch()
    if df is None or df.empty:
        raise ValueError("ETF classification data is empty (KRX login may be required)")
    return df


def fetch_and_cache_tax_classification(cache_path: str = TAX_CACHE_PATH) -> pd.DataFrame:
    """KRX ETF 분류 데이터를 조회하여 parquet 캐시에 저장한다.
    로컬에 KRX_ID/KRX_PW가 설정되어 있어야 한다.
    """
    df = _fetch_krx_etf_classification()
    Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path, index=False)
    print(f"[pykrx_utils] ETF 분류 데이터 캐시 저장 완료: {cache_path} ({len(df)}개 종목)")
    return df


def load_tax_classification(cache_path: str = TAX_CACHE_PATH) -> pd.DataFrame | None:
    """캐시에서 ETF 분류 데이터를 로드한다. 없으면 None."""
    p = Path(cache_path)
    if p.exists():
        try:
            return pd.read_parquet(cache_path)
        except Exception:
            return None
    return None


def get_listing_dates(
    ticker_subset: set[str] | None = None,
    cache_path: str = TAX_CACHE_PATH,
) -> dict[str, str]:
    """KRX ETF 분류 캐시의 LIST_DD 컬럼으로 상장일 맵을 반환한다.

    캐시가 없으면 KRX 분류 데이터를 조회해 캐시를 만든다. 실패 시 빈 dict를 반환하며,
    호출자는 상장일 미상 종목을 lenient하게 허용할 수 있다.
    """
    df = load_tax_classification(cache_path)
    if df is None or df.empty:
        try:
            df = fetch_and_cache_tax_classification(cache_path)
        except Exception:
            return {}

    if df is None or df.empty or "ISU_SRT_CD" not in df.columns or "LIST_DD" not in df.columns:
        return {}

    data = df[["ISU_SRT_CD", "LIST_DD"]].copy()
    data["ISU_SRT_CD"] = data["ISU_SRT_CD"].astype(str).str.strip()
    data["LIST_DD"] = data["LIST_DD"].astype(str).str.replace(r"\D", "", regex=True)
    data = data[(data["ISU_SRT_CD"] != "") & (data["LIST_DD"] != "")]

    if ticker_subset:
        ticker_subset = {str(t).strip() for t in ticker_subset}
        data = data[data["ISU_SRT_CD"].isin(ticker_subset)]

    return dict(zip(data["ISU_SRT_CD"], data["LIST_DD"], strict=False))


def get_taxable_tickers(
    ticker_subset: set[str] | None = None,
    cache_path: str = TAX_CACHE_PATH,
    max_age_days: int = 30,
) -> set[str] | None:
    """KRX ETF 분류 데이터를 기반으로 과세 대상 ticker set을 반환한다.

    1순위: data_cache/ parquet 캐시 (max_age_days 이내)
    2순위: KRX API 실시간 조회 (KRX_ID/KRX_PW 필요)
    실패 시 None 반환 (호출자가 hardcoded fallback 처리)

    ticker_subset이 주어지면 해당 subset 내에서만 필터링한다.
    """
    df = None

    # 1순위: 캐시 확인
    p = Path(cache_path)
    if p.exists():
        try:
            age = time.time() - p.stat().st_mtime
            if age < max_age_days * 86400:
                df = pd.read_parquet(cache_path)
        except Exception:
            pass

    # 2순위: KRX API 실시간 조회 (캐시가 없거나 만료된 경우)
    if df is None:
        try:
            df = _fetch_krx_etf_classification()
            Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(cache_path, index=False)
        except Exception:
            return None

    if df is None or df.empty:
        return None

    taxable = set(df.loc[df["TAX_TP_CD"] != "비과세", "ISU_SRT_CD"].unique())
    if ticker_subset:
        taxable &= ticker_subset
    return taxable


def format_ticker(ticker: str) -> str:
    """종목 코드를 '종목명(코드)' 형태로 포맷합니다.

    ENABLE_TICKER_NAME_LOOKUP=1 이고 종목명 조회 성공 시: 'KODEX 200(069500)' 반환
    비활성화 또는 실패 시: '069500' (ticker 코드) 그대로 반환
    """
    name = get_ticker_name(ticker)
    if name == ticker:
        return ticker
    return f"{name}({ticker})"
