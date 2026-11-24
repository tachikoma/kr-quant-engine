"""P0-A-3: 지수 데이터 캐시 무결성 검증.

``get_index_data()``(KOSPI)와 ``get_us_index_data()``(미국)를 실제로 호출하되
pykrx/yfinance 네트워크 접근만 fixture로 대체한다. 지수 캐시의 정렬·중복·coverage·
지표 재계산·``risk_on`` 판정이 독립 계산과 일치하는지 검사한다.

검사 항목:
1. 캐시 전체 재사용 시 날짜 오름차순·중복 없음
2. 증분 조회·병합 시 데이터/날짜 정합성 (left/right)
3. 증분 조회 실패 시 부분 데이터를 정상 결과로 반환하지 않음
4. 미국 지수 부분 캐시를 요청 기간 전체 커버로 오인하지 않음
5. market_ma/market_ma_slope/risk_on(및 미국 대응)이 독립 재계산과 일치
6. 캐시 재사용과 전체 재조회(force_refresh)의 값·신호 동일

캐시는 임시 디렉터리에서만 생성하고 네트워크를 쓰지 않는다.

실행::

    uv run python scripts/test_index_cache_integrity.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 로컬 .env/셸 설정이 fixture 결과를 바꾸지 않게 고정한다.
os.environ.update(
    {
        "ETF_USE_CACHE": "1",
        "ETF_REFRESH_CACHE": "0",
        "US_RISK_PROXY": "SPY",
    }
)
os.environ["KRX_ID"] = ""
os.environ["KRX_PW"] = ""

import run_etf_backtest as backtest

KOSPI_CODE = backtest.KOSPI_INDEX_CODE
US_PROXY = backtest.US_RISK_PROXY
INDEX_COLUMNS = ["date", "close", "market_ma", "market_ma_slope", "risk_on"]
US_COLUMNS = ["date", "close", "us_market_ma", "us_market_ma_slope", "us_risk_on"]

# 워밍업(MA120 + slope20)을 확보하기 위해 요청 시작보다 1년 앞에서 데이터를 만든다.
SOURCE_START = "2023-01-02"
START = "20240102"
END = "20240329"
KR_DATES = pd.bdate_range(SOURCE_START, "2024-03-29")
# 미국장 휴장(한국장은 개장) / 한국장 휴장(미국장은 개장) 가정 날짜
US_MISSING = pd.Timestamp("2024-01-15")
KR_MISSING = pd.Timestamp("2024-03-01")
US_DATES = pd.DatetimeIndex([d for d in KR_DATES if d != US_MISSING])
KR_DATES_WITH_GAPS = pd.DatetimeIndex([d for d in KR_DATES if d != KR_MISSING])


# --------------------------------------------------------------------------- #
# fixture
# --------------------------------------------------------------------------- #
def build_close_series(dates: pd.DatetimeIndex, seed: float, scale: float) -> pd.DataFrame:
    index = np.arange(len(dates), dtype=float)
    close = scale * np.exp(0.0006 * index + 0.02 * np.sin(index / 11.0 + seed))
    return pd.DataFrame({"date": pd.to_datetime(dates), "close": close})


def independent_indicators(
    source: pd.DataFrame,
    ma_column: str,
    slope_column: str,
    signal_column: str,
    ma_days: int = backtest.MARKET_MA_DAYS,
    slope_days: int = backtest.MARKET_SLOPE_DAYS,
) -> pd.DataFrame:
    """원천 종가에서 지표를 독립 재계산한다(구현과 다른 코드 경로)."""
    frame = source.sort_values("date").reset_index(drop=True).copy()
    frame[ma_column] = frame["close"].rolling(ma_days).mean()
    frame[slope_column] = frame[ma_column] - frame[ma_column].shift(slope_days)
    frame[signal_column] = (frame["close"] >= frame[ma_column]) & (
        frame[slope_column] >= 0
    )
    return frame


def kospi_expected(dates: pd.DatetimeIndex | None = None) -> pd.DataFrame:
    source = build_close_series(KR_DATES_WITH_GAPS if dates is None else dates, 0.7, 2500.0)
    return independent_indicators(
        source, "market_ma", "market_ma_slope", "risk_on"
    )


def us_expected() -> pd.DataFrame:
    source = build_close_series(US_DATES, 1.9, 400.0)
    return independent_indicators(
        source,
        "us_market_ma",
        "us_market_ma_slope",
        "us_risk_on",
        ma_days=backtest.US_MARKET_MA_DAYS,
        slope_days=backtest.US_MARKET_SLOPE_DAYS,
    )


def kospi_expected_window(start: str = START, end: str = END) -> pd.DataFrame:
    """요청 구간만 내려받았을 때(전체 재조회 경로)의 지표 기대값."""
    source = kospi_expected()
    window = source[
        (source["date"] >= pd.to_datetime(start)) & (source["date"] <= pd.to_datetime(end))
    ]
    return independent_indicators(window, "market_ma", "market_ma_slope", "risk_on")


def us_expected_window() -> pd.DataFrame:
    """요청 구간만 내려받았을 때(전체 재조회 경로)의 지표 기대값."""
    source = us_expected()
    window = source[
        (source["date"] >= pd.to_datetime(START)) & (source["date"] <= pd.to_datetime(END))
    ]
    return independent_indicators(
        window,
        "us_market_ma",
        "us_market_ma_slope",
        "us_risk_on",
        ma_days=backtest.US_MARKET_MA_DAYS,
        slope_days=backtest.US_MARKET_SLOPE_DAYS,
    )


class FakeKrxStock:
    """``get_stock().get_index_ohlcv_by_date`` 대역 (pykrx 한국어 컬럼/날짜 인덱스)."""

    def __init__(self, source: pd.DataFrame) -> None:
        self.source = source
        self.calls: list[tuple[str, str, str]] = []
        self.fail_predicate: Callable[[str, str], bool] | None = None

    def get_index_ohlcv_by_date(self, start: str, end: str, code: str) -> pd.DataFrame:
        self.calls.append((start, end, code))
        if self.fail_predicate is not None and self.fail_predicate(start, end):
            raise RuntimeError("simulated KRX failure")
        s = pd.to_datetime(start)
        e = pd.to_datetime(end)
        sel = self.source[(self.source["date"] >= s) & (self.source["date"] <= e)]
        frame = pd.DataFrame(
            {
                "시가": sel["close"].to_numpy() * 0.999,
                "고가": sel["close"].to_numpy() * 1.002,
                "저가": sel["close"].to_numpy() * 0.998,
                "종가": sel["close"].to_numpy(),
                "거래량": np.full(len(sel), 1_000_000, dtype=np.int64),
                "거래대금": sel["close"].to_numpy() * 1_000_000,
            },
            index=pd.DatetimeIndex(sel["date"], name="날짜"),
        )
        return frame


class FakeYFinance:
    """yfinance 다운로드 대역 (Date 인덱스 + MultiIndex 컬럼)."""

    def __init__(self, source: pd.DataFrame) -> None:
        self.source = source
        self.calls: list[tuple[str, str, str]] = []
        self.fail_predicate: Callable[[str, str], bool] | None = None

    def download(self, ticker: str, start: object = None, end: object = None, **kwargs: object):
        del kwargs
        start_ts = pd.to_datetime(start)
        end_ts = pd.to_datetime(end) - pd.Timedelta(days=1)
        self.calls.append((str(ticker), start_ts.strftime("%Y%m%d"), end_ts.strftime("%Y%m%d")))
        if self.fail_predicate is not None and self.fail_predicate(
            start_ts.strftime("%Y%m%d"), end_ts.strftime("%Y%m%d")
        ):
            raise RuntimeError("simulated yfinance failure")
        sel = self.source[(self.source["date"] >= start_ts) & (self.source["date"] <= end_ts)]
        frame = pd.DataFrame(
            {
                ("Adj Close", ticker): sel["close"].to_numpy(),
                ("Close", ticker): sel["close"].to_numpy(),
            },
            index=pd.DatetimeIndex(sel["date"], name="Date"),
        )
        frame.columns = pd.MultiIndex.from_tuples(frame.columns)
        return frame


@contextmanager
def temp_cwd() -> Iterator[Path]:
    previous = Path.cwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            yield Path(tmp)
        finally:
            os.chdir(previous)


@contextmanager
def patched(
    *,
    stock: FakeKrxStock | None = None,
    yfinance: FakeYFinance | None = None,
    refresh: bool = False,
    start: str = START,
    end: str = END,
):
    originals = {
        "get_stock": backtest.get_stock,
        "_call_capture_stderr": backtest._call_capture_stderr,
        "yf": backtest.yf,
        "HAS_KRX_CREDENTIALS": backtest.HAS_KRX_CREDENTIALS,
        "START": backtest.START,
        "END": backtest.END,
        "ETF_REFRESH_CACHE": os.environ.get("ETF_REFRESH_CACHE"),
    }
    if stock is not None:
        backtest.get_stock = lambda: stock
        backtest.HAS_KRX_CREDENTIALS = True
    if yfinance is not None:
        backtest.yf = yfinance
    backtest._call_capture_stderr = lambda func, *args, **kwargs: func(*args, **kwargs)
    backtest.START = start
    backtest.END = end
    os.environ["ETF_REFRESH_CACHE"] = "1" if refresh else "0"
    try:
        yield
    finally:
        for name, value in originals.items():
            if name == "ETF_REFRESH_CACHE":
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            else:
                setattr(backtest, name, value)


def write_index_cache(frame: pd.DataFrame) -> None:
    path = Path("data_cache") / "index.parquet"
    path.parent.mkdir(exist_ok=True)
    frame.to_parquet(path, index=False)


def write_us_cache(frame: pd.DataFrame) -> None:
    path = Path("data_cache") / f"us_index_{US_PROXY}.parquet"
    path.parent.mkdir(exist_ok=True)
    frame.to_parquet(path, index=False)


# --------------------------------------------------------------------------- #
# 검증 helpers
# --------------------------------------------------------------------------- #
def assert_sorted_unique(
    frame: pd.DataFrame, label: str, columns: list[str], exact_columns: bool = True
) -> None:
    if frame.empty:
        raise AssertionError(f"{label}: 결과가 비어 있습니다")
    dates = pd.to_datetime(frame["date"])
    if dates.duplicated().any():
        dup = dates[dates.duplicated()].dt.strftime("%Y-%m-%d").tolist()[:3]
        raise AssertionError(f"{label}: 중복 날짜 {len(dup)}건 (예: {dup})")
    if not dates.is_monotonic_increasing:
        raise AssertionError(f"{label}: 날짜가 오름차순이 아닙니다")
    if exact_columns:
        if list(frame.columns) != columns:
            raise AssertionError(f"{label}: 컬럼 스키마 불일치 {list(frame.columns)}")
    else:
        missing = [column for column in columns if column not in frame.columns]
        if missing:
            raise AssertionError(f"{label}: 필수 컬럼 누락 {missing}")


def assert_indicator_match(
    actual: pd.DataFrame,
    expected: pd.DataFrame,
    label: str,
    columns: tuple[str, ...],
    start: str = START,
    end: str = END,
) -> None:
    """요청 구간의 지표·신호가 독립 재계산과 일치하는지 확인한다."""
    left = actual.sort_values("date").reset_index(drop=True)
    right = expected[
        (expected["date"] >= pd.to_datetime(start)) & (expected["date"] <= pd.to_datetime(end))
    ].sort_values("date").reset_index(drop=True)
    if list(pd.to_datetime(left["date"])) != list(pd.to_datetime(right["date"])):
        missing = sorted(set(pd.to_datetime(right["date"])) - set(pd.to_datetime(left["date"])))
        extra = sorted(set(pd.to_datetime(left["date"])) - set(pd.to_datetime(right["date"])))
        raise AssertionError(
            f"{label}: 날짜 집합 불일치 missing={[d.date().isoformat() for d in missing[:3]]} "
            f"extra={[d.date().isoformat() for d in extra[:3]]}"
        )
    if not np.allclose(
        left["close"].to_numpy(dtype=float),
        right["close"].to_numpy(dtype=float),
        rtol=0.0,
        atol=1e-9,
    ):
        raise AssertionError(f"{label}: close 값이 독립 계산과 다릅니다")
    for column in columns:
        if not np.allclose(
            left[column].to_numpy(dtype=float),
            right[column].to_numpy(dtype=float),
            rtol=0.0,
            atol=1e-9,
            equal_nan=True,
        ):
            raise AssertionError(f"{label}: {column} 값이 독립 계산과 다릅니다")


def assert_risk_on_selection(
    frame: pd.DataFrame,
    expected: pd.DataFrame,
    label: str,
    as_of_dates: list[pd.Timestamp],
    signal_column: str = "risk_on",
    risk_fn: Callable[[pd.DataFrame, pd.Timestamp], bool] | None = None,
) -> None:
    """결정일별 risk 판정이 '기준일 이하 마지막 거래일'의 독립 신호와 같은지 확인한다."""
    resolver = risk_fn or backtest.is_risk_on
    exp = expected.sort_values("date").reset_index(drop=True)
    for as_of in as_of_dates:
        rows = exp[exp["date"] <= as_of]
        if rows.empty:
            continue
        last = rows.iloc[-1]
        expected_signal = (
            True
            if pd.isna(last["market_ma"]) or pd.isna(last["market_ma_slope"])
            else bool(last[signal_column])
        )
        actual = resolver(frame, pd.Timestamp(as_of))
        if bool(actual) != expected_signal:
            raise AssertionError(
                f"{label}: {as_of.date()} risk 판정 불일치 actual={actual} expected={expected_signal}"
            )


def assert_no_fetch(calls: list[object], label: str) -> None:
    if calls:
        raise AssertionError(f"{label}: 조회가 발생했습니다 {calls}")


# --------------------------------------------------------------------------- #
# 시나리오
# --------------------------------------------------------------------------- #
def check_kospi_full_cache_sorted() -> None:
    """1. KOSPI 전체 캐시 재사용: 역순·중복 캐시여도 정렬·중복 제거."""
    expected = kospi_expected()
    corrupted = pd.concat(
        [expected.iloc[::-1], expected.iloc[150:151]], ignore_index=True
    )
    stock = FakeKrxStock(expected)
    stock.fail_predicate = lambda *_args: True
    with temp_cwd():
        write_index_cache(corrupted)
        with patched(stock=stock):
            frame = backtest.get_index_data()
        assert_no_fetch(stock.calls, "check_kospi_full_cache_sorted")
        assert_sorted_unique(frame, "check_kospi_full_cache_sorted", INDEX_COLUMNS)
        assert_indicator_match(
            frame, expected, "check_kospi_full_cache_sorted", ("market_ma", "market_ma_slope")
        )
        assert_risk_on_selection(
            frame,
            expected,
            "check_kospi_full_cache_sorted",
            [pd.Timestamp("2024-01-03"), pd.Timestamp("2024-02-29"), pd.Timestamp("2024-03-29")],
        )


def check_kospi_incremental_right() -> None:
    """2a. KOSPI 최신 구간 증분 병합."""
    expected = kospi_expected()
    cache_end = pd.Timestamp("2024-02-29")
    cached = expected[expected["date"] <= cache_end]
    stock = FakeKrxStock(expected)
    with temp_cwd():
        write_index_cache(cached)
        with patched(stock=stock):
            frame = backtest.get_index_data()
        if len(stock.calls) != 1:
            raise AssertionError(f"최신 구간 조회 1회가 필요합니다: {stock.calls}")
        fetch_start, fetch_end, code = stock.calls[0]
        if pd.to_datetime(fetch_start) != cache_end + pd.Timedelta(days=1) or fetch_end != END:
            raise AssertionError(f"증분 조회 구간 오류: {stock.calls[0]}")
        if code != KOSPI_CODE:
            raise AssertionError(f"지수 코드 오류: {code}")
        assert_sorted_unique(frame, "check_kospi_incremental_right", INDEX_COLUMNS)
        assert_indicator_match(
            frame,
            expected,
            "check_kospi_incremental_right",
            ("market_ma", "market_ma_slope"),
        )
        persisted = pd.read_parquet(Path("data_cache") / "index.parquet")
        assert_sorted_unique(
            persisted,
            "check_kospi_incremental_right:persisted",
            INDEX_COLUMNS,
            exact_columns=False,
        )
        if pd.to_datetime(persisted["date"]).max() < pd.Timestamp(END):
            raise AssertionError("캐시 갱신 후 최신 날짜가 요청 종료일보다 이릅니다")


def check_kospi_incremental_left() -> None:
    """2b. KOSPI 과거 구간 증분 병합 (조회 구간/정합성만 확인, 워밍업 지표는 캐시 시작부터)."""
    expected = kospi_expected()
    cache_start = pd.Timestamp("2023-12-01")
    cached = expected[expected["date"] >= cache_start]
    stock = FakeKrxStock(expected)
    request_start = "20231101"
    with temp_cwd():
        write_index_cache(cached)
        with patched(stock=stock, start=request_start):
            frame = backtest.get_index_data()
        if not stock.calls:
            raise AssertionError("과거 구간 증분 조회가 없습니다")
        fetch_start, fetch_end, _code = stock.calls[0]
        if fetch_start != request_start:
            raise AssertionError(f"과거 구간 시작일 오류: {fetch_start}")
        if pd.to_datetime(fetch_end) != cache_start - pd.Timedelta(days=1):
            raise AssertionError(f"과거 구간 종료일 오류: {fetch_end}")
        assert_sorted_unique(frame, "check_kospi_incremental_left", INDEX_COLUMNS)
        left = expected[
            (expected["date"] >= pd.to_datetime(request_start))
            & (expected["date"] <= pd.Timestamp(END))
        ]
        if not np.allclose(
            frame["close"].to_numpy(dtype=float),
            left["close"].to_numpy(dtype=float),
            rtol=0.0,
            atol=1e-9,
        ):
            raise AssertionError("과거 구간 병합 결과의 close가 원천과 다릅니다")


def _cache_bytes() -> bytes:
    return (Path("data_cache") / "index.parquet").read_bytes()


def check_kospi_partial_side_failure_left() -> None:
    """left 조회 실패 + right 성공이면 부분 병합을 반환하지 않고 전체 재조회로 복구한다."""
    expected = kospi_expected()
    cached = expected[
        (expected["date"] >= pd.Timestamp("2023-12-01"))
        & (expected["date"] <= pd.Timestamp("2024-02-29"))
    ]
    stock = FakeKrxStock(expected)
    stock.fail_predicate = lambda _start, end: pd.to_datetime(end) < pd.Timestamp("2023-12-01")
    with temp_cwd():
        write_index_cache(cached)
        with patched(stock=stock, start="20230901"):
            frame = backtest.get_index_data()
        if not any(call[0] == "20230901" and call[1] == END for call in stock.calls):
            raise AssertionError(f"전체 재조회로 복구되지 않았습니다: {stock.calls}")
        assert_sorted_unique(frame, "check_kospi_partial_side_failure_left", INDEX_COLUMNS)
        # 전체 재조회 경로는 요청 구간만 내려받아 지표를 계산한다(기존 동작).
        assert_indicator_match(
            frame,
            kospi_expected_window(start="20230901"),
            "check_kospi_partial_side_failure_left",
            ("market_ma", "market_ma_slope"),
            start="20230901",
        )


def check_kospi_partial_side_failure_right() -> None:
    """left 성공 + right 조회 실패여도 부분 병합을 반환하지 않는다."""
    expected = kospi_expected()
    cached = expected[
        (expected["date"] >= pd.Timestamp("2023-12-01"))
        & (expected["date"] <= pd.Timestamp("2024-02-29"))
    ]
    stock = FakeKrxStock(expected)
    stock.fail_predicate = lambda start, _end: pd.to_datetime(start) > pd.Timestamp("2024-02-29")
    with temp_cwd():
        write_index_cache(cached)
        with patched(stock=stock, start="20230901"):
            frame = backtest.get_index_data()
        if not any(call[0] == "20230901" and call[1] == END for call in stock.calls):
            raise AssertionError(f"전체 재조회로 복구되지 않았습니다: {stock.calls}")
        assert_sorted_unique(frame, "check_kospi_partial_side_failure_right", INDEX_COLUMNS)
        assert_indicator_match(
            frame,
            kospi_expected_window(start="20230901"),
            "check_kospi_partial_side_failure_right",
            ("market_ma", "market_ma_slope"),
            start="20230901",
        )


def check_kospi_all_sides_failure_raises() -> None:
    """양쪽 증분과 전체 재조회가 모두 실패하면 예외로 전파하고 캐시를 보존한다."""
    expected = kospi_expected()
    cached = expected[
        (expected["date"] >= pd.Timestamp("2023-12-01"))
        & (expected["date"] <= pd.Timestamp("2024-02-29"))
    ]
    stock = FakeKrxStock(expected)
    stock.fail_predicate = lambda *_args: True
    with temp_cwd():
        write_index_cache(cached)
        before = _cache_bytes()
        with patched(stock=stock, start="20230901"):
            try:
                backtest.get_index_data()
            except RuntimeError:
                pass
            else:
                raise AssertionError("전체 재조회 실패가 예외로 전파되지 않았습니다")
        if _cache_bytes() != before:
            raise AssertionError("실패 후 기존 캐시가 덮어써졌습니다")


def check_kospi_partial_failure_does_not_overwrite() -> None:
    """한쪽 조회 실패 후 전체 재조회까지 실패하면 부분 결과를 반환/저장하지 않는다."""
    expected = kospi_expected()
    cached = expected[
        (expected["date"] >= pd.Timestamp("2023-12-01"))
        & (expected["date"] <= pd.Timestamp("2024-02-29"))
    ]
    stock = FakeKrxStock(expected)
    stock.fail_predicate = lambda start, end: (
        pd.to_datetime(end) < pd.Timestamp("2023-12-01")
        or (start == "20230901" and end == END)
    )
    with temp_cwd():
        write_index_cache(cached)
        before = _cache_bytes()
        with patched(stock=stock, start="20230901"):
            try:
                backtest.get_index_data()
            except RuntimeError:
                pass
            else:
                raise AssertionError("부분 결과가 정상 반환되었습니다")
        if _cache_bytes() != before:
            raise AssertionError("부분 병합 결과로 기존 캐시가 덮어써졌습니다")


def check_kospi_incremental_failure() -> None:
    """3. KOSPI 증분 조회 실패 시 부분 캐시를 정상 결과로 반환하지 않는다."""
    expected = kospi_expected()
    cached = expected[expected["date"] <= pd.Timestamp("2024-02-29")]
    stock = FakeKrxStock(expected)
    stock.fail_predicate = lambda start, _end: pd.to_datetime(start) > pd.Timestamp("2024-02-29")
    with temp_cwd():
        write_index_cache(cached)
        with patched(stock=stock):
            frame = backtest.get_index_data()
        if not any(call[0] == START and call[1] == END for call in stock.calls):
            raise AssertionError(f"실패 후 전체 재조회가 없습니다: {stock.calls}")
        assert_sorted_unique(frame, "check_kospi_incremental_failure", INDEX_COLUMNS)
        if pd.to_datetime(frame["date"]).max() < pd.Timestamp(END):
            raise AssertionError("부분 데이터가 정상 결과로 반환되었습니다")


def check_kospi_cache_equals_refetch() -> None:
    """6. KOSPI 캐시 재사용과 전체 재조회 결과 동일 (캐시는 로더가 생성)."""
    expected = kospi_expected()
    stock = FakeKrxStock(expected)
    with temp_cwd():
        with patched(stock=stock, refresh=True):
            refetch_frame = backtest.get_index_data()
        stock.calls.clear()
        with patched(stock=stock):
            cached_frame = backtest.get_index_data()
        assert_no_fetch(stock.calls, "check_kospi_cache_equals_refetch")
        assert_sorted_unique(cached_frame, "check_kospi_cache_equals_refetch:cached", INDEX_COLUMNS)
        assert_sorted_unique(
            refetch_frame, "check_kospi_cache_equals_refetch:refetch", INDEX_COLUMNS
        )
        # 전체 재조회 경로는 요청 구간만 내려받으므로 구간 기준 독립 계산과 대조한다.
        assert_indicator_match(
            refetch_frame,
            kospi_expected_window(),
            "check_kospi_cache_equals_refetch:refetch",
            ("market_ma", "market_ma_slope"),
        )
        for column in ("close", "market_ma", "market_ma_slope"):
            if not np.allclose(
                cached_frame[column].to_numpy(dtype=float),
                refetch_frame[column].to_numpy(dtype=float),
                rtol=0.0,
                atol=1e-9,
                equal_nan=True,
            ):
                raise AssertionError(f"캐시/재조회 불일치: {column}")
        if not cached_frame["risk_on"].astype(bool).equals(refetch_frame["risk_on"].astype(bool)):
            raise AssertionError("캐시/재조회 risk_on 불일치")


def check_us_partial_cache_not_complete() -> None:
    """4. 미국 지수 부분 캐시를 요청 기간 전체 커버로 오인하지 않는다."""
    expected = us_expected()
    partial = expected[expected["date"] <= pd.Timestamp("2024-02-15")]
    yfinance = FakeYFinance(expected)
    with temp_cwd():
        write_us_cache(partial)
        with patched(yfinance=yfinance):
            frame = backtest.get_us_index_data()
        if not yfinance.calls:
            raise AssertionError("부분 캐시인데 재조회가 없었습니다")
        assert_sorted_unique(frame, "check_us_partial_cache_not_complete", US_COLUMNS)
        if pd.to_datetime(frame["date"]).max() < pd.Timestamp(END):
            raise AssertionError(
                f"부분 캐시가 전체 커버로 반환되었습니다 (max={frame['date'].max().date()})"
            )
        # 부분 캐시의 기존 이력을 병합하므로 지표는 전체 이력 기준으로 계산된다.
        assert_indicator_match(
            frame,
            expected,
            "check_us_partial_cache_not_complete",
            ("us_market_ma", "us_market_ma_slope"),
        )
        if frame["us_market_ma"].isna().all():
            raise AssertionError("재조회 후에도 MA가 전부 NaN입니다(워밍업 이력 소실)")


def check_us_holiday_edges_reuse_cache() -> None:
    """미국 휴장 경계에서도 지표·신호가 열화되지 않는다(재조회 시 이력 병합)."""
    us_dates = US_DATES[US_DATES != pd.Timestamp("2024-03-29")]  # Good Friday(미국 휴장)
    source = build_close_series(us_dates, 1.9, 400.0)
    expected = independent_indicators(
        source,
        "us_market_ma",
        "us_market_ma_slope",
        "us_risk_on",
        ma_days=backtest.US_MARKET_MA_DAYS,
        slope_days=backtest.US_MARKET_SLOPE_DAYS,
    )
    # 과거에 더 긴 구간을 조회해 워밍업 이력이 있는 캐시 (실제 운영에서 발생 가능한 상태)
    cache = expected[expected["date"] <= pd.Timestamp("2024-03-28")]
    yfinance = FakeYFinance(source)
    with temp_cwd():
        write_us_cache(cache)
        with patched(yfinance=yfinance, start="20240101", end="20240329"):
            frame = backtest.get_us_index_data()
        # 휴장일(Good Friday) 종료 구간은 재조회될 수 있다. 중요한 것은 재조회가
        # 기존 이력을 병합해 신호를 열화시키지 않는 것이다.
        if not yfinance.calls:
            print("  (참고) 휴장 경계에서 재조회 없음")
        assert_sorted_unique(frame, "check_us_holiday_edges_reuse_cache", US_COLUMNS)
        last = frame.sort_values("date").iloc[-1]
        if pd.isna(last["us_market_ma"]):
            raise AssertionError("휴장 경계 캐시 재사용에서 MA가 NaN입니다")
        last_expected = expected[expected["date"] == pd.Timestamp("2024-03-28")].iloc[-1]
        if not np.isclose(
            float(last["us_market_ma"]), float(last_expected["us_market_ma"]), rtol=0.0, atol=1e-9
        ):
            raise AssertionError("휴장 경계 캐시 재사용의 MA가 독립 계산과 다릅니다")
        expected_signal = bool(last_expected["us_risk_on"])
        actual_signal = backtest.is_us_risk_on(frame, pd.Timestamp("2024-03-29"))
        if bool(actual_signal) != expected_signal:
            raise AssertionError(
                f"휴장 경계 risk 판정 불일치 actual={actual_signal} expected={expected_signal}"
            )


def check_us_refetch_preserves_history() -> None:
    """부분 캐시 재조회가 기존 이력을 보존해 지표·신호를 열화시키지 않는다."""
    expected = us_expected()
    stale = expected[expected["date"] <= pd.Timestamp("2024-02-15")]
    yfinance = FakeYFinance(expected)
    with temp_cwd():
        write_us_cache(stale)
        with patched(yfinance=yfinance, start="20230102"):
            frame = backtest.get_us_index_data()
        if not yfinance.calls:
            raise AssertionError("부분 캐시인데 재조회가 없었습니다")
        assert_sorted_unique(frame, "check_us_refetch_preserves_history", US_COLUMNS)
        assert_indicator_match(
            frame,
            expected,
            "check_us_refetch_preserves_history",
            ("us_market_ma", "us_market_ma_slope"),
            start="20230102",
        )
        yfinance.calls.clear()
        yfinance.fail_predicate = lambda *_args: True
        with patched(yfinance=yfinance, start="20230102"):
            again = backtest.get_us_index_data()
        assert_no_fetch(yfinance.calls, "check_us_refetch_preserves_history:repeat")
        for column in ("close", "us_market_ma", "us_market_ma_slope"):
            if not np.allclose(
                frame[column].to_numpy(dtype=float),
                again[column].to_numpy(dtype=float),
                rtol=0.0,
                atol=1e-9,
                equal_nan=True,
            ):
                raise AssertionError(f"재조회 후 재사용 결과 불일치: {column}")


def check_us_recent_gap_refetches() -> None:
    """실제 거래일 누락(휴장 아님)은 캐시로 인정하지 않고 재조회해 최신 신호를 쓴다."""
    dates = pd.DatetimeIndex(
        [d for d in pd.bdate_range("2023-01-02", "2024-04-02") if d != US_MISSING]
    )
    source = build_close_series(dates, 1.9, 400.0)
    source.loc[source.index[-1], "close"] = source.loc[source.index[-1], "close"] * 0.90
    expected = independent_indicators(
        source,
        "us_market_ma",
        "us_market_ma_slope",
        "us_risk_on",
        ma_days=backtest.US_MARKET_MA_DAYS,
        slope_days=backtest.US_MARKET_SLOPE_DAYS,
    )
    if not bool(expected.iloc[-2]["us_risk_on"]):
        raise AssertionError("fixture 오류: 급락 직전일 신호가 True여야 합니다")
    if bool(expected.iloc[-1]["us_risk_on"]):
        raise AssertionError("fixture 오류: 급락일 신호가 False여야 합니다")
    stale = expected[expected["date"] <= pd.Timestamp("2024-04-01")]
    yfinance = FakeYFinance(source)
    with temp_cwd():
        write_us_cache(stale)
        with patched(yfinance=yfinance, end="20240402"):
            frame = backtest.get_us_index_data()
        if not yfinance.calls:
            raise AssertionError("1거래일 누락인데 재조회가 없었습니다")
        assert_sorted_unique(frame, "check_us_recent_gap_refetches", US_COLUMNS)
        assert_indicator_match(
            frame,
            expected,
            "check_us_recent_gap_refetches",
            ("us_market_ma", "us_market_ma_slope"),
            end="20240402",
        )
        if pd.to_datetime(frame["date"]).max() != pd.Timestamp("2024-04-02"):
            raise AssertionError("재조회 후에도 최신 거래일이 반영되지 않았습니다")
        yfinance.calls.clear()
        yfinance.fail_predicate = lambda *_args: True
        with patched(yfinance=yfinance, end="20240402"):
            again = backtest.get_us_index_data()
        assert_no_fetch(yfinance.calls, "check_us_recent_gap_refetches:repeat")
        for column in ("close", "us_market_ma", "us_market_ma_slope"):
            if not np.allclose(
                frame[column].to_numpy(dtype=float),
                again[column].to_numpy(dtype=float),
                rtol=0.0,
                atol=1e-9,
                equal_nan=True,
            ):
                raise AssertionError(f"재조회 후 재사용 결과 불일치: {column}")


def check_us_merge_failure_keeps_cache() -> None:
    """병합 실패 시 짧은 재조회 결과로 캐시를 교체하지 않는다(워밍업 소실 방지)."""
    expected = us_expected()
    broken = expected[expected["date"] <= pd.Timestamp("2024-02-15")][
        ["date", "us_market_ma"]
    ].copy()
    yfinance = FakeYFinance(expected)
    with temp_cwd():
        path = Path("data_cache") / f"us_index_{US_PROXY}.parquet"
        path.parent.mkdir(exist_ok=True)
        broken.to_parquet(path, index=False)
        with patched(yfinance=yfinance):
            frame = backtest.get_us_index_data()
        if not yfinance.calls:
            raise AssertionError("부분 캐시인데 재조회가 없었습니다")
        if frame.empty:
            raise AssertionError("병합 실패 시 결과가 비어 있습니다")
        persisted = pd.read_parquet(path)
        if "close" in persisted.columns:
            raise AssertionError("병합 실패 후 짧은 결과로 캐시가 교체되었습니다")


def check_us_force_refresh_preserves_history() -> None:
    """전체 재조회(force refresh)도 기존 이력을 병합해 신호를 유지한다."""
    expected = us_expected()
    yfinance = FakeYFinance(expected)
    with temp_cwd():
        write_us_cache(expected)
        with patched(yfinance=yfinance, refresh=True):
            frame = backtest.get_us_index_data()
        if not yfinance.calls:
            raise AssertionError("강제 갱신인데 조회가 없습니다")
        assert_sorted_unique(frame, "check_us_force_refresh_preserves_history", US_COLUMNS)
        assert_indicator_match(
            frame,
            expected,
            "check_us_force_refresh_preserves_history",
            ("us_market_ma", "us_market_ma_slope"),
        )
        if frame["us_market_ma"].isna().all():
            raise AssertionError("강제 갱신 후 MA가 전부 NaN입니다(워밍업 소실)")
        persisted = pd.read_parquet(Path("data_cache") / f"us_index_{US_PROXY}.parquet")
        if pd.to_datetime(persisted["date"]).min() > pd.Timestamp("2023-01-05"):
            raise AssertionError("강제 갱신 후 캐시 이력이 축소되었습니다")


def check_us_merge_fills_unfetched_gap() -> None:
    """캐시와 요청 구간 사이의 미조회 구간을 남기지 않는다(내부 결손 방지)."""
    expected = us_expected()
    cached = expected[expected["date"] <= pd.Timestamp("2024-02-15")]
    yfinance = FakeYFinance(expected)
    with temp_cwd():
        write_us_cache(cached)
        # 요청 구간(03-01~03-29)만 조회하면 02-16~02-29가 영구 미조회로 남는다.
        with patched(yfinance=yfinance, start="20240301"):
            frame = backtest.get_us_index_data()
        if not yfinance.calls:
            raise AssertionError("재조회가 없습니다")
        call_start = pd.to_datetime(yfinance.calls[-1][1])
        if call_start > pd.Timestamp("2023-01-05"):
            raise AssertionError(
                f"미조회 구간이 남도록 조회했습니다: start={call_start.date()} calls={yfinance.calls}"
            )
        assert_sorted_unique(frame, "check_us_merge_fills_unfetched_gap", US_COLUMNS)
        assert_indicator_match(
            frame,
            expected,
            "check_us_merge_fills_unfetched_gap",
            ("us_market_ma", "us_market_ma_slope"),
            start="20240301",
        )
        persisted = pd.read_parquet(Path("data_cache") / f"us_index_{US_PROXY}.parquet")
        gap = persisted[
            (pd.to_datetime(persisted["date"]) >= pd.Timestamp("2024-02-16"))
            & (pd.to_datetime(persisted["date"]) <= pd.Timestamp("2024-02-29"))
        ]
        if gap.empty:
            raise AssertionError("미조회 구간(02-16~02-29)이 캐시에 채워지지 않았습니다")


def check_us_backward_request_fills_gap() -> None:
    """캐시보다 이른 구간을 요청해도 병합 후 내부 결손이 남지 않는다(역방향)."""
    dates = pd.bdate_range("2022-01-03", "2024-03-29")
    source = build_close_series(dates, 1.9, 400.0)
    expected = independent_indicators(
        source,
        "us_market_ma",
        "us_market_ma_slope",
        "us_risk_on",
        ma_days=backtest.US_MARKET_MA_DAYS,
        slope_days=backtest.US_MARKET_SLOPE_DAYS,
    )
    cache = expected[expected["date"] >= pd.Timestamp("2024-01-02")]
    yfinance = FakeYFinance(expected)
    with temp_cwd():
        write_us_cache(cache)
        with patched(yfinance=yfinance, start="20220103", end="20221229"):
            backtest.get_us_index_data()
        if not yfinance.calls:
            raise AssertionError("재조회가 없습니다")
        yfinance.calls.clear()
        yfinance.fail_predicate = lambda *_args: True
        with patched(yfinance=yfinance, start="20230102"):
            frame = backtest.get_us_index_data()
        assert_no_fetch(yfinance.calls, "check_us_backward_request_fills_gap")
        assert_sorted_unique(frame, "check_us_backward_request_fills_gap", US_COLUMNS)
        assert_indicator_match(
            frame,
            expected,
            "check_us_backward_request_fills_gap",
            ("us_market_ma", "us_market_ma_slope"),
            start="20230102",
        )


def check_us_corrupt_cache_missing_date() -> None:
    """date 컬럼이 없는 캐시는 예외 없이 신규 조회로 대체되고 덮어쓰지 않는다."""
    expected = us_expected()
    broken = expected[["close", "us_market_ma", "us_market_ma_slope"]].copy()
    path = Path("data_cache") / f"us_index_{US_PROXY}.parquet"
    yfinance = FakeYFinance(expected)
    with temp_cwd():
        path.parent.mkdir(exist_ok=True)
        broken.to_parquet(path, index=False)
        before = path.read_bytes()
        with patched(yfinance=yfinance):
            frame = backtest.get_us_index_data()
        if not yfinance.calls:
            raise AssertionError("손상 캐시인데 신규 조회가 없습니다")
        assert_sorted_unique(frame, "check_us_corrupt_cache_missing_date", US_COLUMNS)
        if pd.to_datetime(frame["date"]).max() < pd.Timestamp(END):
            raise AssertionError("신규 조회 결과가 요청 종료일까지 차지 않습니다")
        if path.read_bytes() != before:
            raise AssertionError("손상 캐시가 덮어써졌습니다")


def check_us_corrupt_cache_invalid_dates() -> None:
    """날짜 값이 깨진 캐시도 예외 없이 신규 조회로 대체된다."""
    expected = us_expected()
    broken = expected[["date", "close"]].head(30).copy()
    broken["date"] = ["not-a-date"] * len(broken)
    path = Path("data_cache") / f"us_index_{US_PROXY}.parquet"
    yfinance = FakeYFinance(expected)
    with temp_cwd():
        path.parent.mkdir(exist_ok=True)
        broken.to_parquet(path, index=False)
        before = path.read_bytes()
        with patched(yfinance=yfinance):
            frame = backtest.get_us_index_data()
        if not yfinance.calls:
            raise AssertionError("손상 캐시인데 신규 조회가 없습니다")
        if path.read_bytes() != before:
            raise AssertionError("손상 캐시가 덮어써졌습니다")
        assert_sorted_unique(frame, "check_us_corrupt_cache_invalid_dates", US_COLUMNS)
        assert_indicator_match(
            frame,
            us_expected_window(),
            "check_us_corrupt_cache_invalid_dates",
            ("us_market_ma", "us_market_ma_slope"),
        )


def check_us_weekend_end_reuses_cache() -> None:
    """주말 종료일은 거래일 갭이 아니므로 재조회 없이 캐시를 재사용한다(과잉 재조회 방지)."""
    expected = us_expected()
    friday = pd.Timestamp("2024-03-29")
    yfinance = FakeYFinance(expected)
    yfinance.fail_predicate = lambda *_args: True
    with temp_cwd():
        write_us_cache(expected[expected["date"] <= friday])
        with patched(yfinance=yfinance, end="20240330"):
            frame = backtest.get_us_index_data()
        assert_no_fetch(yfinance.calls, "check_us_weekend_end_reuses_cache")
        assert_sorted_unique(frame, "check_us_weekend_end_reuses_cache", US_COLUMNS)
        if pd.to_datetime(frame["date"]).max() != friday:
            raise AssertionError("주말 종료일 캐시 재사용 결과의 마지막 거래일이 다릅니다")


def check_us_full_cache_sorted() -> None:
    """1b. 미국 전체 캐시 재사용: 역순·중복 캐시여도 정렬·중복 제거."""
    expected = us_expected()
    corrupted = pd.concat(
        [expected.iloc[::-1], expected.iloc[120:121]], ignore_index=True
    )
    yfinance = FakeYFinance(expected)
    yfinance.fail_predicate = lambda *_args: True
    with temp_cwd():
        write_us_cache(corrupted)
        with patched(yfinance=yfinance):
            frame = backtest.get_us_index_data()
        assert_no_fetch(yfinance.calls, "check_us_full_cache_sorted")
        assert_sorted_unique(frame, "check_us_full_cache_sorted", US_COLUMNS)
        assert_indicator_match(
            frame, expected, "check_us_full_cache_sorted", ("us_market_ma", "us_market_ma_slope")
        )


def check_us_cache_equals_refetch_and_dates() -> None:
    """5·6. 미국 지표·risk_on 일치, 캐시/재조회 동일, 한·미 거래일 차이 처리."""
    expected = us_expected()
    window = us_expected_window()
    with temp_cwd():
        yfinance = FakeYFinance(expected)
        with patched(yfinance=yfinance, refresh=True):
            refetch_frame = backtest.get_us_index_data()
        yfinance.calls.clear()
        yfinance.fail_predicate = lambda *_args: True
        with patched(yfinance=yfinance):
            cached_frame = backtest.get_us_index_data()
        assert_no_fetch(yfinance.calls, "check_us_cache_equals_refetch")
        assert_sorted_unique(cached_frame, "check_us_cache_equals_refetch:cached", US_COLUMNS)
        assert_indicator_match(
            cached_frame,
            window,
            "check_us_cache_equals_refetch",
            ("us_market_ma", "us_market_ma_slope"),
        )
        for column in ("close", "us_market_ma", "us_market_ma_slope"):
            if not np.allclose(
                cached_frame[column].to_numpy(dtype=float),
                refetch_frame[column].to_numpy(dtype=float),
                rtol=0.0,
                atol=1e-9,
                equal_nan=True,
            ):
                raise AssertionError(f"캐시/재조회 불일치: {column}")
        exp = window.sort_values("date").reset_index(drop=True)
        for as_of in [US_MISSING, pd.Timestamp("2024-02-29"), pd.Timestamp("2024-03-29")]:
            rows = exp[exp["date"] <= as_of]
            last = rows.iloc[-1]
            expected_signal = (
                True
                if pd.isna(last["us_market_ma"]) or pd.isna(last["us_market_ma_slope"])
                else bool(last["us_risk_on"])
            )
            actual = backtest.is_us_risk_on(cached_frame, pd.Timestamp(as_of))
            if bool(actual) != expected_signal:
                raise AssertionError(
                    f"is_us_risk_on 불일치 {as_of.date()}: actual={actual} expected={expected_signal}"
                )


def check_us_empty_fallback_unchanged() -> None:
    """미국 데이터가 비었을 때의 기존 fallback(True) 정책을 그대로 확인한다."""
    yfinance = FakeYFinance(pd.DataFrame({"date": pd.to_datetime([]), "close": []}))
    with temp_cwd():
        with patched(yfinance=yfinance, refresh=True):
            frame = backtest.get_us_index_data()
        if not frame.empty:
            raise AssertionError("빈 미국 데이터인데 결과가 비어있지 않습니다")
        if backtest.is_us_risk_on(frame, pd.Timestamp("2024-02-29")) is not True:
            raise AssertionError("빈 미국 데이터의 risk-on fallback 정책이 바뀌었습니다")


def main() -> int:
    checks: list[tuple[str, Callable[[], None]]] = [
        ("1. KOSPI 전체 캐시 정렬/중복", check_kospi_full_cache_sorted),
        ("2a. KOSPI 최신 구간 증분 병합", check_kospi_incremental_right),
        ("2b. KOSPI 과거 구간 증분 병합", check_kospi_incremental_left),
        ("3. KOSPI 증분 실패 fallback", check_kospi_incremental_failure),
        ("3b. KOSPI left 실패 + right 성공", check_kospi_partial_side_failure_left),
        ("3c. KOSPI left 성공 + right 실패", check_kospi_partial_side_failure_right),
        ("3d. KOSPI 양쪽+전체 실패 예외", check_kospi_all_sides_failure_raises),
        ("3e. KOSPI 부분 실패 캐시 보존", check_kospi_partial_failure_does_not_overwrite),
        ("4. 미국 부분 캐시 coverage", check_us_partial_cache_not_complete),
        ("1b. 미국 전체 캐시 정렬/중복", check_us_full_cache_sorted),
        ("4b. 미국 주말 종료일 캐시 재사용", check_us_weekend_end_reuses_cache),
        ("4c. 미국 휴장 경계 캐시 재사용", check_us_holiday_edges_reuse_cache),
        ("4c-1. 미국 캐시 date 컬럼 누락", check_us_corrupt_cache_missing_date),
        ("4c-2. 미국 캐시 날짜값 손상", check_us_corrupt_cache_invalid_dates),
        ("4d. 미국 재조회 시 이력 보존", check_us_refetch_preserves_history),
        ("4e. 미국 최근 거래일 누락 재조회", check_us_recent_gap_refetches),
        ("4f. 미국 병합 실패 시 캐시 보존", check_us_merge_failure_keeps_cache),
        ("4g. 미국 강제 갱신 이력 보존", check_us_force_refresh_preserves_history),
        ("4h. 미국 미조회 구간 병합", check_us_merge_fills_unfetched_gap),
        ("4i. 미국 역방향 구간 병합", check_us_backward_request_fills_gap),
        ("5/6. 미국 지표·risk_on·캐시=재조회", check_us_cache_equals_refetch_and_dates),
        ("KOSPI 캐시=재조회", check_kospi_cache_equals_refetch),
        ("미국 빈 데이터 fallback 정책", check_us_empty_fallback_unchanged),
    ]
    failures = 0
    for label, check in checks:
        try:
            check()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {label}: {exc}")
        except Exception as exc:  # noqa: BLE001 - 표면화해서 실패로 집계
            failures += 1
            print(f"FAIL {label}: 예상치 못한 예외 {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {label}")
    if failures:
        print(f"\nindex cache integrity checks FAILED ({failures}건)")
        return 1
    print("\nindex cache integrity checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
