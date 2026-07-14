from pathlib import Path
from collections.abc import Callable
import json
import logging
import os
import argparse
from datetime import date, datetime

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except Exception:
    yf = None

logger = logging.getLogger(__name__)

# 백테스트 전용: 기본 슬리피지 및 호가 스프레드 (환경변수로 재정의 가능)
# ETF_BASE_SLIPPAGE: 예) 0.0005 (5bp)
# ETF_SPREAD_PCT: 예) 0.0005 (기본 0.0005)
from config_utils import parse_pct_env, parse_fraction_env
from etf_distributions import (
    add_distributions,
    distribution_cash_for_holdings,
    distributions_file_sha256,
    distributions_path,
    load_distributions,
)


def load_dotenv(dotenv_path: str | Path | None = None) -> None:
    path = Path(dotenv_path) if dotenv_path is not None else Path(__file__).resolve().parent / ".env"
    if not path.exists():
        return

    try:
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
                value = value.strip()
                if not (value.startswith('"') and value.endswith('"')) \
                   and not (value.startswith("'") and value.endswith("'")):
                    comment_idx = value.find(" #")
                    if comment_idx > 0:
                        value = value[:comment_idx].strip()
                value = value.strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        # 실패해도 무시하고 진행
        pass


load_dotenv()


from pykrx_utils import (
    _call_capture_stderr,
    _range_has_weekday,
    fetch_etf_ohlcv_with_nav,
    get_listing_dates,
    get_ticker_name,
)
from etf_shared import (
    ETF_LIST,
    ETF_MAX_POSITIONS,
    ETF_SELL_RANK_BUFFER,
    ETF_TAXABLE_SELL_TAX_PCT,
    KOSPI_INDEX_CODE,
    MARKET_MA_DAYS,
    MARKET_SLOPE_DAYS,
    TAXABLE_ETF_TICKERS,
    rank_etfs,
    apply_buy_cost,
    apply_sell_value,
    build_rebalance_orders,
    add_deviation_flag,
    add_liquidity_flag,
    add_listing_flag,
    add_price_basis_columns,
    get_valuation_price,
    get_strategy_config,
    get_allowed_groups,
    is_ticker_allowed,
    is_ticker_risk_on,
    update_last_valid_prices,
)

strategy_cfg = get_strategy_config()
REBALANCE_STEP_DAYS = strategy_cfg["rebalance_step_days"]  # env override 반영 (기본 10)
SLIPPAGE_PCT = parse_pct_env("ETF_BASE_SLIPPAGE", strategy_cfg.get("default_slippage_pct", 0.0005))
SPREAD_PCT = parse_pct_env("ETF_SPREAD_PCT", strategy_cfg.get("spread_pct", 0.0005))
BASE_SLIPPAGE = SLIPPAGE_PCT

from pykrx import stock

HAS_KRX_CREDENTIALS = bool(os.environ.get("KRX_ID") and os.environ.get("KRX_PW"))

# KRX 인증 정보 검증
if not HAS_KRX_CREDENTIALS:
    print("⚠️  경고: KRX_ID 또는 KRX_PW 환경 변수가 설정되지 않았습니다.")
    print("   .env 파일을 생성하고 KRX 인증 정보를 설정해주세요.")
    print("   예: cp .env.sample .env && nano .env")
    print()

INITIAL_CASH = 1_000_000
OUTPUT_DIR = Path("outputs_etf_only")

# 실행 모드: single(일반 백테스트) | experiment(슬리피지 민감도 비교)
RUN_MODE = os.environ.get("ETF_BACKTEST_MODE", "single").strip().lower()

# 일반 백테스트 기본 슬리피지(퍼센트 단위, 예: 0.001 = 10bp)

# 슬리피지 민감도 테스트 옵션(퍼센트 단위, 예: 0.0005 = 5bp)
SLIPPAGE_OPTIONS = [0.0005, 0.001, 0.002, 0.003]

# 단일 실행 시 벤치마크 비교 결과 포함 여부
ENABLE_BENCHMARK = os.environ.get("ETF_ENABLE_BENCHMARK", "1") == "1"

# 비교 실험을 위한 시장 필터(risk-on/off) 사용 여부
USE_MARKET_FILTER = True
ENABLE_MULTI_INDEX_RISK = os.environ.get("ENABLE_MULTI_INDEX_RISK", "0") == "1"
MULTI_INDEX_GATING_MODE = (
    os.environ.get("MULTI_INDEX_GATING_MODE", "hybrid").strip().lower() or "hybrid"
)
US_RISK_PROXY = os.environ.get("US_RISK_PROXY", "SPY").strip().upper() or "SPY"
US_MARKET_MA_DAYS = int(os.environ.get("US_MARKET_MA_DAYS", str(MARKET_MA_DAYS)))
US_MARKET_SLOPE_DAYS = int(os.environ.get("US_MARKET_SLOPE_DAYS", str(MARKET_SLOPE_DAYS)))

# ETF 후보군 선택 관련 상수는 etf_shared 모듈에서 관리합니다.

BENCHMARK_TICKER = "069500"  # KODEX 200


PERIODS = [
    ("2016_2019", "2016-01-01", "2019-12-31"),
    ("2020_2021", "2020-01-01", "2021-12-31"),
    ("2022_2023", "2022-01-01", "2023-12-31"),
    ("2024_2026", "2024-01-01", "2026-04-30"),
]

# 백테스트 기본 기간: 시작일 기본은 20160105, 종료일 기본은 오늘(또는 마지막 영업일)
START_DEFAULT = "20160105"
END_DEFAULT = date.today().strftime("%Y%m%d")
START = START_DEFAULT
END = END_DEFAULT


# 날짜 인자 정규화: 여러 포맷(YYYYMMDD, YYYY-MM-DD 등)을 허용하여 'YYYYMMDD' 반환
def _normalize_date_arg(date_str: str | None) -> str | None:
    if date_str is None:
        return None
    s = str(date_str).strip()
    if not s:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y%m%d")
        except ValueError:
            continue
    raise ValueError(f"잘못된 날짜 형식: {date_str}. YYYYMMDD 또는 YYYY-MM-DD 를 사용하세요.")


def _parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ETF 전용 백테스트 실행기")
    parser.add_argument("--start", "-s", help="시작일 (YYYYMMDD 또는 YYYY-MM-DD). 기본: 20160101", default=None)
    parser.add_argument("--end", "-e", help="종료일 (YYYYMMDD 또는 YYYY-MM-DD). 기본: 오늘(또는 마지막 영업일)", default=None)
    parser.add_argument("--mode", "-m", choices=["single", "experiment", "risk_off_compare"], help="실행 모드: single | experiment | risk_off_compare (옵션)", default=None)
    return parser.parse_args()


