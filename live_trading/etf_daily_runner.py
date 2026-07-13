"""
ETF 하루 1회 실행 러너

운영 의도:
- 장 시작 전에 1회 실행하여 당일 주문 계획을 확정한다.
- 필요 시 장 시작 시각까지 대기한 뒤 실제 주문을 제출한다.
- 매도 우선 제출 후 체결/예수금 재확인 뒤 매수를 제출한다.
- 상태 파일을 저장하여 중복 실행과 복구를 지원한다.

주의:
- 기본값은 안전 모드(LIVE_ORDER_ENABLED=0)이며, 실제 주문을 전송하지 않는다.
- 실제 주문 전송은 키움 엔드포인트 환경 변수가 모두 준비된 경우에만 가능하다.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _load_dotenv(dotenv_path: Path | None = None) -> None:
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


from etf_shared import (
    build_rebalance_orders,
    get_strategy_config,
    rank_etfs,
    ETF_TAXABLE_SELL_TAX_PCT,
    TAXABLE_ETF_TICKERS,
    is_ticker_risk_on,
    add_deviation_flag,
    add_liquidity_flag,
    add_listing_flag,
    add_price_basis_columns,
)
from config_utils import parse_pct_env, parse_fraction_env
from etf_distributions import add_distributions, load_distributions

try:
    from pykrx_utils import format_ticker as _format_ticker
except Exception:

    def _format_ticker(ticker: str) -> str:  # type: ignore[misc]
        return ticker


try:
    from live_trading.kiwoom_adapter import KiwoomAdapter
except Exception:
    KiwoomAdapter = None

try:
    from live_trading.kis_adapter import KisAdapter
except Exception:
    KisAdapter = None

try:
    from live_trading.telegram_notifier import TelegramNotifier
except Exception:
    TelegramNotifier = None

import pandas as pd
from pykrx import stock
from pykrx_utils import (
    KRX_PASSWORD_CHANGE_URL,
    _call_capture_stderr,
    _range_has_weekday,
    check_krx_auth_status,
    fetch_etf_ohlcv_with_nav,
    get_listing_dates,
)


STATE_DIR = PROJECT_ROOT / "runtime_state"
STATE_PATH = STATE_DIR / "etf_daily_state.json"
OUTPUT_DIR = PROJECT_ROOT / "outputs_etf_only"

logger = logging.getLogger(__name__)

# 기본 실행 시각: 한국 시장 개장 직전
DEFAULT_PLAN_TIME = "08:50"
DEFAULT_MARKET_OPEN_TIME = "09:00"


@dataclass
class RunnerConfig:
    wait_until_open: bool
    enable_live_order: bool
    force: bool
    force_rebalance: bool
    market_filter: bool
    rebalance_step_days: int
    market_ma_days: int
    market_slope_days: int
    max_positions: int
    sell_rank_buffer: int
    plan_time: str
    market_open_time: str
    sell_cutoff_time: str
    buy_cutoff_time: str
    order_poll_interval_sec: int
    sell_fill_timeout_sec: int
    buy_fill_timeout_sec: int
    cancel_unfilled_orders: bool
    retry_unfilled_orders: bool
    retry_order_type: str
    retry_fill_timeout_sec: int
    protect_external_holdings: bool
    block_live_after_cutoff: bool
    # 실전에서 인위적 슬리피지 적용 여부 및 값
    apply_slippage_in_live: bool
    live_slippage_pct: float
    # 실전에서 API가 없을 때의 호가 스프레드 fallback
    spread_pct: float
    enable_catchup: bool
    max_asset_pct: float = 0.50
    liquidate_on_risk_off: bool = True
    max_premium_discount: float = 0.02
    max_live_spread_pct: float = 0.005
    log_level: str = "INFO"
    log_file: str = ""


def _parse_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_hhmm(value: str) -> dt.time:
    hour, minute = value.split(":")
    return dt.time(hour=int(hour), minute=int(minute))


def _today_kst() -> dt.date:
    # 시스템 시간이 한국 시간이 아닐 수 있으므로 오프셋 기반으로 단순 보정
    # 외부 의존성을 늘리지 않기 위해 timezone 라이브러리 대신 UTC+9 고정 사용
    now_utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=dt.timezone.utc)
    now_kst = now_utc.astimezone(dt.timezone(dt.timedelta(hours=9)))
    return now_kst.date()


def _now_kst() -> dt.datetime:
    now_utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=dt.timezone.utc)
    return now_utc.astimezone(dt.timezone(dt.timedelta(hours=9)))


def _is_weekday(day: dt.date) -> bool:
    return day.weekday() < 5


def _date_to_krx(day: dt.date) -> str:
    return day.strftime("%Y%m%d")


def _date_to_iso(day: dt.date) -> str:
    return day.isoformat()


def _load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        with STATE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)

    return {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _extract_exec_price(row: dict) -> float | None:
    """주문 상태/응답에서 체결가를 찾아 반환합니다. API 스펙이 다양하므로 여러 키를 시도합니다."""
    candidates = [
        "executed_price",
        "avg_filled_price",
        "avg_fill_price",
        "avg_price",
        "filled_price",
        "fill_price",
        "exec_price",
        "price",
    ]

    for container_key in ("last_status", "response"):
        cont = row.get(container_key)
        if isinstance(cont, dict):
            for key in candidates:
                if key in cont:
                    try:
                        return float(cont.get(key))
                    except Exception:
                        return None
            # trades 리스트 내부 탐색
            trades = cont.get("trades") or cont.get("filled_trades") or cont.get("exec_trades")
            if isinstance(trades, list) and trades:
                for t in trades:
                    if isinstance(t, dict):
                        for k in ("price", "trade_price", "exec_price"):
                            if k in t:
                                try:
                                    return float(t.get(k))
                                except Exception:
                                    return None

    # 직접 row 레벨에 가격이 있을 수 있음
    for key in candidates:
        if key in row:
            try:
                return float(row.get(key))
            except Exception:
                return None

    return None


def _append_execution_log(executed_orders: list[dict], run_id: str, trading_date: str) -> None:
    """`outputs_etf_only/execution_log.csv`에 실행 결과를 append합니다."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / "execution_log.csv"
    header = [
        "run_id",
        "trading_date",
        "timestamp",
        "order_id",
        "ticker",
        "side",
        "mode",
        "requested_qty",
        "filled_qty",
        "remaining_qty",
        "is_filled",
        "executed_price",
        "error",
        "status",
    ]

    rows = []
    ts = _now_kst().isoformat()
    for r in executed_orders:
        order_id = r.get("order_id", "") or ""
        ticker = r.get("ticker", "")
        side = r.get("side", "")
        mode = r.get("mode", "")
        requested = int(r.get("requested_qty", r.get("qty", 0) or 0))
        filled = int(r.get("filled_qty", 0) or 0)
        remaining = int(r.get("remaining_qty", max(requested - filled, 0)) or 0)
        is_filled = bool(r.get("is_filled", False))
        exec_price = _extract_exec_price(r)
        error = r.get("error", "") or ""
        status_summary = ""
        try:
            status_summary = json.dumps(
                r.get("last_status") or r.get("response") or {}, ensure_ascii=False
            )
        except Exception:
            status_summary = str(r.get("last_status") or r.get("response") or "")

        rows.append(
            [
                run_id,
                trading_date,
                ts,
                order_id,
                ticker,
                side,
                mode,
                requested,
                filled,
                remaining,
                is_filled,
                (exec_price if exec_price is not None else ""),
                error,
                status_summary,
            ]
        )

    write_header = not path.exists()
    try:
        import csv as _csv

        with path.open("a", encoding="utf-8", newline="") as f:
            writer = _csv.writer(f)
            if write_header:
                writer.writerow(header)
            for row in rows:
                writer.writerow(row)
    except Exception as e:
        logger.info(f"⚠️ 실행로그 저장 실패: {e}")


