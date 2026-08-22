import argparse
import hashlib
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from functools import wraps
from pathlib import Path
from typing import Any

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
from config_utils import parse_fraction_env, parse_pct_env
from etf_corporate_actions import (
    ApprovalBlocker,
    ApprovalReport,
    CorporateActionBlocked,
    CorporateActionLedger,
    EventType,
    HoldingState,
    LifecycleState,
    apply_lifecycle_event,
    create_distribution_receivable,
    final_approval_report,
    load_corporate_action_ledger,
    process_pending_receivables,
    process_settlement,
    transform_split_holding,
)
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


import etf_shared as _etf_shared
from etf_shared import (
    ETF_LIST,
    ETF_MAX_POSITIONS,
    ETF_SELL_RANK_BUFFER,
    ETF_TAXABLE_SELL_TAX_PCT,
    KOSPI_INDEX_CODE,
    MARKET_MA_DAYS,
    MARKET_SLOPE_DAYS,
    TAXABLE_ETF_TICKERS,
    add_deviation_flag,
    add_liquidity_flag,
    add_listing_flag,
    add_price_basis_columns,
    apply_buy_cost,
    apply_sell_value,
    build_gating_decision,
    build_rebalance_orders,
    ensure_universe_initialized,
    get_strategy_config,
    get_valuation_price,
    rank_etfs,
    update_last_valid_prices,
)
from pykrx_utils import (
    _call_capture_stderr,
    _range_has_weekday,
    fetch_etf_ohlcv_with_nav,
    get_listing_dates,
    get_ticker_name,
)

strategy_cfg = get_strategy_config(initialize_universe=False)
REBALANCE_STEP_DAYS = strategy_cfg["rebalance_step_days"]  # env override 반영 (기본 10)
SLIPPAGE_PCT = parse_pct_env("ETF_BASE_SLIPPAGE", strategy_cfg.get("default_slippage_pct", 0.0005))
SPREAD_PCT = parse_pct_env("ETF_SPREAD_PCT", strategy_cfg.get("spread_pct", 0.0005))
BASE_SLIPPAGE = SLIPPAGE_PCT

# Lazy-loaded in ``main`` after strict approval preflight.  Importing pykrx can
# authenticate immediately, which must not happen for a blocked approval run.
stock = None


def get_stock():
    """Return the pykrx stock client, importing it only for network work."""
    global stock
    if stock is None:
        from pykrx import stock as pykrx_stock

        stock = pykrx_stock
    return stock