def normalize_ohlcv(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if df is None or df.empty:
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

    df = df.reset_index()
    df["ticker"] = ticker
    df = df.rename(
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
    if "date" not in df.columns and "index" in df.columns:
        df = df.rename(columns={"index": "date"})

    required_columns = ["date", "ticker", "open", "close"]
    missing_columns = [col for col in required_columns if col not in df.columns]
    if missing_columns:
        raise ValueError(f"{ticker}: missing columns {missing_columns}; actual columns={list(df.columns)}")

    if "volume" not in df.columns:
        df["volume"] = 0
    if "trading_value" not in df.columns:
        df["trading_value"] = df["close"] * df["volume"]
    if "high" not in df.columns:
        df["high"] = df["close"]
    if "low" not in df.columns:
        df["low"] = df["close"]
    if "nav" not in df.columns:
        df["nav"] = pd.NA
    if "base_index" not in df.columns:
        df["base_index"] = pd.NA

    out = df[
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
    out["date"] = pd.to_datetime(out["date"])
    return out


class _RefreshPriceCacheRequired(Exception):
    pass


def _return_basis_requires_nav() -> bool:
    """NAV 기반 수익률이 필요한지 확인한다 (``ETF_RETURN_BASIS=nav``)."""
    return os.environ.get("ETF_RETURN_BASIS", "price").strip().lower() == "nav"


def _return_basis_requires_distributions() -> bool:
    """분배금 파일이 필요한지 확인한다 (``ETF_RETURN_BASIS=total_return``)."""
    return os.environ.get("ETF_RETURN_BASIS", "price").strip().lower() == "total_return"


def _ensure_price_cache_schema(df: pd.DataFrame, ticker: str) -> None:
    """NAV 모드일 때 캐시에 nav 컬럼이 충분히 있는지 점검한다. price/total_return 모드는 건너뛴다."""
    if not _return_basis_requires_nav():
        return
    if "nav" not in df.columns:
        raise _RefreshPriceCacheRequired(f"{ticker} 캐시에 nav 컬럼이 없어 NAV 모드 재조회 필요")
    nav_ratio = float(df["nav"].notna().mean())
    if nav_ratio < 0.5:
        raise _RefreshPriceCacheRequired(
            f"{ticker} 캐시 nav non-null 비율 {nav_ratio:.0%} < 50% — NAV 모드 재조회 필요"
        )


def normalize_index_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """인덱스(지수)용 OHLCV 정규화: 다양한 pykrx 반환 포맷을 지원합니다.

    반환 컬럼 최소 요구: `date`, `close`.
    """
    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        return pd.DataFrame(columns=["date", "close"])

    # 만약 인덱스가 날짜형이면 reset_index로 컬럼 추가
    try:
        if hasattr(df, "index") and (isinstance(df.index, pd.DatetimeIndex) or np.issubdtype(df.index.dtype, np.datetime64)):
            tmp = df.reset_index()
        else:
            tmp = df.reset_index()
    except Exception:
        tmp = pd.DataFrame(df)

    # 후보 컬럼명 탐색
    date_col = None
    close_col = None
    for c in tmp.columns:
        cn = str(c)
        lc = cn.lower()
        if date_col is None and ("날짜" in cn or lc == "date" or lc == "trd_dd" or lc.startswith("date") or lc == "index"):
            date_col = c
        if close_col is None and ("종가" in cn or "clsprc" in lc or "clsp" in lc or "close" in lc):
            close_col = c

    # 추가 탐색: date가 없으면 파서가 가능한 컬럼을 찾아본다
    if date_col is None:
        for c in tmp.columns:
            try:
                parsed = pd.to_datetime(tmp[c], errors="coerce")
                if parsed.notna().sum() > 0:
                    date_col = c
                    tmp[c] = parsed
                    break
            except Exception:
                continue

    # close가 없으면 숫자형 컬럼 중 첫 번째를 사용
    if close_col is None:
        for c in tmp.columns:
            if c == date_col:
                continue
            if pd.api.types.is_numeric_dtype(tmp[c]):
                close_col = c
                break

    if date_col is None or close_col is None:
        raise ValueError(f"인덱스 데이터에 date/close 컬럼이 없습니다. 컬럼 목록: {list(tmp.columns)}")

    out = tmp.rename(columns={date_col: "date", close_col: "close"})
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    return out


def get_price(
    ticker: str, listing_dates: dict[str, str] | None = None
) -> pd.DataFrame:
    cache_dir = Path("data_cache")
    cache_dir.mkdir(exist_ok=True)
    use_cache = os.environ.get("ETF_USE_CACHE", "1") != "0"
    force_refresh = os.environ.get("ETF_REFRESH_CACHE", "0") == "1"
    cache_parquet = cache_dir / f"{ticker}.parquet"
    cache_csv_pattern = list(cache_dir.glob(f"{ticker}_*.csv"))

    # 상장일 기반으로 조회 시작일 클램핑 — 상장 전 기간 요청을 방지
    effective_start = START
    if listing_dates:
        ld = listing_dates.get(str(ticker).strip())
        if ld:
            ld_ts = pd.to_datetime(ld)
            s_ts = pd.to_datetime(START)
            if s_ts < ld_ts:
                effective_start = ld_ts.strftime("%Y%m%d")

    # 기존 CSV 캐시(구형 포맷)가 있으면 병합하여 Parquet 마스터로 마이그레이션 시도
    if use_cache and not force_refresh and not cache_parquet.exists() and cache_csv_pattern:
        try:
            parts = []
            for f in sorted(cache_csv_pattern):
                try:
                    p = pd.read_csv(f, parse_dates=["date"])
                    parts.append(p)
                except Exception:
                    continue
            if parts:
                df_mig = pd.concat(parts, ignore_index=True).drop_duplicates(subset=["date"]).sort_values("date")
                if "ticker" not in df_mig.columns:
                    df_mig["ticker"] = str(ticker)
                df_mig["ticker"] = df_mig["ticker"].astype(str)
                try:
                    tmp = cache_parquet.with_suffix(".parquet.tmp")
                    df_mig.to_parquet(tmp)
                    os.replace(tmp, cache_parquet)
                    print(f"[캐시] {ticker} CSV->Parquet 마이그레이션: {cache_parquet}")
                except Exception as e:
                    print(f"[캐시] {ticker} 마이그레이션(Parquet) 실패: {e} — CSV 보존")
        except Exception as e:
            print(f"[캐시] {ticker} 마이그레이션 실패: {e}")

    # 캐시 사용 가능하면 Parquet에서 증분 로드/갱신
    if use_cache and cache_parquet.exists() and not force_refresh:
        try:
            df_cached = pd.read_parquet(cache_parquet)
            if "date" in df_cached.columns:
                df_cached["date"] = pd.to_datetime(df_cached["date"])
            _ensure_price_cache_schema(df_cached, ticker)

            start_req = pd.to_datetime(effective_start)
            end_req = pd.to_datetime(END)

            cached_min = df_cached["date"].min()
            cached_max = df_cached["date"].max()

            # 완전 커버되면 서브셋 반환
            if cached_min <= start_req and cached_max >= end_req:
                subset = df_cached[(df_cached["date"] >= start_req) & (df_cached["date"] <= end_req)].copy()
                print(f"[캐시] {ticker} 재사용: {cache_parquet} ({cached_min.date()}~{cached_max.date()})")
                return subset

            to_concat = [df_cached]
            fetched = False

            # 왼쪽(이전) 구간이 필요하면 조회
            if start_req < cached_min:
                fetch_start = start_req.strftime("%Y%m%d")
                fetch_end = (cached_min - pd.Timedelta(days=1)).strftime("%Y%m%d")
                # 주말 전용 범위라면 조회를 건너뜁니다 (공휴일은 판별하지 않음)
                if not _range_has_weekday(fetch_start, fetch_end):
                    raw_left = pd.DataFrame()
                    print(f"[캐시] {ticker} left 범위 주말 전용({fetch_start}~{fetch_end}) — 조회 생략")
                else:
                    try:
                        raw_left = _call_capture_stderr(
                            fetch_etf_ohlcv_with_nav, fetch_start, fetch_end, ticker
                        )
                    except Exception as e:
                        print(f"[캐시] {ticker} left 호출 실패: {e}")
                    else:
                        try:
                            df_left = raw_left.copy() if isinstance(raw_left, pd.DataFrame) else pd.DataFrame()
                            if isinstance(df_left, pd.DataFrame) and not df_left.empty:
                                to_concat.insert(0, df_left)
                                fetched = True
                                print(f"[캐시] {ticker} left 증분 수집: {fetch_start}~{fetch_end}")
                            else:
                                print(f"[캐시] {ticker} left 증분 비어있음: {fetch_start}~{fetch_end}")
                        except Exception as e:
                            print(f"[캐시] {ticker} left 증분 처리 실패: {e}")

            # 오른쪽(최신) 구간이 필요하면 조회
            if end_req > cached_max:
                fetch_start = (cached_max + pd.Timedelta(days=1)).strftime("%Y%m%d")
                fetch_end = end_req.strftime("%Y%m%d")
                # 주말 전용 범위면 조회 건너뜀
                if not _range_has_weekday(fetch_start, fetch_end):
                    raw_right = pd.DataFrame()
                    print(f"[캐시] {ticker} right 범위 주말 전용({fetch_start}~{fetch_end}) — 조회 생략")
                else:
                    try:
                        raw_right = _call_capture_stderr(
                            fetch_etf_ohlcv_with_nav, fetch_start, fetch_end, ticker
                        )
                    except Exception as e:
                        print(f"[캐시] {ticker} right 호출 실패: {e}")
                    else:
                        try:
                            df_right = raw_right.copy() if isinstance(raw_right, pd.DataFrame) else pd.DataFrame()
                            if isinstance(df_right, pd.DataFrame) and not df_right.empty:
                                to_concat.append(df_right)
                                fetched = True
                                print(f"[캐시] {ticker} right 증분 수집: {fetch_start}~{fetch_end}")
                            else:
                                print(f"[캐시] {ticker} right 증분 비어있음: {fetch_start}~{fetch_end}")
                        except Exception as e:
                            print(f"[캐시] {ticker} right 증분 처리 실패: {e}")

            if fetched:
                parts = [p for p in to_concat if isinstance(p, pd.DataFrame) and not p.empty]
                if not parts:
                    print(f"[캐시] {ticker} 증분 병합 대상이 없습니다; 기존 캐시 사용")
                    df_new = df_cached.copy()
                else:
                    try:
                        df_new = pd.concat(parts, ignore_index=True)
                    except Exception as e:
                        meta = [(type(p), list(p.columns) if hasattr(p, "columns") else None) for p in parts]
                        print(f"[캐시] {ticker} 증분 병합 실패: {e}; parts={meta}")
                        df_new = df_cached.copy()

                df_new = df_new.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
                try:
                    tmp = cache_parquet.with_suffix(".parquet.tmp")
                    df_new.to_parquet(tmp)
                    os.replace(tmp, cache_parquet)
                    print(f"[캐시] {ticker} 캐시 업데이트: {cache_parquet}")
                except Exception as e:
                    print(f"[캐시] {ticker} 캐시 저장 실패(Parquet): {e}")
                subset = df_new[(df_new["date"] >= start_req) & (df_new["date"] <= end_req)].copy()
                return subset
            else:
                # 일부만 캐시되어 있고 증분 불가한 경우 가능하면 서브셋 반환
                subset = df_cached[(df_cached["date"] >= start_req) & (df_cached["date"] <= end_req)].copy()
                if not subset.empty:
                    print(f"[캐시] {ticker} 부분 재사용: {cache_parquet} ({cached_min.date()}~{cached_max.date()})")
                    return subset
        except _RefreshPriceCacheRequired as e:
            print(f"[캐시] {e} — 전체 재조회로 대체")
        except Exception as e:
            print(f"[캐시] {ticker} 캐시 읽기 실패: {e} — 전체 재조회로 대체")

    # 캐시가 없거나 강제 갱신인 경우 전체 조회 후 저장
    try:
        df = _call_capture_stderr(fetch_etf_ohlcv_with_nav, effective_start, END, ticker)
        if use_cache:
            try:
                tmp = cache_parquet.with_suffix(".parquet.tmp")
                df.to_parquet(tmp)
                os.replace(tmp, cache_parquet)
                print(f"[캐시] 저장(Parquet): {cache_parquet}")
            except Exception as e:
                # Parquet 저장 실패시 CSV로 저장
                try:
                    csv_file = cache_dir / f"{ticker}.csv"
                    tmp_csv = cache_dir / f"{ticker}.csv.tmp"
                    df.to_csv(tmp_csv, index=False)
                    os.replace(tmp_csv, csv_file)
                    print(f"[캐시] Parquet 저장 실패({e}) — CSV 저장: {csv_file}")
                except Exception as e2:
                    print(f"[캐시] {ticker} 캐시 저장 실패: {e2}")
        return df
    except Exception as e:
        print(f"❌ 오류: 종목 {ticker} 데이터 조회 실패: {str(e)}")
        raise RuntimeError(f"Cannot fetch price data for ticker {ticker}") from e


def get_index_data() -> pd.DataFrame:
    """KOSPI 지수 데이터를 조회하고 기술적 지표를 계산한다.

    인덱스 데이터는 단일 Parquet 캐시(data_cache/index.parquet)를 사용하여 증분 갱신합니다.
    """
    cache_dir = Path("data_cache")
    cache_dir.mkdir(exist_ok=True)
    use_cache = os.environ.get("ETF_USE_CACHE", "1") != "0"
    force_refresh = os.environ.get("ETF_REFRESH_CACHE", "0") == "1"
    cache_parquet = cache_dir / "index.parquet"

    # 캐시가 있고 사용 허용이면 읽어서 요청 범위를 커버하는지 확인
    if use_cache and cache_parquet.exists() and not force_refresh:
        try:
            idx_cached = pd.read_parquet(cache_parquet)
            if "date" in idx_cached.columns:
                idx_cached["date"] = pd.to_datetime(idx_cached["date"])

            start_req = pd.to_datetime(START)
            end_req = pd.to_datetime(END)
            cached_min = idx_cached["date"].min()
            cached_max = idx_cached["date"].max()

            if cached_min <= start_req and cached_max >= end_req:
                print(f"[캐시] KOSPI 인덱스 재사용: {cache_parquet} ({cached_min.date()}~{cached_max.date()})")
                df = idx_cached.copy()
                df = df[(df["date"] >= start_req) & (df["date"] <= end_req)]
                return df[["date", "close", "market_ma", "market_ma_slope", "risk_on"]]

            to_concat = [idx_cached]
            fetched = False

            if start_req < cached_min:
                fetch_start = start_req.strftime("%Y%m%d")
                fetch_end = (cached_min - pd.Timedelta(days=1)).strftime("%Y%m%d")
                # 주말 전용 범위면 조회 생략
                if not _range_has_weekday(fetch_start, fetch_end):
                    raw_left = pd.DataFrame()
                    print(f"[캐시] KOSPI left 범위 주말 전용({fetch_start}~{fetch_end}) — 조회 생략")
                else:
                    try:
                        raw_left = _call_capture_stderr(stock.get_index_ohlcv_by_date, fetch_start, fetch_end, KOSPI_INDEX_CODE)
                    except Exception as e:
                        print(f"[캐시] KOSPI left 호출 실패: {e}")
                    else:
                        try:
                            if hasattr(raw_left, "columns"):
                                print(f"[캐시][debug] raw_left type={type(raw_left)}, columns={list(raw_left.columns)}")
                                try:
                                    print(raw_left.head().to_string())
                                except Exception:
                                    pass
                            left = normalize_index_ohlcv(raw_left)
                            if isinstance(left, pd.DataFrame) and not left.empty:
                                to_concat.insert(0, left)
                                fetched = True
                                print(f"[캐시] KOSPI left 증분 수집: {fetch_start}~{fetch_end}")
                            else:
                                print(f"[캐시] KOSPI left 증분 비어있음: {fetch_start}~{fetch_end}")
                        except Exception as e:
                            print(f"[캐시] KOSPI left 정규화 실패: {e}; 원본 컬럼={list(raw_left.columns) if hasattr(raw_left, 'columns') else None}")

            if end_req > cached_max:
                fetch_start = (cached_max + pd.Timedelta(days=1)).strftime("%Y%m%d")
                fetch_end = end_req.strftime("%Y%m%d")
                # 주말 전용 범위면 조회 생략
                if not _range_has_weekday(fetch_start, fetch_end):
                    raw_right = pd.DataFrame()
                    print(f"[캐시] KOSPI right 범위 주말 전용({fetch_start}~{fetch_end}) — 조회 생략")
                else:
                    try:
                        raw_right = _call_capture_stderr(stock.get_index_ohlcv_by_date, fetch_start, fetch_end, KOSPI_INDEX_CODE)
                    except Exception as e:
                        print(f"[캐시] KOSPI right 호출 실패: {e}")
                    else:
                        try:
                            if hasattr(raw_right, "columns"):
                                print(f"[캐시][debug] raw_right type={type(raw_right)}, columns={list(raw_right.columns)}")
                                try:
                                    print(raw_right.head().to_string())
                                except Exception:
                                    pass
                            right = normalize_index_ohlcv(raw_right)
                            if isinstance(right, pd.DataFrame) and not right.empty:
                                to_concat.append(right)
                                fetched = True
                                print(f"[캐시] KOSPI right 증분 수집: {fetch_start}~{fetch_end}")
                            else:
                                print(f"[캐시] KOSPI right 증분 비어있음: {fetch_start}~{fetch_end}")
                        except Exception as e:
                            print(f"[캐시] KOSPI right 정규화 실패: {e}; 원본 컬럼={list(raw_right.columns) if hasattr(raw_right, 'columns') else None}")

            if fetched:
                # 빈 프레임을 제외하여 concat 관련 FutureWarning 제거
                parts = [p for p in to_concat if isinstance(p, pd.DataFrame) and not p.empty]
                if not parts:
                    print("[캐시] KOSPI 증분 병합 대상이 없습니다; 기존 캐시 사용 시도")
                    idx_new = idx_cached.copy()
                else:
                    try:
                        idx_new = pd.concat(parts, ignore_index=True)
                    except Exception as e:
                        meta = [ (type(p), list(p.columns) if hasattr(p, 'columns') else None) for p in parts ]
                        print(f"[캐시] KOSPI 증분 병합 실패: {e}; parts={meta}")
                        idx_new = idx_cached.copy()

                idx_new = idx_new.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)

                if "close" not in idx_new.columns:
                    print("[캐시] KOSPI 병합 결과에 'close' 컬럼이 없습니다; 캐시 재사용")
                    idx_new = idx_cached.copy()

                idx_new["market_ma"] = idx_new["close"].rolling(MARKET_MA_DAYS).mean()
                idx_new["market_ma_slope"] = idx_new["market_ma"] - idx_new["market_ma"].shift(MARKET_SLOPE_DAYS)
                idx_new["risk_on"] = (idx_new["close"] >= idx_new["market_ma"]) & (idx_new["market_ma_slope"] >= 0)
                try:
                    tmp = cache_parquet.with_suffix(".parquet.tmp")
                    idx_new.to_parquet(tmp)
                    os.replace(tmp, cache_parquet)
                    print(f"[캐시] KOSPI 인덱스 캐시 업데이트: {cache_parquet}")
                except Exception as e:
                    print(f"[캐시] KOSPI 캐시 저장 실패: {e}")
                df = idx_new[(idx_new["date"] >= start_req) & (idx_new["date"] <= end_req)]
                return df[["date", "close", "market_ma", "market_ma_slope", "risk_on"]]
        except Exception as e:
            print(f"[캐시] KOSPI 캐시 읽기 실패: {e}; 재조회")

    # 캐시가 없거나 강제 갱신인 경우 전체 조회
    if not HAS_KRX_CREDENTIALS:
        raise RuntimeError(
            "KRX 인증 정보가 필요합니다. KOSPI 지수 데이터를 조회할 수 없습니다.\n"
            "다음 단계를 따르세요:\n"
            "1. .env.sample을 참고하여 .env 파일을 생성하세요\n"
            "2. KRX_ID와 KRX_PW를 설정하세요\n"
            "3. 다시 실행하세요"
        )

    try:
        idx_raw = _call_capture_stderr(stock.get_index_ohlcv_by_date, START, END, KOSPI_INDEX_CODE)
    except Exception as e:
        raise RuntimeError(
            f"KOSPI 지수 데이터 조회 중 오류 발생: {str(e)}\n"
            "KRX 인증 정보를 확인하고 다시 시도하세요."
        ) from e

    if idx_raw is None or (isinstance(idx_raw, pd.DataFrame) and idx_raw.empty):
        raise RuntimeError("No KOSPI index data returned.")
    try:
        idx = normalize_index_ohlcv(idx_raw)
    except Exception as e:
        raise RuntimeError(
            f"KOSPI 지수 데이터 포맷 오류(정규화 실패): {e}\n"
            f"조회한 데이터 구조: {list(idx_raw.columns) if hasattr(idx_raw, 'columns') else str(type(idx_raw))}"
        ) from e
    idx["market_ma"] = idx["close"].rolling(MARKET_MA_DAYS).mean()
    idx["market_ma_slope"] = idx["market_ma"] - idx["market_ma"].shift(MARKET_SLOPE_DAYS)
    idx["risk_on"] = (idx["close"] >= idx["market_ma"]) & (idx["market_ma_slope"] >= 0)

    if use_cache:
        try:
            tmp = cache_parquet.with_suffix(".parquet.tmp")
            idx.to_parquet(tmp)
            os.replace(tmp, cache_parquet)
            print(f"[캐시] KOSPI 인덱스 저장: {cache_parquet}")
        except Exception as e:
            try:
                csv_file = cache_dir / "index.csv"
                tmp_csv = cache_dir / "index.csv.tmp"
                idx.to_csv(tmp_csv, index=False)
                os.replace(tmp_csv, csv_file)
                print(f"[캐시] Parquet 저장 실패({e}) — CSV 저장: {csv_file}")
            except Exception as e2:
                print(f"[캐시] 인덱스 캐시 저장 실패: {e2}")

    return idx[["date", "close", "market_ma", "market_ma_slope", "risk_on"]]


def is_risk_on(index_df: pd.DataFrame, date: pd.Timestamp) -> bool:
    rows = index_df[index_df["date"] <= date]
    if rows.empty:
        return True
    last = rows.iloc[-1]
    if pd.isna(last["market_ma"]) or pd.isna(last["market_ma_slope"]):
        return True
    return bool(last["risk_on"])


def get_us_index_data() -> pd.DataFrame:
    """미국 지수(ETF proxy) 데이터를 조회해 risk_on 시그널을 계산한다."""
    if yf is None:
        print("[경고] yfinance를 불러오지 못해 미국 risk 시그널을 비활성화합니다.")
        return pd.DataFrame(columns=["date", "close", "us_market_ma", "us_market_ma_slope", "us_risk_on"])

    cache_dir = Path("data_cache")
    cache_dir.mkdir(exist_ok=True)
    cache_file = cache_dir / f"us_index_{US_RISK_PROXY}.parquet"
    use_cache = os.environ.get("ETF_USE_CACHE", "1") != "0"
    force_refresh = os.environ.get("ETF_REFRESH_CACHE", "0") == "1"

    if use_cache and cache_file.exists() and not force_refresh:
        try:
            cached = pd.read_parquet(cache_file)
            cached["date"] = pd.to_datetime(cached["date"])
            req_start = pd.to_datetime(START)
            req_end = pd.to_datetime(END)
            out = cached[(cached["date"] >= req_start) & (cached["date"] <= req_end)].copy()
            if not out.empty:
                return out[["date", "close", "us_market_ma", "us_market_ma_slope", "us_risk_on"]]
        except Exception:
            pass

    try:
        end_dt = pd.to_datetime(END) + pd.Timedelta(days=1)
        raw = yf.download(
            US_RISK_PROXY,
            start=pd.to_datetime(START),
            end=end_dt,
            progress=False,
            auto_adjust=False,
        )
    except Exception as e:
        print(f"[경고] 미국 지수 조회 실패({US_RISK_PROXY}): {e} — us_risk_on=True로 폴백")
        return pd.DataFrame(columns=["date", "close", "us_market_ma", "us_market_ma_slope", "us_risk_on"])

    if raw is None or raw.empty:
        print(f"[경고] 미국 지수 데이터 없음({US_RISK_PROXY}) — us_risk_on=True로 폴백")
        return pd.DataFrame(columns=["date", "close", "us_market_ma", "us_market_ma_slope", "us_risk_on"])

    raw = raw.reset_index()

    # yfinance가 MultiIndex 컬럼으로 반환하는 경우(예: ('Close','SPY'))를 평탄화한다.
    if getattr(raw.columns, "nlevels", 1) > 1:
        flat_cols = []
        for col in raw.columns:
            if isinstance(col, tuple):
                candidates = [str(x) for x in col if x is not None and str(x)]
                flat_cols.append("_".join(candidates))
            else:
                flat_cols.append(str(col))
        raw.columns = flat_cols

    close_col = None
    for candidate in ["Adj Close", "Close", "Adj_Close", "Close_SPY", "Adj_Close_SPY"]:
        if candidate in raw.columns:
            close_col = candidate
            break
    if close_col is None:
        for c in raw.columns:
            c_lower = str(c).lower().replace(" ", "_")
            if "adj_close" in c_lower or c_lower.startswith("close") or "_close" in c_lower:
                close_col = c
                break
    if close_col is None:
        print(f"[경고] 미국 지수 종가 컬럼을 찾지 못했습니다({US_RISK_PROXY})")
        return pd.DataFrame(columns=["date", "close", "us_market_ma", "us_market_ma_slope", "us_risk_on"])

    date_col = "Date" if "Date" in raw.columns else ("date" if "date" in raw.columns else None)
    if date_col is None:
        print(f"[경고] 미국 지수 날짜 컬럼을 찾지 못했습니다({US_RISK_PROXY})")
        return pd.DataFrame(columns=["date", "close", "us_market_ma", "us_market_ma_slope", "us_risk_on"])

    close_series = raw[close_col]
    if isinstance(close_series, pd.DataFrame):
        close_series = close_series.iloc[:, 0]

    us = pd.DataFrame(
        {
            "date": pd.to_datetime(raw[date_col]),
            "close": pd.to_numeric(close_series, errors="coerce"),
        }
    )
    us = us.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    us["us_market_ma"] = us["close"].rolling(US_MARKET_MA_DAYS).mean()
    us["us_market_ma_slope"] = us["us_market_ma"] - us["us_market_ma"].shift(US_MARKET_SLOPE_DAYS)
    us["us_risk_on"] = (us["close"] >= us["us_market_ma"]) & (us["us_market_ma_slope"] >= 0)

    if use_cache:
        try:
            tmp = cache_file.with_suffix(".parquet.tmp")
            us.to_parquet(tmp)
            os.replace(tmp, cache_file)
        except Exception:
            pass

    req_start = pd.to_datetime(START)
    req_end = pd.to_datetime(END)
    out = us[(us["date"] >= req_start) & (us["date"] <= req_end)].copy()
    return out[["date", "close", "us_market_ma", "us_market_ma_slope", "us_risk_on"]]


def is_us_risk_on(us_index_df: pd.DataFrame | None, date: pd.Timestamp) -> bool:
    if us_index_df is None or us_index_df.empty:
        return True
    rows = us_index_df[us_index_df["date"] <= date]
    if rows.empty:
        return True
    last = rows.iloc[-1]
    if pd.isna(last.get("us_market_ma")) or pd.isna(last.get("us_market_ma_slope")):
        return True
    return bool(last["us_risk_on"])


def safe_get(series: pd.Series, key: str):
    value = series.get(key)
    if value is None or pd.isna(value) or value <= 0:
        return None
    return float(value)


def load_etf_price() -> pd.DataFrame:
    """ETF_LIST 전 종목의 가격 데이터를 로드하고 전처리한다.

    분배금 파일이 있으면 분배금 컬럼을 병합하고, ``ETF_RETURN_BASIS``에 따라
    ``close_adj``(랭킹 기준 수익률용), ``ret_60``, ``ret_120`` 등을 계산한다.
    """
    frames = []
    failed = []
    empty = []

    listing_dates = get_listing_dates(ticker_subset=set(map(str, ETF_LIST)))

    total = len(ETF_LIST)
    for idx, ticker in enumerate(ETF_LIST, start=1):
        print(f"[데이터] {idx}/{total} 조회: {ticker}")
        try:
            df = get_price(ticker, listing_dates=listing_dates)
            if df is None or df.empty:
                empty.append(ticker)
                print(f"[데이터] {ticker} 비어있음")
                continue
            frames.append(df)
        except Exception as exc:
            failed.append((ticker, str(exc)))
            print(f"[데이터] {ticker} 수집 실패: {exc}")

    if not frames:
        raise RuntimeError(f"No ETF data collected. empty={empty}, failed={failed[:5]}")

    if failed:
        print(f"[경고] 수집 실패 ETF: {failed[:3]}")
    if empty:
        print(f"[경고] 데이터가 비어 있는 ETF: {empty}")

    price = pd.concat(frames, ignore_index=True)
    price = price.sort_values(["ticker", "date"]).copy()
    # 티커 컬럼이 숫자형으로 들어오는 경우 대비해 문자열로 일관성 유지
    if "ticker" in price.columns:
        try:
            price["ticker"] = price["ticker"].astype(str)
        except Exception:
            price["ticker"] = price["ticker"].apply(lambda x: str(x))

    distributions = load_distributions(required=_return_basis_requires_distributions())
    price = add_distributions(price, distributions)
    price = add_liquidity_flag(price)
    price = add_listing_flag(price, listing_dates)
    price = add_deviation_flag(price)
    price = add_price_basis_columns(price)

    grouped = price.groupby("ticker")
    price["ret_60"] = grouped["close_adj"].pct_change(60)
    price["ret_120"] = grouped["close_adj"].pct_change(120)
    price["ma20"] = grouped["close_adj"].transform(lambda x: x.rolling(20).mean())
    price["ma60"] = grouped["close_adj"].transform(lambda x: x.rolling(60).mean())
    price["trend_ok"] = (price["close_adj"] > price["ma20"]) & (price["ma20"] > price["ma60"])
    return price



def run_etf_strategy(
    initial_cash: float,
    common_dates: list[pd.Timestamp],
    index_df: pd.DataFrame,
    use_market_filter: bool = True,
    max_positions: int = ETF_MAX_POSITIONS,
    slippage: float = SLIPPAGE_PCT,
    # noqa: PLR0913 — 전략 파라미터가 많음
    risk_off_liquidate: bool = True,
    price_data: pd.DataFrame | None = None,
    max_asset_pct: float | None = None,
    target_weight_rebalance: bool | None = None,
    rebalance_band_pct: float | None = None,
    trim_overweight_positions: bool | None = None,
    exit_check_days: int | None = None,
    trailing_stop_pct: float | None = None,
    portfolio_trailing_stop_pct: float | None = None,
    us_index_df: pd.DataFrame | None = None,
    enable_multi_index_risk: bool = False,
    *,
    rebalance_observer: Callable[[dict], None] | None = None,
):
    """ETF 로테이션 전략을 백테스트한다.

    리밸런싱 시점에서 랭킹 상위 종목을 매수하고, 분배락일에는 보유 수량에 한하여
    현금분배금을 계산해 자산에 반영한다. 같은 날 신규 매수분에는 분배금이 귀속되지
    않는다.

    Args:
        rebalance_observer: 선택적 콜백. 각 리밸런싱 전후 상태(의사결정, 주문, 체결)
           를 담은 dict를 전달한다. ``None``이면 기존 동작과 동일하다.
    """
    price = price_data.copy() if price_data is not None else load_etf_price()
    price_by_date = {dt: day.set_index("ticker") for dt, day in price.groupby("date")}

    cash = float(initial_cash)
    holdings = {}
    holding_cost_basis = {}
    holding_peak_closes: dict[str, float] = {}
    portfolio_peak_equity: float | None = None
    last_valid_closes: dict[str, float] = {}
    trades = []
    equity_rows = []

    ticker_names: dict[str, str] = {t: get_ticker_name(t) for t in ETF_LIST}
    effective_exit_check_days = (
        int(exit_check_days)
        if exit_check_days is not None
        else int(os.environ.get("ETF_EXIT_CHECK_DAYS", "0"))
    )
    effective_trailing_stop_pct = (
        float(trailing_stop_pct)
        if trailing_stop_pct is not None
        else parse_fraction_env("ETF_TRAILING_STOP_PCT", 0.0)
    )
    effective_portfolio_trailing_stop_pct = (
        float(portfolio_trailing_stop_pct)
        if portfolio_trailing_stop_pct is not None
        else parse_fraction_env("ETF_PORTFOLIO_TRAILING_STOP_PCT", 0.0)
    )
    if effective_exit_check_days < 0:
        raise ValueError("ETF_EXIT_CHECK_DAYS는 0 이상이어야 합니다.")
    if not 0 <= effective_trailing_stop_pct < 1:
        raise ValueError("ETF_TRAILING_STOP_PCT는 0 이상 1 미만이어야 합니다.")
    if not 0 <= effective_portfolio_trailing_stop_pct < 1:
        raise ValueError("ETF_PORTFOLIO_TRAILING_STOP_PCT는 0 이상 1 미만이어야 합니다.")

    def execute_exit(
        ticker: str,
        qty: int,
        next_open: pd.Series,
        next_dt: pd.Timestamp,
        reason: str,
    ) -> bool:
        nonlocal cash

        open_price = safe_get(next_open, ticker)
        if open_price is None:
            return False
        reference_price = open_price * (1 - SPREAD_PCT / 2)
        cost_basis = holding_cost_basis.get(ticker)
        tax_rate = ETF_TAXABLE_SELL_TAX_PCT if ticker in TAXABLE_ETF_TICKERS else 0.0
        net_value = apply_sell_value(
            reference_price,
            int(qty),
            tax_rate,
            slippage,
            cost_basis_per_share=cost_basis,
        )
        sell_price_adj = reference_price * (1 - slippage)
        taxable_gain = (
            max(0.0, int(qty) * (sell_price_adj - cost_basis))
            if cost_basis is not None
            else 0.0
        )
        estimated_tax = taxable_gain * tax_rate
        cash += net_value
        holdings.pop(ticker, None)
        holding_cost_basis.pop(ticker, None)
        holding_peak_closes.pop(ticker, None)
        trades.append(
            {
                "date": next_dt,
                "ticker": ticker,
                "name": get_ticker_name(ticker),
                "side": "SELL",
                "reason": reason,
                "qty": int(qty),
                "price": reference_price,
                "net_value": net_value,
                "cash_flow": net_value,
                "estimated_tax": estimated_tax,
                "cash_after": cash,
            }
        )
        return True

    warmup_days = max(120, MARKET_MA_DAYS + MARKET_SLOPE_DAYS)
    for i, dt in enumerate(common_dates[:-1]):
        if i < warmup_days:
            continue

        next_dt = common_dates[i + 1]
        today = price_by_date.get(dt, pd.DataFrame())
        next_day = price_by_date.get(next_dt, pd.DataFrame())
        if today.empty or next_day.empty:
            continue

        next_open = next_day["open"]
        next_close = next_day["close"]
        entitled_holdings = dict(holdings)
        distribution_cash = distribution_cash_for_holdings(
            entitled_holdings,
            next_day.get("distribution"),
            parse_pct_env("ETF_DISTRIBUTION_TAX_PCT", 0.0),
        )
        update_last_valid_prices(last_valid_closes, today.get("close"))
        should_rebalance = (i - warmup_days) % REBALANCE_STEP_DAYS == 0
        rebalance_order_count = 0
        exit_order_count = 0
        stopped_tickers: set[str] = set()
        portfolio_stop_triggered = False
        observer_event: dict | None = None

        current_market_value = 0.0
        for ticker, qty in holdings.items():
            close_price = get_valuation_price(ticker, today.get("close"), last_valid_closes)
            if close_price is not None:
                current_market_value += qty * close_price
        current_equity = cash + current_market_value
        if holdings:
            portfolio_peak_equity = max(
                portfolio_peak_equity or current_equity,
                current_equity,
            )

        # Exit-only overlay: 보유 중 형성된 종가 고점 대비 하락을 별도 주기로 확인하고
        # 다음 거래일 시가에 전량 매도한다. 신규 진입은 기존 리밸런싱 주기를 유지한다.
        for ticker in holdings:
            close_price = safe_get(today.get("close"), ticker)
            if close_price is not None:
                holding_peak_closes[ticker] = max(
                    holding_peak_closes.get(ticker, close_price),
                    close_price,
                )
        should_check_exit = (
            effective_exit_check_days > 0
            and (
                effective_trailing_stop_pct > 0
                or effective_portfolio_trailing_stop_pct > 0
            )
            and (i - warmup_days) % effective_exit_check_days == 0
        )
        should_stop_portfolio = (
            should_check_exit
            and effective_portfolio_trailing_stop_pct > 0
            and portfolio_peak_equity is not None
            and current_equity
            <= portfolio_peak_equity * (1 - effective_portfolio_trailing_stop_pct)
        )
        if should_stop_portfolio:
            sellable = all(safe_get(next_open, ticker) is not None for ticker in holdings)
            if sellable:
                for ticker, qty in list(holdings.items()):
                    if execute_exit(
                        ticker,
                        int(qty),
                        next_open,
                        next_dt,
                        "ETF_PORTFOLIO_TRAILING_STOP",
                    ):
                        stopped_tickers.add(ticker)
                        exit_order_count += 1
                portfolio_peak_equity = None
                portfolio_stop_triggered = True

        if (
            should_check_exit
            and effective_trailing_stop_pct > 0
            and not portfolio_stop_triggered
        ):
            for ticker, qty in list(holdings.items()):
                close_price = safe_get(today.get("close"), ticker)
                peak_close = holding_peak_closes.get(ticker)
                if close_price is None or peak_close is None:
                    continue
                if close_price > peak_close * (1 - effective_trailing_stop_pct):
                    continue

                if execute_exit(
                    ticker,
                    int(qty),
                    next_open,
                    next_dt,
                    "ETF_TRAILING_STOP",
                ):
                    stopped_tickers.add(ticker)
                    exit_order_count += 1

        if should_rebalance:
            # 시장 필터 + 랭킹으로 목표 종목 결정
            kospi_risk_on = is_risk_on(index_df, dt) if use_market_filter else True
            us_risk_on = (
                is_us_risk_on(us_index_df, dt) if (use_market_filter and enable_multi_index_risk) else True
            )
            allowed_groups = get_allowed_groups(
                kospi_risk_on,
                us_risk_on,
                gating_mode=MULTI_INDEX_GATING_MODE,
            )
            ranked = rank_etfs(today.reset_index())
            # targets를 문자열 리스트로 통일
            if not ranked.empty:
                ticker_list = [str(t) for t in ranked["ticker"]]
                if kospi_risk_on:
                    targets = ticker_list[:max_positions + ETF_SELL_RANK_BUFFER]
                elif risk_off_liquidate:
                    # KOSPI risk_off + liquidate: foreign/commodity만 buy target
                    # (domestic ETFs는 targets에서 제외되어 매도됨)
                    if enable_multi_index_risk:
                        targets = [t for t in ticker_list if is_ticker_allowed(t, allowed_groups)]
                    else:
                        targets = [t for t in ticker_list if is_ticker_risk_on(t, False)]
                    targets = targets[:max_positions + ETF_SELL_RANK_BUFFER]
                else:
                    # KOSPI risk_off + hold: 기존 포지션 유지, 신규 매수 없음
                    targets = []
            else:
                targets = []
            if portfolio_stop_triggered:
                targets = []
            if stopped_tickers:
                targets = [ticker for ticker in targets if ticker not in stopped_tickers]

            allow_empty_target_sell = (
                (not kospi_risk_on) if risk_off_liquidate else False
            )
            empty_target_protected = (not targets) and (not allow_empty_target_sell)

            pre_holdings_snapshot = dict(holdings)
            pre_cash = cash
            pre_market_value = 0.0
            for ticker, qty in pre_holdings_snapshot.items():
                close_price = get_valuation_price(ticker, today.get("close"), last_valid_closes)
                if close_price is not None:
                    pre_market_value += qty * close_price
            pre_equity = pre_cash + pre_market_value

            if rebalance_observer is not None:
                observer_event = {
                    "decision_date": dt,
                    "risk_on": kospi_risk_on,
                    "kospi_risk_on": kospi_risk_on,
                    "us_risk_on": us_risk_on,
                    "multi_index_enabled": enable_multi_index_risk,
                    "multi_index_gating_mode": MULTI_INDEX_GATING_MODE,
                    "allowed_groups": sorted(list(allowed_groups)),
                    "n_candidates": len(ranked),
                    "ranked_tickers": [str(t) for t in ranked.get("ticker", [])],
                    "targets": list(targets),
                    "allow_empty_target_sell": allow_empty_target_sell,
                    "empty_target_protected": empty_target_protected,
                    "pre_cash": pre_cash,
                    "pre_holdings": pre_holdings_snapshot,
                    "pre_market_value": pre_market_value,
                    "pre_equity": pre_equity,
                }

            # 최신 참조가격(다음 시가)을 기반으로 호가 스프레드를 적용한 매수/매도 참조가격 사전 생성
            latest_prices = next_open.to_dict()
            all_tickers = set(map(str, holdings.keys())) | set(targets)
            latest_buy_prices = {}
            latest_sell_prices = {}
            for t in all_tickers:
                op = safe_get(next_open, t)
                if op is None:
                    latest_buy_prices[t] = None
                    latest_sell_prices[t] = None
                else:
                    latest_buy_prices[t] = op * (1 + SPREAD_PCT / 2)
                    latest_sell_prices[t] = op * (1 - SPREAD_PCT / 2)

            # 실전 주문 생성 로직 재사용
            effective_max_asset_pct = (
                max_asset_pct
                if max_asset_pct is not None
                else parse_fraction_env("MAX_ASSET_PCT", 0.50)
            )
            effective_target_weight_rebalance = (
                target_weight_rebalance
                if target_weight_rebalance is not None
                else bool(strategy_cfg.get("target_weight_rebalance", False))
            )
            effective_rebalance_band_pct = (
                rebalance_band_pct
                if rebalance_band_pct is not None
                else float(strategy_cfg.get("rebalance_band_pct", 0.05))
            )
            effective_trim_overweight_positions = (
                trim_overweight_positions
                if trim_overweight_positions is not None
                else bool(strategy_cfg.get("trim_overweight_positions", False))
            )

            orders = build_rebalance_orders(
                current_holdings=holdings,
                target_tickers=targets,
                latest_prices=latest_prices,
                available_cash=cash,
                latest_buy_prices=latest_buy_prices,
                latest_sell_prices=latest_sell_prices,
                current_cost_basis=holding_cost_basis,
                max_positions=max_positions,
                sell_rank_buffer=ETF_SELL_RANK_BUFFER,
                slippage=slippage,
                sell_tax_pct=ETF_TAXABLE_SELL_TAX_PCT,
                taxable_tickers=TAXABLE_ETF_TICKERS,
                allow_empty_target_sell=allow_empty_target_sell,
                generate_orders=True,
                max_asset_pct=effective_max_asset_pct,
                ticker_names=ticker_names,
                target_weight_rebalance=effective_target_weight_rebalance,
                rebalance_band_pct=effective_rebalance_band_pct,
                trim_overweight_positions=effective_trim_overweight_positions,
            )
            rebalance_order_count = len(orders)

            # 생성된 주문을 즉시 전량 체결로 모사 (백테스트 단순화)
            for o in orders:
                raw_ticker = o.get("ticker")
                ticker = str(raw_ticker)
                qty = int(o.get("qty", 0) or 0)
                ref_price = o.get("reference_price")
                side = o.get("side")

                if side == "SELL":
                    held_qty = int(holdings.get(ticker, 0) or 0)
                    remaining_qty = max(held_qty - qty, 0)
                    if remaining_qty > 0:
                        holdings[ticker] = remaining_qty
                    else:
                        holdings.pop(ticker, None)
                        holding_cost_basis.pop(ticker, None)
                        holding_peak_closes.pop(ticker, None)
                    cash += float(o.get("estimated_value", 0.0))
                    trades.append(
                        {
                            "date": next_dt,
                            "ticker": ticker,
                            "name": get_ticker_name(ticker),
                            "side": "SELL",
                            "reason": o.get("reason", "ETF_REBALANCE"),
                            "qty": qty,
                            "price": ref_price,
                            "net_value": float(o.get("estimated_value", 0.0)),
                            "cash_flow": float(o.get("estimated_value", 0.0)),
                            "estimated_tax": float(o.get("estimated_tax", 0.0)),
                            "cash_after": cash,
                        }
                    )
                else:  # BUY
                    cost = float(o.get("estimated_value", 0.0))
                    if qty <= 0:
                        continue
                    prev_qty = int(holdings.get(ticker, 0) or 0)
                    holdings[ticker] = prev_qty + qty
                    fill_unit_cost = cost / qty if qty > 0 else 0.0
                    if ticker in holding_cost_basis and prev_qty > 0:
                        previous_total_cost = float(holding_cost_basis[ticker]) * prev_qty
                        holding_cost_basis[ticker] = (previous_total_cost + cost) / holdings[ticker]
                    else:
                        holding_cost_basis[ticker] = fill_unit_cost
                    cash -= cost
                    trades.append(
                        {
                            "date": next_dt,
                            "ticker": ticker,
                            "name": get_ticker_name(ticker),
                            "side": "BUY",
                            "reason": o.get("reason", "ETF_REBALANCE"),
                            "qty": qty,
                            "price": ref_price,
                            "net_value": cost,
                            "cash_flow": -cost,
                            "estimated_tax": 0.0,
                            "cash_after": cash,
                        }
                    )

        cash += distribution_cash
        update_last_valid_prices(last_valid_closes, next_close)
        market_value = 0.0
        for ticker, qty in holdings.items():
            close_price = get_valuation_price(ticker, next_close, last_valid_closes)
            if close_price is not None:
                market_value += qty * close_price
                holding_peak_closes[ticker] = max(
                    holding_peak_closes.get(ticker, close_price),
                    close_price,
                )
        post_equity = cash + market_value
        if holdings:
            portfolio_peak_equity = max(portfolio_peak_equity or post_equity, post_equity)
        else:
            portfolio_peak_equity = None

        equity_rows.append(
            {
                "date": next_dt,
                "equity": post_equity,
                "cash": cash,
                "market_value": market_value,
                "holdings": ",".join(sorted(map(str, holdings.keys()))),
                "distribution_cash": distribution_cash,
                "rebalance_decision": should_rebalance,
                "rebalance_order_count": rebalance_order_count,
                "exit_order_count": exit_order_count,
            }
        )

        if observer_event is not None:
            post_holdings_snapshot = dict(holdings)
            post_equity = cash + market_value
            observer_event.update(
                {
                    "execution_date": next_dt,
                    "post_cash": cash,
                    "post_holdings": post_holdings_snapshot,
                    "post_market_value": market_value,
                    "post_equity": post_equity,
                    "n_orders": len(orders),
                    "n_buys": sum(1 for order in orders if order.get("side") == "BUY"),
                    "n_sells": sum(1 for order in orders if order.get("side") == "SELL"),
                    "held_unchanged": pre_holdings_snapshot == post_holdings_snapshot,
                    "uninvested": not post_holdings_snapshot,
                }
            )
            try:
                rebalance_observer(observer_event)
            except Exception:
                logger.exception("rebalance_observer 실행 실패")
                raise

    return pd.DataFrame(equity_rows), pd.DataFrame(trades)


def run_kodex200_buy_and_hold(initial_cash: float, common_dates: list[pd.Timestamp]) -> pd.DataFrame:
    """KODEX200 벤치마크 바이앤홀드 전략을 백테스트한다.

    ``ETF_RETURN_BASIS=total_return``이면 분배금을 현금으로 수령해 자산에 반영한다
    (실전에서는 증권사 예수금에 자동 반영되므로 러너와 동일한 랭킹만 공유).
    """
    price = get_price(BENCHMARK_TICKER, listing_dates=get_listing_dates({BENCHMARK_TICKER}))
    if price.empty:
        raise RuntimeError(f"No benchmark data for {BENCHMARK_TICKER}")

    distributions = load_distributions(required=_return_basis_requires_distributions())
    price = add_distributions(price, distributions)
    price_by_date = {dt: day.set_index("ticker") for dt, day in price.groupby("date")}
    cash = float(initial_cash)
    qty = 0
    bought = False
    last_valid_closes: dict[str, float] = {}
    equity_rows = []

    for i, current_dt in enumerate(common_dates[:-1]):
        next_dt = common_dates[i + 1]
        current_day = price_by_date.get(current_dt, pd.DataFrame())
        next_day = price_by_date.get(next_dt, pd.DataFrame())
        if next_day.empty:
            continue

        if not current_day.empty:
            update_last_valid_prices(last_valid_closes, current_day.get("close"))

        next_open = next_day["open"]
        next_close = next_day["close"]
        entitled_qty = qty

        if not bought:
            open_price = safe_get(next_open, BENCHMARK_TICKER)
            if open_price is None:
                continue
            # 벤치마크 매수도 스프레드 반영
            exec_buy_price = open_price * (1 + SPREAD_PCT / 2)
            unit_cost = apply_buy_cost(exec_buy_price, SLIPPAGE_PCT)
            qty = int(cash // unit_cost)
            if qty > 0:
                cash -= qty * unit_cost
                bought = True

        distribution_cash = distribution_cash_for_holdings(
            {BENCHMARK_TICKER: entitled_qty},
            next_day.get("distribution"),
            parse_pct_env("ETF_DISTRIBUTION_TAX_PCT", 0.0),
        )
        cash += distribution_cash
        close_price = get_valuation_price(BENCHMARK_TICKER, next_close, last_valid_closes)
        market_value = qty * close_price if close_price is not None else 0.0
        equity_rows.append(
            {
                "date": next_dt,
                "equity_kodex200_bh": cash + market_value,
                "cash_kodex200_bh": cash,
                "market_value_kodex200_bh": market_value,
                "distribution_cash_kodex200_bh": distribution_cash,
            }
        )

    return pd.DataFrame(equity_rows)



def calc_stats(df: pd.DataFrame, equity_col: str, risk_free: float = 0.0) -> dict:
    temp = df[["date", equity_col]].dropna().copy()
    temp["date"] = pd.to_datetime(temp["date"])
    temp = temp.sort_values("date").reset_index(drop=True)

    # 일간 수익률: 첫 관측치는 NaN이 되므로 분석에서 제외하기 위해 dropna() 사용
    returns = temp[equity_col].pct_change().dropna()

    total_return = temp[equity_col].iloc[-1] / temp[equity_col].iloc[0] - 1
    years = max((temp["date"].iloc[-1] - temp["date"].iloc[0]).days / 365.25, 1 / 365.25)
    cagr = (temp[equity_col].iloc[-1] / temp[equity_col].iloc[0]) ** (1 / years) - 1

    drawdown = temp[equity_col] / temp[equity_col].cummax() - 1
    mdd = drawdown.min()
    # 주의: mdd는 음수로 반환됩니다 (예: -0.25 == -25% 최대 낙폭)

    trough_idx = drawdown.idxmin()
    peak_idx = temp.loc[:trough_idx, equity_col].idxmax()
    peak_value = float(temp.loc[peak_idx, equity_col])
    recovery_mask = temp.loc[trough_idx + 1 :, equity_col] >= peak_value
    recovery_idx = recovery_mask[recovery_mask].index.min() if recovery_mask.any() else None
    current_peak_idx = temp[equity_col].idxmax()
    current_drawdown = float(drawdown.iloc[-1])
    current_drawdown_days = int(
        (temp["date"].iloc[-1] - temp.loc[current_peak_idx, "date"]).days
    )

    # 변동성은 모집단 기준(ddof=0) 표준편차로 계산
    volatility = returns.std(ddof=0) * np.sqrt(252) if not returns.empty else 0.0

    # 샤프: risk_free는 연간 비율(예: 0.01)을 입력, 기본 0
    if volatility == 0:
        sharpe = np.nan
    else:
        rf_daily = risk_free / 252
        sharpe = (returns.mean() - rf_daily) * 252 / volatility

    # Sortino는 하방 변동성만 사용한다. 목표수익률은 Sharpe와 동일하게 무위험수익률로 둔다.
    rf_daily = risk_free / 252
    downside_returns = np.minimum(returns - rf_daily, 0.0)
    downside_deviation = (
        np.sqrt(np.mean(np.square(downside_returns))) * np.sqrt(252)
        if not returns.empty
        else 0.0
    )
    sortino = (
        (returns.mean() - rf_daily) * 252 / downside_deviation
        if downside_deviation > 0
        else np.nan
    )

    # CVaR(95%): 일간 수익률 하위 5% 구간의 평균 손실.
    var_95 = returns.quantile(0.05) if not returns.empty else np.nan
    cvar_95 = returns[returns <= var_95].mean() if not returns.empty else np.nan
    ulcer_index = float(np.sqrt(np.mean(np.square(drawdown)))) if not drawdown.empty else 0.0
    tail_loss = returns.quantile(0.05) if not returns.empty else np.nan
    tail_gain = returns.quantile(0.95) if not returns.empty else np.nan
    tail_ratio = tail_gain / abs(tail_loss) if pd.notna(tail_loss) and tail_loss != 0 else np.nan
    max_drawdown_abs = abs(mdd)

    return {
        "initial": temp[equity_col].iloc[0],
        "final": temp[equity_col].iloc[-1],
        "total_return": total_return,
        "cagr": cagr,
        "mdd": mdd,
        "mdd_peak_date": str(temp.loc[peak_idx, "date"].date()),
        "mdd_trough_date": str(temp.loc[trough_idx, "date"].date()),
        "mdd_recovery_date": (
            str(temp.loc[recovery_idx, "date"].date()) if recovery_idx is not None else None
        ),
        "mdd_in_progress": recovery_idx is None,
        "current_drawdown": current_drawdown,
        "current_drawdown_peak_date": str(temp.loc[current_peak_idx, "date"].date()),
        "current_drawdown_days": current_drawdown_days,
        "volatility": volatility,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": cagr / max_drawdown_abs if max_drawdown_abs > 0 else np.nan,
        "cvar_95_daily": cvar_95,
        "ulcer_index": ulcer_index,
        "tail_ratio": tail_ratio,
        # Recovery factor는 누적수익률을 최대낙폭 절대값으로 나눈 값이다.
        "recovery_factor": total_return / max_drawdown_abs if max_drawdown_abs > 0 else np.nan,
    }


def calc_trading_stats(
    trades: pd.DataFrame,
    equity: pd.Series,
    dates: pd.Series,
    rebalance_decisions: pd.Series | None = None,
) -> dict:
    """거래내역에서 회전율·보유기간·리밸런싱 빈도를 계산한다.

    gross turnover은 매수와 매도의 총 체결대금을 평균 운용자산으로
    나눈 값이다. one-way turnover은 매수·매도 중 작은 금액을 사용한다.
    """
    period_days = max((pd.to_datetime(dates).max() - pd.to_datetime(dates).min()).days, 1)
    years = period_days / 365.25
    if trades.empty:
        scheduled_rebalance_count = (
            int(pd.Series(rebalance_decisions).fillna(False).astype(bool).sum())
            if rebalance_decisions is not None
            else 0
        )
        return {
            "trade_count": 0,
            "rebalance_count": 0,
            "rebalances_per_year": 0.0,
            "turnover": 0.0,
            "annual_turnover": 0.0,
            "gross_turnover": 0.0,
            "annual_gross_turnover": 0.0,
            "one_way_turnover": 0.0,
            "annual_one_way_turnover": 0.0,
            "buy_value": 0.0,
            "sell_value": 0.0,
            "avg_holding_days": np.nan,
            "avg_closed_holding_days": np.nan,
            "avg_open_holding_days": np.nan,
            "oldest_open_holding_days": np.nan,
            "open_quantity": 0,
            "scheduled_rebalance_count": scheduled_rebalance_count,
            "trade_rebalance_count": 0,
            "no_trade_rebalance_count": scheduled_rebalance_count,
            "scheduled_rebalances_per_year": (
                scheduled_rebalance_count / years
                if years > 0
                else float(scheduled_rebalance_count)
            ),
            "trade_rebalances_per_year": 0.0,
        }

    data = trades.copy()
    data["date"] = pd.to_datetime(data["date"])
    data["qty"] = pd.to_numeric(data["qty"], errors="coerce").fillna(0).astype(int)
    data["net_value"] = pd.to_numeric(data["net_value"], errors="coerce").fillna(0.0).abs()
    buy_value = float(data.loc[data["side"].str.upper() == "BUY", "net_value"].sum())
    sell_value = float(data.loc[data["side"].str.upper() == "SELL", "net_value"].sum())
    total_traded_value = buy_value + sell_value
    average_equity = float(pd.to_numeric(equity, errors="coerce").mean())
    gross_turnover = total_traded_value / average_equity if average_equity > 0 else np.nan
    one_way_turnover = min(buy_value, sell_value) / average_equity if average_equity > 0 else np.nan

    # FIFO로 매수 수량과 매도 수량을 대응시켜, 부분매도가 있어도 수량 가중 보유기간을 계산한다.
    open_lots: dict[str, list[list[object]]] = {}
    holding_day_weight = 0.0
    closed_qty = 0
    for row in data.sort_values("date").itertuples(index=False):
        ticker = str(row.ticker)
        qty = int(row.qty)
        if qty <= 0:
            continue
        if str(row.side).upper() == "BUY":
            open_lots.setdefault(ticker, []).append([row.date, qty])
            continue
        if str(row.side).upper() != "SELL":
            continue
        remaining = qty
        for lot in open_lots.get(ticker, []):
            if remaining <= 0:
                break
            matched = min(remaining, int(lot[1]))
            holding_day_weight += matched * max((row.date - lot[0]).days, 0)
            closed_qty += matched
            lot[1] = int(lot[1]) - matched
            remaining -= matched
        open_lots[ticker] = [lot for lot in open_lots.get(ticker, []) if int(lot[1]) > 0]

    trade_rebalance_count = int(data["date"].dt.normalize().nunique())
    scheduled_rebalance_count = (
        int(pd.Series(rebalance_decisions).fillna(False).astype(bool).sum())
        if rebalance_decisions is not None
        else trade_rebalance_count
    )
    as_of_date = pd.to_datetime(dates).max()
    open_holding_day_weight = 0.0
    open_qty = 0
    oldest_open_holding_days = 0
    for lots in open_lots.values():
        for lot_date, lot_qty in lots:
            qty = int(lot_qty)
            age_days = max((as_of_date - lot_date).days, 0)
            open_holding_day_weight += qty * age_days
            open_qty += qty
            oldest_open_holding_days = max(oldest_open_holding_days, age_days)

    avg_closed_holding_days = holding_day_weight / closed_qty if closed_qty > 0 else np.nan
    return {
        "trade_count": int(len(data)),
        # 기존 키는 하위 호환을 위해 거래 발생일/gross 정의를 유지한다.
        "rebalance_count": trade_rebalance_count,
        "rebalances_per_year": (
            trade_rebalance_count / years if years > 0 else float(trade_rebalance_count)
        ),
        "turnover": gross_turnover,
        "annual_turnover": gross_turnover / years if years > 0 else np.nan,
        "gross_turnover": gross_turnover,
        "annual_gross_turnover": gross_turnover / years if years > 0 else np.nan,
        "one_way_turnover": one_way_turnover,
        "annual_one_way_turnover": one_way_turnover / years if years > 0 else np.nan,
        "buy_value": buy_value,
        "sell_value": sell_value,
        "scheduled_rebalance_count": scheduled_rebalance_count,
        "trade_rebalance_count": trade_rebalance_count,
        "no_trade_rebalance_count": max(
            scheduled_rebalance_count - trade_rebalance_count, 0
        ),
        "scheduled_rebalances_per_year": (
            scheduled_rebalance_count / years
            if years > 0
            else float(scheduled_rebalance_count)
        ),
        "trade_rebalances_per_year": (
            trade_rebalance_count / years if years > 0 else float(trade_rebalance_count)
        ),
        "avg_holding_days": avg_closed_holding_days,
        "avg_closed_holding_days": avg_closed_holding_days,
        "avg_open_holding_days": (
            open_holding_day_weight / open_qty if open_qty > 0 else np.nan
        ),
        "oldest_open_holding_days": oldest_open_holding_days if open_qty > 0 else np.nan,
        "open_quantity": open_qty,
    }


def build_return_reports(df: pd.DataFrame, equity_col: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """월간/연간 수익률과 1년·3년 롤링 위험지표를 반환한다."""
    temp = df[["date", equity_col]].dropna().copy().sort_values("date")
    temp["date"] = pd.to_datetime(temp["date"])
    temp = temp.set_index("date")
    daily_returns = temp[equity_col].pct_change()
    monthly = temp[equity_col].resample("ME").last().pct_change().dropna().rename("return").reset_index()
    annual = temp[equity_col].resample("YE").last().pct_change().dropna().rename("return").reset_index()

    def rolling_mdd(values: np.ndarray) -> float:
        return float((values / np.maximum.accumulate(values) - 1).min())

    def rolling_sharpe(values: np.ndarray) -> float:
        volatility = values.std(ddof=0) * np.sqrt(252)
        return float(values.mean() * 252 / volatility) if volatility > 0 else np.nan

    def rolling_sortino(values: np.ndarray) -> float:
        downside = np.sqrt(np.mean(np.square(np.minimum(values, 0.0)))) * np.sqrt(252)
        return float(values.mean() * 252 / downside) if downside > 0 else np.nan

    window_1y = 252
    window_3y = 252 * 3
    rolling = pd.DataFrame({"date": temp.index, "equity": temp[equity_col].to_numpy()})
    # temp의 DatetimeIndex와 rolling의 RangeIndex가 자동 정렬되지 않도록 위치 기준으로 대입한다.
    rolling["rolling_1y_cagr"] = temp[equity_col].pct_change(window_1y).to_numpy()
    rolling["rolling_1y_mdd"] = (
        temp[equity_col]
        .rolling(window_1y, min_periods=window_1y)
        .apply(rolling_mdd, raw=True)
        .to_numpy()
    )
    rolling["rolling_1y_sharpe"] = (
        daily_returns.rolling(window_1y, min_periods=window_1y)
        .apply(rolling_sharpe, raw=True)
        .to_numpy()
    )
    rolling["rolling_1y_sortino"] = (
        daily_returns.rolling(window_1y, min_periods=window_1y)
        .apply(rolling_sortino, raw=True)
        .to_numpy()
    )
    rolling["rolling_3y_cagr"] = (
        (temp[equity_col] / temp[equity_col].shift(window_3y)) ** (1 / 3) - 1
    ).to_numpy()
    rolling["rolling_3y_mdd"] = (
        temp[equity_col]
        .rolling(window_3y, min_periods=window_3y)
        .apply(rolling_mdd, raw=True)
        .to_numpy()
    )
    rolling["rolling_3y_sharpe"] = (
        daily_returns.rolling(window_3y, min_periods=window_3y)
        .apply(rolling_sharpe, raw=True)
        .to_numpy()
    )
    rolling["rolling_3y_sortino"] = (
        daily_returns.rolling(window_3y, min_periods=window_3y)
        .apply(rolling_sortino, raw=True)
        .to_numpy()
    )
    return monthly, annual, rolling


def get_backtest_period(df: pd.DataFrame, equity_col: str) -> dict:
    temp = df[["date", equity_col]].dropna().copy()
    temp["date"] = pd.to_datetime(temp["date"])
    temp = temp.sort_values("date")

    start_dt = temp["date"].iloc[0]
    end_dt = temp["date"].iloc[-1]
    calendar_days = int((end_dt - start_dt).days)
    years = max(calendar_days / 365.25, 1 / 365.25)

    return {
        "start": str(start_dt.date()),
        "end": str(end_dt.date()),
        "trading_days": int(len(temp)),
        "calendar_days": calendar_days,
        "years": years,
    }


def calc_period_stats(
    df: pd.DataFrame,
    equity_col: str,
    period_name: str,
    start: str,
    end: str,
    risk_free: float = 0.0,
) -> dict | None:
    temp = df[["date", equity_col]].dropna().copy()
    temp["date"] = pd.to_datetime(temp["date"])
    start_dt = pd.Timestamp(start)
    end_dt = pd.Timestamp(end)
    temp = temp[(temp["date"] >= start_dt) & (temp["date"] <= end_dt)].sort_values("date")

    if len(temp) < 2:
        return None

    returns = temp[equity_col].pct_change().dropna()
    total_return = temp[equity_col].iloc[-1] / temp[equity_col].iloc[0] - 1
    years = max((temp["date"].iloc[-1] - temp["date"].iloc[0]).days / 365.25, 1 / 365.25)
    cagr = (temp[equity_col].iloc[-1] / temp[equity_col].iloc[0]) ** (1 / years) - 1

    drawdown = temp[equity_col] / temp[equity_col].cummax() - 1
    mdd = drawdown.min()
    # 주의: mdd는 음수로 반환됩니다 (예: -0.25 == -25% 최대 낙폭)

    volatility = returns.std(ddof=0) * np.sqrt(252) if not returns.empty else 0.0
    if volatility == 0:
        sharpe = np.nan
    else:
        rf_daily = risk_free / 252
        sharpe = (returns.mean() - rf_daily) * 252 / volatility

    return {
        "period": period_name,
        "start": str(temp["date"].iloc[0].date()),
        "end": str(temp["date"].iloc[-1].date()),
        "strategy": equity_col,
        "initial": temp[equity_col].iloc[0],
        "final": temp[equity_col].iloc[-1],
        "total_return": total_return,
        "cagr": cagr,
        "mdd": mdd,
        "volatility": volatility,
        "sharpe": sharpe,
    }


def build_period_comparison(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for period_name, start, end in PERIODS:
        benchmark_stats = calc_period_stats(df, "equity_kodex200_bh", period_name, start, end)
        if benchmark_stats is not None:
            benchmark_stats["strategy"] = "KODEX200_BuyHold"
            rows.append(benchmark_stats)

        for slip in SLIPPAGE_OPTIONS:
            label = f"slip_{int(slip * 10000)}bp"
            equity_col = f"equity_{label}"
            if equity_col not in df.columns:
                continue
            slip_stats = calc_period_stats(df, equity_col, period_name, start, end)
            if slip_stats is not None:
                slip_stats["strategy"] = f"ETF_{label}"
                rows.append(slip_stats)

    if not rows:
        return pd.DataFrame()

    comparison = pd.DataFrame(rows)
    comparison = comparison[
        [
            "period",
            "strategy",
            "start",
            "end",
            "initial",
            "final",
            "total_return",
            "cagr",
            "mdd",
            "volatility",
            "sharpe",
        ]
    ]
    return comparison


def run_single_mode() -> tuple[pd.DataFrame, pd.DataFrame]:
    index_df = get_index_data()
    us_index_df = get_us_index_data() if ENABLE_MULTI_INDEX_RISK else None
    common_dates = list(index_df["date"])

    strategy_cfg = get_strategy_config()
    result, trades = run_etf_strategy(
        INITIAL_CASH,
        common_dates,
        index_df,
        use_market_filter=USE_MARKET_FILTER,
        max_positions=ETF_MAX_POSITIONS,
        slippage=BASE_SLIPPAGE,
        risk_off_liquidate=strategy_cfg.get("liquidate_on_risk_off", True),
        us_index_df=us_index_df,
        enable_multi_index_risk=ENABLE_MULTI_INDEX_RISK,
    )

    if ENABLE_BENCHMARK:
        try:
            benchmark_curve = run_kodex200_buy_and_hold(INITIAL_CASH, common_dates)
        except Exception as e:
            print(f"[경고] 벤치마크 수집 실패: {e} — 벤치마크 병합을 생략합니다.")
            benchmark_curve = pd.DataFrame()

        if isinstance(benchmark_curve, pd.DataFrame) and "date" in benchmark_curve.columns:
            try:
                benchmark_curve["date"] = pd.to_datetime(benchmark_curve["date"])
            except Exception:
                pass

        if not isinstance(benchmark_curve, pd.DataFrame) or benchmark_curve.empty or not {"date", "equity_kodex200_bh"}.issubset(set(benchmark_curve.columns)):
            print("[경고] 벤치마크 데이터가 없거나 컬럼 누락 — 병합을 건너뜁니다.")
        else:
            benchmark_curve = benchmark_curve[["date", "equity_kodex200_bh"]]
            result = pd.merge(result, benchmark_curve, on="date", how="outer")

    result = result.sort_values("date")
    result["equity"] = result["equity"].ffill().fillna(INITIAL_CASH)
    result["cash"] = result["cash"].ffill().fillna(INITIAL_CASH)
    result["market_value"] = result["market_value"].ffill().fillna(0)
    result["rebalance_decision"] = (
        result["rebalance_decision"].astype("boolean").fillna(False).astype(bool)
    )
    result["rebalance_order_count"] = (
        result["rebalance_order_count"].fillna(0).astype(int)
    )
    result["exit_order_count"] = result["exit_order_count"].fillna(0).astype(int)

    if ENABLE_BENCHMARK and "equity_kodex200_bh" in result.columns:
        result["equity_kodex200_bh"] = result["equity_kodex200_bh"].ffill().fillna(INITIAL_CASH)

    return result, trades


def run_experiment_mode() -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    index_df = get_index_data()
    us_index_df = get_us_index_data() if ENABLE_MULTI_INDEX_RISK else None
    common_dates = list(index_df["date"])

    results = []
    trades_dict = {}

    strategy_cfg = get_strategy_config()
    risk_off_liquidate = strategy_cfg.get("liquidate_on_risk_off", True)

    for slip in SLIPPAGE_OPTIONS:
        curve, trades = run_etf_strategy(
            INITIAL_CASH,
            common_dates,
            index_df,
            True,
            2,
            slip,
            risk_off_liquidate=risk_off_liquidate,
            us_index_df=us_index_df,
            enable_multi_index_risk=ENABLE_MULTI_INDEX_RISK,
        )
        label = f"slip_{int(slip*10000)}bp"
        curve = curve.rename(
            columns={
                "equity": f"equity_{label}",
                "market_value": f"market_value_{label}",
            }
        )
        curve = curve[["date", f"equity_{label}", f"market_value_{label}"]]
        results.append(curve)
        trades_dict[label] = trades

    try:
        benchmark_curve = run_kodex200_buy_and_hold(INITIAL_CASH, common_dates)
    except Exception as e:
        print(f"[경고] 벤치마크 수집 실패: {e} — 기본 벤치마크로 대체합니다.")
        benchmark_curve = pd.DataFrame({"date": common_dates, "equity_kodex200_bh": [INITIAL_CASH] * len(common_dates)})

    if isinstance(benchmark_curve, pd.DataFrame) and "date" in benchmark_curve.columns:
        try:
            benchmark_curve["date"] = pd.to_datetime(benchmark_curve["date"])
        except Exception:
            pass

    if not {"date", "equity_kodex200_bh"}.issubset(set(benchmark_curve.columns)) or benchmark_curve.empty:
        print("[경고] 벤치마크 데이터 형식 불완전 — 기본 벤치마크로 대체합니다.")
        benchmark_curve = pd.DataFrame({"date": common_dates, "equity_kodex200_bh": [INITIAL_CASH] * len(common_dates)})

    benchmark_curve = benchmark_curve[["date", "equity_kodex200_bh"]]
    result = benchmark_curve
    for curve in results:
        result = pd.merge(result, curve, on="date", how="outer")

    result = result.sort_values("date")

    for slip in SLIPPAGE_OPTIONS:
        label = f"slip_{int(slip*10000)}bp"
        result[f"equity_{label}"] = result[f"equity_{label}"].ffill().fillna(INITIAL_CASH)
        result[f"market_value_{label}"] = result[f"market_value_{label}"].ffill().fillna(0)

    result["equity_kodex200_bh"] = result["equity_kodex200_bh"].ffill().fillna(INITIAL_CASH)

    return result, trades_dict



def summarize_single(df: pd.DataFrame, trades: pd.DataFrame) -> tuple[dict, dict | None, dict]:
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])

    strategy_stats = calc_stats(df, "equity")
    period = get_backtest_period(df, "equity")
    invested_ratio = (df["market_value"] / df["equity"]).replace([np.inf, -np.inf], np.nan).infer_objects(copy=False).mean()
    trading_stats = calc_trading_stats(
        trades,
        df["equity"],
        df["date"],
        df.get("rebalance_decision"),
    )
    strategy_stats.update({"avg_invested_ratio": invested_ratio, **trading_stats})

    print("\n=== 일반 백테스트 결과 ===")
    print("모드: single")
    print(f"슬리피지: {BASE_SLIPPAGE * 10000:.0f}bp")
    print(f"스프레드: {SPREAD_PCT * 10000:.0f}bp")
    print(f"백테스트 기간: {period['start']} ~ {period['end']} ({period['trading_days']} 거래일, {period['years']:.2f}년)")
    print(f"초기 자산: {strategy_stats['initial']:,.0f}")
    print(f"최종 자산: {strategy_stats['final']:,.0f}")
    print(f"누적 수익률: {strategy_stats['total_return']:.2%}")
    print(f"CAGR: {strategy_stats['cagr']:.2%}")
    print(f"MDD: {strategy_stats['mdd']:.2%}")
    print(
        "MDD 구간: "
        f"{strategy_stats['mdd_peak_date']} → {strategy_stats['mdd_trough_date']} "
        f"(회복: {strategy_stats['mdd_recovery_date'] or '미회복'})"
    )
    print(
        f"현재 낙폭: {strategy_stats['current_drawdown']:.2%} "
        f"({strategy_stats['current_drawdown_peak_date']} 고점 이후 "
        f"{strategy_stats['current_drawdown_days']}일)"
    )
    print(f"변동성(연환산): {strategy_stats['volatility']:.2%}")
    print(f"샤프: {strategy_stats['sharpe']:.4f}")
    print(f"Sortino: {strategy_stats['sortino']:.4f}")
    print(f"Calmar: {strategy_stats['calmar']:.4f}")
    print(f"CVaR(95%, 일간): {strategy_stats['cvar_95_daily']:.2%}")
    print(f"Ulcer Index: {strategy_stats['ulcer_index']:.4f}")
    print(f"Tail Ratio: {strategy_stats['tail_ratio']:.4f}")
    print(f"Recovery Factor: {strategy_stats['recovery_factor']:.4f}")
    print(f"거래 수: {trading_stats['trade_count']}")
    print(
        "연환산 회전율(gross/one-way): "
        f"{trading_stats['annual_gross_turnover']:.2%} / "
        f"{trading_stats['annual_one_way_turnover']:.2%}"
    )
    print(f"평균 보유기간(청산 lot): {trading_stats['avg_closed_holding_days']:.1f}일")
    if pd.notna(trading_stats["avg_open_holding_days"]):
        print(
            f"평균 보유기간(미청산 lot): "
            f"{trading_stats['avg_open_holding_days']:.1f}일 "
            f"(최장 {trading_stats['oldest_open_holding_days']}일)"
        )
    print(
        "리밸런싱 판단/거래 발생/무거래: "
        f"{trading_stats['scheduled_rebalance_count']} / "
        f"{trading_stats['trade_rebalance_count']} / "
        f"{trading_stats['no_trade_rebalance_count']}"
    )
    print(f"평균 투자 비중: {invested_ratio:.4f}")

    benchmark_stats = None
    if ENABLE_BENCHMARK and "equity_kodex200_bh" in df.columns:
        benchmark_stats = calc_stats(df, "equity_kodex200_bh")
        print("\n=== 벤치마크(KODEX200 Buy&Hold) ===")
        print(f"최종 자산: {benchmark_stats['final']:,.0f}")
        print(f"누적 수익률: {benchmark_stats['total_return']:.2%}")
        print(f"CAGR: {benchmark_stats['cagr']:.2%}")
        print(f"MDD: {benchmark_stats['mdd']:.2%}")
        print(f"샤프: {benchmark_stats['sharpe']:.4f}")
        print(f"Sortino: {benchmark_stats['sortino']:.4f}")
        print(f"Calmar: {benchmark_stats['calmar']:.4f}")
        print(f"CVaR(95%, 일간): {benchmark_stats['cvar_95_daily']:.2%}")
        print("\n=== 벤치마크 대비 ===")
        print(
            "요약: "
            f"누적수익률 {strategy_stats['total_return'] - benchmark_stats['total_return']:+.2%}, "
            f"CAGR {strategy_stats['cagr'] - benchmark_stats['cagr']:+.2%}, "
            f"MDD {strategy_stats['mdd'] - benchmark_stats['mdd']:+.2%}"
        )

    return strategy_stats, benchmark_stats, period


def save_single_analytics(
    df: pd.DataFrame,
    strategy_stats: dict,
    benchmark_stats: dict | None,
) -> None:
    """단일 백테스트용 비교표와 시계열 성과 리포트를 저장한다."""
    comparison_rows = [{"strategy": "ETF_Strategy", **strategy_stats}]
    if benchmark_stats is not None:
        comparison_rows.append({"strategy": "KODEX200_BuyHold", **benchmark_stats})
    comparison = pd.DataFrame(comparison_rows)
    comparison.to_csv(OUTPUT_DIR / "performance_comparison.csv", index=False, encoding="utf-8-sig")

    monthly, annual, rolling = build_return_reports(df, "equity")
    monthly.to_csv(OUTPUT_DIR / "monthly_returns.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUTPUT_DIR / "annual_returns.csv", index=False, encoding="utf-8-sig")
    rolling.to_csv(OUTPUT_DIR / "rolling_metrics.csv", index=False, encoding="utf-8-sig")


def summarize_experiment(df: pd.DataFrame, trades_dict: dict) -> tuple[dict, list[dict], list[dict]]:
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    period = get_backtest_period(df, "equity_kodex200_bh")

    rows = []
    benchmark_stats = calc_stats(df, "equity_kodex200_bh")
    rows.append({"strategy": "KODEX200_BuyHold", **benchmark_stats})

    for slip in SLIPPAGE_OPTIONS:
        label = f"slip_{int(slip*10000)}bp"
        stats = calc_stats(df, f"equity_{label}")
        rows.append({"strategy": f"ETF_{label}", **stats})

    comparison = pd.DataFrame(rows)

    print(f"\n백테스트 기간: {period['start']} ~ {period['end']} ({period['trading_days']} 거래일, {period['years']:.2f}년)")
    print("\n=== 슬리피지 민감도 테스트 ===")
    display_cols = [
        "strategy", "final", "total_return", "cagr", "mdd", "calmar",
        "sortino", "cvar_95_daily", "volatility", "sharpe",
    ]
    print(comparison[display_cols].to_string(index=False, float_format=lambda x: f"{x:,.4f}"))

    details = []
    for slip in SLIPPAGE_OPTIONS:
        label = f"slip_{int(slip*10000)}bp"
        trades = trades_dict[label]
        invested_ratio = (df[f"market_value_{label}"] / df[f"equity_{label}"]).mean()

        print(f"\n=== ETF {label} 상세 ===")
        print(f"거래 수: {len(trades)}")
        print(f"평균 투자 비중: {invested_ratio:.4f}")
        details.append({
            "strategy": f"ETF_{label}",
            "trade_count": int(len(trades)),
            "avg_invested_ratio": float(invested_ratio),
        })

    return period, comparison.to_dict(orient="records"), details


def _to_json_serializable(stats: dict) -> dict:
    serializable = {}
    for key, value in stats.items():
        if isinstance(value, (np.integer, np.floating)):
            serializable[key] = float(value)
        elif pd.isna(value):
            serializable[key] = None
        else:
            serializable[key] = value
    return serializable


def _build_performance_config() -> dict:
    """performance.json에 기록할 실행 설정 블록을 반환한다."""
    return {
        "run_mode": RUN_MODE,
        "return_basis": strategy_cfg.get("return_basis", "price"),
        "distributions_file": str(distributions_path()),
        "distributions_sha256": distributions_file_sha256(),
        "distribution_tax_pct": parse_pct_env("ETF_DISTRIBUTION_TAX_PCT", 0.0),
        "min_listing_days": strategy_cfg.get("min_listing_days", 60),
        "max_premium_discount": strategy_cfg.get("max_premium_discount", 0.02),
        "min_avg_trading_value": strategy_cfg.get("min_avg_trading_value", 1_000_000_000),
        "max_asset_pct": parse_fraction_env("MAX_ASSET_PCT", 0.50),
        "target_weight_rebalance": strategy_cfg.get("target_weight_rebalance", False),
        "rebalance_band_pct": strategy_cfg.get("rebalance_band_pct", 0.05),
        "trim_overweight_positions": strategy_cfg.get("trim_overweight_positions", False),
        "exit_check_days": int(os.environ.get("ETF_EXIT_CHECK_DAYS", "0")),
        "trailing_stop_pct": parse_fraction_env("ETF_TRAILING_STOP_PCT", 0.0),
        "portfolio_trailing_stop_pct": parse_fraction_env(
            "ETF_PORTFOLIO_TRAILING_STOP_PCT", 0.0
        ),
        "liquidate_on_risk_off": strategy_cfg.get("liquidate_on_risk_off", True),
        "slippage": BASE_SLIPPAGE,
        "spread_pct": SPREAD_PCT,
        "rebalance_step_days": strategy_cfg.get("rebalance_step_days", 10),
        "market_ma_days": strategy_cfg.get("market_ma_days", 120),
        "market_slope_days": strategy_cfg.get("market_slope_days", 20),
        "enable_multi_index_risk": ENABLE_MULTI_INDEX_RISK,
        "multi_index_gating_mode": MULTI_INDEX_GATING_MODE,
        "us_risk_proxy": US_RISK_PROXY,
        "us_market_ma_days": US_MARKET_MA_DAYS,
        "us_market_slope_days": US_MARKET_SLOPE_DAYS,
        "max_positions": strategy_cfg.get("max_positions", 2),
        "sell_rank_buffer": strategy_cfg.get("sell_rank_buffer", 3),
        "enable_benchmark": ENABLE_BENCHMARK,
    }


def run_risk_off_compare_mode() -> None:
    """liquidate_on_risk_off=True vs False 비교 실행"""
    index_df = get_index_data()
    us_index_df = get_us_index_data() if ENABLE_MULTI_INDEX_RISK else None
    common_dates = list(index_df["date"])

    print("\n=== Risk-Off 행동 비교: Liquidate(매도) vs Hold(보유) ===")
    print("실행 중...\n")

    curve_liquidate, trades_liquidate = run_etf_strategy(
        INITIAL_CASH, common_dates, index_df,
        use_market_filter=True, max_positions=ETF_MAX_POSITIONS,
        slippage=BASE_SLIPPAGE, risk_off_liquidate=True,
        us_index_df=us_index_df, enable_multi_index_risk=ENABLE_MULTI_INDEX_RISK,
    )
    curve_liquidate = curve_liquidate.rename(columns={
        "equity": "equity_liquidate",
        "market_value": "market_value_liquidate",
    })

    curve_hold, trades_hold = run_etf_strategy(
        INITIAL_CASH, common_dates, index_df,
        use_market_filter=True, max_positions=ETF_MAX_POSITIONS,
        slippage=BASE_SLIPPAGE, risk_off_liquidate=False,
        us_index_df=us_index_df, enable_multi_index_risk=ENABLE_MULTI_INDEX_RISK,
    )
    curve_hold = curve_hold.rename(columns={
        "equity": "equity_hold",
        "market_value": "market_value_hold",
    })

    result = curve_liquidate[["date", "equity_liquidate", "market_value_liquidate"]].copy()
    result = pd.merge(result, curve_hold[["date", "equity_hold", "market_value_hold"]], on="date", how="outer")

    if ENABLE_BENCHMARK:
        try:
            benchmark_curve = run_kodex200_buy_and_hold(INITIAL_CASH, common_dates)
        except Exception as e:
            print(f"[경고] 벤치마크 수집 실패: {e}")
            benchmark_curve = pd.DataFrame()

        if isinstance(benchmark_curve, pd.DataFrame) and not benchmark_curve.empty and "equity_kodex200_bh" in benchmark_curve.columns:
            benchmark_curve["date"] = pd.to_datetime(benchmark_curve["date"])
            result = pd.merge(result, benchmark_curve[["date", "equity_kodex200_bh"]], on="date", how="outer")

    result = result.sort_values("date")
    for col in ["equity_liquidate", "equity_hold", "market_value_liquidate", "market_value_hold"]:
        if col in result.columns:
            result[col] = result[col].ffill().fillna(INITIAL_CASH if col.startswith("equity") else 0)
    if "equity_kodex200_bh" in result.columns:
        result["equity_kodex200_bh"] = result["equity_kodex200_bh"].ffill().fillna(INITIAL_CASH)

    # 통계 출력
    stats_liquidate = calc_stats(result, "equity_liquidate")
    stats_hold = calc_stats(result, "equity_hold")

    print("\n=== Risk-Off 행동 비교 결과 ===")
    print(f"백테스트 기간: {common_dates[0].date()} ~ {common_dates[-1].date()} ({len(common_dates)} 거래일)")
    print(f"{'전략':<20} {'최종자산':>12} {'수익률':>10} {'CAGR':>8} {'MDD':>8} {'샤프':>8} {'변동성':>8}")
    print("-" * 74)
    def _fmt_f(v): return f"{v:>8.2%}" if isinstance(v, float) else f"{v:>8}"
    def _fmt_d(v): return f"{v:>12,.0f}" if isinstance(v, float) else f"{v:>12}"
    print(f"{'Liquidate(매도)':<20} {_fmt_d(stats_liquidate['final'])} {_fmt_f(stats_liquidate['total_return'])} {_fmt_f(stats_liquidate['cagr'])} {_fmt_f(stats_liquidate['mdd'])} {stats_liquidate['sharpe']:>8.4f} {_fmt_f(stats_liquidate['volatility'])}")
    print(f"{'Hold(보유)':<20}     {_fmt_d(stats_hold['final'])} {_fmt_f(stats_hold['total_return'])} {_fmt_f(stats_hold['cagr'])} {_fmt_f(stats_hold['mdd'])} {stats_hold['sharpe']:>8.4f} {_fmt_f(stats_hold['volatility'])}")

    if ENABLE_BENCHMARK and "equity_kodex200_bh" in result.columns:
        stats_bh = calc_stats(result, "equity_kodex200_bh")
        print(f"{'KODEX200 BH':<20} {_fmt_d(stats_bh['final'])} {_fmt_f(stats_bh['total_return'])} {_fmt_f(stats_bh['cagr'])} {_fmt_f(stats_bh['mdd'])} {stats_bh['sharpe']:>8.4f} {_fmt_f(stats_bh['volatility'])}")

    print(f"\n매도 거래 수: Liquidate={len(trades_liquidate)}, Hold={len(trades_hold)}")

    # CSV 저장
    save_cols = ["date", "equity_liquidate", "equity_hold"]
    if "equity_kodex200_bh" in result.columns:
        save_cols.append("equity_kodex200_bh")
    result[save_cols].to_csv(OUTPUT_DIR / "risk_off_comparison.csv", index=False, encoding="utf-8-sig")
    trades_liquidate.to_csv(OUTPUT_DIR / "etf_trades_liquidate.csv", index=False, encoding="utf-8-sig")
    trades_hold.to_csv(OUTPUT_DIR / "etf_trades_hold.csv", index=False, encoding="utf-8-sig")
    print(f"\n저장 완료: {OUTPUT_DIR / 'risk_off_comparison.csv'}")
    print(f"저장 완료: {OUTPUT_DIR / 'etf_trades_liquidate.csv'}")
    print(f"저장 완료: {OUTPUT_DIR / 'etf_trades_hold.csv'}")


def main():
    # CLI 인자 파싱: 시작/종료일과 모드(옵션)를 받아 전역 변수로 설정
    args = _parse_cli_args()
    global START, END, RUN_MODE

    if args.start:
        try:
            START = _normalize_date_arg(args.start) or START
        except Exception as e:
            print(f"❌ 시작일 파싱 오류: {e}")
            exit(1)
    if args.end:
        try:
            END = _normalize_date_arg(args.end) or END
        except Exception as e:
            print(f"❌ 종료일 파싱 오류: {e}")
            exit(1)
    if args.mode:
        RUN_MODE = args.mode

    # 날짜 유효성 체크
    try:
        s_dt = datetime.strptime(START, "%Y%m%d")
        e_dt = datetime.strptime(END, "%Y%m%d")
        if s_dt > e_dt:
            print("❌ 오류: 시작일이 종료일보다 큽니다.")
            exit(1)
    except Exception as e:
        print(f"❌ 날짜 검증 실패: {e}")
        exit(1)

    print(f"백테스트 기간: {START} ~ {END} / 모드: {RUN_MODE}")

    from pykrx_utils import check_krx_auth_status, KRX_PASSWORD_CHANGE_URL
    krx_status = check_krx_auth_status()
    if krx_status == "password_change_needed":
        print()
        print("=" * 60)
        print("KRX 비밀번호 변경이 필요합니다.")
        print(f"{KRX_PASSWORD_CHANGE_URL} 에서 비밀번호를 변경한 후")
        print(".env 파일의 KRX_PW를 업데이트하고 다시 실행하세요.")
        print("=" * 60)
        print()
        exit(1)

    OUTPUT_DIR.mkdir(exist_ok=True)

    if RUN_MODE not in {"single", "experiment", "risk_off_compare"}:
        print(f"\n❌ 잘못된 ETF_BACKTEST_MODE 값: {RUN_MODE}")
        print("   허용값: single | experiment | risk_off_compare")
        exit(1)

    if RUN_MODE == "risk_off_compare":
        try:
            run_risk_off_compare_mode()
        except RuntimeError as e:
            print(f"\n❌ 실행 중 오류 발생:\n{str(e)}")
            exit(1)
        except Exception as e:
            print(f"\n❌ 예기치 않은 오류 발생: {str(e)}")
            import traceback
            traceback.print_exc()
            exit(1)
        return

    try:
        if RUN_MODE == "single":
            result, trades = run_single_mode()
        else:
            result, trades_dict = run_experiment_mode()
    except RuntimeError as e:
        print(f"\n❌ 실행 중 오류 발생:\n{str(e)}")
        exit(1)
    except Exception as e:
        print(f"\n❌ 예기치 않은 오류 발생: {str(e)}")
        import traceback
        traceback.print_exc()
        exit(1)
    
    if result.empty:
        raise RuntimeError("ETF-only backtest produced no result rows.")

    if RUN_MODE == "single":
        curve_to_save = result.copy()
        curve_to_save = curve_to_save.rename(columns={"equity": "equity_strategy", "equity_kodex200_bh": "equity_benchmark"})
        save_cols = [
            "date",
            "equity_strategy",
            "cash",
            "market_value",
            "holdings",
            "distribution_cash",
            "rebalance_decision",
            "rebalance_order_count",
            "exit_order_count",
        ]
        if "equity_benchmark" in curve_to_save.columns:
            save_cols.append("equity_benchmark")
        curve_to_save[save_cols].to_csv(OUTPUT_DIR / "etf_equity_curve.csv", index=False, encoding="utf-8-sig")
        trades.to_csv(OUTPUT_DIR / "etf_trades.csv", index=False, encoding="utf-8-sig")

        strategy_stats, benchmark_stats, period = summarize_single(result, trades)
        save_single_analytics(result, strategy_stats, benchmark_stats)
        payload = {
            "mode": "single",
            "slippage": BASE_SLIPPAGE,
            "period": _to_json_serializable(period),
            "strategy": _to_json_serializable(strategy_stats),
            "benchmark": _to_json_serializable(benchmark_stats) if benchmark_stats is not None else None,
            "config": _to_json_serializable(_build_performance_config()),
        }
        with (OUTPUT_DIR / "performance.json").open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        print(f"저장 완료: {OUTPUT_DIR / 'etf_equity_curve.csv'}")
        print(f"저장 완료: {OUTPUT_DIR / 'etf_trades.csv'}")
        print(f"저장 완료: {OUTPUT_DIR / 'performance.json'}")
        print(f"저장 완료: {OUTPUT_DIR / 'performance_comparison.csv'}")
        print(f"저장 완료: {OUTPUT_DIR / 'monthly_returns.csv'}")
        print(f"저장 완료: {OUTPUT_DIR / 'annual_returns.csv'}")
        print(f"저장 완료: {OUTPUT_DIR / 'rolling_metrics.csv'}")
    else:
        result.to_csv(OUTPUT_DIR / "etf_equity_curve.csv", index=False, encoding="utf-8-sig")
        # 필요 시 슬리피지별 체결 내역 저장
        for slip in SLIPPAGE_OPTIONS:
            label = f"slip_{int(slip*10000)}bp"
            trades_dict[label].to_csv(OUTPUT_DIR / f"etf_trades_{label}.csv", index=False, encoding="utf-8-sig")
        cols = ["date", "equity_kodex200_bh"] + [f"equity_slip_{int(s*10000)}bp" for s in SLIPPAGE_OPTIONS]
        result[cols].to_csv(OUTPUT_DIR / "slippage_comparison.csv", index=False, encoding="utf-8-sig")

        period, comparison_records, details = summarize_experiment(result, trades_dict)

        experiment_payload = {
            "mode": "experiment",
            "period": _to_json_serializable(period),
            "slippage_options": SLIPPAGE_OPTIONS,
            "slippage_comparison": [_to_json_serializable(row) for row in comparison_records],
            "details": [_to_json_serializable(row) for row in details],
            "config": _to_json_serializable(_build_performance_config()),
        }
        with (OUTPUT_DIR / "performance.json").open("w", encoding="utf-8") as f:
            json.dump(experiment_payload, f, ensure_ascii=False, indent=2)

        print(f"저장 완료: {OUTPUT_DIR / 'etf_equity_curve.csv'}")
        for slip in SLIPPAGE_OPTIONS:
            label = f"slip_{int(slip*10000)}bp"
            print(f"저장 완료: {OUTPUT_DIR / f'etf_trades_{label}.csv'}")
        print(f"저장 완료: {OUTPUT_DIR / 'slippage_comparison.csv'}")
        print(f"저장 완료: {OUTPUT_DIR / 'performance.json'}")


if __name__ == "__main__":
    main()
