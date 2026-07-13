"""결측 종가가 보유자산 평가액을 0으로 만들지 않는지 확인한다."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from etf_shared import get_valuation_price, update_last_valid_prices


def assert_equal(actual, expected, message):
    if actual != expected:
        raise AssertionError(f"{message}: expected={expected}, actual={actual}")


def main() -> None:
    last_valid = {}

    day1 = pd.Series({"069500": 10_000.0, "091160": 20_000.0})
    update_last_valid_prices(last_valid, day1)
    assert_equal(get_valuation_price("069500", day1, last_valid), 10_000.0, "현재 종가")

    day2 = pd.Series({"069500": np.nan, "091160": 21_000.0})
    assert_equal(
        get_valuation_price("069500", day2, last_valid),
        10_000.0,
        "결측 종가는 직전 유효 종가로 평가",
    )
    assert_equal(get_valuation_price("091160", day2, last_valid), 21_000.0, "최신 종가 갱신")

    invalid = pd.Series({"069500": 0.0, "091160": -1.0})
    update_last_valid_prices(last_valid, invalid)
    assert_equal(last_valid["069500"], 10_000.0, "0원 가격 무시")
    assert_equal(last_valid["091160"], 21_000.0, "음수 가격 무시")
    assert_equal(get_valuation_price("999999", day2, last_valid), None, "이력 없는 결측 가격")

    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