def setup_logging(level: str = "INFO", log_file: str = "") -> None:
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_file:
        fh = TimedRotatingFileHandler(log_file, when="midnight", backupCount=30, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


def _read_env_config() -> RunnerConfig:
    strategy_cfg = get_strategy_config()
    return RunnerConfig(
        wait_until_open=_parse_bool("WAIT_UNTIL_MARKET_OPEN", True),
        enable_live_order=_parse_bool("LIVE_ORDER_ENABLED", False),
        force=_parse_bool("DAILY_RUN_FORCE", False),
        force_rebalance=_parse_bool("FORCE_REBALANCE", False),
        market_filter=_parse_bool("USE_MARKET_FILTER", True),
        rebalance_step_days=int(strategy_cfg["rebalance_step_days"]),
        market_ma_days=int(strategy_cfg["market_ma_days"]),
        market_slope_days=int(strategy_cfg["market_slope_days"]),
        max_positions=int(strategy_cfg["max_positions"]),
        sell_rank_buffer=int(strategy_cfg["sell_rank_buffer"]),
        plan_time=os.environ.get("DAILY_PLAN_TIME", DEFAULT_PLAN_TIME),
        market_open_time=os.environ.get("MARKET_OPEN_TIME", DEFAULT_MARKET_OPEN_TIME),
        sell_cutoff_time=os.environ.get("SELL_CUTOFF_TIME", "09:05"),
        buy_cutoff_time=os.environ.get("BUY_CUTOFF_TIME", "09:10"),
        order_poll_interval_sec=int(os.environ.get("ORDER_POLL_INTERVAL_SEC", "2")),
        sell_fill_timeout_sec=int(os.environ.get("SELL_FILL_TIMEOUT_SEC", "300")),
        buy_fill_timeout_sec=int(os.environ.get("BUY_FILL_TIMEOUT_SEC", "300")),
        cancel_unfilled_orders=_parse_bool("CANCEL_UNFILLED_ORDERS", False),
        retry_unfilled_orders=_parse_bool("RETRY_UNFILLED_ORDERS", False),
        retry_order_type=os.environ.get("RETRY_ORDER_TYPE", "MARKET").upper(),
        retry_fill_timeout_sec=int(os.environ.get("RETRY_FILL_TIMEOUT_SEC", "90")),
        protect_external_holdings=_parse_bool("PROTECT_EXTERNAL_HOLDINGS", True),
        block_live_after_cutoff=_parse_bool("BLOCK_LIVE_AFTER_CUTOFF", True),
        apply_slippage_in_live=_parse_bool("APPLY_SLIPPAGE_IN_LIVE", False),
        live_slippage_pct=parse_pct_env(
            "LIVE_SLIPPAGE_PCT", strategy_cfg.get("default_slippage_pct", 0.0005)
        ),
        spread_pct=parse_pct_env("LIVE_SPREAD_PCT", strategy_cfg.get("spread_pct", 0.0005)),
        enable_catchup=_parse_bool("ENABLE_CATCHUP", True),
        max_asset_pct=parse_fraction_env("MAX_ASSET_PCT", 0.50),
        liquidate_on_risk_off=_parse_bool("LIQUIDATE_ON_RISK_OFF", True),
        max_premium_discount=float(strategy_cfg.get("max_premium_discount", 0.02)),
        max_live_spread_pct=float(strategy_cfg.get("max_live_spread_pct", 0.005)),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        log_file=os.environ.get("LOG_FILE", ""),
    )


def _cutoff_deadline(cutoff_time_hhmm: str, timeout_sec: int) -> dt.datetime:
    now = _now_kst()
    timeout_deadline = now + dt.timedelta(seconds=max(timeout_sec, 0))
    cutoff_time = _parse_hhmm(cutoff_time_hhmm)
    cutoff_dt = dt.datetime.combine(now.date(), cutoff_time, tzinfo=now.tzinfo)
    # 컷오프가 이미 지난 경우: cutoff 제약을 무시하고 timeout_sec만 기준으로 사용합니다.
    # (예: --force-live로 09:20에 실행 시 09:10 컷오프로 인해 deadline이 과거가 되어
    #  체결 대기 루프가 0회 실행되는 것을 방지 — BUG-1)
    if cutoff_dt <= now:
        return timeout_deadline
    return min(timeout_deadline, cutoff_dt)


def _earlier_hhmm(left: str, right: str) -> str:
    return left if _parse_hhmm(left) <= _parse_hhmm(right) else right


def _wait_until(target_time_hhmm: str) -> None:
    target_time = _parse_hhmm(target_time_hhmm)
    now = _now_kst()
    target_dt = dt.datetime.combine(now.date(), target_time, tzinfo=now.tzinfo)

    if now >= target_dt:
        return

    seconds = int((target_dt - now).total_seconds())
    logger.info(f"[대기] 장 시작 전까지 {seconds}초 대기합니다. 목표 시각={target_time_hhmm}")

    while True:
        now = _now_kst()
        if now >= target_dt:
            break
        time.sleep(1)


def _safe_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (float, int)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return 0.0
    return float(text)


def _normalize_ohlcv(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
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
    if "date" not in data.columns and "index" in data.columns:
        data = data.rename(columns={"index": "date"})
    data["ticker"] = ticker

    if "volume" not in data:
        data["volume"] = 0
    if "trading_value" not in data:
        data["trading_value"] = data["close"] * data["volume"]
    if "high" not in data:
        data["high"] = data["close"]
    if "low" not in data:
        data["low"] = data["close"]
    if "nav" not in data:
        data["nav"] = pd.NA
    if "base_index" not in data:
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
    out["date"] = pd.to_datetime(out["date"])
    return out


def _load_snapshot(
    etf_list: list[str], lookback_days: int = 220, ticker_names: dict[str, str] | None = None
) -> pd.DataFrame:
    """지정 기간의 ETF 가격 스냅샷을 로드하고 전처리한다.

    분배금 병합 → 유동성/상장일/괴리율 플래그 → 수익률 기준 컬럼(price/nav/total_return)
    순서로 처리하여 랭킹에 사용할 수 있는 DataFrame을 반환한다.
    """
    end_day = _today_kst()
    start_day = end_day - dt.timedelta(days=lookback_days)
    start = _date_to_krx(start_day)
    end = _date_to_krx(end_day)

    logger.info(f"[데이터] ETF 가격 로드 시작: {len(etf_list)}개 티커, 기간={start}~{end}")
    t0 = dt.datetime.now()

    frames: list[pd.DataFrame] = []
    _tn = ticker_names or {}
    for i, ticker in enumerate(etf_list, 1):
        dn = _tn.get(ticker, ticker)
        logger.info(f"  ({i}/{len(etf_list)}) {dn} 조회 중...")
        t1 = dt.datetime.now()
        if not _range_has_weekday(start, end):
            elapsed = (dt.datetime.now() - t1).total_seconds()
            logger.info(f"스킵(주말만 해당) — 데이터 없음 ({elapsed:.1f}초)")
            continue
        try:
            raw = _call_capture_stderr(fetch_etf_ohlcv_with_nav, start, end, ticker)
        except Exception as e:
            elapsed = (dt.datetime.now() - t1).total_seconds()
            logger.info(f"조회 실패 ({elapsed:.1f}초): {e}")
            continue
        elapsed = (dt.datetime.now() - t1).total_seconds()
        price = raw.copy() if isinstance(raw, pd.DataFrame) else pd.DataFrame()
        if price.empty:
            logger.info(f"데이터 없음 ({elapsed:.1f}초)")
            continue
        logger.info(f"완료 (행수={len(price)}, {elapsed:.1f}초)")
        frames.append(price)

    total_elapsed = (dt.datetime.now() - t0).total_seconds()
    logger.info(f"[데이터] ETF 가격 로드 완료 ({total_elapsed:.1f}초 소요)")

    if not frames:
        raise RuntimeError("ETF 가격 데이터가 비어 있습니다.")

    price_df = pd.concat(frames, ignore_index=True).sort_values(["ticker", "date"]).copy()
    # 백테스트와 동일 전처리: 분배금 병합 + 유동성/상장일/괴리율 플래그 + 수익률 기준 컬럼
    return_basis = os.environ.get("ETF_RETURN_BASIS", "price").strip().lower()
    distributions = load_distributions(required=return_basis == "total_return")
    price_df = add_distributions(price_df, distributions)
    listing_dates = get_listing_dates(ticker_subset=set(map(str, etf_list)))
    price_df = add_liquidity_flag(price_df)
    price_df = add_listing_flag(price_df, listing_dates)
    price_df = add_deviation_flag(price_df)
    price_df = add_price_basis_columns(price_df)
    grouped = price_df.groupby("ticker")
    price_df["ret_60"] = grouped["close_adj"].pct_change(60)
    price_df["ret_120"] = grouped["close_adj"].pct_change(120)
    price_df["ma20"] = grouped["close_adj"].transform(lambda x: x.rolling(20).mean())
    price_df["ma60"] = grouped["close_adj"].transform(lambda x: x.rolling(60).mean())
    price_df["trend_ok"] = (price_df["close_adj"] > price_df["ma20"]) & (
        price_df["ma20"] > price_df["ma60"]
    )

    snapshot = price_df.groupby("ticker").tail(1).reset_index(drop=True)
    return snapshot


def _load_market_risk_on(market_index_code: str, ma_days: int, slope_days: int) -> bool:
    end_day = _today_kst()
    start_day = end_day - dt.timedelta(days=260)
    logger.info(f"[시장필터] KOSPI 지수({market_index_code}) 조회 중... ")
    t0 = dt.datetime.now()
    start_s = _date_to_krx(start_day)
    end_s = _date_to_krx(end_day)
    if not _range_has_weekday(start_s, end_s):
        elapsed = (dt.datetime.now() - t0).total_seconds()
        logger.info(f"데이터 없음 (주말 범위 스킵) ({elapsed:.1f}초) → risk_on=True (기본값)")
        return True
    try:
        idx = _call_capture_stderr(stock.get_index_ohlcv_by_date, start_s, end_s, market_index_code)
    except Exception as e:
        elapsed = (dt.datetime.now() - t0).total_seconds()
        logger.info(f"조회 실패 ({elapsed:.1f}초): {e}")
        logger.info("  → risk_on=True (기본값)")
        return True
    elapsed = (dt.datetime.now() - t0).total_seconds()
    if idx is None or idx.empty:
        logger.info(f"데이터 없음 ({elapsed:.1f}초) → risk_on=True (기본값)")
        return True

    idx = idx.reset_index().rename(columns={"날짜": "date", "종가": "close"})
    idx["date"] = pd.to_datetime(idx["date"])
    idx = idx.sort_values("date").copy()

    idx["market_ma"] = idx["close"].rolling(ma_days).mean()
    idx["market_ma_slope"] = idx["market_ma"] - idx["market_ma"].shift(slope_days)
    last = idx.iloc[-1]
    if pd.isna(last["market_ma"]) or pd.isna(last["market_ma_slope"]):
        logger.info(f"MA 계산 불가 ({elapsed:.1f}초) → risk_on=True (기본값)")
        return True

    risk_on = bool((last["close"] >= last["market_ma"]) and (last["market_ma_slope"] >= 0))
    close_val = last["close"]
    ma_val = last["market_ma"]
    slope_val = last["market_ma_slope"]
    logger.info(
        f"완료 ({elapsed:.1f}초) | "
        f"종가={close_val:,.0f}, MA{ma_days}={ma_val:,.0f}, "
        f"기울기({slope_days}일)={slope_val:+.1f} → risk_on={risk_on}"
    )
    return risk_on


def _load_recent_trading_dates(reference_ticker: str, lookback_days: int = 120) -> list[str]:
    end_day = _today_kst()
    start_day = end_day - dt.timedelta(days=lookback_days)
    logger.info(f"[리밸런싱] 거래일 캘린더 조회 중({reference_ticker})... ")
    t0 = dt.datetime.now()
    start_s = _date_to_krx(start_day)
    end_s = _date_to_krx(end_day)
    if not _range_has_weekday(start_s, end_s):
        elapsed = (dt.datetime.now() - t0).total_seconds()
        logger.info(f"데이터 없음 (주말 범위 스킵) ({elapsed:.1f}초)")
        return []
    try:
        df = _call_capture_stderr(stock.get_market_ohlcv_by_date, start_s, end_s, reference_ticker)
    except Exception as e:
        elapsed = (dt.datetime.now() - t0).total_seconds()
        logger.info(f"조회 실패 ({elapsed:.1f}초): {e} → 빈 리스트 반환")
        return []
    elapsed = (dt.datetime.now() - t0).total_seconds()
    data = _normalize_ohlcv(df, reference_ticker)
    if data.empty:
        logger.info(f"데이터 없음 ({elapsed:.1f}초)")
        return []
    dates = [d.date().isoformat() for d in sorted(data["date"].unique())]
    logger.info(f"완료 ({elapsed:.1f}초, 거래일 {len(dates)}개)")
    return dates


def _should_rebalance(
    today: str, state: dict[str, Any], step_days: int, reference_ticker: str
) -> bool:
    last = state.get("last_rebalance_date")
    if not last:
        logger.info("[리밸런싱] 마지막 리밸런싱 기록 없음 → 즉시 실행")
        return True

    trading_dates = _load_recent_trading_dates(reference_ticker=reference_ticker, lookback_days=220)
    if not trading_dates or last not in trading_dates:
        # last_rebalance_date가 캘린더에 없으면 판단 불가
        logger.info(f"[리밸런싱] last={last}가 거래일 캘린더에 없음 → 스킵")
        return False

    last_idx = trading_dates.index(last)
    # 장 전/중 실행 시 오늘 데이터가 아직 없을 수 있으므로
    # today가 목록에 없으면 마지막 거래일 다음 거래일로 간주한다.
    today_idx = trading_dates.index(today) if today in trading_dates else len(trading_dates)
    elapsed_days = today_idx - last_idx
    due = elapsed_days >= step_days
    logger.info(
        f"[리밸런싱] 마지막={last}, 경과={elapsed_days}거래일, 주기={step_days}일 → "
        f"{'실행 예정' if due else '아직 아님'}"
    )
    return due


def _needs_catchup(state: dict[str, Any], risk_on: bool, enable_catchup: bool) -> bool:
    if not enable_catchup or not risk_on:
        return False
    return state.get("status") == "PARTIAL_FILLED"


def _build_catchup_orders(
    state: dict[str, Any],
    latest_buy_prices: dict[str, float],
    latest_prices: dict[str, float],
    ticker_names: dict[str, str],
) -> list[dict[str, Any]]:
    """state의 미체결 BUY 주문 정보를 읽어 catch-up 매수 주문을 생성한다.

    rebalance_due=False + needs_catchup=True 일 때 호출.
    build_rebalance_orders를 거치지 않고 직접 남은 수량만큼 매수 주문을 만든다.
    """
    orders: list[dict[str, Any]] = []
    prev_orders = state.get("orders", [])
    if not prev_orders:
        logger.info("[캐치업] 상태 파일에 이전 주문 정보 없음 → 주문 생성 생략")
        return orders

    from collections import defaultdict

    ticker_remaining: dict[str, int] = defaultdict(int)
    for o in prev_orders:
        if o.get("side") != "BUY":
            continue
        ticker = str(o.get("ticker", ""))
        remaining = int(o.get("remaining_qty", 0))
        if not ticker or remaining <= 0:
            continue
        ticker_remaining[ticker] += remaining

    for ticker, remaining in ticker_remaining.items():
        price = latest_buy_prices.get(ticker)
        if price is None or pd.isna(price) or price <= 0:
            price = latest_prices.get(ticker)
        if price is None or pd.isna(price) or price <= 0:
            logger.info(f"[캐치업][스킵] {ticker_names.get(ticker, ticker)}: 매수 가격 없음")
            continue

        cost = remaining * float(price)
        orders.append(
            {
                "side": "BUY",
                "ticker": ticker,
                "display_name": ticker_names.get(ticker, ticker),
                "qty": remaining,
                "reference_price": float(price),
                "estimated_value": float(cost),
                "reason": "CATCHUP",
            }
        )
        logger.info(
            f"[캐치업] {ticker_names.get(ticker, ticker)} {remaining}주 매수 (참고가={float(price):,.0f})"
        )

    if not orders:
        logger.info("[캐치업] 채울 미체결 주문이 없습니다.")
    return orders


def _snapshot_nav_map(snapshot: pd.DataFrame) -> dict[str, float]:
    if snapshot is None or snapshot.empty or "nav" not in snapshot.columns:
        return {}
    out: dict[str, float] = {}
    for _, row in snapshot.iterrows():
        ticker = str(row.get("ticker", "")).strip()
        nav = row.get("nav")
        if not ticker or nav is None or pd.isna(nav) or float(nav) <= 0:
            continue
        out[ticker] = float(nav)
    return out


def _filter_buy_orders_by_live_guards(
    orders: list[dict[str, Any]],
    latest_prices: dict[str, float],
    latest_buy_prices: dict[str, float],
    latest_sell_prices: dict[str, float],
    snapshot_nav: dict[str, float],
    config: RunnerConfig,
    ticker_names: dict[str, str],
    guards_enabled: bool,
) -> list[dict[str, Any]]:
    if not guards_enabled or not orders:
        return orders

    filtered: list[dict[str, Any]] = []
    for order in orders:
        if order.get("side") != "BUY":
            filtered.append(order)
            continue

        ticker = str(order.get("ticker", "")).strip()
        dn = ticker_names.get(ticker, order.get("display_name", ticker))
        skip_reasons: list[str] = []

        buy_price = latest_buy_prices.get(ticker)
        sell_price = latest_sell_prices.get(ticker)
        if buy_price is not None and sell_price is not None:
            try:
                ask = float(buy_price)
                bid = float(sell_price)
                mid = (ask + bid) / 2
                if ask > 0 and bid > 0 and mid > 0:
                    spread = abs(ask - bid) / mid
                    if spread > config.max_live_spread_pct:
                        skip_reasons.append(
                            f"스프레드 {spread:.2%} > {config.max_live_spread_pct:.2%}"
                        )
            except Exception:
                pass

        nav = snapshot_nav.get(ticker)
        if nav is not None and nav > 0:
            ref_price = latest_prices.get(ticker) or order.get("reference_price")
            try:
                deviation = (float(ref_price) - float(nav)) / float(nav)
                if abs(deviation) > config.max_premium_discount:
                    skip_reasons.append(
                        f"괴리율 {deviation:+.2%} > {config.max_premium_discount:.2%}"
                    )
            except Exception:
                pass

        if skip_reasons:
            logger.info(f"[매수가드][스킵] {dn}: {', '.join(skip_reasons)}")
            continue

        filtered.append(order)

    return filtered


def _build_plan(
    config: RunnerConfig,
    api: Any | None,
    market_order_margin_rate: float = 0.0,
) -> dict[str, Any]:
    strategy_cfg = get_strategy_config()
    etf_list = strategy_cfg["etf_list"]
    external_holdings: dict[str, int] = {}

    # 1단계: 잔고/예수금 조회
    if api is None:
        logger.info("[계획수립] API 없음 — 모의 데이터로 판단만 수행")
        holdings = {"069500": 10}
        cash = 1_000_000.0
        latest_prices = {ticker: 100000.0 for ticker in etf_list}
        latest_buy_prices = dict(latest_prices)
        latest_sell_prices = dict(latest_prices)
        holdings_for_rebalance = dict(holdings)
    else:
        logger.info("[계획수립] 잔고/예수금/현재가 조회 중...")
        t0 = dt.datetime.now()
        holdings = api.get_holdings()
        _get_cash = api.get_available_cash if hasattr(api, "get_available_cash") else api.get_cash
        cash = float(_get_cash())

        if config.protect_external_holdings:
            etf_set = set(etf_list)
            external_holdings = {
                ticker: qty for ticker, qty in holdings.items() if ticker not in etf_set
            }
            holdings_for_rebalance = {
                ticker: qty for ticker, qty in holdings.items() if ticker in etf_set
            }
            if external_holdings:
                logger.info(
                    f"\n[보호] 전략 유니버스 외 보유종목 {len(external_holdings)}개는 매도 대상에서 제외합니다."
                )
        else:
            holdings_for_rebalance = dict(holdings)

        # 리밸런싱 대상 보유 종목만 포함해서 가격 조회
        price_tickers = list(set(etf_list) | set(holdings_for_rebalance.keys()))
        latest_prices = api.get_prices(price_tickers)
        latest_buy_prices = dict(latest_prices)
        latest_sell_prices = dict(latest_prices)
        if hasattr(api, "get_bid_ask_prices"):
            bid_ask_prices = api.get_bid_ask_prices(price_tickers)
            for ticker in price_tickers:
                row = bid_ask_prices.get(ticker, {})
                buy_price = row.get("buy_price")
                sell_price = row.get("sell_price")
                if buy_price is not None:
                    latest_buy_prices[ticker] = float(buy_price)
                if sell_price is not None:
                    latest_sell_prices[ticker] = float(sell_price)
        elapsed = (dt.datetime.now() - t0).total_seconds()
        logger.info(f"완료 ({elapsed:.1f}초) | 보유종목={len(holdings)}개, 예수금={cash:,.0f}")

    # 로그/알림용 종목명 매핑 구성 (모든 후보 + 보유 종목 + 유니버스 외 보유)
    _all_tickers = list(
        set(etf_list) | set(holdings_for_rebalance.keys()) | set(external_holdings.keys())
    )
    ticker_names: dict[str, str] = {t: _format_ticker(t) for t in _all_tickers}

    # 2단계: ETF 가격 스냅샷 로드
    snapshot = _load_snapshot(etf_list, ticker_names=ticker_names)
    snapshot_nav = _snapshot_nav_map(snapshot)
    ranked = rank_etfs(snapshot)

    if not ranked.empty:
        logger.info("[랭킹] ETF 순위:")
        for _, row in ranked.iterrows():
            trend_mark = "✓" if row.get("trend_ok") else "✗"
            logger.info(
                f"  {ticker_names.get(row['ticker'], row['ticker'])}  ret_60={row.get('ret_60', float('nan')):.2%}  "
                f"ret_120={row.get('ret_120', float('nan')):.2%}  trend={trend_mark}"
            )

    # 3단계: 시장 필터
    risk_on = True
    if config.market_filter:
        risk_on = _load_market_risk_on(
            market_index_code=strategy_cfg["market_index_code"],
            ma_days=config.market_ma_days,
            slope_days=config.market_slope_days,
        )
    else:
        logger.info("[시장필터] USE_MARKET_FILTER=0 — 시장 필터 비활성화")

    # 4단계: 리밸런싱 주기 판단
    state = _load_state()
    today = _date_to_iso(_today_kst())
    rebalance_due = config.force_rebalance or _should_rebalance(
        today=today,
        state=state,
        step_days=config.rebalance_step_days,
        reference_ticker=etf_list[0],
    )
    if config.force_rebalance:
        logger.info("[강제] FORCE_REBALANCE=1 — 리밸런싱 주기를 무시하고 강제 실행합니다.")

    needs_catchup = _needs_catchup(state, risk_on, config.enable_catchup)
    if needs_catchup:
        logger.info("[캐치업] 전일 미체결 주문 감지 → 오늘 리밸런싱을 실행하여 포지션을 채웁니다.")

    # 5단계: 목표 티커 결정
    if not risk_on:
        if config.liquidate_on_risk_off:
            # KOSPI risk_off: foreign/commodity만 buy target 유지, domestic은 매도
            if not ranked.empty:
                ticker_list = [str(t) for t in ranked["ticker"]]
                target = [t for t in ticker_list if is_ticker_risk_on(t, False)]
                target = target[: config.max_positions + config.sell_rank_buffer]
            else:
                target = []
            if target:
                logger.info(
                    f"[계획수립] risk_on=False → foreign/commodity만 목표: {[ticker_names.get(t, t) for t in target]}"
                )
            else:
                logger.info(
                    "[계획수립] risk_on=False → 전량 매도 모드 (foreign/commodity 목표 없음)"
                )
        else:
            # LIQUIDATE_ON_RISK_OFF=0: 기존 보유 유지, 신규 매수 없음
            logger.info(
                "[계획수립] risk_on=False + liquidate_on_risk_off=False → 기존 보유 유지 (신규 매수 없음)"
            )
            target = []
    elif not rebalance_due and not needs_catchup:
        # 리밸런싱 미도달일에는 실제 주문을 생성하지 않지만, 플랜 상에는
        # 현재 보유를 목표로 표시하여 '유지' 의도를 명확히 합니다.
        logger.info(
            "[계획수립] rebalance_due=False → 리밸런싱 불필요 (유지: 목표를 현재 보유로 표시)"
        )
        target = list(holdings_for_rebalance.keys())
    else:
        target = (
            ranked.head(config.max_positions + config.sell_rank_buffer)["ticker"].tolist()
            if not ranked.empty
            else []
        )
        label = "캐치업 목표" if needs_catchup else "목표"
        logger.info(f"[계획수립] {label} 티커 확정: {[ticker_names.get(t, t) for t in target]}")

    # rebalance_due=False 이고 시장이 위험상태(risk_on=True)인 경우
    # 리밸런싱이 필요 없으므로 주문 생성을 건너뛰고 즉시 빈 주문 계획을 반환한다.
    # (risk_on=False 인 경우는 전량 매도 모드이므로 기존 동작을 유지)
    # (방어) 빌드 함수에서 빈 target인 경우 매도를 허용할지 여부를 제어합니다.

    if target:
        logger.info("[계획수립] 목표 티커 가격 조회 결과:")
        for ticker in target:
            buy_price = latest_buy_prices.get(ticker)
            sell_price = latest_sell_prices.get(ticker)
            buy_bad = buy_price is None or pd.isna(buy_price) or buy_price <= 0
            sell_bad = sell_price is None or pd.isna(sell_price) or sell_price <= 0
            dn = ticker_names.get(ticker, ticker)
            if buy_bad and sell_bad:
                logger.info(
                    f"  {dn}: 매수/매도 가격 미조회/비정상 (buy={buy_price}, sell={sell_price})"
                )
            else:
                buy_text = "N/A" if buy_bad else f"{float(buy_price):,.0f}"
                sell_text = "N/A" if sell_bad else f"{float(sell_price):,.0f}"
                logger.info(f"  {dn}: buy={buy_text}, sell={sell_text}")

    if needs_catchup and not rebalance_due:
        orders = _build_catchup_orders(state, latest_buy_prices, latest_prices, ticker_names)
    else:
        orders = build_rebalance_orders(
            current_holdings=holdings_for_rebalance,
            target_tickers=target,
            latest_prices=latest_prices,
            available_cash=cash,
            latest_buy_prices=latest_buy_prices,
            latest_sell_prices=latest_sell_prices,
            max_positions=config.max_positions,
            max_asset_pct=config.max_asset_pct,
            sell_rank_buffer=config.sell_rank_buffer,
            allow_empty_target_sell=not risk_on if config.liquidate_on_risk_off else False,
            # 실전에서는 API의 실제 bid/ask를 신뢰하고 인위적 슬리피지를 적용하지 않는 것이 기본 정책입니다.
            # config.apply_slippage_in_live=True 인 경우에만 라이브 슬리피지를 적용합니다.
            slippage=(
                0.0
                if (
                    api is not None
                    and config.enable_live_order
                    and not config.apply_slippage_in_live
                )
                else float(
                    getattr(
                        config,
                        "live_slippage_pct",
                        strategy_cfg.get("default_slippage_pct", 0.0005),
                    )
                )
            ),
            sell_tax_pct=ETF_TAXABLE_SELL_TAX_PCT,
            taxable_tickers=TAXABLE_ETF_TICKERS,
            generate_orders=(rebalance_due or needs_catchup),
            ticker_names=ticker_names,
            market_order_margin_rate=market_order_margin_rate,
        )

    orders = _filter_buy_orders_by_live_guards(
        orders,
        latest_prices=latest_prices,
        latest_buy_prices=latest_buy_prices,
        latest_sell_prices=latest_sell_prices,
        snapshot_nav=snapshot_nav,
        config=config,
        ticker_names=ticker_names,
        guards_enabled=api is not None,
    )

    # KIS: 각 매수 주문별로 개별 종목/가격 기준 nrcvb_buy_qty 조회하여 수량 제한
    if hasattr(api, "get_buyable_info"):
        for o in orders:
            if o.get("side") == "BUY":
                _t = o.get("ticker")
                _p = int(latest_buy_prices.get(_t, 0) or 0)
                if _p > 0:
                    _info = api.get_buyable_info(_t, _p)
                    _nrcvb_qty = int(_info.get("nrcvb_buy_qty", "0"))
                    if _nrcvb_qty > 0 and o["qty"] > _nrcvb_qty:
                        logger.info(
                            f"[KIS제한] {o.get('display_name', o.get('ticker', ''))} 수량 {o['qty']}→{_nrcvb_qty}주 (nrcvb_buy_qty)"
                        )
                        o["qty"] = _nrcvb_qty
                        o["estimated_value"] = _nrcvb_qty * float(o.get("reference_price", 0))

    sell_orders = [o for o in orders if o.get("side") == "SELL"]
    buy_orders = [o for o in orders if o.get("side") == "BUY"]

    return {
        "today": today,
        "risk_on": risk_on,
        "rebalance_due": rebalance_due,
        "needs_catchup": needs_catchup,
        "holdings": holdings,
        "cash": cash,
        "target": target,
        "ticker_names": ticker_names,
        "snapshot_nav": snapshot_nav,
        "blocked_external_holdings": external_holdings,
        "ranked_top": ranked.head(10).to_dict(orient="records") if not ranked.empty else [],
        "sell_orders": sell_orders,
        "buy_orders": buy_orders,
        "all_orders": orders,
        "market_order_margin_rate": market_order_margin_rate,
    }


def _submit_orders(
    api: Any,
    side: str,
    orders: list[dict[str, Any]],
    dry_run: bool,
    order_type: str = "MARKET",
    attempt: int = 1,
    notifier: Any = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    for order in orders:
        ticker = str(order.get("ticker"))
        display_name = str(order.get("display_name", ticker))
        qty = int(order.get("qty", 0))
        if qty <= 0:
            continue

        if dry_run:
            results.append(
                {
                    "ticker": ticker,
                    "display_name": display_name,
                    "side": side,
                    "attempt": attempt,
                    "order_type": order_type,
                    "qty": qty,
                    "requested_qty": qty,
                    "filled_qty": 0,
                    "remaining_qty": qty,
                    "is_filled": False,
                    "submitted": False,
                    "mode": "DRY_RUN",
                    "order_id": "",
                }
            )
            continue

        try:
            response = api.place_order(side=side, ticker=ticker, qty=qty, order_type=order_type)
            order_id = response.get("order_id", "")
            results.append(
                {
                    "ticker": ticker,
                    "display_name": display_name,
                    "side": side,
                    "attempt": attempt,
                    "order_type": order_type,
                    "qty": qty,
                    "requested_qty": qty,
                    "filled_qty": 0,
                    "remaining_qty": qty,
                    "is_filled": False,
                    "submitted": True,
                    "mode": "LIVE",
                    "order_id": order_id,
                    "response": response.get("response", {}),
                }
            )
            if notifier is not None:
                notifier.notify_order_submitted(side, display_name, qty, order_id, attempt)
        except Exception as exc:
            error_code = exc.msg_cd if hasattr(exc, "msg_cd") else ""
            results.append(
                {
                    "ticker": ticker,
                    "display_name": display_name,
                    "side": side,
                    "attempt": attempt,
                    "order_type": order_type,
                    "qty": qty,
                    "requested_qty": qty,
                    "filled_qty": 0,
                    "remaining_qty": qty,
                    "is_filled": False,
                    "submitted": False,
                    "mode": "LIVE",
                    "order_id": "",
                    "error": str(exc),
                    "error_code": error_code,
                }
            )
            if notifier is not None:
                notifier.notify_order_error(side, display_name, qty, exc)

    return results


def _poll_and_finalize_orders(
    api: Any,
    submitted_results: list[dict[str, Any]],
    timeout_sec: int,
    poll_interval_sec: int,
    cutoff_time_hhmm: str,
    cancel_unfilled_orders: bool,
    notifier: Any = None,
) -> list[dict[str, Any]]:
    results = [dict(r) for r in submitted_results]
    pending_idx = {
        i
        for i, row in enumerate(results)
        if row.get("submitted") and row.get("order_id") and not row.get("is_filled", False)
    }
    if not pending_idx:
        return results

    deadline = _cutoff_deadline(cutoff_time_hhmm, timeout_sec)
    # kt00007 ord_dt 필드에 전달할 오늘 날짜(KST 기준 YYYYMMDD)
    today_krx = _date_to_krx(_now_kst().date())

    while pending_idx and _now_kst() < deadline:
        done_idx: list[int] = []
        for i in list(pending_idx):
            row = results[i]
            order_id = str(row.get("order_id", "")).strip()
            req_qty = int(row.get("requested_qty", row.get("qty", 0)))
            if not order_id or req_qty <= 0:
                done_idx.append(i)
                continue

            try:
                status = api.get_order_status(order_id, today=today_krx)
            except Exception as exc:
                row["status_error"] = str(exc)
                continue

            filled_qty = int(status.get("filled_qty", 0))
            order_qty = int(status.get("order_qty", req_qty))
            if order_qty > 0:
                req_qty = order_qty
            remaining_qty_raw = status.get("remaining_qty")
            if remaining_qty_raw is None:
                remaining_qty = max(req_qty - filled_qty, 0)
            else:
                remaining_qty = max(int(remaining_qty_raw), 0)

            row["requested_qty"] = req_qty
            row["filled_qty"] = max(filled_qty, 0)
            row["remaining_qty"] = remaining_qty
            row["is_filled"] = bool(status.get("is_filled", False)) and row["remaining_qty"] == 0
            row["last_status"] = status

            if row["is_filled"]:
                done_idx.append(i)
                if notifier is not None:
                    price = _extract_exec_price(row)
                    notifier.notify_order_filled(
                        row.get("side", ""),
                        row.get("display_name", row["ticker"]),
                        row["filled_qty"],
                        row["requested_qty"],
                        price,
                    )

        for i in done_idx:
            pending_idx.discard(i)

        if pending_idx:
            time.sleep(max(poll_interval_sec, 1))

    # 컷오프 이후 미체결 정리
    for i in list(pending_idx):
        row = results[i]
        order_id = str(row.get("order_id", "")).strip()
        ticker = str(row.get("ticker", "")).strip()
        remaining_qty = int(row.get("remaining_qty", 0))
        if cancel_unfilled_orders and order_id:
            try:
                cancel_resp = api.cancel_order(order_id=order_id, ticker=ticker, qty=remaining_qty)
                row["cancel_submitted"] = True
                row["cancel_response"] = cancel_resp
                if notifier is not None:
                    notifier.notify_order_cancelled(
                        row.get("side", ""), row.get("display_name", ticker), remaining_qty
                    )
            except Exception as exc:
                row["cancel_submitted"] = False
                row["cancel_error"] = str(exc)
        else:
            row["cancel_submitted"] = False
            row["cancel_skipped"] = True
            if notifier is not None:
                filled = row.get("filled_qty", 0)
                total = row.get("requested_qty", row.get("qty", 0))
                notifier.notify_order_timeout(
                    row.get("side", ""), row.get("display_name", ticker), filled, total
                )

        row["is_filled"] = bool(row.get("remaining_qty", 0) == 0)
        row["timed_out"] = True

    return results


def _all_orders_filled(results: list[dict[str, Any]]) -> bool:
    if not results:
        return True
    for row in results:
        if not row.get("submitted"):
            return False
        if not row.get("is_filled", False):
            return False
    return True


def _wait_for_cancellations(
    api: Any,
    results: list[dict[str, Any]],
    timeout_sec: int = 5,
    poll_interval_sec: int = 1,
) -> None:
    """취소 요청이 제출된 주문들의 remaining_qty가 0이 될 때까지 폴링 대기한다.

    재주문 전에 호출하여, 원주문 취소가 완료되기 전에 재주문이 나가
    중복 체결되는 것을 방지합니다. (BUG-2)
    """
    cancel_rows = [r for r in results if r.get("cancel_submitted") and r.get("order_id")]
    if not cancel_rows:
        return

    today_krx = _date_to_krx(_now_kst().date())
    deadline = _now_kst() + dt.timedelta(seconds=max(timeout_sec, 0))
    logger.info(f"[취소확인] {len(cancel_rows)}건 취소 완료 대기 중 (최대 {timeout_sec}초)...")
    pending = {str(r["order_id"]): r for r in cancel_rows}

    while pending and _now_kst() < deadline:
        for order_id in list(pending.keys()):
            try:
                status = api.get_order_status(order_id, today=today_krx)
            except Exception as exc:
                logger.info(f"[취소확인] {order_id} 상태 조회 실패: {exc}")
                continue
            remaining = int(status.get("remaining_qty", 1))
            if remaining == 0 or not status.get("is_found", True):
                row = pending.pop(order_id)
                row["cancel_confirmed"] = True
                logger.info(f"[취소확인] {order_id} → 취소/체결 완료 (remaining={remaining})")
        if pending:
            time.sleep(max(poll_interval_sec, 1))

    for order_id in pending:
        logger.info(f"[취소확인] {order_id} → 미확인 (타임아웃, 계속 진행)")


def _refresh_order_statuses(
    api: Any,
    results: list[dict[str, Any]],
) -> None:
    """각 주문의 현재 체결 상태를 API로 재조회하여 results를 in-place 업데이트한다.

    재주문 전에 호출하여 실제 체결/잔량을 정확히 파악한 뒤
    재주문 수량을 계산합니다. (BUG-3)
    """
    today_krx = _date_to_krx(_now_kst().date())
    for row in results:
        order_id = str(row.get("order_id", "")).strip()
        if not order_id or not row.get("submitted"):
            continue
        try:
            status = api.get_order_status(order_id, today=today_krx)
        except Exception as exc:
            logger.info(f"[상태재조회] {order_id} 조회 실패: {exc}")
            continue
        filled_qty = int(status.get("filled_qty", 0))
        order_qty = int(status.get("order_qty", row.get("requested_qty", 0)))
        remaining_qty = int(status.get("remaining_qty", 0))
        row["filled_qty"] = max(filled_qty, 0)
        row["remaining_qty"] = max(remaining_qty, 0)
        if order_qty > 0:
            row["requested_qty"] = order_qty
        row["is_filled"] = bool(status.get("is_filled", False)) and row["remaining_qty"] == 0
        logger.info(
            f"[상태재조회] {row.get('display_name', row.get('ticker', order_id))} → "
            f"filled={row['filled_qty']}/{row['requested_qty']}, remaining={row['remaining_qty']}"
        )


def _build_retry_orders(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    retry_orders: list[dict[str, Any]] = []
    for row in results:
        req_qty = int(row.get("requested_qty", row.get("qty", 0)))
        filled_qty = int(row.get("filled_qty", 0))
        remaining_qty = row.get("remaining_qty")
        if remaining_qty is None:
            remaining_qty = max(req_qty - filled_qty, 0)
        else:
            remaining_qty = max(int(remaining_qty), 0)
        ticker = str(row.get("ticker", "")).strip()
        if not ticker:
            continue
        if remaining_qty == 0 and (req_qty - filled_qty) > 0:
            logger.info(
                f"[재시도-스킵] {ticker}: API 잔량=0, 자체계산={req_qty - filled_qty} — 재시도 생략"
            )
        if remaining_qty <= 0:
            continue
        retry_orders.append(
            {
                "ticker": ticker,
                "display_name": row.get("display_name", ticker),
                "qty": remaining_qty,
                "retry_from_order_id": row.get("order_id", ""),
            }
        )
    return retry_orders


def _build_failed_retry_orders(
    results: list[dict[str, Any]],
    api: Any,
    latest_buy_prices: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """제출 실패(submitted=False)한 매수 주문을 KIS get_buyable_info로 가능 수량 확인 후 재시도.

    Kiwoom(hasattr 없다)은 항상 빈 리스트를 반환한다.
    """
    if not hasattr(api, "get_buyable_info"):
        return []
    retry: list[dict[str, Any]] = []
    for row in results:
        if row.get("submitted", True) or row.get("side") != "BUY":
            continue
        ticker = str(row.get("ticker", "")).strip()
        orig_qty = int(row.get("qty", 0))
        if not ticker or orig_qty <= 0:
            continue
        price = int(row.get("reference_price", 0))
        if latest_buy_prices and ticker in latest_buy_prices:
            price = int(latest_buy_prices[ticker])
        if price <= 0:
            continue
        try:
            info = api.get_buyable_info(ticker, price)
            max_qty = int(info.get("nrcvb_buy_qty", "0"))
            error_code = str(row.get("error_code", ""))
            if "40250000" in error_code:
                ord_psbl_cash = float(info.get("ord_psbl_cash", "0"))
                if ord_psbl_cash > 0:
                    max_qty = max(1, int(ord_psbl_cash // price))
        except Exception:
            continue
        if max_qty <= 0 or max_qty >= orig_qty:
            continue
        dn = row.get("display_name", ticker)
        logger.info(
            f"[KIS재시도] {dn} 수량 {orig_qty}→{max_qty}주 (nrcvb_buy_qty, 오류코드={row.get('error_code', '')})"
        )
        retry.append(
            {
                "ticker": ticker,
                "display_name": row.get("display_name", ticker),
                "qty": max_qty,
                "retry_from_order_id": "",
            }
        )
    return retry


def _is_side_fully_filled(primary: list[dict[str, Any]], retry: list[dict[str, Any]]) -> bool:
    required = 0
    filled = 0

    for row in primary:
        required += int(row.get("requested_qty", row.get("qty", 0)))
        filled += int(row.get("filled_qty", 0))

    for row in retry:
        filled += int(row.get("filled_qty", 0))

    if required <= 0:
        return True
    return filled >= required


def _print_plan(plan: dict[str, Any], cfg: RunnerConfig) -> None:
    tn = plan.get("ticker_names") or {}
    logger.info("=== ETF 하루 1회 실행 계획 ===")
    logger.info(f"기준일: {plan['today']}")
    logger.info(f"risk_on: {plan['risk_on']}")
    logger.info(f"rebalance_due: {plan['rebalance_due']}")
    logger.info(f"catchup: {plan.get('needs_catchup', False)}")
    logger.info(f"실주문 모드: {'ON' if cfg.enable_live_order else 'OFF'}")
    logger.info(f"매도 컷오프: {cfg.sell_cutoff_time}, 매수 컷오프: {cfg.buy_cutoff_time}")
    logger.info(
        f"미체결 재주문: {'ON' if cfg.retry_unfilled_orders else 'OFF'} ({cfg.retry_order_type})"
    )
    logger.info(f"유니버스 외 보유 매도 차단: {'ON' if cfg.protect_external_holdings else 'OFF'}")
    logger.info(f"컷오프 이후 실주문 차단: {'ON' if cfg.block_live_after_cutoff else 'OFF'}")
    _margin_rate = plan.get("market_order_margin_rate", 0.0)
    if _margin_rate > 0:
        logger.info(f"시장가 증거금 할증률: {_margin_rate:.0%}")
    logger.info(f"보유종목수: {len(plan['holdings'])}, 예수금: {plan['cash']:,.0f}")
    if plan["holdings"]:
        logger.info("  현재 보유:")
        for ticker, qty in plan["holdings"].items():
            logger.info(f"    {tn.get(ticker, ticker)}: {qty}주")
    blocked_external = plan.get("blocked_external_holdings") or {}
    if blocked_external:
        logger.info("  매도 제외(유니버스 외 보유):")
        for ticker, qty in blocked_external.items():
            logger.info(f"    {tn.get(ticker, ticker)}: {qty}주")
    logger.info(f"목표 티커: {[tn.get(t, t) for t in plan['target']]}")
    logger.info(
        f"매도 주문 수: {len(plan['sell_orders'])}, 매수 주문 수: {len(plan['buy_orders'])}"
    )
    if plan["sell_orders"]:
        logger.info("  매도 주문 상세:")
        for o in plan["sell_orders"]:
            logger.info(
                f"    SELL {o.get('display_name', o['ticker'])} {o['qty']}주 "
                f"(참고가={o.get('reference_price', 0):,.0f}, "
                f"예상금액={o.get('estimated_value', 0):,.0f})"
            )
    if plan["buy_orders"]:
        logger.info("  매수 주문 상세:")
        for o in plan["buy_orders"]:
            logger.info(
                f"    BUY  {o.get('display_name', o['ticker'])} {o['qty']}주 "
                f"(참고가={o.get('reference_price', 0):,.0f}, "
                f"예상금액={o.get('estimated_value', 0):,.0f})"
            )
    if not plan["sell_orders"] and not plan["buy_orders"]:
        reason = []
        if not plan["risk_on"]:
            reason.append("시장 필터 OFF (risk_on=False)")
        if not plan["rebalance_due"] and not plan.get("needs_catchup"):
            reason.append("리밸런싱 주기 미도달")
        if reason:
            logger.info(f"  주문 없음 사유: {', '.join(reason)}")


def run_daily() -> None:
    cfg = _read_env_config()
    setup_logging(cfg.log_level, cfg.log_file)
    state = _load_state()
    today = _date_to_iso(_today_kst())

    logger.info(f"[시작] ETF 데일리 러너 — {today} {_now_kst().strftime('%H:%M:%S')} (KST)")
    logger.info(
        f"  LIVE_ORDER_ENABLED={cfg.enable_live_order}  FORCE={cfg.force}  FORCE_REBALANCE={cfg.force_rebalance}"
    )
    logger.info(
        f"  max_positions={cfg.max_positions}  rebalance_step_days={cfg.rebalance_step_days}"
    )

    if not _is_weekday(_today_kst()):
        logger.info("[종료] 주말이므로 실행하지 않습니다.")
        return

    if (
        (not cfg.force)
        and state.get("trading_date") == today
        and state.get("status") in {"DONE", "NO_ACTION"}
    ):
        logger.info(
            f"[종료] 오늘은 이미 실행 완료 상태입니다 (status={state.get('status')}). "
            "DAILY_RUN_FORCE=1로 강제 실행할 수 있습니다."
        )
        return

    krx_status = check_krx_auth_status()
    if krx_status == "password_change_needed":
        logger.error("=" * 60)
        logger.error("KRX 비밀번호 변경이 필요합니다.")
        logger.error(f"{KRX_PASSWORD_CHANGE_URL} 에서 비밀번호를 변경한 후")
        logger.error(".env 파일의 KRX_PW를 업데이트하고 재실행하세요.")
        logger.error("=" * 60)
        if TelegramNotifier is not None:
            try:
                notifier = TelegramNotifier()
                notifier.notify_error(
                    "KRX 인증",
                    f"비밀번호 변경 필요 — {KRX_PASSWORD_CHANGE_URL} 에서 변경 후 .env 업데이트",
                )
            except Exception:
                pass
        sys.exit(1)
    elif krx_status == "no_credentials":
        logger.info("[경고] KRX_ID/KRX_PW 미설정 — 인증이 필요한 데이터 조회는 실패할 수 있습니다.")

    if cfg.enable_live_order and cfg.block_live_after_cutoff:
        now_dt = _now_kst()
        live_cutoff_hhmm = _earlier_hhmm(cfg.sell_cutoff_time, cfg.buy_cutoff_time)
        live_cutoff_dt = dt.datetime.combine(
            now_dt.date(), _parse_hhmm(live_cutoff_hhmm), tzinfo=now_dt.tzinfo
        )
        if now_dt >= live_cutoff_dt:
            logger.info(
                f"[안전차단] 현재 시각({_now_kst().strftime('%H:%M:%S')})이 "
                f"실주문 컷오프({live_cutoff_hhmm}) 이후입니다. "
                "실주문 실행을 중단합니다."
            )
            logger.info("[안내] 테스트가 목적이라면 LIVE_ORDER_ENABLED=0(드라이런)으로 실행하세요.")
            return

    # --force-live 등으로 컷오프 차단이 꺼진 상태에서 이미 컷오프를 지났으면
    # 체결 대기 컷오프를 now + FORCE_LIVE_CUTOFF_EXTEND_MIN 분으로 자동 연장합니다.
    # (기본 10분: 연장 후 BUY_CUTOFF, 절반값: SELL_CUTOFF)
    if cfg.enable_live_order and (not cfg.block_live_after_cutoff):
        _now_for_extend = _now_kst()
        _live_cutoff_hhmm = _earlier_hhmm(cfg.sell_cutoff_time, cfg.buy_cutoff_time)
        _live_cutoff_dt = dt.datetime.combine(
            _now_for_extend.date(), _parse_hhmm(_live_cutoff_hhmm), tzinfo=_now_for_extend.tzinfo
        )
        if _now_for_extend >= _live_cutoff_dt:
            _extend_min = int(os.environ.get("FORCE_LIVE_CUTOFF_EXTEND_MIN", "10"))
            cfg.buy_cutoff_time = (_now_for_extend + dt.timedelta(minutes=_extend_min)).strftime(
                "%H:%M"
            )
            cfg.sell_cutoff_time = (
                _now_for_extend + dt.timedelta(minutes=max(_extend_min // 2, 1))
            ).strftime("%H:%M")
            logger.info(
                f"[컷오프 연장] 현재 시각({_now_for_extend.strftime('%H:%M:%S')})이 "
                f"컷오프({_live_cutoff_hhmm}) 이후입니다. "
                f"자동 연장 → 매도={cfg.sell_cutoff_time}, 매수={cfg.buy_cutoff_time} "
                f"(FORCE_LIVE_CUTOFF_EXTEND_MIN={_extend_min})"
            )

    now = _now_kst().time()
    plan_time = _parse_hhmm(cfg.plan_time)
    if now < plan_time:
        logger.info(f"[대기] 계획 수립 시각({cfg.plan_time}) 전입니다. 해당 시각까지 대기합니다.")
        _wait_until(cfg.plan_time)

    api = None
    adapter_init_error: Exception | None = None

    broker_type = os.environ.get("BROKER_TYPE", "KIWOOM").upper()
    logger.info(f"[DEBUG] BROKER_TYPE='{os.environ.get('BROKER_TYPE')}' → '{broker_type}'")
    if broker_type == "KIS":
        AdapterClass = KisAdapter
        adapter_name = "KIS"
    else:
        AdapterClass = KiwoomAdapter
        adapter_name = "키움"

    if AdapterClass is not None:
        try:
            api = AdapterClass()
        except Exception as exc:
            adapter_init_error = exc
            logger.info(f"[경고] {adapter_name} 어댑터 초기화 실패: {exc}")

    if cfg.enable_live_order and api is None:
        if AdapterClass is None:
            raise RuntimeError(
                f"LIVE_ORDER_ENABLED=1 이지만 {adapter_name} 어댑터를 로드하지 못했습니다. "
                "실전 모드에서는 API 없이 진행할 수 없습니다."
            )
        raise RuntimeError(
            f"LIVE_ORDER_ENABLED=1 이지만 {adapter_name} 어댑터 초기화에 실패했습니다. "
            "GitHub Actions Runner 환경에서는 증권사 API 서버에 연결되지 않을 수 있습니다. "
            "로컬 실행 또는 Self-hosted Runner 사용을 고려하세요.\n"
            f"원인: {adapter_init_error}"
        )

    run_id = str(uuid.uuid4())
    _market_order_margin_rate = parse_pct_env(
        "MARKET_ORDER_MARGIN_RATE",
        0.20 if broker_type == "KIWOOM" else 0.10,
    )
    plan = _build_plan(cfg, api, market_order_margin_rate=_market_order_margin_rate)
    _print_plan(plan, cfg)

    if not plan["all_orders"]:
        new_state = {
            "trading_date": today,
            "run_id": run_id,
            "status": "NO_ACTION",
            "generated_at": _now_kst().isoformat(),
            "risk_on": plan["risk_on"],
            "rebalance_due": plan["rebalance_due"],
            "target": plan["target"],
            "orders": [],
            "last_rebalance_date": state.get("last_rebalance_date"),
        }
        _save_state(new_state)
        logger.info("[완료] 오늘은 실행할 주문이 없습니다.")
        return

    if cfg.wait_until_open:
        _wait_until(cfg.market_open_time)

    dry_run = (not cfg.enable_live_order) or (api is None)
    if dry_run:
        logger.info(
            "[안전모드] LIVE_ORDER_ENABLED=0 또는 어댑터 미사용 상태입니다. 실제 주문은 전송하지 않습니다."
        )

    # 실전 모드에서만 텔레그램 알림 활성화
    notifier = None
    if (not dry_run) and TelegramNotifier is not None:
        try:
            notifier = TelegramNotifier()
        except Exception as _tg_exc:
            logger.info(f"[경고] 텔레그램 알림 초기화 실패: {_tg_exc}")

    # 1) 매도 우선
    if plan["sell_orders"]:
        logger.info("\n[주문] ─── 매도 단계 ───")
        sell_results = _submit_orders(
            api, "SELL", plan["sell_orders"], dry_run=dry_run, attempt=1, notifier=notifier
        )
        if sell_results:
            for r in sell_results:
                status_txt = (
                    "DRY_RUN"
                    if r.get("mode") == "DRY_RUN"
                    else ("제출완료" if r.get("submitted") else f"오류: {r.get('error')}")
                )
                logger.info(
                    f"  SELL {r.get('display_name', r['ticker'])} {r['qty']}주 → {status_txt}"
                )
        sell_retry_results: list[dict[str, Any]] = []

        if (not dry_run) and api is not None:
            logger.info(
                f"[주문] 매도 체결 대기 중 (타임아웃={cfg.sell_fill_timeout_sec}초, 컷오프={cfg.sell_cutoff_time})"
            )
            sell_results = _poll_and_finalize_orders(
                api=api,
                submitted_results=sell_results,
                timeout_sec=cfg.sell_fill_timeout_sec,
                poll_interval_sec=cfg.order_poll_interval_sec,
                cutoff_time_hhmm=cfg.sell_cutoff_time,
                cancel_unfilled_orders=cfg.cancel_unfilled_orders,
                notifier=notifier,
            )
            for r in sell_results:
                if r.get("is_filled"):
                    _status = "✓ 완료"
                elif not r.get("submitted"):
                    _status = f"✗ 오류({r.get('error_code', '')})"
                elif r.get("timed_out"):
                    _status = "✗ 미체결 (타임아웃)"
                else:
                    _status = "✗ 미체결"
                logger.info(
                    f"  SELL {r.get('display_name', r['ticker'])} 체결={r.get('filled_qty', 0)}/{r.get('requested_qty', r.get('qty', 0))} "
                    f"{_status}"
                )
            if cfg.retry_unfilled_orders:
                for r in sell_results:
                    remain = int(r.get("remaining_qty", 0))
                    oid = str(r.get("order_id", "")).strip()
                    tid = str(r.get("ticker", "")).strip()
                    if remain > 0 and oid:
                        try:
                            api.cancel_order(order_id=oid, ticker=tid, qty=remain)
                            r["cancel_submitted"] = True
                        except Exception:
                            pass
                _wait_for_cancellations(api, sell_results)
                _refresh_order_statuses(api, sell_results)
                retry_sell_orders = _build_retry_orders(sell_results)
                if retry_sell_orders:
                    logger.info(f"[재시도] 매도 잔량 재주문 {len(retry_sell_orders)}건 제출")
                    sell_retry_results = _submit_orders(
                        api,
                        "SELL",
                        retry_sell_orders,
                        dry_run=False,
                        order_type=cfg.retry_order_type,
                        attempt=2,
                        notifier=notifier,
                    )
                    sell_retry_results = _poll_and_finalize_orders(
                        api=api,
                        submitted_results=sell_retry_results,
                        timeout_sec=cfg.retry_fill_timeout_sec,
                        poll_interval_sec=cfg.order_poll_interval_sec,
                        cutoff_time_hhmm=cfg.sell_cutoff_time,
                        cancel_unfilled_orders=cfg.cancel_unfilled_orders,
                        notifier=notifier,
                    )

        # 2) 매도 후 예수금 재확인(실주문 모드에서만)
        refreshed_cash = None
        if (not dry_run) and api is not None:
            try:
                _get_cash = (
                    api.get_available_cash if hasattr(api, "get_available_cash") else api.get_cash
                )
                refreshed_cash = _safe_float(_get_cash())
                logger.info(f"[정보] 매도 후 예수금 재조회: {refreshed_cash:,.0f}")
            except Exception as exc:
                logger.info(f"[경고] 매도 후 예수금 재조회 실패: {exc}")
                refreshed_cash = 0.0

        # 3) 매도 미완전체결이면 매수 차단
        can_buy = dry_run or _is_side_fully_filled(sell_results, sell_retry_results)
        buy_results: list[dict[str, Any]] = []
        buy_retry_results: list[dict[str, Any]] = []

        # 매도 체결이 완료되어 실제 매수가 가능한 경우(실거래)에는
        # 매도 완료 후의 실제 예수금/보유를 기준으로 매수 주문을 재계산합니다.
        # 캐치업 모드에서도 동일하게 재계산하여 market_order_margin_rate 등을 적용합니다.
        if can_buy and (not dry_run) and api is not None:
            try:
                refreshed_holdings = api.get_holdings()
                if cfg.protect_external_holdings:
                    _strategy_cfg = get_strategy_config()
                    _etf_set = set(_strategy_cfg["etf_list"])
                    _before = len(refreshed_holdings)
                    refreshed_holdings = {
                        t: q for t, q in refreshed_holdings.items() if t in _etf_set
                    }
                    if len(refreshed_holdings) < _before:
                        logger.info(
                            f"[보호] 매도 후 재조회된 유니버스 외 {_before - len(refreshed_holdings)}개는 매수계산에서 제외합니다."
                        )
                logger.info(f"[정보] 매도 후 보유 재조회: {len(refreshed_holdings)}개")
            except Exception as exc:
                logger.info(f"[경고] 매도 후 보유 재조회 실패: {exc}")
                refreshed_holdings = plan.get("holdings", {})

            sold_this_cycle = {o["ticker"] for o in plan.get("sell_orders", []) if o.get("ticker")}
            if sold_this_cycle:
                _before_sold = len(refreshed_holdings)
                refreshed_holdings = {
                    t: q for t, q in refreshed_holdings.items() if t not in sold_this_cycle
                }
                if len(refreshed_holdings) < _before_sold:
                    logger.info(
                        f"[방어] D+2 미결제 매도종목 {_before_sold - len(refreshed_holdings)}개를 보유에서 제외했습니다."
                    )

            price_tickers = list(set(plan.get("target", [])) | set(refreshed_holdings.keys()))
            try:
                latest_prices_after = api.get_prices(price_tickers)
                latest_buy_prices_after = dict(latest_prices_after)
                latest_sell_prices_after = dict(latest_prices_after)
                if hasattr(api, "get_bid_ask_prices"):
                    bid_ask = api.get_bid_ask_prices(price_tickers)
                    for ticker in price_tickers:
                        row = bid_ask.get(ticker, {})
                        buy_price = row.get("buy_price")
                        sell_price = row.get("sell_price")
                        if buy_price is not None:
                            latest_buy_prices_after[ticker] = float(buy_price)
                        if sell_price is not None:
                            latest_sell_prices_after[ticker] = float(sell_price)
            except Exception as exc:
                logger.info(f"[경고] 매도 후 가격 재조회 실패: {exc}")
                latest_prices_after = {}
                latest_buy_prices_after = {}
                latest_sell_prices_after = {}

            # 예수금 안전 마진 적용: KIS는 nxdy_excc_amt(위탁증거금 차감 완료) 사용, Kiwoom은 dnca_tot_amt 사용
            # BUDGET_SAFETY_MARGIN_PCT 환경변수로 조정 가능
            _default_margin = 0.03 if broker_type == "KIS" else 0.07
            _budget_safety_margin = parse_pct_env("BUDGET_SAFETY_MARGIN_PCT", _default_margin)
            _effective_cash = (refreshed_cash if refreshed_cash is not None else 0.0) * (
                1.0 - _budget_safety_margin
            )
            try:
                new_orders = build_rebalance_orders(
                    current_holdings=refreshed_holdings,
                    target_tickers=plan.get("target", []),
                    latest_prices=latest_prices_after,
                    available_cash=_effective_cash,
                    latest_buy_prices=latest_buy_prices_after,
                    latest_sell_prices=latest_sell_prices_after,
                    max_positions=cfg.max_positions,
                    max_asset_pct=cfg.max_asset_pct,
                    sell_rank_buffer=cfg.sell_rank_buffer,
                    slippage=(
                        0.0
                        if (
                            api is not None
                            and cfg.enable_live_order
                            and not cfg.apply_slippage_in_live
                        )
                        else cfg.live_slippage_pct
                    ),
                    allow_empty_target_sell=not plan.get("risk_on", True),
                    sell_tax_pct=ETF_TAXABLE_SELL_TAX_PCT,
                    taxable_tickers=TAXABLE_ETF_TICKERS,
                    generate_orders=True,
                    ticker_names=plan.get("ticker_names", {}),
                    market_order_margin_rate=_market_order_margin_rate,
                )
                new_orders = _filter_buy_orders_by_live_guards(
                    new_orders,
                    latest_prices=latest_prices_after,
                    latest_buy_prices=latest_buy_prices_after,
                    latest_sell_prices=latest_sell_prices_after,
                    snapshot_nav=plan.get("snapshot_nav", {}),
                    config=cfg,
                    ticker_names=plan.get("ticker_names", {}),
                    guards_enabled=True,
                )
                new_buy_orders = [o for o in new_orders if o.get("side") == "BUY"]
                plan["buy_orders"] = new_buy_orders
                logger.info(
                    f"[정보] 매도 후 실제 상태로 매수 주문 재계산: 매수 후보={len(new_buy_orders)}건, "
                    f"예수금={refreshed_cash:,.0f} "
                    f"(안전마진 {_budget_safety_margin:.1%} 적용, 실효={_effective_cash:,.0f})"
                )
                # KIS: 각 매수 주문별로 개별 종목/가격 기준 nrcvb_buy_qty 조회하여 수량 제한 (재계산)
                if hasattr(api, "get_buyable_info"):
                    for o in plan["buy_orders"]:
                        if o.get("side") == "BUY":
                            _t = o.get("ticker")
                            _p = int(latest_buy_prices_after.get(_t, 0) or 0)
                            if _p > 0:
                                _info = api.get_buyable_info(_t, _p)
                                _nrcvb_qty = int(_info.get("nrcvb_buy_qty", "0"))
                                if _nrcvb_qty > 0 and o["qty"] > _nrcvb_qty:
                                    dn = o.get("display_name", _t)
                                    logger.info(
                                        f"[KIS제한-재계산] {dn} 수량 {o['qty']}→{_nrcvb_qty}주 (nrcvb_buy_qty)"
                                    )
                                    o["qty"] = _nrcvb_qty
                                    o["estimated_value"] = _nrcvb_qty * float(
                                        o.get("reference_price", 0)
                                    )
                # 복수 매수 주문 합계가 가용 현금 초과 시 마지막 주문 조정
                if len(plan["buy_orders"]) > 1 and refreshed_cash is not None:
                    _total_actual = sum(
                        int(o.get("qty", 0)) * float(o.get("reference_price", 0))
                        for o in plan["buy_orders"]
                    )
                    if _total_actual > refreshed_cash:
                        last = plan["buy_orders"][-1]
                        _price = float(last.get("reference_price", 0))
                        _qty = int(last.get("qty", 0))
                        if _price > 0 and _qty > 0:
                            _reduce = int((_total_actual - refreshed_cash) / _price) + 1
                            _reduce = min(_reduce, _qty - 1)
                            if _reduce > 0:
                                new_qty = _qty - _reduce
                                dn = last.get("display_name", last["ticker"])
                                logger.info(
                                    f"[현금초과] {dn} 수량 {_qty}→{new_qty}주 "
                                    f"(실합계 {_total_actual:,.0f} > 가용 {refreshed_cash:,.0f})"
                                )
                                last["qty"] = new_qty
                                last["estimated_value"] = new_qty * float(
                                    last.get("reference_price", 0)
                                )
            except Exception as exc:
                logger.info(f"[경고] 매도 후 주문 재계산 실패: {exc}")
        elif can_buy and (not dry_run) and api is not None and plan.get("needs_catchup", False):
            logger.info(
                f"[정보] 캐치업 모드: 기존 매수 계획 유지 ({len(plan.get('buy_orders', []))}건)"
            )
    else:
        logger.info("\n[주문] 매도 대상 없음 — 매도 단계를 건너뜁니다.")
        sell_results = []
        sell_retry_results = []
        refreshed_cash = None
        can_buy = True
        buy_results = []
        buy_retry_results = []
    logger.info("\n[주문] ─── 매수 단계 ───")
    if can_buy:
        # 중요도(랭킹) 순으로 매수 주문 정렬 — 1순위 종목이 먼저 제출되어 현금 확보
        _target_order = {t: i for i, t in enumerate(plan.get("target", []))}
        plan["buy_orders"].sort(key=lambda o: _target_order.get(o["ticker"], 999))
        # KIS simulated env: 순차 제출 (선행 주문의 cash reservation 반영을 위해 주문별로 get_buyable_info 재조회)
        _seq_submit = (
            (not dry_run)
            and api is not None
            and hasattr(api, "get_buyable_info")
            and len(plan["buy_orders"]) > 1
        )
        if _seq_submit:
            buy_results = []
            for _bo in plan["buy_orders"]:
                _bt = str(_bo.get("ticker", ""))
                _bp = int(_bo.get("reference_price", 0) or 0)
                if _bt and _bp > 0:
                    try:
                        _bi = api.get_buyable_info(_bt, _bp)
                        _bnq = int(_bi.get("nrcvb_buy_qty", "0"))
                        if _bnq > 0 and int(_bo.get("qty", 0)) > _bnq:
                            _bdn = _bo.get("display_name", _bt)
                            logger.info(
                                f"[KIS제한-순차] {_bdn} 수량 {_bo['qty']}→{_bnq}주 (잔여 nrcvb_buy_qty={_bnq})"
                            )
                            _bo["qty"] = _bnq
                            _bo["estimated_value"] = _bnq * float(_bo.get("reference_price", 0))
                    except Exception:
                        pass
                buy_results.extend(
                    _submit_orders(api, "BUY", [_bo], dry_run=dry_run, attempt=1, notifier=notifier)
                )
        else:
            buy_results = _submit_orders(
                api, "BUY", plan["buy_orders"], dry_run=dry_run, attempt=1, notifier=notifier
            )
        if buy_results:
            for r in buy_results:
                status_txt = (
                    "DRY_RUN"
                    if r.get("mode") == "DRY_RUN"
                    else ("제출완료" if r.get("submitted") else f"오류: {r.get('error')}")
                )
                logger.info(
                    f"  BUY  {r.get('display_name', r['ticker'])} {r['qty']}주 → {status_txt}"
                )
        if (not dry_run) and api is not None:
            logger.info(
                f"[주문] 매수 체결 대기 중 (타임아웃={cfg.buy_fill_timeout_sec}초, 컷오프={cfg.buy_cutoff_time})"
            )
            buy_results = _poll_and_finalize_orders(
                api=api,
                submitted_results=buy_results,
                timeout_sec=cfg.buy_fill_timeout_sec,
                poll_interval_sec=cfg.order_poll_interval_sec,
                cutoff_time_hhmm=cfg.buy_cutoff_time,
                cancel_unfilled_orders=cfg.cancel_unfilled_orders,
                notifier=notifier,
            )
            for r in buy_results:
                if r.get("is_filled"):
                    _status = "✓ 완료"
                elif not r.get("submitted"):
                    _status = f"✗ 오류({r.get('error_code', '')})"
                elif r.get("timed_out"):
                    _status = "✗ 미체결 (타임아웃)"
                else:
                    _status = "✗ 미체결"
                logger.info(
                    f"  BUY  {r.get('display_name', r['ticker'])} 체결={r.get('filled_qty', 0)}/{r.get('requested_qty', r.get('qty', 0))} "
                    f"{_status}"
                )
            if cfg.retry_unfilled_orders:
                for r in buy_results:
                    remain = int(r.get("remaining_qty", 0))
                    oid = str(r.get("order_id", "")).strip()
                    tid = str(r.get("ticker", "")).strip()
                    if remain > 0 and oid:
                        try:
                            api.cancel_order(order_id=oid, ticker=tid, qty=remain)
                            r["cancel_submitted"] = True
                        except Exception:
                            pass
                _wait_for_cancellations(api, buy_results)
                _refresh_order_statuses(api, buy_results)
                retry_buy_orders = _build_retry_orders(buy_results)
                failed_retry_orders = _build_failed_retry_orders(buy_results, api)
                all_retry_orders = retry_buy_orders + failed_retry_orders
                if all_retry_orders:
                    logger.info(
                        f"[재시도] 매수 재주문 {len(all_retry_orders)}건 "
                        f"(미체결 {len(retry_buy_orders)}, 실패재시도 {len(failed_retry_orders)})"
                    )
                    buy_retry_results = _submit_orders(
                        api,
                        "BUY",
                        all_retry_orders,
                        dry_run=False,
                        order_type=cfg.retry_order_type,
                        attempt=2,
                        notifier=notifier,
                    )
                    buy_retry_results = _poll_and_finalize_orders(
                        api=api,
                        submitted_results=buy_retry_results,
                        timeout_sec=cfg.retry_fill_timeout_sec,
                        poll_interval_sec=cfg.order_poll_interval_sec,
                        cutoff_time_hhmm=cfg.buy_cutoff_time,
                        cancel_unfilled_orders=cfg.cancel_unfilled_orders,
                        notifier=notifier,
                    )
    else:
        logger.info("[안전중단] 매도 주문이 전량 체결되지 않아 매수 주문을 제출하지 않습니다.")

    executed_orders = sell_results + sell_retry_results + buy_results + buy_retry_results

    if dry_run:
        run_status = "DONE_DRY_RUN"
    elif (
        can_buy
        and _is_side_fully_filled(sell_results, sell_retry_results)
        and _is_side_fully_filled(buy_results, buy_retry_results)
    ):
        run_status = "DONE"
    elif not can_buy:
        run_status = "BLOCKED_BUY_BY_UNFILLED_SELL"
    else:
        run_status = "PARTIAL_FILLED"

    new_state = {
        "trading_date": today,
        "run_id": run_id,
        "status": run_status,
        "generated_at": _now_kst().isoformat(),
        "risk_on": plan["risk_on"],
        "rebalance_due": plan["rebalance_due"],
        "target": plan["target"],
        "orders": executed_orders,
        "last_rebalance_date": today if plan["rebalance_due"] else state.get("last_rebalance_date"),
    }
    _save_state(new_state)
    try:
        _append_execution_log(executed_orders, run_id, today)
    except Exception as exc:
        logger.info(f"⚠️ 실행로그 기록 중 오류: {exc}")

    logger.info("=== 실행 결과 ===")
    logger.info(f"실행 상태: {run_status}")
    for r in sell_results + sell_retry_results:
        _es_s = (
            "완료"
            if r.get("is_filled")
            else (f"오류({r.get('error_code', '')})" if not r.get("submitted") else "미체결")
        )
        logger.info(
            f"  SELL {r.get('display_name', r['ticker'])} 체결={r.get('filled_qty', 0)}/{r.get('requested_qty', r.get('qty', 0))} "
            f"({_es_s}, {r.get('mode', '')})"
        )
    for r in buy_results + buy_retry_results:
        _es_b = (
            "완료"
            if r.get("is_filled")
            else (f"오류({r.get('error_code', '')})" if not r.get("submitted") else "미체결")
        )
        logger.info(
            f"  BUY  {r.get('display_name', r['ticker'])} 체결={r.get('filled_qty', 0)}/{r.get('requested_qty', r.get('qty', 0))} "
            f"({_es_b}, {r.get('mode', '')})"
        )
    logger.info(f"매도 제출 건수: {len(sell_results)}")
    logger.info(f"매수 제출 건수: {len(buy_results)}")
    logger.info(f"상태 파일: {STATE_PATH}")

    if notifier is not None:
        notifier.notify_daily_summary(
            trading_date=today,
            run_status=run_status,
            sell_results=sell_results + sell_retry_results,
            buy_results=buy_results + buy_retry_results,
        )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ETF 하루 1회 실행 러너")
    parser.add_argument(
        "--force-live",
        action="store_true",
        help=(
            "컷오프 이후에도 실주문 실행을 강제로 허용합니다. "
            "매우 위험하니 프로덕션에서 신중히 사용하세요."
        ),
    )
    args = parser.parse_args()

    if args.force_live:
        # 프로세스 환경변수로 안전차단 플래그를 덮어써서 컷오프 검사 우회
        os.environ["BLOCK_LIVE_AFTER_CUTOFF"] = "0"
        logger.warning("[경고] --force-live: 컷오프 안전차단을 우회합니다. 실제 주문이 발생합니다.")

    run_daily()
