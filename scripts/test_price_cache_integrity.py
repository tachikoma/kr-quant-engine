"""P0-A-2: 가격 데이터 캐시 무결성 검증.

``get_price()``를 실제로 호출하되 외부 데이터 조회(pykrx)만 fixture로 대체한다.
Parquet 캐시 재사용·증분 병합·정렬/중복·경계·재조회 일치·실패 시 불완전 데이터
처리를 검사한다.

검사 항목:
1. 정상 Parquet 캐시 전체 재사용 (조회 없음)
2. 과거·최신 구간 증분 병합 (병합 결과 = 원천 전체, 캐시 파일 갱신)
3. 역순·중복 날짜 캐시 → 출력은 오름차순·중복 없음
4. 시작일·종료일·상장일 경계 (조회 구간이 상장일/종료일로 제한)
5. 캐시 갱신 후 재조회 결과 일치 (무조회)
6. 증분 조회 실패 시 완전 재조회 fallback / 내부 날짜 누락은 임의 보간 없이 보존

캐시는 임시 디렉터리에서만 생성한다. 네트워크·실거래 API·운영 산출물을 쓰지 않는다.

실행::

    uv run python scripts/test_price_cache_integrity.py
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
_PINNED_ENV = {
    "ETF_USE_CACHE": "1",
    "ETF_REFRESH_CACHE": "0",
    "ETF_RETURN_BASIS": "price",
}
os.environ.update(_PINNED_ENV)
os.environ["KRX_ID"] = ""
os.environ["KRX_PW"] = ""

import run_etf_backtest as backtest

TICKER = "000001"
START = "20240102"
END = "20240329"
DATES_ALL = pd.bdate_range("2024-01-02", "2024-03-29")
# 내부 누락일: 휴장일/거래정지처럼 원천 자체에 없는 날. 임의 보간하면 안 된다.
GAP_DATE = pd.Timestamp("2024-02-15")
COLUMNS = [
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


# --------------------------------------------------------------------------- #
# fixture
# --------------------------------------------------------------------------- #
def build_source_frame(dates: pd.DatetimeIndex) -> pd.DataFrame:
    """원천(pykrx) 응답을 모사한 정규화 프레임. 값은 결정적으로 만든다."""
    index = np.arange(len(dates), dtype=float)
    close = 10_000.0 + 25.0 * index + 40.0 * np.sin(index / 3.0)
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "ticker": TICKER,
            "open": close - 5.0,
            "high": close + 12.0,
            "low": close - 12.0,
            "close": close,
            "volume": (1_000 + 10 * index).astype(np.int64),
            "trading_value": close * (1_000 + 10 * index),
            "nav": np.nan,
            "base_index": np.nan,
        }
    )
    return frame[COLUMNS]


class FakeUpstream:
    """``fetch_etf_ohlcv_with_nav`` 대역. 호출 구간을 기록하고 실패를 주입한다."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.calls: list[tuple[str, str]] = []
        self.fail_predicate: Callable[[str, str], bool] | None = None

    def fetch(self, start: str, end: str, ticker: str) -> pd.DataFrame:
        self.calls.append((start, end))
        if self.fail_predicate is not None and self.fail_predicate(start, end):
            raise RuntimeError("simulated upstream failure")
        if ticker != TICKER:
            return self.frame.iloc[0:0].copy()
        s = pd.to_datetime(start)
        e = pd.to_datetime(end)
        return self.frame[(self.frame["date"] >= s) & (self.frame["date"] <= e)].copy()


def write_cache(frame: pd.DataFrame) -> Path:
    path = Path("data_cache") / f"{TICKER}.parquet"
    path.parent.mkdir(exist_ok=True)
    frame.to_parquet(path, index=False)
    return path


def read_cache() -> pd.DataFrame:
    return pd.read_parquet(Path("data_cache") / f"{TICKER}.parquet")


@contextmanager
def temp_cwd() -> Iterator[Path]:
    """``get_price``는 cwd 기준 ``data_cache``를 쓰므로 임시 디렉터리로 이동한다."""
    previous = Path.cwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            yield Path(tmp)
        finally:
            os.chdir(previous)


