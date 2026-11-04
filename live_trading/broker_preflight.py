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
import re
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
# 모의투자를 지원하지 않는 브로커 (주문 검증은 실계좌 1주 테스트뿐 — 사전 승인 필요)
NO_MOCK_BROKERS = {"KB"}
# 어댑터의 주문 필드 매핑(시장가/지정가 구분 등)이 증권사 API 문서로 확정되지 않은 브로커.
# kb_adapter.place_order 에 "주문 구분/가격 필드명 확정" TODO 가 남아 있다. 확정·검증 후 이 집합에서 제거한다.
ORDER_MAPPING_UNVERIFIED = {"KB"}
# KRX ETF 호가가격단위(원). 지정가는 이 단위로 내림한다 (규정 확인 필요 — 틀리면 증권사가 주문을 거부한다).
ETF_TICK_KRW = 5

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


def _credential_forms(raw: str) -> set[str]:
    """한 자격증명 값이 로그/오류 본문에 나타날 수 있는 형태들 (어댑터의 정규화 포함)."""
    from urllib.parse import quote

    v = raw.strip()  # 어댑터(NH 등)는 .strip() 후 사용
    if len(v) < 4:
        return set()  # 너무 짧은 값(예: 상품코드 '01')은 비밀이 아니며 진단 메모를 망가뜨린다
    base = {raw, v, v.replace("-", "")}
    # 하이픈 조각(계좌 CANO/상품코드 분리 등): 조각도 strip, 6자 이상만 (짧은 조각은 일반 텍스트와 충돌)
    base |= {p.strip() for p in v.split("-") if len(p.strip()) >= 6}
    forms = set(base)
    for f in base:  # 오류 본문이 값을 URL 인코딩/JSON 이스케이프해 되울리는 경우
        pct = quote(f, safe="")
        forms.add(pct)
        forms.add(re.sub(r"%[0-9A-F]{2}", lambda m: m.group().lower(), pct))  # 소문자 퍼센트 인코딩
        for ascii_only in (False, True):  # True: 비ASCII 를 \\uXXXX 로 (Python 기본 json.dumps)
            js = json.dumps(f, ensure_ascii=ascii_only)[1:-1]
            forms.add(js)
            forms.add(js.replace("/", "\\/"))  # PHP/JS 계열은 '/' 를 '\\/' 로 이스케이프
            forms.add(re.sub(r"\\u([0-9a-f]{4})", lambda m: "\\u" + m.group(1).upper(), js))  # 대문자 hex
    # 파생 형태에도 길이/내용 필터: 빈 문자열이나 '-'·공백뿐인 형태는 치환하지 않는다
    return {f for f in forms if len(f) >= 4 and f.strip(" -")}


def _scrub(text: str) -> str:
    """출력/리포트에 환경변수 자격증명 값(키·시크릿·계좌)이 섞이지 않도록 마스킹.

    모든 자격증명의 모든 형태를 모아 **길이 내림차순으로 한 번에** 치환한다 (한 값이 다른 값의
    부분 문자열일 때 긴 값이 먼저 가려지도록). 4자 미만 값과 퇴화 형태는 치환하지 않는다.
    """
    out = str(text)
    forms: set[str] = set()
    for names in (*CRED_REQUIRED.values(), *CRED_OPTIONAL.values()):
        for n in names:
            forms |= _credential_forms(os.environ.get(n, ""))
    for f in sorted(forms, key=len, reverse=True):
        out = out.replace(f, "***")
    return out


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
        msg = _scrub(f"{type(e).__name__}: {e}")
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


# 모의(demo) 엔드포인트 표식. None = 코드상 모의 구분 불가(명시 승인 필요).
DEMO_ENDPOINT_HOST: dict[str, str | None] = {
    "KIWOOM": "mockapi.kiwoom.com",
    "KIS": "openapivts.koreainvestment.com",
    "NH": "moapi.nhplug.com",
    "KB": None,
}
POLL_TRIES = 6


def _endpoint_of(api: Any) -> str:
    for getter in (lambda a: a.base_url, lambda a: a._auth.base_url):
        try:
            v = getter(api)
            if isinstance(v, str) and v:
                return v
        except Exception:
            continue
    return ""


