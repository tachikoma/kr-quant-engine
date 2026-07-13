"""비용 포함 FIFO 거래 성과 분해를 합성 거래로 검증한다."""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.trade_performance_attribution import attribute_trades


def main() -> None:
    trades = pd.DataFrame(
        [
            {
                "date": "2024-01-02",
                "ticker": "069500",
                "side": "BUY",
                "qty": 10,
                "price": 100,
                "net_value": 1_010,
            },
            {
                "date": "2024-01-10",
                "ticker": "069500",
                "side": "BUY",
                "qty": 5,
                "price": 110,
                "net_value": 555,
            },
            {
                "date": "2024-02-01",
                "ticker": "069500",
                "side": "SELL",
                "qty": 12,
                "price": 120,
                "net_value": 1_428,
            },
        ]
    )
    realized, by_ticker, summary = attribute_trades(trades)
    expected = (10 * 119 - 1_010) + (2 * 119 - 2 * 111)
    actual = float(realized["net_pnl"].sum())
    if abs(actual - expected) > 1e-9:
        raise AssertionError(f"FIFO 순손익 불일치: expected={expected}, actual={actual}")
    if summary["open_positions_qty"] != {"069500": 3}:
        raise AssertionError(f"잔여 포지션 불일치: {summary['open_positions_qty']}")
    if not summary["cost_aware"] or len(by_ticker) != 1:
        raise AssertionError("비용 인식 또는 종목 집계 실패")
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
