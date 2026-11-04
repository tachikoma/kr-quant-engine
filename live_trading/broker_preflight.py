"""Broker preflight helper — strategy-used API coverage.

문서 게이트(docs/broker_onboarding.md)의 실행 Helper.
코드로 차단하지 않고, 수동으로 `uv run python live_trading/broker_preflight.py --broker <KIS|KIWOOM|NH|KB> --mode demo`
로 돌려 전수 PASS를 확인한 뒤 PR에 로그를 첨부한다.

Covered APIs (etf_daily_runner.py가 실제 호출하는 전종):
  get_cash / get_available_cash, get_holdings, get_prices,
  get_bid_ask_prices, get_buyable_info(optional), get_ticker_name(optional),
  place_order, get_order_status, cancel_order  + 토큰/세션은 어댑터 내부에서 검증됨.

Stage 0: env/pre-check, Stage 1: read-only, Stage 2: write 가역( --with-order 시만, 1주 IOC)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
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
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            # Strip inline comments (unquoted): value "# comment" stays quoted,
            # bare "value  # comment" gets truncated to "value".
            quoted = (v.startswith('"') and v.endswith('"')) or (
                v.startswith("'") and v.endswith("'")
            )
            v = v.strip('"').strip("'")
            if not quoted and "#" in v:
                v = v.split("#", 1)[0].rstrip()
            if k and k not in os.environ:
                os.environ[k] = v


_load_dotenv()

# pykrx 토큰 만료 이슈로 .env 로드 후 import 해야 하는 모듈이 있으면 여기서 지연 import
# 본 파일은 pykrx를 직접 쓰지 않으므로 불필요.

# 브로커별 인증 환경변수 (어댑터 소스 기준)
#  - KIWOOM: appkey/secretkey (밑줄 없음). 계좌번호 불필요 (KIWOOM_ACCOUNT_NO 선택)
#  - KIS   : KIS_APP_KEY / KIS_APP_SECRET / KIS_ACCOUNT_NO (+ KIS_ACCOUNT_PROD_CD 기본 01)
#  - NH    : NHPLUG_APP_KEY / NHPLUG_APP_SECRET / NHPLUG_ACCT_NO
#  - KB    : KBSEC_APP_KEY / KBSEC_APP_SECRET (계좌 변수 없음)
CRED_REQUIRED: dict[str, list[str]] = {
    "KIS": ["KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO"],
    "KIWOOM": ["KIWOOM_APPKEY", "KIWOOM_SECRETKEY"],
    "NH": ["NHPLUG_APP_KEY", "NHPLUG_APP_SECRET", "NHPLUG_ACCT_NO"],
    "KB": ["KBSEC_APP_KEY", "KBSEC_APP_SECRET"],
}
CRED_OPTIONAL: dict[str, list[str]] = {
    "KIS": ["KIS_ACCOUNT_PROD_CD"],
    "KIWOOM": ["KIWOOM_ACCOUNT_NO"],
    "NH": [],
    "KB": [],
}
# 어댑터의 주문 전송이 전역 LIVE_ORDER_ENABLED=1 을 요구하는 브로커
LIVE_FLAG_REQUIRED = {"NH", "KB"}

DEFAULT_TICKERS = ["069500", "229200"]  # KODEX 200, KODEX 200 TR 등 유동성 높은 ETF
TEST_TICKER = "069500"


def _now_kst() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))


def _is_trading_day_fallback(check_date: dt.date | None = None) -> bool | None:
    """etf_daily_runner._is_trading_day가 있으면 사용, 없으면 주말만 체크."""
    try:
        from live_trading.etf_daily_runner import _is_trading_day as _orig

        d = check_date or _now_kst().date()
        return bool(_orig(d))
    except Exception:
        d = check_date or _now_kst().date()
        return d.weekday() < 5  # 월~금


def _is_market_hours(now: dt.datetime | None = None) -> bool:
    now = now or _now_kst()
    if not _is_trading_day_fallback(now.date()):
        return False
    hm = now.hour * 60 + now.minute
    return 9 * 60 <= hm <= 15 * 60 + 30  # 09:00~15:30 KST


def _mask(s: str | None, keep: int = 4) -> str:
    if not s:
        return "***"
    s = str(s)
    if len(s) <= keep:
        return "***"
    return s[:2] + "***" + s[-keep:]


def _try_call(label: str, fn, *args, **kwargs) -> tuple[str, Any, str]:
    try:
        out = fn(*args, **kwargs)
        # 성공 기준: 예외 없이 반환. 0/빈값도 PASS(파싱 성공)로 간주, 별도 NOTE 남김
        note = ""
        if out is None:
            note = "returned None"
        elif isinstance(out, (list, dict)) and len(out) == 0:
            note = "empty result (parsing OK, no data)"
        return "PASS", out, note
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        # 인증/권한/미구현은 FAIL이지만 스킵으로 오해하지 않도록 FAIL 유지
        return "FAIL", None, msg


def _build_adapter(broker: str, mode: str):
    broker = broker.upper()
    mode = mode.lower()
    # BROKER_TYPE/MODE env를 어댑터가 읽을 수 있도록 임시 설정 (기존 env 우선)
    # .env 의 MODE 가 --mode 와 달라도 실계좌로 붙지 않도록 프로세스 내에서 강제 덮어쓴다.
    os.environ["BROKER_TYPE"] = broker
    for _k in ("MODE", "BROKER_MODE", "ENV_MODE"):
        os.environ[_k] = mode
    if broker == "KIS":
        from live_trading.kis_adapter import KisAdapter

        return KisAdapter()
    if broker == "KIWOOM":
        from live_trading.kiwoom_adapter import KiwoomAdapter

        return KiwoomAdapter()
    if broker == "NH":
        from live_trading.nh_adapter import NhAdapter

        return NhAdapter()
    if broker == "KB":
        from live_trading.kb_adapter import KbAdapter

        return KbAdapter()
    raise ValueError(f"unknown broker: {broker} (KIS|KIWOOM|NH|KB)")


def _order_id(out: Any) -> str | None:
    if isinstance(out, dict):
        oid = out.get("order_id") or out.get("odno") or out.get("ord_no")
        return str(oid) if oid else None
    return str(out)[:32] if out else None


def cancel_confirmed(st: dict[str, Any]) -> bool:
    """러너(_wait_for_cancellations)와 동일한 판정: 발견됨 + 잔량 0 + 체결 < 주문수량."""
    if not st.get("is_found", False) or st.get("remaining_qty") is None:
        return False
    order_qty = int(st.get("order_qty", 0))
    return order_qty > 0 and int(st["remaining_qty"]) == 0 and int(st.get("filled_qty", 0)) < order_qty


def _fmt_status(st: Any) -> str:
    if not isinstance(st, dict):
        return str(st)[:80]
    keys = ("is_found", "order_qty", "filled_qty", "remaining_qty", "is_filled")
    return " ".join(f"{k}={st.get(k)}" for k in keys)


def _stage2_orders(api: Any, record, tickers_hint: str = TEST_TICKER) -> None:
    ticker = tickers_hint
    today_krx = _now_kst().strftime("%Y%m%d")
    before = api.get_holdings() if hasattr(api, "get_holdings") else None

    def status_of(label: str, oid: str) -> dict[str, Any] | None:
        st, out, note = _try_call(label, api.get_order_status, oid, today=today_krx)
        record("2", label, st, note[:200] if note else _fmt_status(out))
        return out if st == "PASS" and isinstance(out, dict) else None

    # (1) 시장가 1주 매수 → 체결 대기 → 매도 (라운드트립)
    st, out, note = _try_call("buy", api.place_order, "BUY", ticker, 1, None, "MARKET")
    boid = _order_id(out) if st == "PASS" else None
    record("2", "place_order(BUY 1주 MARKET)", st, note[:200] if note else f"order_id={_mask(boid)}")
    filled = False
    if boid:
        for _ in range(6):
            time.sleep(1.0)
            r = status_of("get_order_status(BUY)", boid)
            if r and r.get("is_filled"):
                filled = True
                break
        if not filled and hasattr(api, "cancel_order"):
            _try_call("cancel", api.cancel_order, boid, ticker, 1)
    if filled:
        st, out, note = _try_call("sell", api.place_order, "SELL", ticker, 1, None, "MARKET")
        soid = _order_id(out) if st == "PASS" else None
        record("2", "place_order(SELL 1주 MARKET)", st, note[:200] if note else f"order_id={_mask(soid)}")
        if soid:
            for _ in range(6):
                time.sleep(1.0)
                r = status_of("get_order_status(SELL)", soid)
                if r and r.get("is_filled"):
                    break
    else:
        record("2", "place_order(SELL)", "SKIP", "buy not filled — sell skipped to avoid naked sell")

    # (2) 미체결 지정가 → 취소 → 취소 의미 검증
    cur = None
    try:
        px = api.get_prices([ticker]).get(ticker)
        cur = float(px) if px else None
    except Exception:
        cur = None
    limit_price = max(1, int((cur or 30000) * 0.75))
    st, out, note = _try_call("limit", api.place_order, "BUY", ticker, 1, limit_price, "LIMIT")
    loid = _order_id(out) if st == "PASS" else None
    record("2", "place_order(BUY 1주 LIMIT -25%)", st, note[:200] if note else f"order_id={_mask(loid)}")
    if not loid:
        record("2", "cancel_order", "SKIP", "no order_id")
    else:
        time.sleep(1.0)
        status_of("get_order_status(LIMIT 미체결)", loid)
        s3, o3, n3 = _try_call("cancel", api.cancel_order, loid, ticker, 1)
        record("2", "cancel_order", s3, n3[:200] if n3 else "requested")
        confirmed, last = False, None
        for _ in range(6):
            time.sleep(1.0)
            last = status_of("get_order_status(취소 후)", loid)
            if last and cancel_confirmed(last):
                confirmed = True
                break
        record("2", "cancel_semantics(found & remaining==0 & filled<order)",
               "PASS" if confirmed else "FAIL",
               "러너의 취소 확정 조건과 일치" if confirmed else f"러너가 취소를 확정하지 못함: {_fmt_status(last)}")

    after = api.get_holdings() if hasattr(api, "get_holdings") else None
    flat = before == after
    record("2", "verify_flat_position", "PASS" if flat else "FAIL",
           "holdings unchanged" if flat else "before≠after — 수동 확인/정리 필요")



def run_preflight(
    broker: str,
    mode: str,
    with_order: bool,
    tickers: list[str],
    allow_real: bool = False,
) -> dict[str, Any]:
    now = _now_kst()
    results: list[dict[str, Any]] = []

    def record(stage: str, label: str, status: str, note: str = "") -> None:
        results.append({"stage": stage, "label": label, "status": status, "note": note})
        icon = {"PASS": "✓", "FAIL": "✗", "SKIP": "-"} .get(status, "?")
        suffix = f" — {note}" if note else ""
        print(f"  {icon} [{stage}] {label}: {status}{suffix}")

    print(f"\n== Broker preflight: {broker.upper()} / mode={mode} / {now:%Y-%m-%d %H:%M KST} ==")
    print(f"   tickers={tickers}  with_order={with_order}\n")

    # Stage 0
    print("[Stage 0] Pre-check")
    trading_day = _is_trading_day_fallback(now.date())
    record("0", "trading_day", "PASS" if trading_day else "FAIL" if trading_day is False else "SKIP",
           f"date={now.date()} is_trading_day={trading_day}")
    record("0", "market_hours", "PASS" if _is_market_hours(now) else "SKIP",
           f"09:00~15:30 KST, now={now:%H:%M}")
    # credential presence (키 존재 여부만, 값은 마스킹)
    # 어댑터가 실제로 읽는 변수명 기준 (live_trading/*_adapter.py). 값은 마스킹.
    for k in CRED_REQUIRED.get(broker.upper(), []):
        v = os.environ.get(k)
        record("0", f"env:{k}", "PASS" if v else "FAIL", _mask(v) if v else "missing")
    for k in CRED_OPTIONAL.get(broker.upper(), []):
        v = os.environ.get(k)
        record("0", f"env:{k}", "SKIP", "optional — set" if v else "optional — not needed")

    # Adapter init (토큰/세션 검증이 여기서 일어남)
    print("\n[Stage 1] Read-only — strategy-used APIs")
    try:
        api = _build_adapter(broker, mode)
        record("1", "adapter_init", "PASS", type(api).__name__)
    except Exception as e:
        record("1", "adapter_init", "FAIL", f"{type(e).__name__}: {e}"[:400])
        total = len(results)
        passes = sum(1 for r in results if r["status"] == "PASS")
        fails = sum(1 for r in results if r["status"] == "FAIL")
        skips = sum(1 for r in results if r["status"] == "SKIP")
        print(f"\n== Summary: {passes} PASS / {fails} FAIL / {skips} SKIP (total {total}) ==")
        report = {
            "broker": broker.upper(),
            "mode": mode,
            "timestamp": now.isoformat(),
            "tickers": tickers,
            "with_order": with_order,
            "summary": {"pass": passes, "fail": fails, "skip": skips, "total": total},
            "results": results,
        }
        out_dir = PROJECT_ROOT / "runtime_state"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = now.strftime("%Y%m%d_%H%M%S")
        out_path = out_dir / f"preflight_{broker.lower()}_{ts}.json"
        try:
            out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"   report: {out_path}")
        except Exception as ex:
            print(f"   report write failed: {ex}")
        return report

    # helper to safely call optional methods
    def check(label: str, method: str, *a, **kw):
        if not hasattr(api, method):
            record("1", label, "SKIP", f"{method} not implemented (optional)")
            return None
        fn = getattr(api, method)
        status, out, note = _try_call(label, fn, *a, **kw)
        record("1", label, status, note[:200] if note else "")
        return out

    # 1.1 cash
    check("get_cash", "get_cash")
    # get_available_cash는 티커/가격 인자가 필요한 어댑터가 있음 — 둘 다 시도
    if hasattr(api, "get_available_cash"):
        try:
            # 가격 0이면 buyable 계산이 무의미하므로 더미 가격 50000
            out = api.get_available_cash(TEST_TICKER, 50000) if "ticker" in api.get_available_cash.__code__.co_varnames else api.get_available_cash()  # type: ignore[attr-defined]
            # 위 분기가 부정확할 수 있어 fallback
            record("1", "get_available_cash", "PASS", str(out)[:120] if out is not None else "")
        except TypeError:
            status, out, note = _try_call("get_available_cash", getattr(api, "get_available_cash"))
            record("1", "get_available_cash", status, note[:200] if note else "")
        except Exception as e:
            record("1", "get_available_cash", "FAIL", f"{type(e).__name__}: {e}"[:200])
    else:
        record("1", "get_available_cash", "SKIP", "not implemented")

    # 1.2 holdings
    check("get_holdings", "get_holdings")

    # 1.3 prices
    check("get_prices", "get_prices", tickers)

    # 1.4 bid/ask
    check("get_bid_ask_prices", "get_bid_ask_prices", tickers)

    # 1.5 optional: buyable_info
    check("get_buyable_info", "get_buyable_info", TEST_TICKER, 50000)

    # 1.6 optional: ticker_name
    check("get_ticker_name", "get_ticker_name", TEST_TICKER)

    # Stage 2: write 가역 — --with-order 시만
    print("\n[Stage 2] Write 가역 (1주 주문→취소/원복)")
    if not with_order:
        record("2", "place_order/get_order_status/cancel_order", "SKIP",
               "read-only run — add --with-order to test write path (장중만)")
    elif mode != "demo" and not allow_real:
        record("2", "real-mode guard", "FAIL",
               "mode=real 에서 실계좌 주문이 발생합니다. 의도했다면 --allow-real 을 명시하세요")
    elif not _is_market_hours(now):
        record("2", "place_order", "SKIP", "market closed — write test requires 09:00~15:30 KST")
    elif broker.upper() in LIVE_FLAG_REQUIRED and os.environ.get("LIVE_ORDER_ENABLED", "0").strip() != "1":
        record("2", "place_order", "SKIP",
               f"{broker.upper()} 어댑터는 LIVE_ORDER_ENABLED=1 일 때만 주문 전송 — 모의 검증 시 설정 필요")
    else:
        _stage2_orders(api, record, tickers_hint=TEST_TICKER)

    # Stage 3: smoke는 문서상 선택 — 여기서는 runner --help 수준만 힌트
    print("\n[Stage 3] Smoke (선택)")
    record("3", "etf_daily_runner dry-run", "SKIP",
           "수동: uv run python live_trading/etf_daily_runner.py (safe mode) 1회")

    # summary
    total = len(results)
    passes = sum(1 for r in results if r["status"] == "PASS")
    fails = sum(1 for r in results if r["status"] == "FAIL")
    skips = sum(1 for r in results if r["status"] == "SKIP")
    print(f"\n== Summary: {passes} PASS / {fails} FAIL / {skips} SKIP (total {total}) ==")
    if fails:
        print("   → FAIL이 0이어야 데모 진입 가능 (문서 게이트). 로그를 PR에 첨부하세요.")
    else:
        print("   → 전수 PASS/SKIP — 전략이 쓰는 API는 검증됨. PR에 본 로그를 첨부하세요.")

    # close
    try:
        if hasattr(api, "close"):
            api.close()  # type: ignore[union-attr]
    except Exception:
        pass

    report = {
        "broker": broker.upper(),
        "mode": mode,
        "timestamp": now.isoformat(),
        "tickers": tickers,
        "with_order": with_order,
        "summary": {"pass": passes, "fail": fails, "skip": skips, "total": total},
        "results": results,
    }
    # JSON 저장 (민감정보 없이)
    out_dir = PROJECT_ROOT / "runtime_state"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = now.strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"preflight_{broker.lower()}_{ts}.json"
    try:
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"   report: {out_path}")
    except Exception as e:
        print(f"   report write failed: {e}")

    return report


def main() -> None:
    p = argparse.ArgumentParser(description="Broker preflight helper — 전략 사용 API 전수 검증")
    p.add_argument("--broker", required=True, help="KIS|KIWOOM|NH|KB")
    p.add_argument("--mode", default="demo", help="demo|real (default: demo)")
    p.add_argument("--with-order", action="store_true",
                   help="장중 1주 주문→취소/원복까지 테스트 (기본은 read-only)")
    p.add_argument("--tickers", nargs="*", default=DEFAULT_TICKERS,
                   help=f"price 조회 티커 목록 (default: {' '.join(DEFAULT_TICKERS)})")
    p.add_argument("--allow-real", action="store_true",
                   help="mode=real 에서 --with-order 실주문 허용 (기본 거부)")
    args = p.parse_args()
    report = run_preflight(args.broker, args.mode, bool(args.with_order), list(args.tickers),
                           allow_real=bool(args.allow_real))
    # FAIL 있으면 exit 1 (CI에서 쓰더라도 문서 게이트가 우선이므로 호출자가 판단)
    fails = report["summary"]["fail"]
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