def order_destination_ok(api: Any, broker: str, mode: str, allow_real: bool) -> tuple[bool, str]:
    """--mode 값이 아니라 어댑터가 실제로 쓰는 주문 목적지를 검증한다.

    URL 을 파싱해 호스트명을 정확히 비교한다 (부분 문자열 비교는
    `https://moapi.nhplug.com@api.nhplug.com` 같은 userinfo 우회에 뚫린다).
    """
    from urllib.parse import urlsplit

    url = _endpoint_of(api)
    if broker.upper() in ORDER_MAPPING_UNVERIFIED:
        return (False, f"{broker.upper()}: 어댑터 주문 필드 매핑(시장가/지정가 구분·가격 필드)이 증권사 API 문서로 "
                       "확정되지 않음(kb_adapter.place_order TODO) — 주문 검증 거부 (mode/--allow-real 무관)")
    if mode != "demo":
        return (allow_real, f"mode={mode}: 실계좌 주문 — --allow-real 명시 필요 (endpoint host={_host_of(url)})")
    expected = DEMO_ENDPOINT_HOST.get(broker.upper())
    if expected is None:
        # 모의투자를 지원하지 않는 브로커(KB): --mode demo 는 의미가 없고 주문은 항상 실계좌다.
        return (False, f"{broker.upper()}: 모의투자 미지원 — --mode demo 로는 주문 검증을 할 수 없음 "
                       f"(host={_host_of(url)}). 실계좌 1주 테스트는 사전 승인 후 --mode real --allow-real")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        has_userinfo = parts.username is not None or parts.password is not None
    except ValueError:
        return (False, "endpoint URL 파싱 실패 — 주문 거부")
    if parts.scheme != "https" or has_userinfo or host != expected:
        return (False, f"effective endpoint host={host or '?'} (scheme={parts.scheme or '?'}"
                       f"{', userinfo 포함' if has_userinfo else ''}) 는 모의({expected})가 아님 — 주문 거부")
    return (True, f"endpoint host={host}")


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        return (urlsplit(url).hostname or "?").lower()
    except ValueError:
        return "?"


def _order_id(out: Any) -> str | None:
    if isinstance(out, dict):
        oid = out.get("order_id") or out.get("odno") or out.get("ord_no")
        return str(oid) if oid else None
    return str(out)[:32] if out else None


def cancel_confirmed(st: dict[str, Any], prior_filled: int = 0) -> bool:
    """러너(_wait_for_cancellations)와 동일: 발견됨 + 잔량 0 + (관측 최대 체결 < 주문수량).

    prior_filled: 이전 폴링에서 관측한 최대 체결수량 (체결수량은 감소하지 않는다).
    """
    if not st.get("is_found", False) or st.get("remaining_qty") is None:
        return False
    order_qty = int(st.get("order_qty", 0))
    filled = max(int(st.get("filled_qty", 0)), int(prior_filled))
    return order_qty > 0 and int(st["remaining_qty"]) == 0 and filled < order_qty


def fully_filled(st: dict[str, Any], prior_filled: int = 0) -> bool:
    order_qty = int(st.get("order_qty", 0))
    filled = max(int(st.get("filled_qty", 0)), int(prior_filled))
    if st.get("remaining_qty") is None or int(st["remaining_qty"]) != 0:
        return False  # 수량 불일치(체결=주문인데 잔량>0)는 미해결로 둔다
    return bool(st.get("is_found", False)) and order_qty > 0 and filled >= order_qty


def _fmt_status(st: Any) -> str:
    if not isinstance(st, dict):
        return str(st)[:80]
    keys = ("is_found", "order_qty", "filled_qty", "remaining_qty", "is_filled")
    return " ".join(f"{k}={st.get(k)}" for k in keys)