def _restore_ticker_groups_on_exit(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        ticker_groups = kwargs.get("ticker_groups")
        original = _etf_shared.ETF_TICKER_GROUPS
        try:
            return func(*args, **kwargs)
        finally:
            if ticker_groups is not None:
                _etf_shared.ETF_TICKER_GROUPS = original

    return wrapped

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
    parser.add_argument(
        "--approval-strict",
        action="store_true",
        help="검증된 corporate-action ledger를 사용하는 승인 전용 실행 경로",
    )
    parser.add_argument(
        "--corporate-actions-ledger",
        default="data/etf_corporate_actions.csv",
        help="strict 모드 corporate-action ledger CSV 경로",
    )
    parser.add_argument(
        "--corporate-actions-manifest",
        default="data/etf_corporate_actions_manifest.json",
        help="strict 모드 corporate-action manifest 경로",
    )
    parser.add_argument(
        "--approval-output-dir",
        default="outputs_approval",
        help="strict 모드 승인 산출물 경로",
    )
    parser.add_argument(
        "--execution-mode",
        choices=["legacy", "ohlcv_capacity"],
        default="legacy",
        help="실행 시나리오: legacy 또는 진단 전용 OHLCV capacity",
    )
    parser.add_argument(
        "--execution-participation-rate",
        type=float,
        default=0.05,
        help="OHLCV capacity 참여율 (기본 0.05)",
    )
    parser.add_argument(
        "--execution-aum",
        default="10000000,100000000,1000000000",
        help="capacity 시나리오별 초기 AUM (쉼표 구분)",
    )
    parser.add_argument(
        "--execution-output-dir",
        default="outputs_execution",
        help="capacity 진단 산출물 경로",
    )
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
                        raw_left = _call_capture_stderr(get_stock().get_index_ohlcv_by_date, fetch_start, fetch_end, KOSPI_INDEX_CODE)
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
                        raw_right = _call_capture_stderr(get_stock().get_index_ohlcv_by_date, fetch_start, fetch_end, KOSPI_INDEX_CODE)
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
        idx_raw = _call_capture_stderr(get_stock().get_index_ohlcv_by_date, START, END, KOSPI_INDEX_CODE)
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


def ensure_no_current_prefix_columns(price_data: pd.DataFrame | None, *, context: str) -> None:
    """전략 입력에 PIT current_ 메타데이터가 섞이면 바로 실패한다."""
    if price_data is None or price_data.empty:
        return
    current_columns = [column for column in price_data.columns if str(column).startswith("current_")]
    if current_columns:
        raise ValueError(
            f"{context}에 PIT current_ 메타데이터가 포함되었습니다: {sorted(current_columns)}"
        )


def load_etf_price(ticker_universe: Sequence[str] | None = None) -> pd.DataFrame:
    """주어진 ETF 유니버스의 가격 데이터를 로드하고 전처리한다.

    분배금 파일이 있으면 분배금 컬럼을 병합하고, ``ETF_RETURN_BASIS``에 따라
    ``close_adj``(랭킹 기준 수익률용), ``ret_60``, ``ret_120`` 등을 계산한다.
    """
    if ticker_universe is None:
        ensure_universe_initialized()
    frames = []
    failed = []
    empty = []

    universe = [str(ticker) for ticker in (ticker_universe if ticker_universe is not None else ETF_LIST)]
    listing_dates = get_listing_dates(ticker_subset=set(universe))

    total = len(universe)
    for idx, ticker in enumerate(universe, start=1):
        print(f"[데이터] {idx}/{total} 조회: {ticker}")
        try:
            df = get_price(ticker, listing_dates=listing_dates)
            if df is None or df.empty:
                empty.append(ticker)
                print(f"[데이터] {ticker} 비어있음")
                continue
            frames.append(df)
        except Exception as exc:  # noqa: BLE001 - isolate one ticker in legacy runs
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



@_restore_ticker_groups_on_exit
def run_etf_strategy(
    initial_cash: float,
    common_dates: list[pd.Timestamp],
    index_df: pd.DataFrame,
    use_market_filter: bool = True,
    max_positions: int = ETF_MAX_POSITIONS,
    slippage: float = SLIPPAGE_PCT,
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
    universe_tickers: Sequence[str] | None = None,
    *,
    rebalance_observer: Callable[[dict], None] | None = None,
    initial_state: dict[str, Any] | None = None,
    return_final_state: bool = False,
    ticker_groups: dict[str, str] | None = None,
    approval_strict: bool = False,
    corporate_action_ledger: CorporateActionLedger | None = None,
    execution_mode: str = "legacy",
    participation_rate: float = 0.05,
):
    """ETF 로테이션 전략을 백테스트한다.

    리밸런싱 시점에서 랭킹 상위 종목을 매수하고, 분배락일에는 보유 수량에 한하여
    현금분배금을 계산해 자산에 반영한다. 같은 날 신규 매수분에는 분배금이 귀속되지
    않는다.

    Args:
        rebalance_observer: 선택적 콜백. 각 리밸런싱 전후 상태(의사결정, 주문, 체결)
           를 담은 dict를 전달한다. ``None``이면 기존 동작과 동일하다.
        approval_strict: 검증된 corporate-action ledger를 사용하는 승인 전용 경로.
           기본값 ``False``이며 legacy 실행에는 ledger를 로드하지 않는다.
        corporate_action_ledger: ``approval_strict`` 실행에 사용할 검증된 ledger.
        execution_mode: ``legacy`` or opt-in ``ohlcv_capacity`` scenario.
        participation_rate: OHLCV capacity participation fraction.
    """
    if execution_mode not in {"legacy", "ohlcv_capacity"}:
        raise ValueError("execution_mode must be exactly legacy or ohlcv_capacity")
    if isinstance(participation_rate, bool):
        raise TypeError("participation_rate must be a finite number")
    try:
        participation_rate = float(participation_rate)
    except (TypeError, ValueError) as exc:
        raise ValueError("participation_rate must be a finite number between 0 and 1") from exc
    if not np.isfinite(participation_rate) or not 0 <= participation_rate <= 1:
        raise ValueError("participation_rate must be a finite number between 0 and 1")
    execution_decide = None
    if execution_mode == "ohlcv_capacity":
        from etf_execution import DecisionType, OHLCVBar, OrderRequest, decide_execution

        execution_decide = decide_execution
    if approval_strict and corporate_action_ledger is None:
        raise ValueError("approval_strict 실행에는 corporate_action_ledger가 필요합니다.")
    if approval_strict and corporate_action_ledger is not None:
        ledger_report = corporate_action_ledger.approval_report()
        if not ledger_report.approval_valid:
            raise CorporateActionBlocked(ledger_report.blockers[0])
    ensure_no_current_prefix_columns(price_data, context="run_etf_strategy(price_data)")
    if universe_tickers is None:
        ensure_universe_initialized()
    universe = [str(ticker) for ticker in (universe_tickers if universe_tickers is not None else ETF_LIST)]
    if approval_strict and corporate_action_ledger is not None:
        coverage_blockers = _strict_coverage_blockers(
            corporate_action_ledger,
            common_dates,
            universe,
        )
        if coverage_blockers:
            raise CorporateActionBlocked(coverage_blockers[0])
    price = price_data.copy() if price_data is not None else load_etf_price(universe)
    price_by_date = {dt: day.set_index("ticker") for dt, day in price.groupby("date")}

    original_ticker_groups = None
    if ticker_groups is not None:
        original_ticker_groups = _etf_shared.ETF_TICKER_GROUPS
        _etf_shared.ETF_TICKER_GROUPS = dict(ticker_groups)

    cash = float(initial_cash)
    holdings = {}
    holding_cost_basis = {}
    holding_peak_closes: dict[str, float] = {}
    portfolio_peak_equity: float | None = None
    last_valid_closes: dict[str, float] = {}
    rebalance_phase_offset = 0
    exit_phase_offset = 0
    if initial_state:
        cash = float(initial_state.get("cash", cash))
        holdings = {str(t): int(q) for t, q in dict(initial_state.get("holdings", {})).items()}
        holding_cost_basis = {
            str(t): float(b)
            for t, b in dict(initial_state.get("holding_cost_basis", {})).items()
        }
        holding_peak_closes = {
            str(t): float(p)
            for t, p in dict(initial_state.get("holding_peak_closes", {})).items()
        }
        peak = initial_state.get("portfolio_peak_equity")
        portfolio_peak_equity = float(peak) if peak is not None else None
        last_valid_closes = {
            str(t): float(p)
            for t, p in dict(initial_state.get("last_valid_closes", {})).items()
        }
        rebalance_phase_offset = int(initial_state.get("rebalance_phase_offset", 0))
        exit_phase_offset = int(initial_state.get("exit_phase_offset", rebalance_phase_offset))
    strict_states: dict[str, HoldingState] = {}
    strict_state_event_ids: dict[str, str] = {}
    pending_receivables = []
    processed_action_ids: set[str] = set()
    strict_action_blockers: list[ApprovalBlocker] = []
    blocked_orders: list[dict[str, Any]] = []
    execution_diagnostics: list[dict[str, Any]] = []
    execution_diagnostic_index: dict[str, dict[str, Any]] = {}
    pending_execution_carries: dict[tuple[str, str, str], dict[str, Any]] = {}
    execution_excluded_buy_tickers: set[str] = set()
    historical_holdings: dict[pd.Timestamp, dict[str, int]] = {}
    if approval_strict:
        strict_states = {
            ticker: HoldingState(
                ticker,
                int(holdings.get(ticker, 0)),
                Decimal(str(holding_cost_basis.get(ticker, 0.0))) * int(holdings.get(ticker, 0)),
            )
            for ticker in universe
        }
        if common_dates:
            historical_holdings[pd.Timestamp(common_dates[0])] = dict(holdings)
    trades = []
    equity_rows = []

    ticker_names: dict[str, str] = {t: get_ticker_name(t) for t in universe}
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

    def _make_execution_order_id(
        *,
        origin_date: str,
        execution_date: pd.Timestamp,
        ticker: str,
        side: str,
        carry_age: int,
        reason: str,
    ) -> str:
        return "|".join(
            (
                str(origin_date),
                pd.Timestamp(execution_date).strftime("%Y-%m-%d"),
                str(ticker),
                str(side),
                str(carry_age),
                str(reason),
            )
        )

    def execute_exit(
        ticker: str,
        qty: int,
        next_open: pd.Series,
        next_dt: pd.Timestamp,
        reason: str,
        due_date: pd.Timestamp | None = None,
    ) -> bool:
        nonlocal cash

        if execution_mode == "ohlcv_capacity":
            execution_excluded_buy_tickers.add(ticker)
            # An exit intent supersedes an outstanding opposite-side BUY
            # remainder; otherwise the later carry could rebuy the position.
            _cancel_pending_execution_carries(
                ticker,
                side="BUY",
                cancellation_date=next_dt,
                reason="pending BUY carry cancelled by exit intent",
            )
            if any(
                pending["ticker"] == ticker and pending["side"] == "SELL"
                for pending in pending_execution_carries.values()
            ):
                # The due SELL carry is the existing exit intent; do not
                # create a duplicate same-side exit before it is retried.
                return 0
            open_price = safe_get(next_open, ticker)
            reference_price = (
                open_price * (1 - SPREAD_PCT / 2) if open_price is not None else None
            )
            order = {
                "ticker": ticker,
                "side": "SELL",
                "qty": int(qty),
                "reference_price": reference_price,
                "reason": reason,
                "execution_order_id": _make_execution_order_id(
                    origin_date=pd.Timestamp(next_dt).strftime("%Y-%m-%d"),
                    execution_date=next_dt,
                    ticker=ticker,
                    side="SELL",
                    carry_age=0,
                    reason=reason,
                ),
            }
            decision = _evaluate_execution_order(
                order,
                origin_date=pd.Timestamp(next_dt).strftime("%Y-%m-%d"),
                carry_age=0,
                execution_date=next_dt,
                execution_day=next_day,
            )
            _store_execution_carry(
                order,
                decision,
                origin_date=pd.Timestamp(next_dt).strftime("%Y-%m-%d"),
                due_date=due_date,
            )
            if decision.filled_qty <= 0:
                return 0
            return _apply_filled_order(
                _reprice_execution_order(order, decision.filled_qty),
                next_dt,
            )

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

    def _strict_holding_state(ticker: str) -> HoldingState:
        state = strict_states.get(ticker)
        if state is not None:
            return state
        qty = int(holdings.get(ticker, 0) or 0)
        state = HoldingState(
            ticker,
            qty,
            Decimal(str(holding_cost_basis.get(ticker, 0.0))) * qty,
        )
        strict_states[ticker] = state
        return state

    def _strict_sync_state(ticker: str, state: HoldingState | None = None) -> None:
        if state is None:
            state = _strict_holding_state(ticker)
        qty = int(holdings.get(ticker, 0) or 0)
        total_cost = Decimal(str(holding_cost_basis.get(ticker, 0.0))) * qty
        strict_states[ticker] = replace(state, quantity=qty, total_cost_basis=total_cost)

    def _record_blocked_order(
        *,
        order_date: pd.Timestamp,
        ticker: str,
        side: str,
        intent: str,
        reason: str,
    ) -> None:
        """Record a strict order rejected by the corporate-action lifecycle."""
        if not approval_strict:
            return
        state = _strict_holding_state(ticker)
        blocked_orders.append(
            {
                "date": order_date,
                "ticker": ticker,
                "side": side,
                "intent": intent,
                "lifecycle_state": state.lifecycle.value,
                "event_id": strict_state_event_ids.get(ticker),
                "reason": reason,
            }
        )

    def _execution_bar(ticker: str, execution_date: pd.Timestamp, day: pd.DataFrame):
        if execution_mode != "ohlcv_capacity":
            return None
        row = day.loc[ticker] if ticker in day.index else None

        def value(name: str, fallback: Any = None) -> Any:
            if row is None:
                return fallback
            raw = row.get(name, fallback)
            if raw is None or pd.isna(raw):
                return fallback
            return float(raw)

        close = value("close", 1.0)
        return OHLCVBar(
            pd.Timestamp(execution_date).strftime("%Y-%m-%d"),
            ticker,
            value("open"),
            value("high", close),
            value("low", close),
            close,
            value("volume", 0.0),
            value("trading_value"),
        )

    def _record_execution_diagnostic(
        decision: Any,
        *,
        origin_date: str,
        order_reason: str,
        execution_order_id: str,
    ) -> None:
        if execution_mode != "ohlcv_capacity":
            return
        diagnostic = decision.to_dict()
        diagnostic.update(
            {
                "origin_date": origin_date,
                "order_reason": order_reason,
                "execution_order_id": execution_order_id,
                "terminal_applied": True,
                "execution_mode": "ohlcv_capacity",
            }
        )
        prior = execution_diagnostic_index.get(execution_order_id)
        if prior is None:
            execution_diagnostics.append(diagnostic)
            execution_diagnostic_index[execution_order_id] = diagnostic
        else:
            prior.update(diagnostic)

    def _evaluate_execution_order(
        order: dict,
        *,
        origin_date: str,
        carry_age: int,
        execution_date: pd.Timestamp,
        execution_day: pd.DataFrame,
    ) -> Any:
        assert execution_decide is not None
        ticker = str(order.get("ticker"))
        side = str(order.get("side"))
        execution_order_id = str(
            order.get("execution_order_id")
            or _make_execution_order_id(
                origin_date=origin_date,
                execution_date=execution_date,
                ticker=ticker,
                side=side,
                carry_age=carry_age,
                reason=str(order.get("reason", "ETF_REBALANCE")),
            )
        )
        order["execution_order_id"] = execution_order_id
        explicit_suspension = False
        if approval_strict:
            explicit_suspension = _strict_holding_state(ticker).lifecycle != LifecycleState.ACTIVE
        request = OrderRequest(
            pd.Timestamp(execution_date).strftime("%Y-%m-%d"),
            ticker,
            side,
            int(order.get("qty", 0) or 0),
            participation_rate,
            carry_age,
            1,
            explicit_suspension,
        )
        decision = execution_decide(request, _execution_bar(ticker, execution_date, execution_day))
        _record_execution_diagnostic(
            decision,
            origin_date=origin_date,
            order_reason=str(order.get("reason", "ETF_REBALANCE")),
            execution_order_id=execution_order_id,
        )
        return decision

    def _record_no_due_carry(order: dict, decision: Any, *, reason: str) -> None:
        execution_order_id = str(order.get("execution_order_id", ""))
        diagnostic = execution_diagnostic_index.get(execution_order_id)
        if diagnostic is None:
            diagnostic = decision.to_dict()
            execution_diagnostics.append(diagnostic)
            execution_diagnostic_index[execution_order_id] = diagnostic
        diagnostic.update(
            {
                "decision": "CARRY_CANCELLED",
                "reason": reason,
                "diagnostic_labels": [
                    *diagnostic.get("diagnostic_labels", []),
                    "CARRY_CANCELLED_NO_DUE_DATE",
                ],
                "next_carry": None,
                "order_reason": str(order.get("reason", "ETF_REBALANCE")),
                "execution_order_id": execution_order_id,
                "execution_mode": "ohlcv_capacity",
            }
        )

    def _store_execution_carry(
        order: dict,
        decision: Any,
        *,
        origin_date: str,
        due_date: pd.Timestamp | None,
    ) -> None:
        if decision.decision is not DecisionType.PARTIAL_CARRY:
            return
        if due_date is None:
            _record_no_due_carry(
                order,
                decision,
                reason="partial remainder has no following trading date",
            )
            return
        due_key = pd.Timestamp(due_date).strftime("%Y-%m-%d")
        carry_key = (due_key, str(order.get("ticker")), str(order.get("side")))
        pending_execution_carries[carry_key] = {
            "origin_date": origin_date,
            "due_date": due_key,
            "ticker": str(order.get("ticker")),
            "side": str(order.get("side")),
            "remaining_qty": decision.remaining_qty,
            "order_reason": str(order.get("reason", "ETF_REBALANCE")),
            "execution_order_id": str(order.get("execution_order_id", "")),
        }

    def _record_cash_limited_carry(
        order: dict,
        decision: Any,
        *,
        affordable_qty: int,
        reason: str,
    ) -> None:
        execution_order_id = str(order.get("execution_order_id", ""))
        diagnostic = execution_diagnostic_index.get(execution_order_id)
        if diagnostic is None:
            diagnostic = decision.to_dict()
            execution_diagnostics.append(diagnostic)
            execution_diagnostic_index[execution_order_id] = diagnostic
        diagnostic.update(
            {
                "decision": "CARRY_CANCELLED",
                "filled_qty": affordable_qty,
                "remaining_qty": max(decision.requested_qty - affordable_qty, 0),
                "reason": reason,
                "diagnostic_labels": [
                    *diagnostic.get("diagnostic_labels", []),
                    "CASH_LIMITED_CARRY_CANCEL",
                ],
                "next_carry": None,
                "order_reason": str(order.get("reason", "ETF_EXECUTION_CARRY")),
                "execution_order_id": execution_order_id,
                "execution_mode": "ohlcv_capacity",
            }
        )

    def _cancel_pending_execution_carries(
        ticker: str,
        *,
        side: str | None = None,
        cancellation_date: pd.Timestamp,
        reason: str,
    ) -> None:
        if execution_mode != "ohlcv_capacity":
            return
        for key, pending in list(pending_execution_carries.items()):
            if pending["ticker"] != ticker or (side is not None and pending["side"] != side):
                continue
            pending_execution_carries.pop(key, None)
            cancellation_id = (
                f"{pending['execution_order_id']}|cancel|"
                f"{pd.Timestamp(cancellation_date).strftime('%Y-%m-%d')}"
            )
            execution_diagnostics.append(
                {
                    "date": pd.Timestamp(cancellation_date).strftime("%Y-%m-%d"),
                    "ticker": ticker,
                    "side": pending["side"],
                    "decision": "CARRY_CANCELLED",
                    "requested_qty": pending["remaining_qty"],
                    "filled_qty": 0,
                    "remaining_qty": pending["remaining_qty"],
                    "capacity_qty": 0,
                    "bar_volume": None,
                    "bar_value": None,
                    "close_volume_notional_estimate": None,
                    "participation_rate": participation_rate,
                    "carry_age": 1,
                    "max_carry_days": 1,
                    "reason": reason,
                    "diagnostic_labels": [
                        "OHLCV_CAPACITY_SCENARIO",
                        "CARRY_CANCELLED_BY_LIFECYCLE",
                    ],
                    "next_carry": None,
                    "origin_date": pending["origin_date"],
                    "due_date": pending["due_date"],
                    "order_reason": pending["order_reason"],
                    "execution_order_id": cancellation_id,
                    "terminal_applied": False,
                    "execution_mode": "ohlcv_capacity",
                }
            )

    def _reprice_execution_order(order: dict, filled_qty: int) -> dict:
        adjusted = dict(order)
        ticker = str(order.get("ticker"))
        reference_price = float(order.get("reference_price") or 0.0)
        adjusted["qty"] = int(filled_qty)
        if order.get("side") == "BUY":
            cost = int(filled_qty) * apply_buy_cost(reference_price, slippage)
            adjusted["estimated_value"] = float(cost)
            adjusted["estimated_tax"] = 0.0
            return adjusted
        cost_basis = holding_cost_basis.get(ticker)
        tax_rate = ETF_TAXABLE_SELL_TAX_PCT if ticker in TAXABLE_ETF_TICKERS else 0.0
        estimated_value = apply_sell_value(
            reference_price,
            int(filled_qty),
            tax_rate,
            slippage,
            cost_basis_per_share=cost_basis,
        )
        sell_price_adj = reference_price * (1 - slippage)
        taxable_gain = (
            max(0.0, int(filled_qty) * (sell_price_adj - cost_basis))
            if cost_basis is not None
            else 0.0
        )
        adjusted["estimated_value"] = float(estimated_value)
        adjusted["estimated_tax"] = float(taxable_gain * tax_rate)
        return adjusted

    def _apply_filled_order(order: dict, execution_date: pd.Timestamp) -> int:
        """Apply one already capacity-approved fill to portfolio state."""
        nonlocal cash
        ticker = str(order.get("ticker"))
        qty = int(order.get("qty", 0) or 0)
        side = str(order.get("side"))
        if qty <= 0:
            return 0
        if side == "SELL":
            qty = min(qty, int(holdings.get(ticker, 0) or 0))
            if qty <= 0:
                return 0
            remaining_qty = max(int(holdings.get(ticker, 0) or 0) - qty, 0)
            if remaining_qty > 0:
                holdings[ticker] = remaining_qty
            else:
                holdings.pop(ticker, None)
                holding_cost_basis.pop(ticker, None)
                holding_peak_closes.pop(ticker, None)
            cash += float(order.get("estimated_value", 0.0))
            trades.append(
                {
                    "date": execution_date,
                    "ticker": ticker,
                    "name": get_ticker_name(ticker),
                    "side": "SELL",
                    "reason": order.get("reason", "ETF_REBALANCE"),
                    "qty": qty,
                    "price": order.get("reference_price"),
                    "net_value": float(order.get("estimated_value", 0.0)),
                    "cash_flow": float(order.get("estimated_value", 0.0)),
                    "estimated_tax": float(order.get("estimated_tax", 0.0)),
                    "cash_after": cash,
                }
            )
        else:
            cost = float(order.get("estimated_value", 0.0))
            previous_qty = int(holdings.get(ticker, 0) or 0)
            holdings[ticker] = previous_qty + qty
            fill_unit_cost = cost / qty
            if ticker in holding_cost_basis and previous_qty > 0:
                previous_total_cost = float(holding_cost_basis[ticker]) * previous_qty
                holding_cost_basis[ticker] = (previous_total_cost + cost) / holdings[ticker]
            else:
                holding_cost_basis[ticker] = fill_unit_cost
            cash = max(0.0, cash - cost)
            trades.append(
                {
                    "date": execution_date,
                    "ticker": ticker,
                    "name": get_ticker_name(ticker),
                    "side": "BUY",
                    "reason": order.get("reason", "ETF_REBALANCE"),
                    "qty": qty,
                    "price": order.get("reference_price"),
                    "net_value": cost,
                    "cash_flow": -cost,
                    "estimated_tax": 0.0,
                    "cash_after": cash,
                }
            )
        if execution_mode == "ohlcv_capacity":
            trades[-1]["execution_order_id"] = order.get("execution_order_id")
        if approval_strict:
            _strict_sync_state(ticker)
        return qty

    def _process_due_execution_carries(
        execution_date: pd.Timestamp,
        execution_day: pd.DataFrame,
    ) -> None:
        if execution_mode != "ohlcv_capacity":
            return
        execution_key = pd.Timestamp(execution_date).strftime("%Y-%m-%d")
        for key, pending in list(pending_execution_carries.items()):
            if pending["due_date"] > execution_key:
                continue
            pending_execution_carries.pop(key, None)
            order = {
                "ticker": pending["ticker"],
                "side": pending["side"],
                "qty": pending["remaining_qty"],
                "reference_price": None,
                "reason": "ETF_EXECUTION_CARRY",
                "execution_order_id": _make_execution_order_id(
                    origin_date=pending["origin_date"],
                    execution_date=execution_date,
                    ticker=pending["ticker"],
                    side=pending["side"],
                    carry_age=1,
                    reason="ETF_EXECUTION_CARRY",
                ),
            }
            open_price = safe_get(execution_day.get("open"), pending["ticker"])
            if open_price is not None:
                spread = SPREAD_PCT / 2
                order["reference_price"] = open_price * (1 + spread if pending["side"] == "BUY" else 1 - spread)
            decision = _evaluate_execution_order(
                order,
                origin_date=pending["origin_date"],
                carry_age=1,
                execution_date=execution_date,
                execution_day=execution_day,
            )
            execution_excluded_buy_tickers.add(pending["ticker"])
            if decision.filled_qty > 0:
                fill_qty = decision.filled_qty
                if pending["side"] == "BUY":
                    unit_cost = apply_buy_cost(float(order["reference_price"]), slippage)
                    affordable_qty = (
                        min(fill_qty, max(0, int(cash // unit_cost))) if unit_cost > 0 else 0
                    )
                    if affordable_qty < fill_qty:
                        _record_cash_limited_carry(
                            order,
                            decision,
                            affordable_qty=affordable_qty,
                            reason="repriced carry BUY remainder cancelled by current cash",
                        )
                    fill_qty = affordable_qty
                if fill_qty <= 0:
                    continue
                _apply_filled_order(
                    _reprice_execution_order(order, fill_qty),
                    execution_date,
                )

    def _strict_unpaid_receivables() -> float:
        return float(sum(receivable.amount for receivable in pending_receivables if not receivable.paid))

    def _strict_process_actions(as_of: pd.Timestamp) -> float:
        """Apply ledger events and pay receivables before today's decisions."""
        nonlocal cash
        if not approval_strict or corporate_action_ledger is None:
            return 0.0

        payment_date = as_of.date()
        pending_receivables_updated, payment_cash = process_pending_receivables(
            pending_receivables, payment_date
        )
        pending_receivables[:] = pending_receivables_updated
        cash += float(payment_cash)

        actions = sorted(
            corporate_action_ledger.events,
            key=lambda action: (
                (
                    action.settlement_date
                    if action.event_type in {EventType.CASH_SETTLEMENT, EventType.REDEMPTION}
                    else max(
                        value
                        for value in (action.event_date, action.record_date, action.ex_date)
                        if value is not None
                    )
                ),
                action.event_id,
            ),
        )
        for action in actions:
            if action.event_id in processed_action_ids:
                continue
            effective_date = (
                action.settlement_date
                if action.event_type in {EventType.CASH_SETTLEMENT, EventType.REDEMPTION}
                else max(
                    value
                    for value in (action.event_date, action.record_date, action.ex_date)
                    if value is not None
                )
            )
            if effective_date is None or effective_date > payment_date:
                continue
            ticker = action.ticker
            if action.event_type in {
                EventType.CASH_SETTLEMENT,
                EventType.REDEMPTION,
                EventType.DELISTING,
                EventType.SUSPENSION_START,
            }:
                _cancel_pending_execution_carries(
                    ticker,
                    cancellation_date=as_of,
                    reason="pending carry cancelled by settlement or delisting",
                )
            state = _strict_holding_state(ticker)
            try:
                if action.event_type == EventType.CASH_DISTRIBUTION:
                    def snapshot_on_or_after(target_date: date | None) -> dict[str, int] | None:
                        if target_date is None:
                            return dict(holdings)
                        candidates = sorted(
                            key
                            for key in historical_holdings
                            if pd.Timestamp(target_date) <= key <= as_of
                        )
                        return historical_holdings[candidates[0]] if candidates else None

                    record_holdings = snapshot_on_or_after(action.record_date)
                    ex_holdings = snapshot_on_or_after(action.ex_date)
                    if record_holdings is None or ex_holdings is None:
                        raise CorporateActionBlocked(
                            ApprovalBlocker(
                                "MISSING_ENTITLEMENT_SNAPSHOT",
                                "strict distribution entitlement lacks an as-of holding snapshot",
                                event_id=action.event_id,
                                ticker=action.ticker,
                                event_date=action.event_date,
                            )
                        )
                    entitlement_quantity = int(ex_holdings.get(ticker, 0) or 0)
                    receivable = (
                        create_distribution_receivable(
                            action,
                            entitlement_quantity,
                            held_on_record_date=int(record_holdings.get(ticker, 0) or 0) > 0,
                            held_on_ex_date=entitlement_quantity > 0,
                        )
                        if entitlement_quantity > 0
                        else None
                    )
                    if receivable is not None:
                        pending_receivables.append(receivable)
                elif action.event_type in {EventType.SPLIT, EventType.REVERSE_SPLIT}:
                    transformed = transform_split_holding(state, action)
                    if transformed.quantity > 0:
                        holdings[ticker] = transformed.quantity
                    else:
                        holdings.pop(ticker, None)
                    if transformed.quantity > 0:
                        holding_cost_basis[ticker] = float(
                            transformed.total_cost_basis / transformed.quantity
                        )
                    _strict_sync_state(ticker, transformed)
                    strict_state_event_ids[ticker] = action.event_id
                elif action.event_type in {
                    EventType.SUSPENSION_START,
                    EventType.SUSPENSION_END,
                    EventType.DELISTING,
                }:
                    _strict_sync_state(ticker, apply_lifecycle_event(state, action))
                    strict_state_event_ids[ticker] = action.event_id
                elif action.event_type in {EventType.CASH_SETTLEMENT, EventType.REDEMPTION}:
                    settlement = process_settlement(state, action, payment_date)
                    strict_states[ticker] = settlement.holding
                    if settlement.cash_paid:
                        cash += float(settlement.cash_paid)
                    holdings.pop(ticker, None)
                    holding_cost_basis.pop(ticker, None)
                    holding_peak_closes.pop(ticker, None)
                    strict_state_event_ids[ticker] = action.event_id
                processed_action_ids.add(action.event_id)
            except CorporateActionBlocked as exc:
                strict_action_blockers.append(exc.blocker)
                processed_action_ids.add(action.event_id)
        pending_receivables_updated, same_day_payment_cash = process_pending_receivables(
            pending_receivables, payment_date
        )
        pending_receivables[:] = pending_receivables_updated
        cash += float(same_day_payment_cash)
        payment_cash += same_day_payment_cash
        return float(payment_cash)

    def _strict_refresh_existing_row(as_of: pd.Timestamp, distribution_cash: float) -> None:
        if not equity_rows or pd.Timestamp(equity_rows[-1]["date"]) != as_of:
            return
        day = price_by_date.get(as_of, pd.DataFrame())
        close_series = day.get("close") if not day.empty else None
        market_value = 0.0
        for ticker, qty in holdings.items():
            if _strict_holding_state(ticker).lifecycle != LifecycleState.ACTIVE:
                continue
            close_price = get_valuation_price(ticker, close_series, last_valid_closes)
            if close_price is not None:
                market_value += qty * close_price
        receivables = _strict_unpaid_receivables()
        equity_rows[-1].update(
            {
                "cash": cash,
                "market_value": market_value,
                "receivables": receivables,
                "equity": cash + market_value + receivables,
                "distribution_cash": float(equity_rows[-1].get("distribution_cash", 0.0))
                + distribution_cash,
            }
        )

    warmup_days = 0 if initial_state else max(120, MARKET_MA_DAYS + MARKET_SLOPE_DAYS)
    for i, dt in enumerate(common_dates[:-1]):
        strict_payment_cash = _strict_process_actions(pd.Timestamp(dt))
        if approval_strict:
            historical_holdings.setdefault(pd.Timestamp(dt), dict(holdings))
            if i < warmup_days:
                historical_holdings[pd.Timestamp(common_dates[i + 1])] = dict(holdings)
        if i < warmup_days:
            continue

        next_dt = common_dates[i + 1]
        carry_due_date = common_dates[i + 2] if i + 2 < len(common_dates) else None
        today = price_by_date.get(dt, pd.DataFrame())
        next_day = price_by_date.get(next_dt, pd.DataFrame())
        if today.empty or next_day.empty:
            if execution_mode == "ohlcv_capacity":
                for pending in list(pending_execution_carries.values()):
                    if pending["due_date"] <= pd.Timestamp(next_dt).strftime("%Y-%m-%d"):
                        _cancel_pending_execution_carries(
                            pending["ticker"],
                            side=pending["side"],
                            cancellation_date=next_dt,
                            reason="carry due date has an empty today or next-day row",
                        )
            continue
        execution_excluded_buy_tickers.clear()

        if approval_strict:
            historical_holdings[pd.Timestamp(next_dt)] = dict(holdings)
            update_last_valid_prices(last_valid_closes, today.get("close"))
            _strict_refresh_existing_row(pd.Timestamp(dt), strict_payment_cash)

        next_open = next_day["open"]
        next_close = next_day["close"]
        if approval_strict:
            entitled_holdings = {}
            distribution_cash = 0.0
        else:
            entitled_holdings = dict(holdings)
            distribution_cash = distribution_cash_for_holdings(
                entitled_holdings,
                next_day.get("distribution"),
                parse_pct_env("ETF_DISTRIBUTION_TAX_PCT", 0.0),
            )
        update_last_valid_prices(last_valid_closes, today.get("close"))
        periodic_rebalance = (
            i - warmup_days + rebalance_phase_offset
        ) % REBALANCE_STEP_DAYS == 0
        # A capacity remainder is eligible on the immediately following
        # trading date, even when that date is outside the normal rebalance
        # cadence.  Re-enter the existing order path so carry fills and
        # opposite-order cancellation share the same apply loop.
        should_rebalance = periodic_rebalance
        rebalance_order_count = 0
        exit_order_count = 0
        stopped_tickers: set[str] = set()
        portfolio_stop_triggered = False
        observer_event: dict | None = None

        current_market_value = 0.0
        for ticker, qty in holdings.items():
            if approval_strict and strict_states.get(ticker, _strict_holding_state(ticker)).lifecycle != LifecycleState.ACTIVE:
                continue
            close_price = get_valuation_price(ticker, today.get("close"), last_valid_closes)
            if close_price is not None:
                current_market_value += qty * close_price
        current_equity = cash + current_market_value
        if approval_strict:
            current_equity += _strict_unpaid_receivables()
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
            and (i - warmup_days + exit_phase_offset) % effective_exit_check_days == 0
        )
        should_stop_portfolio = (
            should_check_exit
            and effective_portfolio_trailing_stop_pct > 0
            and portfolio_peak_equity is not None
            and current_equity
            <= portfolio_peak_equity * (1 - effective_portfolio_trailing_stop_pct)
        )
        if should_stop_portfolio:
            if approval_strict:
                for ticker in holdings:
                    if _strict_holding_state(ticker).lifecycle != LifecycleState.ACTIVE:
                        _record_blocked_order(
                            order_date=next_dt,
                            ticker=ticker,
                            side="SELL",
                            intent="ETF_PORTFOLIO_TRAILING_STOP",
                            reason="portfolio trailing exit rejected because lifecycle is not ACTIVE",
                        )
            sellable = execution_mode == "ohlcv_capacity" or all(
                safe_get(next_open, ticker) is not None
                for ticker in holdings
                if ticker in holdings
                and (
                    not approval_strict
                    or _strict_holding_state(ticker).lifecycle == LifecycleState.ACTIVE
                )
            )
            if sellable:
                for ticker, qty in list(holdings.items()):
                    if approval_strict and _strict_holding_state(ticker).lifecycle != LifecycleState.ACTIVE:
                        continue
                    if execute_exit(
                        ticker,
                        int(qty),
                        next_open,
                        next_dt,
                        "ETF_PORTFOLIO_TRAILING_STOP",
                        carry_due_date,
                    ):
                        if ticker not in holdings:
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
                if approval_strict and _strict_holding_state(ticker).lifecycle != LifecycleState.ACTIVE:
                    _record_blocked_order(
                        order_date=next_dt,
                        ticker=ticker,
                        side="SELL",
                        intent="ETF_TRAILING_STOP",
                        reason="position trailing exit rejected because lifecycle is not ACTIVE",
                    )
                    continue

                if execute_exit(
                    ticker,
                    int(qty),
                    next_open,
                    next_dt,
                    "ETF_TRAILING_STOP",
                    carry_due_date,
                ):
                    if ticker not in holdings:
                        stopped_tickers.add(ticker)
                    exit_order_count += 1

        # Carry retries happen exactly on their stored due date and before a
        # periodic ranking pass. They do not themselves trigger a rebalance.
        _process_due_execution_carries(next_dt, next_day)

        if should_rebalance:
            # 시장 필터 + 랭킹으로 목표 종목 결정
            kospi_risk_on = is_risk_on(index_df, dt) if use_market_filter else True
            us_risk_on = (
                is_us_risk_on(us_index_df, dt) if (use_market_filter and enable_multi_index_risk) else True
            )
            ranked = rank_etfs(today.reset_index())
            ticker_list = [str(t) for t in ranked.get("ticker", [])]
            gating = build_gating_decision(
                ticker_list,
                holdings,
                kospi_risk_on=kospi_risk_on,
                us_risk_on=us_risk_on,
                enable_multi_index_risk=enable_multi_index_risk,
                gating_mode=MULTI_INDEX_GATING_MODE,
                liquidate_on_risk_off=risk_off_liquidate,
            )
            allowed_groups = gating.allowed_groups
            forced_exit_tickers = gating.forced_exit_tickers
            if not kospi_risk_on and not risk_off_liquidate:
                # KOSPI risk_off + hold: 기존 포지션 유지, 신규 매수 없음
                targets = []
            else:
                targets = gating.eligible_ranked[: max_positions + ETF_SELL_RANK_BUFFER]
            if portfolio_stop_triggered:
                targets = []
                forced_exit_tickers = set()
            if stopped_tickers:
                targets = [ticker for ticker in targets if ticker not in stopped_tickers]

            if approval_strict:
                blocked_tickers = {
                    ticker
                    for ticker, state in strict_states.items()
                    if state.lifecycle != LifecycleState.ACTIVE
                }
                for ticker in sorted(set(targets) & blocked_tickers):
                    _record_blocked_order(
                        order_date=next_dt,
                        ticker=ticker,
                        side="BUY",
                        intent="TARGET",
                        reason="target rejected because lifecycle is not ACTIVE",
                    )
                for ticker in sorted(set(forced_exit_tickers) & blocked_tickers):
                    _record_blocked_order(
                        order_date=next_dt,
                        ticker=ticker,
                        side="SELL",
                        intent="FORCED_EXIT",
                        reason="forced exit rejected because lifecycle is not ACTIVE",
                    )
                for ticker in sorted(
                    set(holdings) & blocked_tickers - set(targets) - set(forced_exit_tickers)
                ):
                    _record_blocked_order(
                        order_date=next_dt,
                        ticker=ticker,
                        side="SELL",
                        intent="REBALANCE_EXIT",
                        reason="holding exit rejected because lifecycle is not ACTIVE",
                    )
                targets = [ticker for ticker in targets if ticker not in blocked_tickers]
                forced_exit_tickers = {
                    ticker for ticker in forced_exit_tickers if ticker not in blocked_tickers
                }

            # split은 후보가 비어도 허용 그룹 보유분을 보호한다. hybrid와
            # 멀티 인덱스 비활성 모드는 기존 risk-off 전량매도 동작을 보존한다.
            selective_empty_protection = (
                enable_multi_index_risk
                and str(MULTI_INDEX_GATING_MODE).strip().lower() == "split"
            )
            allow_empty_target_sell = (
                (not kospi_risk_on)
                and risk_off_liquidate
                and not selective_empty_protection
            )
            empty_target_protected = (
                (not targets)
                and (not allow_empty_target_sell)
                and (not forced_exit_tickers)
            )

            pre_holdings_snapshot = dict(holdings)
            pre_cash = cash
            pre_market_value = 0.0
            for ticker, qty in pre_holdings_snapshot.items():
                close_price = get_valuation_price(ticker, today.get("close"), last_valid_closes)
                if close_price is not None:
                    pre_market_value += qty * close_price
            pre_equity = pre_cash + pre_market_value
            if approval_strict:
                pre_equity += _strict_unpaid_receivables()

            if rebalance_observer is not None:
                observer_event = {
                    "decision_date": dt,
                    "risk_on": kospi_risk_on,
                    "kospi_risk_on": kospi_risk_on,
                    "us_risk_on": us_risk_on,
                    "multi_index_enabled": enable_multi_index_risk,
                    "multi_index_gating_mode": MULTI_INDEX_GATING_MODE,
                    "allowed_groups": sorted(list(allowed_groups)),
                    "forced_exit_tickers": sorted(forced_exit_tickers),
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

            order_holdings = holdings
            order_cost_basis = holding_cost_basis
            if approval_strict:
                order_holdings = {
                    ticker: qty
                    for ticker, qty in holdings.items()
                    if strict_states.get(ticker, _strict_holding_state(ticker)).lifecycle
                    == LifecycleState.ACTIVE
                }
                order_cost_basis = {
                    ticker: holding_cost_basis[ticker]
                    for ticker in order_holdings
                    if ticker in holding_cost_basis
                }

            orders = build_rebalance_orders(
                current_holdings=order_holdings,
                target_tickers=targets,
                latest_prices=latest_prices,
                available_cash=cash,
                latest_buy_prices=latest_buy_prices,
                latest_sell_prices=latest_sell_prices,
                current_cost_basis=order_cost_basis,
                max_positions=max_positions,
                sell_rank_buffer=ETF_SELL_RANK_BUFFER,
                slippage=slippage,
                sell_tax_pct=ETF_TAXABLE_SELL_TAX_PCT,
                taxable_tickers=TAXABLE_ETF_TICKERS,
                allow_empty_target_sell=allow_empty_target_sell,
                forced_exit_tickers=forced_exit_tickers,
                generate_orders=True,
                max_asset_pct=effective_max_asset_pct,
                ticker_names=ticker_names,
                target_weight_rebalance=effective_target_weight_rebalance,
                rebalance_band_pct=effective_rebalance_band_pct,
                trim_overweight_positions=effective_trim_overweight_positions,
            )
            if execution_mode == "ohlcv_capacity":
                orders = [
                    order
                    for order in orders
                    if not (
                        order.get("side") == "BUY"
                        and str(order.get("ticker")) in execution_excluded_buy_tickers
                    )
                ]
            rebalance_order_count = len(orders)

            execution_orders = orders
            if execution_mode == "ohlcv_capacity":
                execution_orders = []
                for order in orders:
                    origin_date = pd.Timestamp(dt).strftime("%Y-%m-%d")
                    decision = _evaluate_execution_order(
                        order,
                        origin_date=origin_date,
                        carry_age=0,
                        execution_date=next_dt,
                        execution_day=next_day,
                    )
                    _store_execution_carry(
                        order,
                        decision,
                        origin_date=origin_date,
                        due_date=carry_due_date,
                    )
                    if decision.filled_qty > 0:
                        execution_orders.append(
                            _reprice_execution_order(order, decision.filled_qty)
                        )

            # 생성된 주문을 즉시 전량 체결로 모사 (백테스트 단순화)
            for o in execution_orders:
                _apply_filled_order(o, next_dt)

        if not approval_strict:
            cash += distribution_cash
        update_last_valid_prices(last_valid_closes, next_close)
        market_value = 0.0
        for ticker, qty in holdings.items():
            if approval_strict and strict_states.get(ticker, _strict_holding_state(ticker)).lifecycle != LifecycleState.ACTIVE:
                continue
            close_price = get_valuation_price(ticker, next_close, last_valid_closes)
            if close_price is not None:
                market_value += qty * close_price
                holding_peak_closes[ticker] = max(
                    holding_peak_closes.get(ticker, close_price),
                    close_price,
                )
        unpaid_receivables = _strict_unpaid_receivables() if approval_strict else 0.0
        post_equity = cash + market_value + unpaid_receivables
        if holdings:
            portfolio_peak_equity = max(portfolio_peak_equity or post_equity, post_equity)
        else:
            portfolio_peak_equity = None

        equity_row = {
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
        if approval_strict:
            equity_row["receivables"] = unpaid_receivables
        equity_rows.append(equity_row)

        if observer_event is not None:
            post_holdings_snapshot = dict(holdings)
            post_equity = cash + market_value + (
                _strict_unpaid_receivables() if approval_strict else 0.0
            )
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

    final_stale_tickers: set[str] = set()
    if approval_strict and common_dates:
        final_payment_cash = _strict_process_actions(pd.Timestamp(common_dates[-1]))
        if equity_rows:
            final_day = price_by_date.get(common_dates[-1], pd.DataFrame())
            final_close = final_day.get("close") if not final_day.empty else None
            final_market_value = 0.0
            for ticker, qty in holdings.items():
                if strict_states.get(ticker, _strict_holding_state(ticker)).lifecycle != LifecycleState.ACTIVE:
                    continue
                close_price = safe_get(final_close, ticker)
                if close_price is not None:
                    final_market_value += qty * close_price
            equity_rows[-1].update(
                {
                    "cash": cash,
                    "market_value": final_market_value,
                    "receivables": _strict_unpaid_receivables(),
                    "equity": cash + final_market_value + _strict_unpaid_receivables(),
                    "distribution_cash": float(equity_rows[-1]["distribution_cash"])
                    + final_payment_cash,
                }
            )
        final_day = price_by_date.get(common_dates[-1], pd.DataFrame())
        final_close = final_day.get("close") if not final_day.empty else None
        for ticker, state in strict_states.items():
            if state.quantity <= 0:
                continue
            if state.lifecycle != LifecycleState.ACTIVE:
                final_stale_tickers.add(ticker)
                continue
            if safe_get(final_close, ticker) is None:
                final_stale_tickers.add(ticker)
        final_report = final_approval_report(
            corporate_action_ledger,
            strict_states,
            stale_tickers=final_stale_tickers,
        )
        if strict_action_blockers:
            final_report = ApprovalReport(
                "BLOCKED",
                tuple(
                    sorted(
                        set(final_report.blockers).union(strict_action_blockers),
                        key=lambda blocker: (
                            blocker.code,
                            blocker.event_id or "",
                            blocker.ticker or "",
                            blocker.message,
                        ),
                    )
                ),
                final_report.event_count,
                final_report.ledger_sha256,
            )
    else:
        final_report = None

    if execution_mode == "ohlcv_capacity" and common_dates:
        for pending in list(pending_execution_carries.values()):
            _cancel_pending_execution_carries(
                pending["ticker"],
                cancellation_date=common_dates[-1],
                reason="pending carry cancelled because the run has no following trading date",
            )

    result = (pd.DataFrame(equity_rows), pd.DataFrame(trades))
    if original_ticker_groups is not None:
        _etf_shared.ETF_TICKER_GROUPS = original_ticker_groups
    if return_final_state or approval_strict or execution_mode == "ohlcv_capacity":
        final_equity = None
        if not result[0].empty and "equity" in result[0].columns:
            candidate_final_equity = float(result[0]["equity"].iloc[-1])
            if np.isfinite(candidate_final_equity) and candidate_final_equity > 0:
                final_equity = candidate_final_equity
        final_state = {
            "cash": cash,
            "holdings": holdings,
            "holding_cost_basis": holding_cost_basis,
            "holding_peak_closes": holding_peak_closes,
            "portfolio_peak_equity": portfolio_peak_equity,
            "last_valid_closes": last_valid_closes,
            "rebalance_phase_offset": rebalance_phase_offset,
            "exit_phase_offset": exit_phase_offset,
            "final_equity": final_equity,
        }
        if approval_strict:
            final_state.update(
                {
                    "corporate_action_states": strict_states,
                    "pending_receivables": tuple(pending_receivables),
                    "approval_report": final_report,
                    "approval_stale_tickers": sorted(final_stale_tickers),
                    "blocked_orders": blocked_orders,
                }
            )
        if execution_mode == "ohlcv_capacity":
            final_state.update(
                {
                    "execution_mode": "ohlcv_capacity",
                    "execution_diagnostics": execution_diagnostics,
                    "pending_execution_carries": tuple(pending_execution_carries.values()),
                }
            )
        return (*result, final_state)
    return result


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


def run_single_mode(
    *,
    approval_strict: bool = False,
    corporate_action_ledger: CorporateActionLedger | None = None,
    initial_cash: float = INITIAL_CASH,
    execution_mode: str = "legacy",
    execution_participation_rate: float = 0.05,
    return_final_state: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame] | tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    index_df = get_index_data()
    us_index_df = get_us_index_data() if ENABLE_MULTI_INDEX_RISK else None
    common_dates = list(index_df["date"])

    strategy_cfg = get_strategy_config()
    wants_final_state = approval_strict or return_final_state
    strategy_result = run_etf_strategy(
        initial_cash,
        common_dates,
        index_df,
        use_market_filter=USE_MARKET_FILTER,
        max_positions=ETF_MAX_POSITIONS,
        slippage=BASE_SLIPPAGE,
        risk_off_liquidate=strategy_cfg.get("liquidate_on_risk_off", True),
        us_index_df=us_index_df,
        enable_multi_index_risk=ENABLE_MULTI_INDEX_RISK,
        approval_strict=approval_strict,
        corporate_action_ledger=corporate_action_ledger,
        return_final_state=wants_final_state,
        execution_mode=execution_mode,
        participation_rate=execution_participation_rate,
    )
    if wants_final_state:
        result, trades, final_state = strategy_result
    else:
        result, trades = strategy_result
        final_state = None

    if ENABLE_BENCHMARK and not approval_strict and execution_mode == "legacy":
        try:
            benchmark_curve = run_kodex200_buy_and_hold(initial_cash, common_dates)
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
    result["equity"] = result["equity"].ffill().fillna(initial_cash)
    result["cash"] = result["cash"].ffill().fillna(initial_cash)
    result["market_value"] = result["market_value"].ffill().fillna(0)
    result["rebalance_decision"] = (
        result["rebalance_decision"].astype("boolean").fillna(False).astype(bool)
    )
    result["rebalance_order_count"] = (
        result["rebalance_order_count"].fillna(0).astype(int)
    )
    result["exit_order_count"] = result["exit_order_count"].fillna(0).astype(int)

    if ENABLE_BENCHMARK and not approval_strict and execution_mode == "legacy" and "equity_kodex200_bh" in result.columns:
        result["equity_kodex200_bh"] = result["equity_kodex200_bh"].ffill().fillna(initial_cash)

    if wants_final_state:
        return result, trades, final_state
    return result, trades


def _parse_execution_aums(raw: str | Sequence[int | float]) -> list[int]:
    values = raw.split(",") if isinstance(raw, str) else list(raw)
    aums: list[int] = []
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        try:
            amount = int(text)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid execution AUM: {text}") from exc
        if amount <= 0:
            raise ValueError("execution AUM values must be positive integers")
        aums.append(amount)
    if not aums:
        raise ValueError("at least one execution AUM is required")
    return aums


def _validate_execution_output_dir(output_dir: str | Path) -> Path:
    candidate = Path(output_dir).expanduser().resolve()
    forbidden = [
        OUTPUT_DIR.expanduser().resolve(),
        Path("outputs_approval").expanduser().resolve(),
    ]
    for protected in forbidden:
        if candidate == protected or candidate in protected.parents or protected in candidate.parents:
            raise ValueError(f"execution output directory overlaps protected path: {candidate}")
    return candidate


_EXECUTION_ARTIFACT_NAMES = frozenset(
    {
        "execution_summary.csv",
        "execution_diagnostics.csv",
        "execution_trades.csv",
        "execution_reconciliation.csv",
        "execution_metadata.json",
    }
)


def _prepare_execution_output_dir(output_dir: str | Path) -> Path:
    candidate = _validate_execution_output_dir(output_dir)
    if not candidate.exists():
        return candidate
    if not candidate.is_dir():
        raise ValueError(f"execution output path is not a directory: {candidate}")
    extras = [path.name for path in candidate.iterdir() if path.name not in _EXECUTION_ARTIFACT_NAMES]
    if extras:
        raise ValueError(
            "execution output directory contains unowned stale entries: "
            + ", ".join(sorted(extras))
        )
    for name in _EXECUTION_ARTIFACT_NAMES:
        path = candidate / name
        if path.exists() and not path.is_file():
            raise ValueError(f"execution artifact path is not a file: {path}")
    return candidate


def _validate_execution_cli_contract(
    *,
    execution_mode: str,
    approval_strict: bool,
    run_mode: str,
    raw_aums: str,
    raw_rate: float,
    output_dir: str | Path,
) -> tuple[list[int], float, Path] | None:
    if execution_mode != "ohlcv_capacity":
        return None
    if approval_strict:
        raise ValueError("execution capacity cannot be combined with approval-strict")
    if run_mode != "single":
        raise ValueError("execution capacity requires --mode single")
    aums = _parse_execution_aums(raw_aums)
    rate = float(raw_rate)
    if not np.isfinite(rate) or not 0 <= rate <= 1:
        raise ValueError("execution participation rate must be finite and between 0 and 1")
    return aums, rate, _prepare_execution_output_dir(output_dir)


def _execution_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _commit_execution_outputs(
    *,
    selected_dir: Path,
    summaries: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
    scenario_trades: list[dict[str, Any]],
    reconciliations: list[dict[str, Any]],
    metadata: dict[str, Any],
    summary_columns: list[str],
    diagnostic_columns: list[str],
    trade_columns: list[str],
    reconciliation_columns: list[str],
) -> None:
    selected_dir.parent.mkdir(parents=True, exist_ok=True)
    stage_parent = selected_dir.parent
    staging_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{selected_dir.name}.staging-",
            dir=str(stage_parent),
        )
    )
    backup_dir: Path | None = None
    try:
        pd.DataFrame(summaries, columns=summary_columns).to_csv(
            staging_dir / "execution_summary.csv", index=False, encoding="utf-8-sig"
        )
        pd.DataFrame(diagnostics, columns=diagnostic_columns).to_csv(
            staging_dir / "execution_diagnostics.csv", index=False, encoding="utf-8-sig"
        )
        pd.DataFrame(scenario_trades, columns=trade_columns).to_csv(
            staging_dir / "execution_trades.csv", index=False, encoding="utf-8-sig"
        )
        pd.DataFrame(reconciliations, columns=reconciliation_columns).to_csv(
            staging_dir / "execution_reconciliation.csv", index=False, encoding="utf-8-sig"
        )
        (staging_dir / "execution_metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )

        if selected_dir.exists():
            backup_dir = Path(
                tempfile.mkdtemp(prefix=f".{selected_dir.name}.backup-", dir=str(stage_parent))
            )
            shutil.rmtree(backup_dir)
            os.replace(selected_dir, backup_dir)
        try:
            os.replace(staging_dir, selected_dir)
        except BaseException:
            if backup_dir is not None and backup_dir.exists() and not selected_dir.exists():
                os.replace(backup_dir, selected_dir)
            raise
        if backup_dir is not None:
            shutil.rmtree(backup_dir, ignore_errors=True)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def _execution_date_key(value: Any) -> str:
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return str(value)


def _execution_capacity_scenario(
    *,
    aum: int,
    result: pd.DataFrame,
    trades: pd.DataFrame,
    final_state: dict[str, Any],
    input_hash: str,
    config_hash: str,
    participation_rate: float,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    scenario_id = f"aum_{aum}"
    diagnostics = list(final_state.get("execution_diagnostics", ()))
    if final_state.get("execution_mode") != "ohlcv_capacity":
        raise RuntimeError(f"{scenario_id}: capacity execution mode was not reported")
    if final_state.get("pending_execution_carries"):
        raise RuntimeError(f"{scenario_id}: pending execution carry remains at scenario end")
    diagnostic_ids = [str(row.get("execution_order_id", "")) for row in diagnostics]
    if not diagnostics and not trades.empty:
        raise RuntimeError(f"{scenario_id}: trades exist without execution diagnostics")
    if diagnostics and any(not value for value in diagnostic_ids):
        raise RuntimeError(f"{scenario_id}: every execution diagnostic requires an order ID")
    if diagnostics and len(set(diagnostic_ids)) != len(diagnostic_ids):
        raise RuntimeError(f"{scenario_id}: duplicate execution diagnostic IDs")
    diagnostic_rows: list[dict[str, Any]] = []
    for diagnostic in diagnostics:
        row = {
            "scenario_id": scenario_id,
            "aum": aum,
            "diagnostic_only": True,
            "executable_fill_claim": False,
            "input_hash": input_hash,
            "config_hash": config_hash,
        }
        row.update(diagnostic)
        if isinstance(row.get("diagnostic_labels"), (list, tuple)):
            row["diagnostic_labels"] = json.dumps(row["diagnostic_labels"], ensure_ascii=False)
        diagnostic_rows.append(row)

    diagnostic_lookup: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    diagnostic_by_id = {str(row["execution_order_id"]): row for row in diagnostics}
    for row in diagnostics:
        key = (
            _execution_date_key(row.get("date")),
            str(row.get("ticker")),
            str(row.get("side")),
        )
        diagnostic_lookup.setdefault(key, []).append(row)

    trade_rows: list[dict[str, Any]] = []
    if not trades.empty:
        for trade in trades.to_dict("records"):
            key = (
                _execution_date_key(trade.get("date")),
                str(trade.get("ticker")),
                str(trade.get("side")),
            )
            matched = diagnostic_by_id.get(str(trade.get("execution_order_id")))
            if matched is None:
                candidates = diagnostic_lookup.get(key, [])
                matched = candidates.pop(0) if candidates else {}
            row = dict(trade)
            row.update(
                {
                    "scenario_id": scenario_id,
                    "aum": aum,
                    "execution_mode": "ohlcv_capacity",
                    "diagnostic_only": True,
                    "executable_fill_claim": False,
                    "filled_qty": int(trade.get("qty", 0) or 0),
                    "requested_qty": matched.get("requested_qty"),
                    "capacity_qty": matched.get("capacity_qty"),
                    "decision": matched.get("decision"),
                    "diagnostic_reason": matched.get("reason"),
                    "input_hash": input_hash,
                    "config_hash": config_hash,
                }
            )
            trade_rows.append(row)

    diagnostic_qty_by_id = {
        str(row["execution_order_id"]): int(row.get("filled_qty", 0) or 0)
        for row in diagnostics
    }
    trade_qty_by_id: dict[str, int] = {}
    for row in trade_rows:
        order_id = str(row.get("execution_order_id", ""))
        if not order_id:
            raise RuntimeError(f"{scenario_id}: execution trade is missing an order ID")
        trade_qty_by_id[order_id] = trade_qty_by_id.get(order_id, 0) + int(
            row.get("filled_qty", 0) or 0
        )
    for order_id, filled_qty in diagnostic_qty_by_id.items():
        if filled_qty != trade_qty_by_id.get(order_id, 0):
            raise RuntimeError(f"{scenario_id}: trade/diagnostic quantity mismatch for {order_id}")

    cash_flow_series = (
        trades["cash_flow"] if "cash_flow" in trades.columns else pd.Series(dtype=float)
    )
    trade_cash_flow = float(pd.to_numeric(cash_flow_series, errors="coerce").fillna(0).sum())
    distribution_cash = 0.0
    if "distribution_cash" in result.columns:
        distribution_cash = float(pd.to_numeric(result["distribution_cash"], errors="coerce").fillna(0).sum())
    final_cash = float(final_state.get("cash", 0.0))
    expected_cash = float(aum) + trade_cash_flow + distribution_cash
    cash_delta = final_cash - expected_cash

    net_trade_qty: dict[str, int] = {}
    for trade in trades.to_dict("records"):
        ticker = str(trade.get("ticker"))
        signed_qty = int(trade.get("qty", 0) or 0)
        net_trade_qty[ticker] = net_trade_qty.get(ticker, 0) + (
            signed_qty if trade.get("side") == "BUY" else -signed_qty
        )
    final_holdings = {
        str(ticker): int(quantity)
        for ticker, quantity in dict(final_state.get("holdings", {})).items()
    }
    tickers = sorted(set(net_trade_qty) | set(final_holdings)) or ["<PORTFOLIO>"]
    reconciliation_rows: list[dict[str, Any]] = []
    for ticker in tickers:
        final_qty = final_holdings.get(ticker, 0)
        net_qty = net_trade_qty.get(ticker, 0)
        reconciliation_rows.append(
            {
                "scenario_id": scenario_id,
                "aum": aum,
                "ticker": ticker,
                "initial_cash": aum,
                "trade_cash_flow": trade_cash_flow,
                "distribution_cash": distribution_cash,
                "expected_cash": expected_cash,
                "final_cash": final_cash,
                "cash_delta": cash_delta,
                "net_trade_qty": net_qty,
                "final_holdings_qty": final_qty,
                "holdings_delta": final_qty - net_qty,
                "cash_reconciled": abs(cash_delta) <= 1e-6,
                "holdings_reconciled": final_qty == net_qty,
                "reconciled": abs(cash_delta) <= 1e-6 and final_qty == net_qty,
                "diagnostic_only": True,
                "executable_fill_claim": False,
            }
        )

    final_equity = float(result.iloc[-1]["equity"]) if not result.empty else final_cash
    requested_total = sum(int(row.get("requested_qty", 0) or 0) for row in diagnostics)
    filled_total = sum(int(row.get("filled_qty", 0) or 0) for row in diagnostics)
    actual_filled_total = sum(int(row.get("filled_qty", 0) or 0) for row in trade_rows)
    capacity_total = sum(int(row.get("capacity_qty", 0) or 0) for row in diagnostics)
    summary = {
        "scenario_id": scenario_id,
        "aum": aum,
        "execution_mode": "ohlcv_capacity",
        "participation_rate": participation_rate,
        "diagnostic_only": True,
        "executable_fill_claim": False,
        "orderbook_used": False,
        "final_equity": final_equity,
        "final_cash": final_cash,
        "trade_count": len(trades),
        "diagnostic_count": len(diagnostics),
        "requested_qty_total": requested_total,
        "filled_qty_total": filled_total,
        "actual_trade_filled_qty_total": actual_filled_total,
        "capacity_qty_total": capacity_total,
        "carry_cancel_count": sum(
            1 for row in diagnostics if str(row.get("decision")) == "CARRY_CANCELLED"
        ),
        "reconciled": all(row["reconciled"] for row in reconciliation_rows)
        and filled_total == actual_filled_total,
        "input_hash": input_hash,
        "config_hash": config_hash,
    }
    return summary, diagnostic_rows, trade_rows, reconciliation_rows


def run_execution_capacity_scenarios(
    *,
    aums: Sequence[int | float],
    participation_rate: float = 0.05,
    output_dir: str | Path = "outputs_execution",
    strategy_runner: Callable[..., Any] | None = None,
) -> dict[str, Path]:
    """Run independent diagnostic-only OHLCV scenarios and write isolated outputs."""
    if not np.isfinite(float(participation_rate)) or not 0 <= float(participation_rate) <= 1:
        raise ValueError("execution participation rate must be finite and between 0 and 1")
    parsed_aums = _parse_execution_aums(aums)
    selected_dir = _prepare_execution_output_dir(output_dir)
    runner = strategy_runner or run_single_mode
    if _etf_shared.UNIVERSE_MODE == "auto":
        ensure_universe_initialized()
    execution_universe = [str(ticker) for ticker in getattr(_etf_shared, "ETF_LIST", ETF_LIST)]
    input_payload = {
        "start": START,
        "end": END,
        "mode": "single",
        "universe": execution_universe,
    }
    config_payload = {
        "strategy": get_strategy_config(),
        "base_slippage": BASE_SLIPPAGE,
        "spread_pct": SPREAD_PCT,
        "participation_rate": float(participation_rate),
        "carry_policy": "exactly one following trading date",
    }
    input_hash = _execution_sha256(input_payload)
    config_hash = _execution_sha256(config_payload)
    summaries: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    scenario_trades: list[dict[str, Any]] = []
    reconciliations: list[dict[str, Any]] = []
    scenario_periods: dict[str, list[str]] = {}

    for aum in parsed_aums:
        # Each invocation starts a fresh strategy state; no state is handed to
        # the next AUM scenario.
        result, trades, final_state = runner(
            initial_cash=aum,
            execution_mode="ohlcv_capacity",
            execution_participation_rate=float(participation_rate),
            return_final_state=True,
        )
        if not isinstance(final_state, dict):
            raise TypeError("capacity strategy runner must return a final state mapping")
        scenario_id = f"aum_{aum}"
        scenario_periods[scenario_id] = (
            [_execution_date_key(result["date"].iloc[0]), _execution_date_key(result["date"].iloc[-1])]
            if not result.empty and "date" in result.columns
            else [START, END]
        )
        summary, diag_rows, trade_rows, recon_rows = _execution_capacity_scenario(
            aum=aum,
            result=result,
            trades=trades,
            final_state=final_state,
            input_hash=input_hash,
            config_hash=config_hash,
            participation_rate=float(participation_rate),
        )
        summaries.append(summary)
        diagnostics.extend(diag_rows)
        scenario_trades.extend(trade_rows)
        reconciliations.extend(recon_rows)

    if not all(summary["reconciled"] for summary in summaries):
        raise RuntimeError("execution reconciliation failed; no capacity artifacts were written")

    summary_columns = list(summaries[0]) if summaries else [
        "scenario_id", "aum", "execution_mode", "participation_rate", "diagnostic_only",
        "executable_fill_claim",
    ]
    diagnostic_columns = [
        "scenario_id", "aum", "execution_mode", "diagnostic_only", "executable_fill_claim", "input_hash", "config_hash",
        "date", "ticker", "side", "decision", "requested_qty", "filled_qty", "remaining_qty",
        "capacity_qty", "bar_volume", "bar_value", "close_volume_notional_estimate", "participation_rate",
        "carry_age", "max_carry_days", "reason", "diagnostic_labels", "origin_date", "due_date",
        "order_reason", "execution_order_id", "terminal_applied",
    ]
    trade_columns = [
        "scenario_id", "aum", "execution_mode", "diagnostic_only", "executable_fill_claim", "input_hash", "config_hash",
        "date", "ticker", "side", "reason", "qty", "filled_qty", "requested_qty", "capacity_qty",
        "decision", "diagnostic_reason", "execution_order_id", "price", "net_value", "cash_flow",
        "estimated_tax", "cash_after",
    ]
    reconciliation_columns = list(reconciliations[0]) if reconciliations else [
        "scenario_id", "aum", "ticker", "reconciled"
    ]
    metadata = {
        "mode": "ohlcv_capacity",
        "execution_mode": "ohlcv_capacity",
        "diagnostic_only": True,
        "executable_fill_claim": False,
        "orderbook_used": False,
        "aums": parsed_aums,
        "participation_rate": float(participation_rate),
        "carry_policy": "exactly one following trading date",
        "input_hash": input_hash,
        "config_hash": config_hash,
        "period": {"start": START, "end": END},
        "scenario_periods": scenario_periods,
        "output_dir": str(selected_dir),
        "protected_outputs": [str(OUTPUT_DIR), "outputs_approval"],
        "artifacts": [
            "execution_summary.csv",
            "execution_diagnostics.csv",
            "execution_trades.csv",
            "execution_reconciliation.csv",
            "execution_metadata.json",
        ],
    }
    _commit_execution_outputs(
        selected_dir=selected_dir,
        summaries=summaries,
        diagnostics=diagnostics,
        scenario_trades=scenario_trades,
        reconciliations=reconciliations,
        metadata=metadata,
        summary_columns=summary_columns,
        diagnostic_columns=diagnostic_columns,
        trade_columns=trade_columns,
        reconciliation_columns=reconciliation_columns,
    )
    metadata_path = selected_dir / "execution_metadata.json"
    return {
        "output_dir": selected_dir,
        "metadata": metadata_path,
        "summary": selected_dir / "execution_summary.csv",
        "diagnostics": selected_dir / "execution_diagnostics.csv",
        "trades": selected_dir / "execution_trades.csv",
        "reconciliation": selected_dir / "execution_reconciliation.csv",
    }


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


def _strict_coverage_blockers(
    ledger: CorporateActionLedger,
    common_dates: Sequence[pd.Timestamp],
    universe: Sequence[str],
) -> list[ApprovalBlocker]:
    """Bind the verified manifest scope to the actual strategy run scope."""
    blockers: list[ApprovalBlocker] = []
    verification_start = ledger.manifest.verification_start
    verification_end = ledger.manifest.verification_end
    if not common_dates:
        blockers.append(ApprovalBlocker("EMPTY_STRATEGY_CALENDAR", "strategy common_dates is empty"))
    else:
        run_start = pd.Timestamp(common_dates[0]).date()
        run_end = pd.Timestamp(common_dates[-1]).date()
        if verification_start is None or verification_end is None:
            blockers.append(
                ApprovalBlocker(
                    "RUN_OUTSIDE_VERIFICATION_PERIOD",
                    "manifest verification period is required to bind the strategy calendar",
                )
            )
        else:
            if run_start < verification_start:
                blockers.append(
                    ApprovalBlocker(
                        "RUN_START_OUTSIDE_VERIFICATION_PERIOD",
                        f"strategy starts {run_start.isoformat()} before verified start "
                        f"{verification_start.isoformat()}",
                        event_date=run_start,
                    )
                )
            if run_end > verification_end:
                blockers.append(
                    ApprovalBlocker(
                        "RUN_END_OUTSIDE_VERIFICATION_PERIOD",
                        f"strategy ends {run_end.isoformat()} after verified end "
                        f"{verification_end.isoformat()}",
                        event_date=run_end,
                    )
                )
    covered = set(ledger.manifest.verification_tickers)
    missing = sorted({str(ticker) for ticker in universe} - covered)
    if missing:
        blockers.append(
            ApprovalBlocker(
                "UNIVERSE_OUTSIDE_VERIFICATION_TICKERS",
                "strategy universe is not fully covered by manifest: " + ",".join(missing),
            )
        )
    return blockers


def _validate_approval_output_dir(output_dir: Path) -> Path:
    candidate = output_dir.expanduser().resolve()
    standard = OUTPUT_DIR.expanduser().resolve()
    if candidate == standard or candidate in standard.parents or standard in candidate.parents:
        raise ValueError(
            f"approval output directory overlaps standard output directory: {candidate}"
        )
    return candidate


def _approval_report_payload(
    report: ApprovalReport,
    blocked_orders: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "status": report.status,
        "approval_valid": report.approval_valid,
        "event_count": report.event_count,
        "ledger_sha256": report.ledger_sha256,
        "blockers": [
            {
                "code": blocker.code,
                "message": blocker.message,
                "event_id": blocker.event_id,
                "ticker": blocker.ticker,
                "event_date": blocker.event_date.isoformat() if blocker.event_date else None,
            }
            for blocker in report.blockers
        ],
        "blocked_orders": list(blocked_orders),
    }


def _approval_reproducibility_payload(
    *,
    ledger_path: str,
    manifest_path: str,
    output_dir: str,
    report: ApprovalReport,
) -> dict[str, Any]:
    strict_config = _build_performance_config()
    strict_config["enable_benchmark"] = False
    return {
        "mode": "approval_strict",
        "start": START,
        "end": END,
        "run_mode": RUN_MODE,
        "ledger_path": ledger_path,
        "manifest_path": manifest_path,
        "output_dir": output_dir,
        "ledger_sha256": report.ledger_sha256,
        "strategy_config": strict_config,
        "benchmark_supported": False,
        "status": report.status,
    }


def _write_approval_blocked(
    output_dir: Path,
    report: ApprovalReport,
    *,
    ledger_path: str,
    manifest_path: str,
    blocked_orders: Sequence[dict[str, Any]] = (),
) -> None:
    """Write only deterministic approval diagnostics for a blocked strict run."""
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale_name in (
        "approval_equity_curve.csv",
        "approval_trades.csv",
        "approval_performance.json",
        "performance.json",
    ):
        stale_path = output_dir / stale_name
        if stale_path.exists():
            stale_path.unlink()
    with (output_dir / "approval_report.json").open("w", encoding="utf-8") as handle:
        json.dump(
            _approval_report_payload(report, blocked_orders),
            handle,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    blocker_rows = [
        {
            "code": blocker.code,
            "message": blocker.message,
            "event_id": blocker.event_id,
            "ticker": blocker.ticker,
            "event_date": blocker.event_date.isoformat() if blocker.event_date else None,
        }
        for blocker in report.blockers
    ]
    pd.DataFrame(
        blocker_rows,
        columns=["code", "message", "event_id", "ticker", "event_date"],
    ).to_csv(output_dir / "approval_blockers.csv", index=False, encoding="utf-8-sig")
    with (output_dir / "reproducibility.json").open("w", encoding="utf-8") as handle:
        json.dump(
            _approval_reproducibility_payload(
                ledger_path=ledger_path,
                manifest_path=manifest_path,
                output_dir=str(output_dir),
                report=report,
            ),
            handle,
            ensure_ascii=False,
            indent=2,
            default=str,
        )


def _write_approval_result(
    output_dir: Path,
    result: pd.DataFrame,
    trades: pd.DataFrame,
    report: ApprovalReport,
    *,
    ledger_path: str,
    manifest_path: str,
    blocked_orders: Sequence[dict[str, Any]] = (),
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_dir / "approval_equity_curve.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(output_dir / "approval_trades.csv", index=False, encoding="utf-8-sig")
    stats = calc_stats(result, "equity")
    stats.update(
        calc_trading_stats(
            trades,
            result["equity"],
            result["date"],
            result.get("rebalance_decision"),
        )
    )
    with (output_dir / "approval_report.json").open("w", encoding="utf-8") as handle:
        payload = _approval_report_payload(report, blocked_orders)
        payload["performance"] = _to_json_serializable(stats)
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
    blocker_rows = [
        {
            "code": blocker.code,
            "message": blocker.message,
            "event_id": blocker.event_id,
            "ticker": blocker.ticker,
            "event_date": blocker.event_date.isoformat() if blocker.event_date else None,
        }
        for blocker in report.blockers
    ]
    pd.DataFrame(
        blocker_rows,
        columns=["code", "message", "event_id", "ticker", "event_date"],
    ).to_csv(output_dir / "approval_blockers.csv", index=False, encoding="utf-8-sig")
    with (output_dir / "reproducibility.json").open("w", encoding="utf-8") as handle:
        json.dump(
            _approval_reproducibility_payload(
                ledger_path=ledger_path,
                manifest_path=manifest_path,
                output_dir=str(output_dir),
                report=report,
            ),
            handle,
            ensure_ascii=False,
            indent=2,
            default=str,
        )


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

    execution_aums: list[int] | None = None
    execution_rate: float | None = None
    execution_output_dir: Path | None = None
    try:
        execution_contract = _validate_execution_cli_contract(
            execution_mode=args.execution_mode,
            approval_strict=args.approval_strict,
            run_mode=RUN_MODE,
            raw_aums=args.execution_aum,
            raw_rate=args.execution_participation_rate,
            output_dir=args.execution_output_dir,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"실행 capacity 입력/출력 경로 거부: {exc}")
        return
    if execution_contract is not None:
        execution_aums, execution_rate, execution_output_dir = execution_contract

    approval_output_dir = Path(args.approval_output_dir)
    corporate_action_ledger = None
    if args.approval_strict:
        try:
            approval_output_dir = _validate_approval_output_dir(approval_output_dir)
        except ValueError as exc:
            print(f"승인 출력 경로 거부: {exc}")
            return
        if RUN_MODE != "single":
            report = ApprovalReport(
                "BLOCKED",
                (
                    ApprovalBlocker(
                        "STRICT_MODE_UNSUPPORTED",
                        "approval_strict supports only --mode single; benchmark and experiment paths are excluded",
                    ),
                ),
                0,
                "",
            )
            _write_approval_blocked(
                approval_output_dir,
                report,
                ledger_path=args.corporate_actions_ledger,
                manifest_path=args.corporate_actions_manifest,
            )
            print(f"승인 실행 차단: {approval_output_dir / 'approval_report.json'}")
            return
        try:
            corporate_action_ledger = load_corporate_action_ledger(
                args.corporate_actions_ledger,
                args.corporate_actions_manifest,
            )
            initial_report = corporate_action_ledger.approval_report()
        except (
            AttributeError,
            CorporateActionBlocked,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            report = ApprovalReport(
                "BLOCKED",
                (ApprovalBlocker("LEDGER_LOAD_ERROR", str(exc)),),
                0,
                "",
            )
            _write_approval_blocked(
                approval_output_dir,
                report,
                ledger_path=args.corporate_actions_ledger,
                manifest_path=args.corporate_actions_manifest,
            )
            print(f"승인 실행 차단: {approval_output_dir / 'approval_report.json'}")
            return
        if not initial_report.approval_valid:
            _write_approval_blocked(
                approval_output_dir,
                initial_report,
                ledger_path=args.corporate_actions_ledger,
                manifest_path=args.corporate_actions_manifest,
            )
            print(f"승인 실행 차단: {approval_output_dir / 'approval_report.json'}")
            return
        if _etf_shared.UNIVERSE_MODE == "auto":
            ensure_universe_initialized()
        coverage_blockers = _strict_coverage_blockers(
            corporate_action_ledger,
            [pd.Timestamp(START), pd.Timestamp(END)],
            ETF_LIST,
        )
        if coverage_blockers:
            coverage_report = ApprovalReport(
                "BLOCKED",
                tuple(initial_report.blockers) + tuple(coverage_blockers),
                initial_report.event_count,
                initial_report.ledger_sha256,
            )
            _write_approval_blocked(
                approval_output_dir,
                coverage_report,
                ledger_path=args.corporate_actions_ledger,
                manifest_path=args.corporate_actions_manifest,
            )
            print(f"승인 실행 차단: {approval_output_dir / 'approval_report.json'}")
            return

    from pykrx_utils import KRX_PASSWORD_CHANGE_URL, check_krx_auth_status
    get_stock()
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

    if RUN_MODE not in {"single", "experiment", "risk_off_compare"}:
        print(f"\n❌ 잘못된 ETF_BACKTEST_MODE 값: {RUN_MODE}")
        print("   허용값: single | experiment | risk_off_compare")
        exit(1)

    if args.execution_mode == "ohlcv_capacity":
        try:
            assert execution_aums is not None
            assert execution_rate is not None
            assert execution_output_dir is not None
            artifacts = run_execution_capacity_scenarios(
                aums=execution_aums,
                participation_rate=execution_rate,
                output_dir=execution_output_dir,
                strategy_runner=run_single_mode,
            )
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            print(f"실행 capacity 시나리오 실패: {exc}")
            raise SystemExit(1) from exc
        print(f"실행 capacity 진단 산출물 저장: {artifacts['output_dir']}")
        return

    if not args.approval_strict:
        OUTPUT_DIR.mkdir(exist_ok=True)

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

    if args.approval_strict:
        try:
            result, trades, final_state = run_single_mode(
                approval_strict=True,
                corporate_action_ledger=corporate_action_ledger,
            )
        except Exception as exc:  # noqa: BLE001 - strict runs must emit blockers
            report = ApprovalReport(
                "BLOCKED",
                (ApprovalBlocker("STRICT_EXECUTION_ERROR", str(exc)),),
                len(corporate_action_ledger.events),
                corporate_action_ledger.ledger_sha256,
            )
            _write_approval_blocked(
                approval_output_dir,
                report,
                ledger_path=args.corporate_actions_ledger,
                manifest_path=args.corporate_actions_manifest,
            )
            print(f"승인 실행 차단: {approval_output_dir / 'approval_report.json'}")
            return
        report = final_state["approval_report"]
        if not report.approval_valid or result.empty:
            if result.empty and report.approval_valid:
                report = ApprovalReport(
                    "BLOCKED",
                    (
                        ApprovalBlocker(
                            "EMPTY_RESULT",
                            "strict strategy produced no equity rows",
                        ),
                    ),
                    report.event_count,
                    report.ledger_sha256,
                )
            _write_approval_blocked(
                approval_output_dir,
                report,
                ledger_path=args.corporate_actions_ledger,
                manifest_path=args.corporate_actions_manifest,
                blocked_orders=final_state.get("blocked_orders", ()),
            )
            print(f"승인 실행 차단: {approval_output_dir / 'approval_report.json'}")
            return
        _write_approval_result(
            approval_output_dir,
            result,
            trades,
            report,
            ledger_path=args.corporate_actions_ledger,
            manifest_path=args.corporate_actions_manifest,
            blocked_orders=final_state.get("blocked_orders", ()),
        )
        print(f"승인 실행 완료: {approval_output_dir / 'approval_report.json'}")
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