@contextmanager
def patched_price_io(upstream: FakeUpstream, start: str = START, end: str = END):
    originals = {
        "fetch_etf_ohlcv_with_nav": backtest.fetch_etf_ohlcv_with_nav,
        "_call_capture_stderr": backtest._call_capture_stderr,
        "START": backtest.START,
        "END": backtest.END,
    }
    backtest.fetch_etf_ohlcv_with_nav = upstream.fetch
    backtest._call_capture_stderr = lambda func, *args, **kwargs: func(*args, **kwargs)
    backtest.START = start
    backtest.END = end
    try:
        yield
    finally:
        for name, value in originals.items():
            setattr(backtest, name, value)


# --------------------------------------------------------------------------- #
# 검증 helpers
# --------------------------------------------------------------------------- #
def assert_sorted_unique(frame: pd.DataFrame, label: str) -> None:
    if frame.empty:
        raise AssertionError(f"{label}: 결과가 비어 있습니다")
    dates = pd.to_datetime(frame["date"])
    if dates.duplicated().any():
        dup = dates[dates.duplicated()].dt.strftime("%Y-%m-%d").tolist()[:3]
        raise AssertionError(f"{label}: 중복 날짜 {len(dup)}건 (예: {dup})")
    if not dates.is_monotonic_increasing:
        raise AssertionError(f"{label}: 날짜가 오름차순이 아닙니다")
    if list(frame.columns) != COLUMNS:
        raise AssertionError(f"{label}: 컬럼 스키마 불일치 {list(frame.columns)}")


def assert_matches_source(
    frame: pd.DataFrame,
    source: pd.DataFrame,
    label: str,
    start: str = START,
    end: str = END,
) -> None:
    """요청 기간에 대해 원천과 행/값이 정확히 일치하는지 확인한다."""
    expected = source[
        (source["date"] >= pd.to_datetime(start)) & (source["date"] <= pd.to_datetime(end))
    ].reset_index(drop=True)
    actual = frame.reset_index(drop=True)
    if len(actual) != len(expected):
        missing = sorted(
            set(pd.to_datetime(expected["date"])) - set(pd.to_datetime(actual["date"]))
        )
        extra = sorted(
            set(pd.to_datetime(actual["date"])) - set(pd.to_datetime(expected["date"]))
        )
        raise AssertionError(
            f"{label}: 행 수 불일치 actual={len(actual)} expected={len(expected)} "
            f"missing={[d.date().isoformat() for d in missing[:3]]} "
            f"extra={[d.date().isoformat() for d in extra[:3]]}"
        )
    for column in ("open", "high", "low", "close", "volume", "trading_value"):
        if not np.allclose(
            actual[column].to_numpy(dtype=float),
            expected[column].to_numpy(dtype=float),
            rtol=0.0,
            atol=1e-9,
        ):
            raise AssertionError(f"{label}: {column} 값이 원천과 다릅니다")
    for column in ("nav", "base_index"):
        if not np.allclose(
            actual[column].to_numpy(dtype=float),
            expected[column].to_numpy(dtype=float),
            rtol=0.0,
            atol=1e-9,
            equal_nan=True,
        ):
            raise AssertionError(f"{label}: {column} 값이 원천과 다릅니다")
    if not actual["ticker"].astype(str).equals(expected["ticker"].astype(str)):
        raise AssertionError(f"{label}: ticker 값이 원천과 다릅니다")
    if list(pd.to_datetime(actual["date"])) != list(pd.to_datetime(expected["date"])):
        raise AssertionError(f"{label}: 날짜 집합이 원천과 다릅니다")


def assert_no_fetch(upstream: FakeUpstream, label: str) -> None:
    if upstream.calls:
        raise AssertionError(f"{label}: 캐시 재사용인데 조회가 발생했습니다 {upstream.calls}")