def _stage2_orders(
    api: Any, record, tickers_hint: str = TEST_TICKER, *, broker: str, mode: str, allow_real: bool = False
) -> None:
    """주문 왕복/취소 검증. 모든 주문은 최종 상태(체결 또는 취소 확정)까지 확인하고,
    확정하지 못하면 FAIL 기록 후 이후 주문을 중단한다 (미해결 주문/포지션을 남기지 않기 위함).

    broker/mode 는 필수 키워드 인자다: 이 함수를 직접 호출해도 run_preflight 와 동일한 승인 검사
    (실전 모드 승인, 주문 목적지, 매핑 미확정 브로커 차단)를 스스로 수행한다.
    """
    if mode != "demo" and not allow_real:
        record("2", "stage2-authorization", "FAIL", "mode=real 주문은 --allow-real 필요 — 주문 거부")
        return
    ok, detail = order_destination_ok(api, broker, mode, allow_real)
    if not ok:
        record("2", "stage2-authorization", "FAIL", detail)
        return
    ticker = tickers_hint
    today_krx = _now_kst().strftime("%Y%m%d")
    before = api.get_holdings() if hasattr(api, "get_holdings") else None
    unresolved: list[str] = []

    def abort(msg: str) -> None:
        record("2", "stage2_unresolved", "FAIL", msg + " — 증권사 앱에서 미체결/보유를 수동 확인·정리하세요")

    def status_of(label: str, oid: str) -> dict[str, Any] | None:
        st, out, note = _try_call(label, api.get_order_status, oid, today=today_krx)
        record("2", label, st, note if note else _fmt_status(out))
        return out if st == "PASS" and isinstance(out, dict) else None

    def place(label: str, side: str, price: float | None, otype: str) -> str | None:
        st, out, note = _try_call(label, api.place_order, side, ticker, 1, price, otype)
        oid = _order_id(out) if st == "PASS" else None
        if st != "PASS":
            record("2", label, "FAIL", note)
            return None
        if not oid:
            # 접수 여부 불명 — 추가 주문 금지 (러너의 OrderSubmissionUncertain 과 동일 원칙)
            record("2", label, "FAIL", "order_id 없음 — 접수 여부 불명(uncertain), 이후 주문 중단")
            unresolved.append("order_id missing")
            return None
        record("2", label, "PASS", f"order_id={_mask(oid)}")
        return oid

    def wait_fill(label: str, oid: str) -> tuple[bool, int]:
        seen = 0
        for _ in range(POLL_TRIES):
            time.sleep(1.0)
            r = status_of(label, oid)
            if r:
                seen = max(seen, int(r.get("filled_qty", 0)))
                if fully_filled(r, seen):
                    return True, seen
        return False, seen

    def cancel_and_resolve(label: str, oid: str, seen: int) -> str:
        """취소 요청 후 최종 상태 확정. 반환: 'filled' | 'cancelled' | 'unresolved'."""
        s3, o3, n3 = _try_call(label + "_cancel", api.cancel_order, oid, ticker, 1)
        record("2", f"cancel_order({label})", s3, n3 if n3 else "requested")
        # 취소 요청 실패여도 최종 상태를 확인한다 (이미 체결/취소되었을 수 있음)
        for _ in range(POLL_TRIES):
            time.sleep(1.0)
            r = status_of(f"get_order_status({label} 취소 후)", oid)
            if not r:
                continue
            seen = max(seen, int(r.get("filled_qty", 0)))
            if fully_filled(r, seen):
                return "filled"
            if cancel_confirmed(r, seen):
                return "cancelled"
        return "unresolved"

    # (1) 시장가 1주 매수 → 체결 확인 → 매도
    boid = place("place_order(BUY 1주 MARKET)", "BUY", None, "MARKET")
    if boid is None:
        abort("매수 주문 미확정")
        return
    filled, seen = wait_fill("get_order_status(BUY)", boid)
    if not filled:
        outcome = cancel_and_resolve("BUY", boid, seen)
        if outcome == "unresolved":
            abort("매수 주문의 최종 상태(체결/취소)를 확정하지 못함")
            return
        if outcome == "cancelled":
            record("2", "roundtrip", "FAIL",
                   "incomplete: 시장가 매수가 체결되지 않아(취소됨) 시장가 체결·매도 경로를 검증하지 못함 — 재시도 필요")
            filled = False
        else:
            filled = True  # 취소와 체결이 경합해 체결됨 → 반드시 매도해 원복
    if filled:
        soid = place("place_order(SELL 1주 MARKET)", "SELL", None, "MARKET")
        if soid is None:
            abort("매도 주문 미확정 — 매수 1주가 보유 상태로 남아 있음")
            return
        sold, sseen = wait_fill("get_order_status(SELL)", soid)
        if not sold:
            outcome = cancel_and_resolve("SELL", soid, sseen)
            if outcome != "filled":
                abort("매도 미체결 — 매수 1주가 보유 상태로 남아 있음 (매도 " +
                      ("취소됨" if outcome == "cancelled" else "상태 불명") + ")")
                return
        record("2", "roundtrip", "PASS", "buy→sell 체결")

    # (2) 미체결 지정가 → 취소 → 취소 의미 검증
    cur = None
    try:
        px = api.get_prices([ticker]).get(ticker)
        cur = float(px) if px else None
    except Exception:
        cur = None
    if not cur or cur <= 0:
        # 임의 가격으로 지정가를 내지 않는다 (가격제한/호가단위 위반 또는 의도치 않은 체결 위험)
        record("2", "limit_price", "FAIL", "현재가를 얻지 못해 지정가 취소 테스트를 수행하지 않음")
        abort("지정가 단계 생략 — 현재가 조회 실패")
        return
    # -25% 후 호가단위(ETF 5원)로 내림. 현재가가 호가단위 미만이면 주문하지 않는다.
    limit_price = int(cur * 0.75) // ETF_TICK_KRW * ETF_TICK_KRW
    if limit_price < ETF_TICK_KRW:
        record("2", "limit_price", "FAIL", f"계산된 지정가({limit_price})가 호가단위 미만 — 주문하지 않음")
        abort("지정가 단계 생략 — 가격 계산 불가")
        return
    loid = place("place_order(BUY 1주 LIMIT -25%)", "BUY", limit_price, "LIMIT")
    if loid is None:
        abort("지정가 주문 미확정")
        return
    time.sleep(1.0)
    first = status_of("get_order_status(LIMIT 미체결)", loid)
    seen = int(first.get("filled_qty", 0)) if first else 0
    outcome = cancel_and_resolve("LIMIT", loid, seen)
    if outcome == "cancelled":
        record("2", "cancel_semantics(found & remaining==0 & filled<order)", "PASS",
               "러너의 취소 확정 조건과 일치")
    elif outcome == "filled":
        record("2", "cancel_semantics", "FAIL", "지정가(-25%)가 체결됨 — 1주 보유 상태, 수동 매도 필요")
    else:
        abort("지정가 주문의 취소를 확정하지 못함 (러너도 재주문을 확정하지 못함)")
        return

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
        note = _scrub(note)[:400]  # 마스킹 후에 자른다 (경계에 걸친 값 노출 방지)
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
    if broker.upper() in NO_MOCK_BROKERS:
        record("0", "mock_support", "SKIP",
               f"{broker.upper()}는 모의투자 미지원 — 읽기 전용 호출도 실서버/실계좌에 대해 수행됨, 주문 검증은 --mode real --allow-real 만 가능")
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
        record("1", "adapter_init", "FAIL", f"{type(e).__name__}: {e}")
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
        record("1", label, status, note if note else "")
        return out

    # 1.1 cash
    check("get_cash", "get_cash")
    # get_available_cash는 티커/가격 인자가 필요한 어댑터가 있음 — 둘 다 시도
    if hasattr(api, "get_available_cash"):
        try:
            # 가격 0이면 buyable 계산이 무의미하므로 더미 가격 50000
            out = api.get_available_cash(TEST_TICKER, 50000) if "ticker" in api.get_available_cash.__code__.co_varnames else api.get_available_cash()  # type: ignore[attr-defined]
            # 위 분기가 부정확할 수 있어 fallback
            record("1", "get_available_cash", "PASS", str(out) if out is not None else "")
        except TypeError:
            status, out, note = _try_call("get_available_cash", getattr(api, "get_available_cash"))
            record("1", "get_available_cash", status, note if note else "")
        except Exception as e:
            record("1", "get_available_cash", "FAIL", f"{type(e).__name__}: {e}")
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
        ok, detail = order_destination_ok(api, broker, mode, allow_real)
        if not ok:
            record("2", "order-destination guard", "FAIL", detail)
        else:
            record("2", "order-destination guard", "PASS", detail)
            try:
                _stage2_orders(api, record, tickers_hint=TEST_TICKER,
                               broker=broker, mode=mode, allow_real=allow_real)
            except Exception as e:  # noqa: BLE001
                record("2", "stage2_unresolved", "FAIL",
                       f"unexpected {type(e).__name__}: {e} — 증권사 앱에서 미체결/보유를 수동 확인·정리하세요")

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
