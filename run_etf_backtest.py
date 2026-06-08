from pathlib import Path
import json
import os
import argparse
from datetime import date, datetime

import numpy as np
import pandas as pd

from pykrx_utils import _call_capture_stderr, _range_has_weekday, get_ticker_name
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
    build_rebalance_orders,
    get_strategy_config,
)

# 백테스트 전용: 기본 슬리피지 및 호가 스프레드 (환경변수로 재정의 가능)
# ETF_BASE_SLIPPAGE: 예) 0.0005 (5bp)
# ETF_SPREAD_PCT: 예) 0.0005 (기본 0.0005)
from config_utils import parse_pct_env
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
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        # 실패해도 무시하고 진행
        pass


load_dotenv()

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

# ETF 후보군 선택 관련 상수는 etf_shared 모듈에서 관리합니다.

BENCHMARK_TICKER = "069500"  # KODEX 200


PERIODS = [
    ("2016_2019", "2016-01-01", "2019-12-31"),
    ("2020_2021", "2020-01-01", "2021-12-31"),
    ("2022_2023", "2022-01-01", "2023-12-31"),
    ("2024_2026", "2024-01-01", "2026-04-30"),
]

# 백테스트 기본 기간: 시작일 기본은 20160101, 종료일 기본은 오늘(또는 마지막 영업일)
START_DEFAULT = "20160101"
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
    parser.add_argument("--mode", "-m", choices=["single", "experiment"], help="실행 모드: single 또는 experiment (옵션)", default=None)
    return parser.parse_args()


def normalize_ohlcv(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["date", "ticker", "open", "close", "volume", "trading_value"])

    df = df.reset_index()
    df["ticker"] = ticker
    df = df.rename(
        columns={
            "날짜": "date",
            "시가": "open",
            "종가": "close",
            "거래량": "volume",
            "거래대금": "trading_value",
        }
    )

    required_columns = ["date", "ticker", "open", "close"]
    missing_columns = [col for col in required_columns if col not in df.columns]
    if missing_columns:
        raise ValueError(f"{ticker}: missing columns {missing_columns}; actual columns={list(df.columns)}")

    if "volume" not in df.columns:
        df["volume"] = 0
    if "trading_value" not in df.columns:
        df["trading_value"] = df["close"] * df["volume"]

    out = df[["date", "ticker", "open", "close", "volume", "trading_value"]].copy()
    out["date"] = pd.to_datetime(out["date"])
    return out


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


def get_price(ticker: str) -> pd.DataFrame:
    cache_dir = Path("data_cache")
    cache_dir.mkdir(exist_ok=True)
    use_cache = os.environ.get("ETF_USE_CACHE", "1") != "0"
    force_refresh = os.environ.get("ETF_REFRESH_CACHE", "0") == "1"
    cache_parquet = cache_dir / f"{ticker}.parquet"
    cache_csv_pattern = list(cache_dir.glob(f"{ticker}_*.csv"))

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

            start_req = pd.to_datetime(START)
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
                        raw_left = _call_capture_stderr(stock.get_market_ohlcv_by_date, fetch_start, fetch_end, ticker)
                    except Exception as e:
                        print(f"[캐시] {ticker} left 호출 실패: {e}")
                    else:
                        try:
                            if hasattr(raw_left, "columns"):
                                print(f"[캐시][debug] {ticker} raw_left type={type(raw_left)}, columns={list(raw_left.columns)}")
                                try:
                                    print(raw_left.head().to_string())
                                except Exception:
                                    pass
                            df_left = normalize_ohlcv(raw_left, ticker)
                            if isinstance(df_left, pd.DataFrame) and not df_left.empty:
                                to_concat.insert(0, df_left)
                                fetched = True
                                print(f"[캐시] {ticker} left 증분 수집: {fetch_start}~{fetch_end}")
                            else:
                                print(f"[캐시] {ticker} left 증분 비어있음: {fetch_start}~{fetch_end}")
                        except Exception as e:
                            print(f"[캐시] {ticker} left 정규화 실패: {e}")

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
                        raw_right = _call_capture_stderr(stock.get_market_ohlcv_by_date, fetch_start, fetch_end, ticker)
                    except Exception as e:
                        print(f"[캐시] {ticker} right 호출 실패: {e}")
                    else:
                        try:
                            if hasattr(raw_right, "columns"):
                                print(f"[캐시][debug] {ticker} raw_right type={type(raw_right)}, columns={list(raw_right.columns)}")
                                try:
                                    print(raw_right.head().to_string())
                                except Exception:
                                    pass
                            df_right = normalize_ohlcv(raw_right, ticker)
                            if isinstance(df_right, pd.DataFrame) and not df_right.empty:
                                to_concat.append(df_right)
                                fetched = True
                                print(f"[캐시] {ticker} right 증분 수집: {fetch_start}~{fetch_end}")
                            else:
                                print(f"[캐시] {ticker} right 증분 비어있음: {fetch_start}~{fetch_end}")
                        except Exception as e:
                            print(f"[캐시] {ticker} right 정규화 실패: {e}")

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
        except Exception as e:
            print(f"[캐시] {ticker} 캐시 읽기 실패: {e} — 전체 재조회로 대체")

    # 캐시가 없거나 강제 갱신인 경우 전체 조회 후 저장
    try:
        raw = _call_capture_stderr(stock.get_market_ohlcv_by_date, START, END, ticker)
        df = normalize_ohlcv(raw, ticker)
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


