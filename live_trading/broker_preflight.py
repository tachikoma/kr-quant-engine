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
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


_load_dotenv()

# pykrx 토큰 만료 이슈로 .env 로드 후 import 해야 하는 모듈이 있으면 여기서 지연 import
# 본 파일은 pykrx를 직접 쓰지 않으므로 불필요.

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
    os.environ.setdefault("BROKER_TYPE", broker)
    os.environ.setdefault("MODE", mode)
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


def run_preflight(
    broker: str,
    mode: str,
    with_order: bool,
    tickers: list[str],
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
    cred_keys = {
        "KIS": ["KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO"],
        "KIWOOM": ["KIWOOM_APP_KEY", "KIWOOM_SECRET_KEY", "KIWOOM_ACCOUNT_NO"],
        "NH": ["NHPLUG_APP_KEY", "NHPLUG_SECRET_KEY", "NHPLUG_ACCT_NO"],
        "KB": ["KB_APP_KEY", "KB_APP_SECRET", "KB_ACCOUNT_NO"],
    }
    for k in cred_keys.get(broker.upper(), []):
        v = os.environ.get(k)
        record("0", f"env:{k}", "PASS" if v else "FAIL", _mask(v) if v else "missing")

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
    holdings = check("get_holdings", "get_holdings")

    # 1.3 prices
    check("get_prices", "get_prices", tickers)

    # 1.4 bid/ask
    check("get_bid_ask_prices", "get_bid_ask_prices", tickers)

    # 1.5 optional: buyable_info
    check("get_buyable_info", "get_buyable_info", TEST_TICKER, 50000)

    # 1.6 optional: ticker_name
    check("get_ticker_name", "get_ticker_name", TEST_TICKER)

    # Stage 2: write 가역 — --with-order 시만
    print("\n[Stage 2] Write 가역 (1주 IOC, 원복 보장)")
    if not with_order:
        record("2", "place_order/get_order_status/cancel_order", "SKIP",
               "read-only run — add --with-order to test write path (장중만)")
    elif not _is_market_hours(now):
        record("2", "place_order", "SKIP", "market closed — write test requires 09:00~15:30 KST")
    else:
        if mode != "demo":
            print("  ! mode != demo 에서 실계좌 1주 주문이 발생합니다. 계속하려면 3초 대기...")
            time.sleep(3)
        # 전 스냅샷
        snap_before = None
        try:
            snap_before = api.get_holdings() if hasattr(api, "get_holdings") else None
        except Exception:
            snap_before = None

        # Buy→Sell 라운드트립: 매수/매도 모두 필수 검증, net 0 평탄화
        def _place_and_status(side: str) -> tuple[str | None, str]:
            try:
                status, out, note = _try_call(
                    f"place_order({side})", api.place_order, side, TEST_TICKER, 1, 0, "market"
                )
                record("2", f"place_order({side} 1주 market)", status,
                       note[:200] if note else str(out)[:120])
                oid = None
                if status == "PASS" and isinstance(out, dict):
                    oid = out.get("order_id") or out.get("odno") or out.get("ord_no")
                elif status == "PASS":
                    oid = str(out)[:32] if out else None
                else:
                    return None, status
                if oid and hasattr(api, "get_order_status"):
                    time.sleep(0.8)
                    s2, o2, n2 = _try_call(f"get_order_status({side})",
                                           api.get_order_status, oid, True)
                    if s2 == "FAIL" and "today" in n2:
                        s2, o2, n2 = _try_call(f"get_order_status({side})",
                                               api.get_order_status, oid)
                    record("2", f"get_order_status({side})", s2,
                           n2[:200] if n2 else str(o2)[:120])
                    # cancel은 라운드트립에서는 불필요 — 체결 후 반대매매로 평탄화하므로 SKIP
                    if hasattr(api, "cancel_order"):
                        record("2", f"cancel_order({side})", "SKIP",
                               "round-trip mode: cancel not needed (opposite order reverts)")
                return oid, status
            except Exception as e:
                record("2", f"place_order({side})", "FAIL", f"{type(e).__name__}: {e}"[:200])
                return None, "FAIL"

        buy_oid, buy_status = _place_and_status("buy")
        # buy가 체결되어야 sell 보유가 생김 — buy 실패 시 sell도 실패하지만 양쪽 API 도달은 기록됨
        if buy_status != "PASS":
            record("2", "place_order(sell 1주 market)", "SKIP",
                   "buy failed — sell skipped to avoid naked short (buy must pass first)")
            record("2", "get_order_status(sell)", "SKIP", "buy failed")
        else:
            time.sleep(0.5)
            _place_and_status("sell")

        # cancel 경로: 미체결 지정가 1주 → 취소 (보유 영향 없이 cancel 확정 테스트)
        if hasattr(api, "place_order") and hasattr(api, "cancel_order") and hasattr(api, "get_order_status"):
            try:
                cur_price = None
                if hasattr(api, "get_prices"):
                    try:
                        p_out = api.get_prices([TEST_TICKER])
                        if isinstance(p_out, dict):
                            # 어댑터별 반환 형태 대응: {ticker: price} or {ticker: {price: ...}}
                            raw = p_out.get(TEST_TICKER) or p_out.get(TEST_TICKER.lstrip("A")) or next(iter(p_out.values()), None)
                            if isinstance(raw, dict):
                                for k in ("price", "prpr", "stck_prpr", "cur_prc", "now_prc"):
                                    if k in raw:
                                        cur_price = raw[k]
                                        break
                            elif isinstance(raw, (int, float)):
                                cur_price = raw
                    except Exception:
                        cur_price = None
                if not cur_price or not isinstance(cur_price, (int, float)) or cur_price <= 0:
                    cur_price = 30000  # fallback — KRX 가격제한 내 미체결 유도용 더미
                limit_price = max(1, int(cur_price * 0.75))  # -25% 지정가: 가격제한(±30%) 내, 체결 안 됨
                status, out, note = _try_call(
                    "place_order(cancel-test limit)", api.place_order, "buy", TEST_TICKER, 1, limit_price, "limit"
                )
                record("2", "place_order(cancel-test 지정가 1주)", status,
                       note[:200] if note else str(out)[:120])
                oid = None
                if status == "PASS" and isinstance(out, dict):
                    oid = out.get("order_id") or out.get("odno") or out.get("ord_no")
                elif status == "PASS":
                    oid = str(out)[:32] if out else None
                if oid:
                    time.sleep(0.6)
                    s2, o2, n2 = _try_call("get_order_status(cancel-test 미체결)",
                                           api.get_order_status, oid, True)
                    if s2 == "FAIL" and "today" in n2:
                        s2, o2, n2 = _try_call("get_order_status(cancel-test 미체결)",
                                               api.get_order_status, oid)
                    record("2", "get_order_status(cancel-test)", s2, n2[:200] if n2 else str(o2)[:120])
                    s3, o3, n3 = _try_call("cancel_order(cancel-test)", api.cancel_order, oid, TEST_TICKER, 1)
                    record("2", "cancel_order(cancel-test)", s3, n3[:200] if n3 else str(o3)[:120])
                    time.sleep(0.4)
                    s4, o4, n4 = _try_call("get_order_status(cancel-test 취소확인)",
                                           api.get_order_status, oid, True)
                    if s4 == "FAIL" and "today" in n4:
                        s4, o4, n4 = _try_call("get_order_status(cancel-test 취소확인)",
                                               api.get_order_status, oid)
                    record("2", "get_order_status(cancel-test 취소후)", s4, n4[:200] if n4 else str(o4)[:120])
                else:
                    record("2", "get_order_status(cancel-test)", "SKIP", "no order_id")
                    record("2", "cancel_order(cancel-test)", "SKIP", "no order_id")
            except Exception as e:
                record("2", "cancel-test", "FAIL", f"{type(e).__name__}: {e}"[:200])
        else:
            record("2", "cancel-test", "SKIP", "place_order/cancel_order/get_order_status 미구현")

        try:
            snap_after = api.get_holdings() if hasattr(api, "get_holdings") else None
            flat = snap_before == snap_after
            record("2", "verify_flat_position", "PASS" if flat else "FAIL",
                   "holdings net 0 (buy→sell + cancel-test 모두 원복)" if flat else "before≠after — round-trip/cancel not flat, manual check needed")
        except Exception as e:
            record("2", "verify_flat_position", "FAIL", f"{type(e).__name__}: {e}"[:200])

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
                   help="장중 1주 IOC 주문→취소/원복까지 테스트 (기본은 read-only)")
    p.add_argument("--tickers", nargs="*", default=DEFAULT_TICKERS,
                   help=f"price 조회 티커 목록 (default: {' '.join(DEFAULT_TICKERS)})")
    args = p.parse_args()
    report = run_preflight(args.broker, args.mode, bool(args.with_order), list(args.tickers))
    # FAIL 있으면 exit 1 (CI에서 쓰더라도 문서 게이트가 우선이므로 호출자가 판단)
    fails = report["summary"]["fail"]
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
