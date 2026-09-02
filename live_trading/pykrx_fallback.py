"""
NH 모의(demo) currentPrice 미지원(IGW40023) 대비 pykrx 어제종가 fallback 모듈.

- MODE=demo 에서만 사용한다 (실전은 runner가 게이트 — 이 모듈을 import/호출하지 않음).
- pykrx `get_market_ohlcv_by_date` 로 기준일 기준 최근 거래일 종가를 조회한다.
- `runtime_state/last_valid_prices.json` 캐시 (TTL 1일), 조회 실패 시 캐시로 fallback.
- KRX_ID/KRX_PW 는 `.env` 에서 로드하며, pykrx import 전에 반드시 로드한다.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = PROJECT_ROOT / "runtime_state" / "last_valid_prices.json"
CACHE_TTL = dt.timedelta(days=1)


def _load_dotenv(dotenv_path: Path | None = None) -> None:
    """`.env` 로드 (pykrx import 전에 KRX_ID/KRX_PW 필요). etf_daily_runner와 동일 로직."""
    path = dotenv_path or (PROJECT_ROOT / ".env")
    if not path.exists():
        return

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
            if not (value.startswith('"') and value.endswith('"')) and not (
                value.startswith("'") and value.endswith("'")
            ):
                comment_idx = value.find(" #")
                if comment_idx > 0:
                    value = value[:comment_idx].strip()
            value = value.strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


def _parse_date(value: str | dt.date) -> dt.date:
    if isinstance(value, dt.date):
        return value
    s = str(value).strip()
    if "-" in s:
        try:
            return dt.date.fromisoformat(s)
        except ValueError:
            pass
    elif len(s) == 8 and s.isdigit():
        return dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    raise ValueError(f"지원하지 않는 날짜 형식: {value!r}")


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _load_cache() -> dict[str, Any] | None:
    if not CACHE_PATH.exists():
        return None
    try:
        with CACHE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return None


def _cache_fresh(cache: dict[str, Any] | None) -> bool:
    if not cache:
        return False
    fetched_at = cache.get("fetched_at", "")
    try:
        ts = dt.datetime.fromisoformat(fetched_at)
    except (ValueError, TypeError):
        return False
    return (_now_utc() - ts) <= CACHE_TTL


def _write_cache(date: dt.date, prices: dict[str, float]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CACHE_PATH.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "date": date.isoformat(),
                    "prices": prices,
                    "fetched_at": _now_utc().isoformat(timespec="seconds"),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("[pykrx-fallback] 캐시 저장 실패: %s", exc)


def get_universe_prices(tickers: list[str], date: str | dt.date) -> dict[str, float]:
    """pykrx로 기준일 기준 최근 거래일 종가를 조회한다. (demo 전용)

    - `date` 기준 최근 7일 범위에서 마지막 거래일 종가를 사용 (주말/공휴일 자동 커버).
    - 성공한 티커만 반환하며, 실패 티커는 경고 로그만 남긴다.
    - 전체 실패 시 TTL 1일 이내 캐시로 fallback한다.
    - 403/인증 오류 등은 티커별 예외로 흡수되어 전체 실패 시 캐시 경로로 빠진다.
    """
    ref_date = _parse_date(date)
    start = ref_date - dt.timedelta(days=7)
    start_ymd = start.strftime("%Y%m%d")
    end_ymd = ref_date.strftime("%Y%m%d")

    # lazy import: 모듈 레벨 _load_dotenv() 이후에 실행되어 KRX_ID/KRX_PW가 반영된다.
    from pykrx import stock

    prices: dict[str, float] = {}
    failed: list[str] = []
    for ticker in tickers:
        t = str(ticker).strip()
        if not t:
            continue
        try:
            df = stock.get_market_ohlcv_by_date(start_ymd, end_ymd, t)
            if df is None or df.empty:
                failed.append(t)
                continue
            close_col = "종가" if "종가" in df.columns else "close"
            p = float(df.iloc[-1][close_col])
            if p > 0:
                prices[t] = p
            else:
                failed.append(t)
        except Exception as exc:  # noqa: BLE001 - pykrx는 다양한 예외를 던지므로 티커별 격리
            failed.append(t)
            logger.warning("[pykrx-fallback] %s 조회 실패: %s", t, exc)

    if prices:
        _write_cache(ref_date, prices)
        if failed:
            logger.warning(
                "[pykrx-fallback] %d개 티커 조회 실패: %s", len(failed), ", ".join(failed)
            )
        return prices

    # 전체 실패 → TTL 1일 이내 캐시 fallback
    cache = _load_cache()
    if cache is not None and _cache_fresh(cache):
        wanted = {str(t).strip() for t in tickers}
        cached = cache.get("prices", {})
        out = {t: float(p) for t, p in cached.items() if t in wanted}
        if out:
            logger.warning(
                "[pykrx-fallback] pykrx 조회 전체 실패 → 캐시 사용 (date=%s, %d개)",
                cache.get("date", "?"),
                len(out),
            )
            return out
    logger.warning("[pykrx-fallback] pykrx 조회 실패 및 캐시 미유효 — 가격 미확보")
    return {}