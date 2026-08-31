"""
NH PLUG 모의 실연동 필드매핑 확정 스크립트.

용도: NHPLUG_BASE_URL=https://moapi.nhplug.com:8443 에서
  krstockInquiryBalance Output_0/Output_1, currentPrice Output_0 실제 키를 덤프하여
  nh_adapter.py의 TODO 필드매핑을 확정한다.

휴장·장마감에도 조회계는 동작 (전일 종가 반환).

사용:
  NHPLUG_APP_KEY=... NHPLUG_APP_SECRET=... \
  NHPLUG_BASE_URL=https://moapi.nhplug.com:8443 \
  uv run python scripts/verify_nh_mapping.py --tickers 005930,069500,091160

  # 상세 원시 JSON까지 보고 싶으면 --raw
  uv run python scripts/verify_nh_mapping.py --raw

환경변수:
  NHPLUG_APP_KEY, NHPLUG_APP_SECRET (필수)
  NHPLUG_BASE_URL (기본 https://api.nhplug.com:8443, 모의는 https://moapi.nhplug.com:8443)
  NHPLUG_AUTH_URL (기본 base와 동일, 모의도 live로 토큰 발급)
  NHPLUG_ACCT_NO (필수, 종합매매 계좌 명시)
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# 프로젝트 루트를 sys.path에 추가 (uv run scripts/... 에서 live_trading import 보장)
import pathlib

_root = pathlib.Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


def _load_dotenv(dotenv_path: pathlib.Path | None = None) -> None:
    """etf_daily_runner와 동일한 수동 .env 로더 (python-dotenv 미의존)."""
    path = dotenv_path or (_root / ".env")
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
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if not (v.startswith('"') and v.endswith('"')) and not (v.startswith("'") and v.endswith("'")):
                idx = v.find(" #")
                if idx > 0:
                    v = v[:idx].strip()
            v = v.strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


_load_dotenv()


def _mask(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 4:
        return "***"
    return s[:2] + "***" + s[-2:]


def main() -> None:
    parser = argparse.ArgumentParser(description="NH PLUG 필드매핑 검증")
    parser.add_argument("--tickers", default="005930,069500,091160", help="콤마 구분 티커")
    parser.add_argument("--raw", action="store_true", help="원시 JSON 전체 출력")
    parser.add_argument("--balance-only", action="store_true", help="잔고만 조회")
    parser.add_argument("--price-only", action="store_true", help="시세만 조회")
    args = parser.parse_args()

    # import는 env 로드 후
    from live_trading.nh_adapter import NhAdapter

    base_url = os.environ.get("NHPLUG_BASE_URL", "https://api.nhplug.com:8443")
    adapter = None
    try:
        adapter = NhAdapter()
    except Exception as exc:
        print(f"[FATAL] NhAdapter 초기화 실패: {exc}", file=sys.stderr)
        print(f"  base_url={base_url}", file=sys.stderr)
        print(f"  app_key={_mask(os.environ.get('NHPLUG_APP_KEY',''))}", file=sys.stderr)
        print(f"  acct_no={os.environ.get('NHPLUG_ACCT_NO','(auto)')}", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] base_url={adapter.base_url}")
    print(f"[INFO] auth_url={adapter.auth_url}")
    print(f"[INFO] acct_no={adapter.acct_no}")
    print(f"[INFO] token_cached={'yes' if adapter.access_token else 'no'}")
    print()

    # --- Balance (krstockInquiryBalance) ---
    if not args.price_only:
        print("=" * 72)
        print("[1] krstockInquiryBalance  (get_cash / get_holdings 원천)")
        print("=" * 72)
        try:
            raw = adapter._call("/krstock/inquiry/v1/balance", {"act_no": adapter.acct_no})
            out0 = raw.get("Output_0", {})
            out1 = raw.get("Output_1", [])
            if isinstance(out0, list):
                out0 = out0[0] if out0 else {}
            print(f"rsp_cd={raw.get('rsp_cd')} rsp_msg={raw.get('rsp_msg')}")
            print(f"Output_0 type={type(out0).__name__} keys={list(out0.keys()) if isinstance(out0, dict) else 'N/A'}")
            if isinstance(out0, dict):
                # 예수금 후보
                candidates = ["ord_psbl_amt", "dnca", "evlu_amt", "cma_evlu_amt", "nxdy_excc_amt", "pchs_amt", "tot_evlu_amt"]
                print("  -- 예수금 후보 필드 --")
                for k in candidates:
                    if k in out0:
                        print(f"    {k} = {out0[k]}")
                # 전체 덤프 (raw 아니면 키만)
                if args.raw:
                    print("\n  Output_0 raw:")
                    print(json.dumps(out0, ensure_ascii=False, indent=2)[:4000])
                else:
                    print(f"  Output_0 sample: {json.dumps(out0, ensure_ascii=False)[:500]}")

            print(f"\nOutput_1 type={type(out1).__name__} len={len(out1) if isinstance(out1, list) else 'N/A'}")
            if isinstance(out1, list) and out1:
                print(f"  Output_1[0] keys={list(out1[0].keys()) if isinstance(out1[0], dict) else 'N/A'}")
                if args.raw:
                    print(json.dumps(out1[0], ensure_ascii=False, indent=2)[:3000])
                else:
                    print(f"  Output_1[0] sample: {json.dumps(out1[0], ensure_ascii=False)[:600]}")
            elif isinstance(out1, dict):
                print(f"  Output_1 keys={list(out1.keys())}")
                print(json.dumps(out1, ensure_ascii=False, indent=2)[:3000])

            # adapter 헬퍼로 파싱된 값
            print(f"\n  => adapter.get_cash() = {adapter.get_cash()}")
            print(f"  => adapter.get_holdings() = {adapter.get_holdings()}")
        except Exception as exc:
            print(f"[ERROR] balance 호출 실패: {exc}", file=sys.stderr)
            import traceback

            traceback.print_exc()

        print()

    # --- Price (currentPrice) ---
    if not args.balance_only:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
        print("=" * 72)
        print(f"[2] currentPrice  tickers={tickers}")
        print("=" * 72)
        for ticker in tickers:
            print(f"\n-- ticker={ticker} --")
            for market_cd in ("KRX", "UNT", ""):
                try:
                    payload = {"iem_cd": ticker}
                    if market_cd:
                        payload["market_cd"] = market_cd
                    raw = adapter._call("/krstock/quote/v1/currentPrice", payload)
                    out0 = raw.get("Output_0", {})
                    if isinstance(out0, list):
                        out0 = out0[0] if out0 else {}
                    print(
                        f"  market_cd={market_cd or '(none)'} rsp_cd={raw.get('rsp_cd')} "
                        f"rsp_msg={raw.get('rsp_msg')} keys={list(out0.keys()) if isinstance(out0, dict) else type(out0)}"
                    )
                    if isinstance(out0, dict) and out0:
                        for k in ("prpr", "stck_prpr", "cur_prc", "price", "now_prc", "close", "prc"):
                            if k in out0:
                                print(f"    {k} = {out0[k]}")
                        if args.raw:
                            print(json.dumps(out0, ensure_ascii=False, indent=2)[:2000])
                        else:
                            # 한 줄 샘플
                            print(f"    sample: {json.dumps(out0, ensure_ascii=False)[:400]}")
                        # 값이 있으면 다음 market_cd 스킵
                        has_price = any(
                            out0.get(k) not in (None, "", "0") for k in ("prpr", "stck_prpr", "cur_prc", "price")
                        )
                        if has_price:
                            break
                except Exception as exc:
                    print(f"  market_cd={market_cd} ERROR: {exc}")

        # adapter 헬퍼
        try:
            print(f"\n  => adapter.get_prices({tickers}) = {adapter.get_prices(tickers)}")
            print(f"  => adapter.get_bid_ask_prices({tickers}) = {adapter.get_bid_ask_prices(tickers)}")
        except Exception as exc:
            print(f"[ERROR] adapter.get_prices 실패: {exc}")

    if adapter:
        adapter.close()


if __name__ == "__main__":
    main()
