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
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from etf_shared import build_rebalance_orders, get_strategy_config, rank_etfs

try:
    from live_trading.kiwoom_adapter import KiwoomAdapter
except Exception:
    KiwoomAdapter = None


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
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()

import pandas as pd
from pykrx import stock


STATE_DIR = PROJECT_ROOT / "runtime_state"
STATE_PATH = STATE_DIR / "etf_daily_state.json"

# 기본 실행 시각: 한국 시장 개장 직전
DEFAULT_PLAN_TIME = "08:50"
DEFAULT_MARKET_OPEN_TIME = "09:00"


@dataclass
class RunnerConfig:
    wait_until_open: bool
    enable_live_order: bool
    force: bool
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


def _read_env_config() -> RunnerConfig:
    strategy_cfg = get_strategy_config()
    return RunnerConfig(
        wait_until_open=_parse_bool("WAIT_UNTIL_MARKET_OPEN", True),
        enable_live_order=_parse_bool("LIVE_ORDER_ENABLED", False),
        force=_parse_bool("DAILY_RUN_FORCE", False),
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
        cancel_unfilled_orders=_parse_bool("CANCEL_UNFILLED_ORDERS", True),
        retry_unfilled_orders=_parse_bool("RETRY_UNFILLED_ORDERS", True),
        retry_order_type=os.environ.get("RETRY_ORDER_TYPE", "MARKET").upper(),
        retry_fill_timeout_sec=int(os.environ.get("RETRY_FILL_TIMEOUT_SEC", "90")),
    )


def _cutoff_deadline(cutoff_time_hhmm: str, timeout_sec: int) -> dt.datetime:
    now = _now_kst()
    timeout_deadline = now + dt.timedelta(seconds=max(timeout_sec, 0))
    cutoff_time = _parse_hhmm(cutoff_time_hhmm)
    cutoff_dt = dt.datetime.combine(now.date(), cutoff_time, tzinfo=now.tzinfo)
    return min(timeout_deadline, cutoff_dt)


def _wait_until(target_time_hhmm: str) -> None:
    target_time = _parse_hhmm(target_time_hhmm)
    now = _now_kst()
    target_dt = dt.datetime.combine(now.date(), target_time, tzinfo=now.tzinfo)

    if now >= target_dt:
        return

    seconds = int((target_dt - now).total_seconds())
    print(f"[대기] 장 시작 전까지 {seconds}초 대기합니다. 목표 시각={target_time_hhmm}")

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
        return pd.DataFrame(columns=["date", "ticker", "open", "close", "volume", "trading_value"])

    data = df.reset_index().rename(
        columns={
            "날짜": "date",
            "시가": "open",
            "종가": "close",
            "거래량": "volume",
            "거래대금": "trading_value",
        }
    )
    data["ticker"] = ticker

    if "volume" not in data:
        data["volume"] = 0
    if "trading_value" not in data:
        data["trading_value"] = data["close"] * data["volume"]

    out = data[["date", "ticker", "open", "close", "volume", "trading_value"]].copy()
    out["date"] = pd.to_datetime(out["date"])
    return out


def _load_snapshot(etf_list: list[str], lookback_days: int = 220) -> pd.DataFrame:
    end_day = _today_kst()
    start_day = end_day - dt.timedelta(days=lookback_days)
    start = _date_to_krx(start_day)
    end = _date_to_krx(end_day)

    frames: list[pd.DataFrame] = []
    for ticker in etf_list:
        raw = stock.get_market_ohlcv_by_date(start, end, ticker)
        price = _normalize_ohlcv(raw, ticker)
        if price.empty:
            continue
        frames.append(price)

    if not frames:
        raise RuntimeError("ETF 가격 데이터가 비어 있습니다.")

    price_df = pd.concat(frames, ignore_index=True).sort_values(["ticker", "date"]).copy()
    grouped = price_df.groupby("ticker")
    price_df["ret_60"] = grouped["close"].pct_change(60)
    price_df["ret_120"] = grouped["close"].pct_change(120)
    price_df["ma20"] = grouped["close"].transform(lambda x: x.rolling(20).mean())
    price_df["ma60"] = grouped["close"].transform(lambda x: x.rolling(60).mean())
    price_df["trend_ok"] = (price_df["close"] > price_df["ma20"]) & (price_df["ma20"] > price_df["ma60"])

    snapshot = price_df.groupby("ticker").tail(1).reset_index(drop=True)
    return snapshot