def safe_get(series: pd.Series, key: str):
    value = series.get(key)
    if value is None or pd.isna(value) or value <= 0:
        return None
    return float(value)


def load_etf_price() -> pd.DataFrame:
    frames = []
    failed = []
    empty = []

    total = len(ETF_LIST)
    for idx, ticker in enumerate(ETF_LIST, start=1):
        print(f"[데이터] {idx}/{total} 조회: {ticker}")
        try:
            df = get_price(ticker)
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
    grouped = price.groupby("ticker")
    price["ret_60"] = grouped["close"].pct_change(60)
    price["ret_120"] = grouped["close"].pct_change(120)
    price["ma20"] = grouped["close"].transform(lambda x: x.rolling(20).mean())
    price["ma60"] = grouped["close"].transform(lambda x: x.rolling(60).mean())
    price["trend_ok"] = (price["close"] > price["ma20"]) & (price["ma20"] > price["ma60"])
    return price



def run_etf_strategy(initial_cash: float, common_dates: list[pd.Timestamp], index_df: pd.DataFrame, use_market_filter: bool = True, max_positions: int = ETF_MAX_POSITIONS, slippage: float = SLIPPAGE_PCT):
    price = load_etf_price()
    price_by_date = {dt: day.set_index("ticker") for dt, day in price.groupby("date")}

    cash = float(initial_cash)
    holdings = {}
    holding_cost_basis = {}
    trades = []
    equity_rows = []

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
        should_rebalance = (i - warmup_days) % REBALANCE_STEP_DAYS == 0

        if should_rebalance:
            # 시장 필터 + 랭킹으로 목표 종목 결정
            risk_on = is_risk_on(index_df, dt) if use_market_filter else True
            ranked = rank_etfs(today.reset_index())
            # targets를 문자열 리스트로 통일
            targets = [str(t) for t in ranked.head(max_positions)["ticker"].tolist()] if risk_on else []

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
            max_asset_pct_env = os.environ.get("MAX_ASSET_PCT")
            max_asset_pct = None
            if max_asset_pct_env is not None and max_asset_pct_env.strip() != "" and float(max_asset_pct_env) > 0:
                try:
                    max_asset_pct = float(max_asset_pct_env)
                except Exception:
                    max_asset_pct = None

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
                allow_empty_target_sell=False,
                generate_orders=True,
                max_asset_pct=max_asset_pct,
            )

            # 생성된 주문을 즉시 전량 체결로 모사 (백테스트 단순화)
            for o in orders:
                raw_ticker = o.get("ticker")
                ticker = str(raw_ticker)
                qty = int(o.get("qty", 0) or 0)
                ref_price = o.get("reference_price")
                side = o.get("side")

                if side == "SELL":
                    holdings.pop(ticker, None)
                    holding_cost_basis.pop(ticker, None)
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
                            "cash_after": cash,
                        }
                    )

        market_value = 0.0
        for ticker, qty in holdings.items():
            close_price = safe_get(next_close, ticker)
            if close_price is not None:
                market_value += qty * close_price

        equity_rows.append(
            {
                "date": next_dt,
                "equity": cash + market_value,
                "cash": cash,
                "market_value": market_value,
                "holdings": ",".join(sorted(map(str, holdings.keys()))),
            }
        )

    return pd.DataFrame(equity_rows), pd.DataFrame(trades)


