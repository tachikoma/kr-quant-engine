"""현금분배금 병합, 재투자 수익률 및 보유자 귀속을 검증한다."""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from etf_distributions import (
    add_distributions,
    add_total_return_price,
    distribution_cash_for_holdings,
    load_distributions,
)


def assert_close(actual: float, expected: float, message: str) -> None:
    if abs(actual - expected) > 1e-9:
        raise AssertionError(f"{message}: expected={expected}, actual={actual}")


def main() -> None:
    fixture = Path("/tmp/kr_quant_etf_distributions_test.csv")
    fixture.write_text(
        "ticker,ex_date,amount_per_share,payment_date,source\n"
        "069500,2024-01-03,5,2024-01-05,test\n",
        encoding="utf-8",
    )
    distributions = load_distributions(fixture, required=True)
    assert distributions.iloc[0]["ticker"] == "069500"

    price = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
            "ticker": ["069500"] * 3,
            "close": [100.0, 95.0, 104.5],
        }
    )
    result = add_total_return_price(add_distributions(price, distributions))
    assert_close(result.iloc[0]["close_total_return"], 100.0, "초기 total return")
    assert_close(result.iloc[1]["close_total_return"], 100.0, "분배락 회복")
    assert_close(result.iloc[2]["close_total_return"], 110.0, "분배금 재투자 이후 상승")

    price_only = add_total_return_price(price)
    assert_close(price_only.iloc[2]["close_total_return"], 104.5, "분배금 컬럼 없는 가격수익률")

    gross = distribution_cash_for_holdings(
        {"069500": 10, "091160": 3}, {"069500": 5.0}, tax_pct=0.0
    )
    net = distribution_cash_for_holdings({"069500": 10}, {"069500": 5.0}, tax_pct=0.154)
    assert_close(gross, 50.0, "gross 분배금")
    assert_close(net, 42.3, "세후 분배금")

    empty_fixture = Path("/tmp/kr_quant_etf_distributions_empty.csv")
    empty_fixture.write_text(
        "ticker,ex_date,amount_per_share,payment_date,source\n", encoding="utf-8"
    )
    try:
        load_distributions(empty_fixture, required=True)
    except ValueError:
        pass
    else:
        raise AssertionError("total_return 필수 모드에서 빈 파일을 허용했습니다")

    fixture.unlink(missing_ok=True)
    empty_fixture.unlink(missing_ok=True)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