def _load_market_risk_on(market_index_code: str, ma_days: int, slope_days: int) -> bool:
    end_day = _today_kst()
    start_day = end_day - dt.timedelta(days=260)
    idx = stock.get_index_ohlcv_by_date(_date_to_krx(start_day), _date_to_krx(end_day), market_index_code)
    if idx is None or idx.empty:
        return True

    idx = idx.reset_index().rename(columns={"날짜": "date", "종가": "close"})
    idx["date"] = pd.to_datetime(idx["date"])
    idx = idx.sort_values("date").copy()

    idx["market_ma"] = idx["close"].rolling(ma_days).mean()
    idx["market_ma_slope"] = idx["market_ma"] - idx["market_ma"].shift(slope_days)
    last = idx.iloc[-1]
    if pd.isna(last["market_ma"]) or pd.isna(last["market_ma_slope"]):
        return True
    return bool((last["close"] >= last["market_ma"]) and (last["market_ma_slope"] >= 0))


def _load_recent_trading_dates(reference_ticker: str, lookback_days: int = 120) -> list[str]:
    end_day = _today_kst()
    start_day = end_day - dt.timedelta(days=lookback_days)
    df = stock.get_market_ohlcv_by_date(_date_to_krx(start_day), _date_to_krx(end_day), reference_ticker)
    data = _normalize_ohlcv(df, reference_ticker)
    if data.empty:
        return []
    return [d.date().isoformat() for d in sorted(data["date"].unique())]


def _should_rebalance(today: str, state: dict[str, Any], step_days: int, reference_ticker: str) -> bool:
    last = state.get("last_rebalance_date")
    if not last:
        return True

    trading_dates = _load_recent_trading_dates(reference_ticker=reference_ticker, lookback_days=220)
    if today not in trading_dates or last not in trading_dates:
        # 거래일 캘린더가 불완전한 경우 보수적으로 리밸런스하지 않음
        return False

    last_idx = trading_dates.index(last)
    today_idx = trading_dates.index(today)
    return (today_idx - last_idx) >= step_days


def _build_plan(config: RunnerConfig, api: Any | None) -> dict[str, Any]:
    strategy_cfg = get_strategy_config()
    etf_list = strategy_cfg["etf_list"]

    if api is None:
        # 어댑터가 없으면 최소한의 안전한 모의 데이터로 판단 단계만 수행
        holdings = {"069500": 10}
        cash = 1_000_000.0
        latest_prices = {ticker: 100000.0 for ticker in etf_list}
    else:
        holdings = api.get_holdings()
        cash = float(api.get_cash())
        latest_prices = api.get_prices(etf_list)

    snapshot = _load_snapshot(etf_list)
    ranked = rank_etfs(snapshot)

    risk_on = True
    if config.market_filter:
        risk_on = _load_market_risk_on(
            market_index_code=strategy_cfg["market_index_code"],
            ma_days=config.market_ma_days,
            slope_days=config.market_slope_days,
        )

    state = _load_state()
    today = _date_to_iso(_today_kst())
    rebalance_due = _should_rebalance(
        today=today,
        state=state,
        step_days=config.rebalance_step_days,
        reference_ticker=etf_list[0],
    )

    if (not risk_on) or (not rebalance_due):
        target = []
    else:
        target = ranked.head(config.max_positions)["ticker"].tolist() if not ranked.empty else []

    orders = build_rebalance_orders(
        current_holdings=holdings,
        target_tickers=target,
        latest_prices=latest_prices,
        available_cash=cash,
        max_positions=config.max_positions,
        sell_rank_buffer=config.sell_rank_buffer,
    )

    sell_orders = [o for o in orders if o.get("side") == "SELL"]
    buy_orders = [o for o in orders if o.get("side") == "BUY"]

    return {
        "today": today,
        "risk_on": risk_on,
        "rebalance_due": rebalance_due,
        "holdings": holdings,
        "cash": cash,
        "target": target,
        "ranked_top": ranked.head(10).to_dict(orient="records") if not ranked.empty else [],
        "sell_orders": sell_orders,
        "buy_orders": buy_orders,
        "all_orders": orders,
    }