# --------------------------------------------------------------------------- #
# 시나리오
# --------------------------------------------------------------------------- #
def check_full_cache_reuse() -> None:
    """1. 정상 캐시 전체 재사용."""
    source = build_source_frame(DATES_ALL)
    upstream = FakeUpstream(source)

    def fail(*_args: object) -> None:
        raise AssertionError("전체 캐시 재사용인데 외부 조회가 호출되었습니다")

    upstream.fetch = fail  # type: ignore[method-assign]
    with temp_cwd():
        write_cache(source)
        with patched_price_io(upstream):
            frame = backtest.get_price(TICKER)
    assert_sorted_unique(frame, "check_full_cache_reuse")
    assert_matches_source(frame, source, "check_full_cache_reuse")


def check_incremental_merge() -> None:
    """2. 과거·최신 구간 증분 병합."""
    source = build_source_frame(DATES_ALL)
    upstream = FakeUpstream(source)
    cached = source[
        (source["date"] >= pd.Timestamp("2024-02-01"))
        & (source["date"] <= pd.Timestamp("2024-02-29"))
    ]
    with temp_cwd():
        write_cache(cached)
        with patched_price_io(upstream):
            frame = backtest.get_price(TICKER)
        if len(upstream.calls) != 2:
            raise AssertionError(f"증분 조회 횟수가 2회가 아닙니다: {upstream.calls}")
        left, right = upstream.calls
        if left[0] != START or pd.to_datetime(left[1]) != cached["date"].min() - pd.Timedelta(days=1):
            raise AssertionError(f"과거 구간 조회 범위 오류: {left}")
        if pd.to_datetime(right[0]) != cached["date"].max() + pd.Timedelta(days=1) or right[1] != END:
            raise AssertionError(f"최신 구간 조회 범위 오류: {right}")
        assert_sorted_unique(frame, "check_incremental_merge")
        assert_matches_source(frame, source, "check_incremental_merge")
        persisted = read_cache()
        assert_sorted_unique(persisted, "check_incremental_merge:persisted")
        assert_matches_source(persisted, source, "check_incremental_merge:persisted")


def check_reversed_duplicate_cache() -> None:
    """3. 역순·중복 날짜 캐시를 정렬·중복 제거해 반환."""
    source = build_source_frame(DATES_ALL)
    upstream = FakeUpstream(source)
    original_marker = source.iloc[10].copy()
    conflicting = original_marker.copy()
    conflicting["close"] = float(conflicting["close"]) + 999.0
    corrupted = pd.concat([source.iloc[::-1], conflicting.to_frame().T], ignore_index=True)
    with temp_cwd():
        write_cache(corrupted)
        with patched_price_io(upstream):
            frame = backtest.get_price(TICKER)
        assert_no_fetch(upstream, "check_reversed_duplicate_cache")
        assert_sorted_unique(frame, "check_reversed_duplicate_cache")
        assert_matches_source(frame, source, "check_reversed_duplicate_cache")


def check_date_boundaries() -> None:
    """4. 시작일·종료일·상장일 경계."""
    source = build_source_frame(DATES_ALL)
    upstream = FakeUpstream(source)
    listing_date = "20240201"
    with temp_cwd():
        with patched_price_io(upstream):
            frame = backtest.get_price(TICKER, listing_dates={TICKER: listing_date})
        if len(upstream.calls) != 1:
            raise AssertionError(f"최초 조회는 1회여야 합니다: {upstream.calls}")
        call_start, call_end = upstream.calls[0]
        if call_start != listing_date:
            raise AssertionError(f"상장일 클램핑 실패: start={call_start} (기대 {listing_date})")
        if call_end != END:
            raise AssertionError(f"종료일 경계 오류: end={call_end} (기대 {END})")
        assert_sorted_unique(frame, "check_date_boundaries")
        assert_matches_source(frame, source, "check_date_boundaries", start=listing_date)
        if pd.to_datetime(frame["date"]).min() < pd.Timestamp(listing_date):
            raise AssertionError("상장일 이전 데이터가 포함되었습니다")