def run_kodex200_buy_and_hold(initial_cash: float, common_dates: list[pd.Timestamp]) -> pd.DataFrame:
    price = get_price(BENCHMARK_TICKER)
    if price.empty:
        raise RuntimeError(f"No benchmark data for {BENCHMARK_TICKER}")

    price_by_date = {dt: day.set_index("ticker") for dt, day in price.groupby("date")}
    cash = float(initial_cash)
    qty = 0
    bought = False
    equity_rows = []

    for i, _dt in enumerate(common_dates[:-1]):
        next_dt = common_dates[i + 1]
        next_day = price_by_date.get(next_dt, pd.DataFrame())
        if next_day.empty:
            continue

        next_open = next_day["open"]
        next_close = next_day["close"]

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

        close_price = safe_get(next_close, BENCHMARK_TICKER)
        market_value = qty * close_price if close_price is not None else 0.0
        equity_rows.append(
            {
                "date": next_dt,
                "equity_kodex200_bh": cash + market_value,
                "cash_kodex200_bh": cash,
                "market_value_kodex200_bh": market_value,
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

    # 변동성은 모집단 기준(ddof=0) 표준편차로 계산
    volatility = returns.std(ddof=0) * np.sqrt(252) if not returns.empty else 0.0

    # 샤프: risk_free는 연간 비율(예: 0.01)을 입력, 기본 0
    if volatility == 0:
        sharpe = np.nan
    else:
        rf_daily = risk_free / 252
        sharpe = (returns.mean() - rf_daily) * 252 / volatility

    return {
        "initial": temp[equity_col].iloc[0],
        "final": temp[equity_col].iloc[-1],
        "total_return": total_return,
        "cagr": cagr,
        "mdd": mdd,
        "volatility": volatility,
        "sharpe": sharpe,
    }


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
    common_dates = list(index_df["date"])

    result, trades = run_etf_strategy(
        INITIAL_CASH,
        common_dates,
        index_df,
        use_market_filter=USE_MARKET_FILTER,
        max_positions=ETF_MAX_POSITIONS,
        slippage=BASE_SLIPPAGE,
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

    if ENABLE_BENCHMARK and "equity_kodex200_bh" in result.columns:
        result["equity_kodex200_bh"] = result["equity_kodex200_bh"].ffill().fillna(INITIAL_CASH)

    return result, trades


def run_experiment_mode() -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    index_df = get_index_data()
    common_dates = list(index_df["date"])

    results = []
    trades_dict = {}

    for slip in SLIPPAGE_OPTIONS:
        curve, trades = run_etf_strategy(INITIAL_CASH, common_dates, index_df, True, 2, slip)
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
    print(f"변동성(연환산): {strategy_stats['volatility']:.2%}")
    print(f"샤프: {strategy_stats['sharpe']:.4f}")
    print(f"거래 수: {len(trades)}")
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
        print("\n=== 벤치마크 대비 ===")
        print(
            "요약: "
            f"누적수익률 {strategy_stats['total_return'] - benchmark_stats['total_return']:+.2%}, "
            f"CAGR {strategy_stats['cagr'] - benchmark_stats['cagr']:+.2%}, "
            f"MDD {strategy_stats['mdd'] - benchmark_stats['mdd']:+.2%}"
        )

    return strategy_stats, benchmark_stats, period


def summarize_experiment(df: pd.DataFrame, trades_dict: dict):
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
    display_cols = ["strategy", "final", "total_return", "cagr", "mdd", "volatility", "sharpe"]
    print(comparison[display_cols].to_string(index=False, float_format=lambda x: f"{x:,.4f}"))

    for slip in SLIPPAGE_OPTIONS:
        label = f"slip_{int(slip*10000)}bp"
        trades = trades_dict[label]
        invested_ratio = (df[f"market_value_{label}"] / df[f"equity_{label}"]).mean()

        print(f"\n=== ETF {label} 상세 ===")
        print(f"거래 수: {len(trades)}")
        print(f"평균 투자 비중: {invested_ratio:.4f}")


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

    if RUN_MODE not in {"single", "experiment"}:
        print(f"\n❌ 잘못된 ETF_BACKTEST_MODE 값: {RUN_MODE}")
        print("   허용값: single | experiment")
        exit(1)

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
        save_cols = ["date", "equity_strategy", "cash", "market_value", "holdings"]
        if "equity_benchmark" in curve_to_save.columns:
            save_cols.append("equity_benchmark")
        curve_to_save[save_cols].to_csv(OUTPUT_DIR / "etf_equity_curve.csv", index=False, encoding="utf-8-sig")
        trades.to_csv(OUTPUT_DIR / "etf_trades.csv", index=False, encoding="utf-8-sig")

        strategy_stats, benchmark_stats, period = summarize_single(result, trades)
        payload = {
            "mode": "single",
            "slippage": BASE_SLIPPAGE,
            "period": _to_json_serializable(period),
            "strategy": _to_json_serializable(strategy_stats),
            "benchmark": _to_json_serializable(benchmark_stats) if benchmark_stats is not None else None,
        }
        with (OUTPUT_DIR / "performance.json").open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        print(f"저장 완료: {OUTPUT_DIR / 'etf_equity_curve.csv'}")
        print(f"저장 완료: {OUTPUT_DIR / 'etf_trades.csv'}")
        print(f"저장 완료: {OUTPUT_DIR / 'performance.json'}")
    else:
        result.to_csv(OUTPUT_DIR / "etf_equity_curve.csv", index=False, encoding="utf-8-sig")
        # 필요 시 슬리피지별 체결 내역 저장
        for slip in SLIPPAGE_OPTIONS:
            label = f"slip_{int(slip*10000)}bp"
            trades_dict[label].to_csv(OUTPUT_DIR / f"etf_trades_{label}.csv", index=False, encoding="utf-8-sig")
        cols = ["date", "equity_kodex200_bh"] + [f"equity_slip_{int(s*10000)}bp" for s in SLIPPAGE_OPTIONS]
        result[cols].to_csv(OUTPUT_DIR / "slippage_comparison.csv", index=False, encoding="utf-8-sig")

        summarize_experiment(result, trades_dict)
        print(f"저장 완료: {OUTPUT_DIR / 'etf_equity_curve.csv'}")
        for slip in SLIPPAGE_OPTIONS:
            label = f"slip_{int(slip*10000)}bp"
            print(f"저장 완료: {OUTPUT_DIR / f'etf_trades_{label}.csv'}")
        print(f"저장 완료: {OUTPUT_DIR / 'slippage_comparison.csv'}")


if __name__ == "__main__":
    main()