def _submit_orders(
    api: Any,
    side: str,
    orders: list[dict[str, Any]],
    dry_run: bool,
    order_type: str = "MARKET",
    attempt: int = 1,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    for order in orders:
        ticker = str(order.get("ticker"))
        qty = int(order.get("qty", 0))
        if qty <= 0:
            continue

        if dry_run:
            results.append(
                {
                    "ticker": ticker,
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
            results.append(
                {
                    "ticker": ticker,
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
                    "order_id": response.get("order_id", ""),
                    "response": response.get("response", {}),
                }
            )
        except Exception as exc:
            results.append(
                {
                    "ticker": ticker,
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
                }
            )

    return results


def _poll_and_finalize_orders(
    api: Any,
    submitted_results: list[dict[str, Any]],
    timeout_sec: int,
    poll_interval_sec: int,
    cutoff_time_hhmm: str,
    cancel_unfilled_orders: bool,
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

            row["requested_qty"] = req_qty
            row["filled_qty"] = max(filled_qty, 0)
            row["remaining_qty"] = max(req_qty - filled_qty, 0)
            row["is_filled"] = bool(status.get("is_filled", False)) or (row["remaining_qty"] == 0)
            row["last_status"] = status

            if row["is_filled"]:
                done_idx.append(i)

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
            except Exception as exc:
                row["cancel_submitted"] = False
                row["cancel_error"] = str(exc)
        else:
            row["cancel_submitted"] = False
            row["cancel_skipped"] = True

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


def _build_retry_orders(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    retry_orders: list[dict[str, Any]] = []
    for row in results:
        req_qty = int(row.get("requested_qty", row.get("qty", 0)))
        filled_qty = int(row.get("filled_qty", 0))
        remaining_qty = max(req_qty - filled_qty, 0)
        ticker = str(row.get("ticker", "")).strip()
        if not ticker or remaining_qty <= 0:
            continue
        retry_orders.append(
            {
                "ticker": ticker,
                "qty": remaining_qty,
                "retry_from_order_id": row.get("order_id", ""),
            }
        )
    return retry_orders


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
    print("=== ETF 하루 1회 실행 계획 ===")
    print(f"기준일: {plan['today']}")
    print(f"risk_on: {plan['risk_on']}")
    print(f"rebalance_due: {plan['rebalance_due']}")
    print(f"실주문 모드: {'ON' if cfg.enable_live_order else 'OFF'}")
    print(f"매도 컷오프: {cfg.sell_cutoff_time}, 매수 컷오프: {cfg.buy_cutoff_time}")
    print(f"미체결 재주문: {'ON' if cfg.retry_unfilled_orders else 'OFF'} ({cfg.retry_order_type})")
    print(f"보유종목수: {len(plan['holdings'])}, 예수금: {plan['cash']:,.0f}")
    print(f"목표 티커: {plan['target']}")
    print(f"매도 주문 수: {len(plan['sell_orders'])}, 매수 주문 수: {len(plan['buy_orders'])}")


def run_daily() -> None:
    cfg = _read_env_config()
    state = _load_state()
    today = _date_to_iso(_today_kst())

    if not _is_weekday(_today_kst()):
        print("[종료] 주말이므로 실행하지 않습니다.")
        return

    if (
        (not cfg.force)
        and state.get("trading_date") == today
        and state.get("status") in {"DONE", "NO_ACTION"}
    ):
        print("[종료] 오늘은 이미 실행 완료 상태입니다. DAILY_RUN_FORCE=1로 강제 실행할 수 있습니다.")
        return

    now = _now_kst().time()
    plan_time = _parse_hhmm(cfg.plan_time)
    if now < plan_time:
        print(f"[대기] 계획 수립 시각({cfg.plan_time}) 전입니다. 해당 시각까지 대기합니다.")
        _wait_until(cfg.plan_time)

    api = None
    if KiwoomAdapter is not None:
        try:
            api = KiwoomAdapter()
        except Exception as exc:
            print(f"[경고] 키움 어댑터 초기화 실패, 안전 모드로 계속 진행합니다: {exc}")

    run_id = str(uuid.uuid4())
    plan = _build_plan(cfg, api)
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
        print("[완료] 오늘은 실행할 주문이 없습니다.")
        return

    if cfg.wait_until_open:
        _wait_until(cfg.market_open_time)

    dry_run = (not cfg.enable_live_order) or (api is None)
    if dry_run:
        print("[안전모드] LIVE_ORDER_ENABLED=0 또는 어댑터 미사용 상태입니다. 실제 주문은 전송하지 않습니다.")

    # 1) 매도 우선
    sell_results = _submit_orders(api, "SELL", plan["sell_orders"], dry_run=dry_run, attempt=1)
    sell_retry_results: list[dict[str, Any]] = []

    if (not dry_run) and api is not None:
        sell_results = _poll_and_finalize_orders(
            api=api,
            submitted_results=sell_results,
            timeout_sec=cfg.sell_fill_timeout_sec,
            poll_interval_sec=cfg.order_poll_interval_sec,
            cutoff_time_hhmm=cfg.sell_cutoff_time,
            cancel_unfilled_orders=cfg.cancel_unfilled_orders,
        )
        if cfg.retry_unfilled_orders:
            retry_sell_orders = _build_retry_orders(sell_results)
            if retry_sell_orders:
                print(f"[재시도] 매도 잔량 재주문 {len(retry_sell_orders)}건 제출")
                sell_retry_results = _submit_orders(
                    api,
                    "SELL",
                    retry_sell_orders,
                    dry_run=False,
                    order_type=cfg.retry_order_type,
                    attempt=2,
                )
                sell_retry_results = _poll_and_finalize_orders(
                    api=api,
                    submitted_results=sell_retry_results,
                    timeout_sec=cfg.retry_fill_timeout_sec,
                    poll_interval_sec=cfg.order_poll_interval_sec,
                    cutoff_time_hhmm=cfg.sell_cutoff_time,
                    cancel_unfilled_orders=cfg.cancel_unfilled_orders,
                )

    # 2) 매도 후 예수금 재확인(실주문 모드에서만)
    if (not dry_run) and api is not None:
        try:
            refreshed_cash = _safe_float(api.get_cash())
            print(f"[정보] 매도 후 예수금 재조회: {refreshed_cash:,.0f}")
        except Exception as exc:
            print(f"[경고] 매도 후 예수금 재조회 실패: {exc}")

    # 3) 매도 미완전체결이면 매수 차단
    can_buy = dry_run or _is_side_fully_filled(sell_results, sell_retry_results)
    buy_results: list[dict[str, Any]] = []
    buy_retry_results: list[dict[str, Any]] = []
    if can_buy:
        buy_results = _submit_orders(api, "BUY", plan["buy_orders"], dry_run=dry_run, attempt=1)
        if (not dry_run) and api is not None:
            buy_results = _poll_and_finalize_orders(
                api=api,
                submitted_results=buy_results,
                timeout_sec=cfg.buy_fill_timeout_sec,
                poll_interval_sec=cfg.order_poll_interval_sec,
                cutoff_time_hhmm=cfg.buy_cutoff_time,
                cancel_unfilled_orders=cfg.cancel_unfilled_orders,
            )
            if cfg.retry_unfilled_orders:
                retry_buy_orders = _build_retry_orders(buy_results)
                if retry_buy_orders:
                    print(f"[재시도] 매수 잔량 재주문 {len(retry_buy_orders)}건 제출")
                    buy_retry_results = _submit_orders(
                        api,
                        "BUY",
                        retry_buy_orders,
                        dry_run=False,
                        order_type=cfg.retry_order_type,
                        attempt=2,
                    )
                    buy_retry_results = _poll_and_finalize_orders(
                        api=api,
                        submitted_results=buy_retry_results,
                        timeout_sec=cfg.retry_fill_timeout_sec,
                        poll_interval_sec=cfg.order_poll_interval_sec,
                        cutoff_time_hhmm=cfg.buy_cutoff_time,
                        cancel_unfilled_orders=cfg.cancel_unfilled_orders,
                    )
    else:
        print("[안전중단] 매도 주문이 전량 체결되지 않아 매수 주문을 제출하지 않습니다.")

    executed_orders = sell_results + sell_retry_results + buy_results + buy_retry_results

    if dry_run:
        run_status = "DONE_DRY_RUN"
    elif can_buy and _is_side_fully_filled(sell_results, sell_retry_results) and _is_side_fully_filled(buy_results, buy_retry_results):
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

    print("=== 실행 결과 ===")
    print(f"실행 상태: {run_status}")
    print(f"매도 제출 건수: {len(sell_results)}")
    print(f"매수 제출 건수: {len(buy_results)}")
    print(f"상태 파일: {STATE_PATH}")


if __name__ == "__main__":
    run_daily()
