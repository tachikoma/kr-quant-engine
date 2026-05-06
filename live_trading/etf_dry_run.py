

"""
ETF 드라이런 모듈

- 실제 주문은 실행하지 않음
- 백테스트 전략 로직을 재사용함
- 오늘 어떤 거래가 발생할지를 출력함
"""

import datetime as dt
import os
import sys
from pathlib import Path
from typing import Dict

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _load_dotenv(dotenv_path: Path | None = None) -> None:
    """.env 파일을 os.environ에 적재한다(이미 설정된 키는 유지)."""
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


# .env를 가장 먼저 로드 — KRX_ID / KRX_PW 등이 이후 임포트에서 참조됨
_load_dotenv()

# 백테스트 전략 로직 재사용
from run_etf_backtest import (
    get_strategy_config,
    select_target_etfs,
    build_rebalance_orders,
    rank_etfs,
)

# 라이브 트레이딩(키움) 어댑터
try:
    from live_trading.kiwoom_adapter import KiwoomAdapter
except Exception:
    KiwoomAdapter = None


def get_current_holdings() -> Dict[str, int]:
    """키움에서 보유 수량을 조회한다(불가 시 Mock 데이터로 대체)."""
    if KiwoomAdapter:
        try:
            api = KiwoomAdapter()
            return api.get_holdings()
        except Exception as e:
            print(f"[경고] 보유 수량 조회 실패, Mock 데이터로 대체합니다: {e}")
    return {
        "069500": 10,
    }


def get_cash() -> float:
    """키움에서 예수금을 조회한다(불가 시 Mock 데이터로 대체)."""
    if KiwoomAdapter:
        try:
            api = KiwoomAdapter()
            return api.get_cash()
        except Exception as e:
            print(f"[경고] 예수금 조회 실패, Mock 데이터로 대체합니다: {e}")
    return 1_000_000


def get_latest_prices(etf_list: list[str]) -> Dict[str, float]:
    """키움에서 최신 가격을 조회한다(불가 시 Mock 데이터로 대체)."""
    if KiwoomAdapter:
        try:
            api = KiwoomAdapter()
            return api.get_prices(etf_list)
        except Exception as e:
            print(f"[경고] 가격 조회 실패, Mock 데이터로 대체합니다: {e}")
    return {ticker: 100_000 for ticker in etf_list}


def mock_snapshot(etf_list: list[str]) -> pd.DataFrame:
    """
    순위 산출용 Mock 입력 데이터(추후 실데이터로 교체)
    """
    data = []
    for i, ticker in enumerate(etf_list):
        data.append(
            {
                "ticker": ticker,
                "ret_60": 0.1 + i * 0.01,
                "ret_120": 0.15 + i * 0.01,
                "trend_ok": True,
            }
        )

    return pd.DataFrame(data)


def _format_order_for_display(order: dict) -> dict:
    """주문 원본을 유지한 채, 화면 출력용으로만 값을 한국어 라벨로 변환한다."""
    side_map = {
        "BUY": "매수",
        "SELL": "매도",
    }
    reason_map = {
        "ETF_REBALANCE": "ETF 리밸런싱",
    }
    phase_map = {
        "SELL_FIRST": "매도 우선",
        "BUY_AFTER_SELL_FILLED": "매도 체결 후 매수",
        "BUY_NOW": "즉시 매수",
    }

    out = dict(order)
    if "side" in out:
        out["side"] = side_map.get(out["side"], out["side"])
    if "reason" in out:
        out["reason"] = reason_map.get(out["reason"], out["reason"])
    if "phase" in out:
        out["phase"] = phase_map.get(out["phase"], out["phase"])
    if "blocked_in_live_mode" in out:
        out["blocked_in_live_mode"] = "예" if out["blocked_in_live_mode"] else "아니오"
    return out


def run_dry_run():
    config = get_strategy_config()

    print("=== ETF 드라이런 ===")
    print(f"실행 시각: {dt.datetime.now()}")

    etf_list = config["etf_list"]

    # 데이터 조회(키움 또는 Mock 데이터 대체)
    holdings = get_current_holdings()
    cash = get_cash()
    prices = get_latest_prices(etf_list)
    snapshot = mock_snapshot(etf_list)

    ranked = rank_etfs(snapshot)
    target = select_target_etfs(snapshot, config["max_positions"])

    print("\n--- 현재 보유 종목 ---")
    print(holdings)
    print(f"예수금: {cash:,.0f}")

    print("\n--- 목표 ETF ---")
    print(target)

    print("\n--- 상위 랭킹 ---")
    print(ranked.head(5))

    orders = build_rebalance_orders(
        current_holdings=holdings,
        target_tickers=target,
        latest_prices=prices,
        available_cash=cash,
        max_positions=config["max_positions"],
        sell_rank_buffer=config["sell_rank_buffer"],
    )

    has_sell_orders = any(order["side"] == "SELL" for order in orders)
    for order in orders:
        if order["side"] == "SELL":
            order["phase"] = "SELL_FIRST"
        elif has_sell_orders:
            order["phase"] = "BUY_AFTER_SELL_FILLED"
            order["blocked_in_live_mode"] = True
            order["block_reason"] = "매도 체결 및 예수금 재확인 이후에만 매수 주문을 제출할 수 있습니다."
        else:
            order["phase"] = "BUY_NOW"
            order["blocked_in_live_mode"] = False

    print("\n=== 드라이런 주문 ===")
    if not orders:
        print("오늘은 거래가 없습니다")
    else:
        for o in orders:
            print(_format_order_for_display(o))

    if has_sell_orders:
        print("\n=== 라이브 모드 안전 규칙 ===")
        print("매도 주문을 먼저 제출해야 합니다.")
        print("매도 체결 확인 및 예수금 재확인 전까지 매수 주문은 차단됩니다.")


if __name__ == "__main__":
    run_dry_run()