def check_reload_matches_updated_cache() -> None:
    """5. 캐시 갱신 후 재조회 결과 일치."""
    source = build_source_frame(DATES_ALL)
    upstream = FakeUpstream(source)
    cached = source[source["date"] <= pd.Timestamp("2024-02-29")]
    with temp_cwd():
        write_cache(cached)
        with patched_price_io(upstream):
            first = backtest.get_price(TICKER)
            calls_after_first = len(upstream.calls)

            def fail(*_args: object) -> None:
                raise AssertionError("갱신된 캐시 재사용인데 조회가 발생했습니다")

            upstream.fetch = fail  # type: ignore[method-assign]
            second = backtest.get_price(TICKER)
        if calls_after_first == 0:
            raise AssertionError("최신 구간이 비어 있는데 조회가 없었습니다")
        assert_sorted_unique(second, "check_reload_matches_updated_cache")
        assert_matches_source(second, source, "check_reload_matches_updated_cache")
        for column in ("open", "close", "volume"):
            if not np.allclose(
                first[column].to_numpy(dtype=float),
                second[column].to_numpy(dtype=float),
                rtol=0.0,
                atol=1e-9,
            ):
                raise AssertionError(f"재조회 결과 불일치: {column}")


def check_incremental_failure_falls_back() -> None:
    """6a. 증분 조회 실패 시 부분 데이터를 정상으로 반환하지 않는다."""
    source = build_source_frame(DATES_ALL)
    upstream = FakeUpstream(source)
    cached = source[source["date"] <= pd.Timestamp("2024-02-29")]

    def fail_latest(start: str, end: str) -> bool:
        return pd.to_datetime(start) > pd.Timestamp("2024-02-29")

    upstream.fail_predicate = fail_latest
    with temp_cwd():
        write_cache(cached)
        with patched_price_io(upstream):
            try:
                frame = backtest.get_price(TICKER)
            except Exception as exc:
                raise AssertionError(
                    f"증분 실패가 완전 재조회로 복구되지 않고 예외로 끝났습니다: {exc}"
                ) from exc
        if not any(call == (START, END) or call[0] == START for call in upstream.calls):
            raise AssertionError(
                f"증분 실패 후 전체 재조회가 없습니다: {upstream.calls}"
            )
        assert_sorted_unique(frame, "check_incremental_failure_falls_back")
        assert_matches_source(frame, source, "check_incremental_failure_falls_back")


def check_internal_gap_preserved() -> None:
    """6b. 원천 내부 날짜 누락은 보간하지 않고 그대로 보존한다."""
    source = build_source_frame(DATES_ALL[DATES_ALL != GAP_DATE])
    upstream = FakeUpstream(source)
    with temp_cwd():
        write_cache(source)
        with patched_price_io(upstream):
            frame = backtest.get_price(TICKER)
        assert_no_fetch(upstream, "check_internal_gap_preserved")
        assert_sorted_unique(frame, "check_internal_gap_preserved")
        assert_matches_source(frame, source, "check_internal_gap_preserved")
        if GAP_DATE in set(pd.to_datetime(frame["date"])):
            raise AssertionError("원천에 없는 날짜가 생성되었습니다")
        if frame["close"].isna().any():
            raise AssertionError("누락일을 NaN 행으로 채웠습니다")


def main() -> int:
    checks: list[tuple[str, Callable[[], None]]] = [
        ("1. 전체 캐시 재사용", check_full_cache_reuse),
        ("2. 증분 병합", check_incremental_merge),
        ("3. 역순/중복 캐시 정규화", check_reversed_duplicate_cache),
        ("4. 시작일/종료일/상장일 경계", check_date_boundaries),
        ("5. 갱신 후 재조회 일치", check_reload_matches_updated_cache),
        ("6a. 증분 실패 fallback", check_incremental_failure_falls_back),
        ("6b. 내부 누락 보존", check_internal_gap_preserved),
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
        print(f"\nprice cache integrity checks FAILED ({failures}건)")
        return 1
    print("\nprice cache integrity checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
