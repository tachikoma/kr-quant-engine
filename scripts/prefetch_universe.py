#!/usr/bin/env python3
"""유니버스 전 종목의 가격 데이터를 병렬로 사전 수집한다.

ETF_UNIVERSE_MODE=auto 시 500+ 종목의 pykrx 데이터를 순차 수집하면 ~5시간이 소요된다.
이 스크립트는 multiprocessing.Pool을 사용하여 병렬 수집(~38분)을 수행한다.

Usage:
    uv run python scripts/prefetch_universe.py [--workers N] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import pandas as pd

# 프로젝트 루트를 sys.path에 추가
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def _load_env() -> None:
    """환경변수 로드 (run_etf_backtest와 동일 로직)."""
    env_path = _ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not (v.startswith('"') and v.endswith('"')) \
           and not (v.startswith("'") and v.endswith("'")):
            comment_idx = v.find(" #")
            if comment_idx > 0:
                v = v[:comment_idx].strip()
        v = v.strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def _range_has_weekday(start_ymd: str, end_ymd: str) -> bool:
    """주말-only 범위 확인."""
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


def _fetch_one(args: tuple[str, str, str, dict[str, str]]) -> tuple[str, str, int]:
    """단일 ticker의 가격 데이터를 fetch하여 parquet에 저장한다.

    multiprocessing.Pool용 워커 함수.
    spawn 모드에서 자식 프로세스가 모듈을 다시 import하므로
    모든 import는 여기서 처리한다.

    Returns: (ticker, status, row_count)
    """
    ticker, start, end, listing_dates = args
    cache_path = _ROOT / "data_cache" / f"{ticker}.parquet"
    cache_path.parent.mkdir(exist_ok=True)

    # listing date clamping
    effective_start = start
    ld = listing_dates.get(ticker.strip())
    if ld:
        try:
            ld_ts = pd.Timestamp(ld)
            s_ts = pd.Timestamp(start)
            if s_ts < ld_ts:
                effective_start = ld_ts.strftime("%Y%m%d")
        except Exception:
            pass

    # 캐시가 이미 있으면 스킵
    if cache_path.exists():
        return ticker, "cached", 0

    # 주말-only 범위 스킵
    if not _range_has_weekday(effective_start, end):
        return ticker, "weekend_only", 0

    from pykrx_utils import fetch_etf_ohlcv_with_nav

    try:
        df = fetch_etf_ohlcv_with_nav(effective_start, end, ticker)
        if df is None or df.empty:
            return ticker, "empty", 0
        if "ticker" in df.columns:
            df["ticker"] = df["ticker"].astype(str)
        else:
            df["ticker"] = ticker
        df.to_parquet(cache_path, index=False)
        return ticker, "ok", len(df)
    except Exception as e:
        return ticker, f"error:{e}", 0


def main() -> None:
    parser = argparse.ArgumentParser(description="유니버스 전 종목 병렬 prefetch")
    parser.add_argument("--workers", type=int, default=8, help="병렬 워커 수 (기본 8)")
    parser.add_argument("--dry-run", action="store_true", help="fetch 없이 통계만 출력")
    parser.add_argument("--start", default="20160105", help="조회 시작일 (기본 20160105)")
    parser.add_argument("--end", default=date.today().strftime("%Y%m%d"), help="조회 종료일 (기본 오늘)")
    args = parser.parse_args()

    _load_env()

    from etf_shared import ETF_LIST, UNIVERSE_MODE
    from pykrx_utils import get_listing_dates

    print(f"[prefetch] UNIVERSE_MODE={UNIVERSE_MODE}")
    print(f"[prefetch] ETF_LIST: {len(ETF_LIST)}종목")

    # 캐시 현황 파악
    cache_dir = _ROOT / "data_cache"
    cached_tickers = set()
    if cache_dir.exists():
        universe_set = set(ETF_LIST)
        for f in cache_dir.glob("*.parquet"):
            if f.stem in universe_set:
                cached_tickers.add(f.stem)

    to_fetch = [t for t in ETF_LIST if t not in cached_tickers]
    print(f"[prefetch] 캐시 보유: {len(cached_tickers)}종목, 미수집: {len(to_fetch)}종목")

    if args.dry_run:
        print("[prefetch] dry-run: fetch 없이 종료")
        return

    if not to_fetch:
        print("[prefetch] 모든 종목이 캐시되어 있어 fetch 불필요")
        return

    # 상장일 조회
    print("[prefetch] 상장일 데이터 로드 중...")
    listing_dates = get_listing_dates(ticker_subset=set(ETF_LIST))
    print(f"[prefetch] 상장일 로드 완료: {len(listing_dates)}종목")

    # multiprocessing 인자 준비
    work_items = [(t, args.start, args.end, listing_dates) for t in to_fetch]

    est_minutes = len(to_fetch) * 35 // args.workers // 60
    print(f"[prefetch] 병렬 fetch 시작: {len(to_fetch)}종목, {args.workers}workers")
    print(f"[prefetch] 예상 시간: ~{est_minutes}분")

    t0 = time.time()
    ok_count = 0
    empty_count = 0
    error_count = 0
    errors: list[tuple[str, str]] = []

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_fetch_one, item): item[0] for item in work_items}
        done_count = 0
        for future in as_completed(futures):
            ticker, status, rows = future.result()
            done_count += 1

            if status == "ok":
                ok_count += 1
            elif status in ("cached", "weekend_only"):
                ok_count += 1
            elif status == "empty":
                empty_count += 1
            else:
                error_count += 1
                errors.append((ticker, status))

            if done_count % 10 == 0 or done_count == len(to_fetch):
                elapsed = time.time() - t0
                rate = done_count / elapsed if elapsed > 0 else 0
                remaining = (len(to_fetch) - done_count) / rate if rate > 0 else 0
                print(
                    f"[prefetch] {done_count}/{len(to_fetch)} "
                    f"({done_count * 100 // len(to_fetch)}%) "
                    f"OK={ok_count} empty={empty_count} err={error_count} "
                    f"elapsed={elapsed:.0f}s eta={remaining:.0f}s"
                )

    elapsed = time.time() - t0
    print(f"\n[prefetch] 완료 ({elapsed:.0f}초)")
    print(f"  성공: {ok_count}, 빈 데이터: {empty_count}, 실패: {error_count}")
    if errors:
        print("  실패 목록 (최대 20개):")
        for t, s in errors[:20]:
            print(f"    {t}: {s}")


if __name__ == "__main__":
    main()
