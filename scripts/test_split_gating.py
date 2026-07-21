"""split 멀티 인덱스 게이팅과 선택적 강제청산 회귀 테스트."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from etf_shared import build_gating_decision, build_rebalance_orders


def assert_equal(actual, expected, message):
    if actual != expected:
        raise AssertionError(f"{message}: expected={expected}, actual={actual}")


def test_signal_matrix():
    expected = {
        (True, True): {"domestic_equity", "foreign_investment", "commodity"},
        (True, False): {"domestic_equity", "commodity"},
        (False, True): {"foreign_investment", "commodity"},
        (False, False): {"commodity"},
    }
    for signals, allowed_groups in expected.items():
        decision = build_gating_decision(
            [],
            {},
            kospi_risk_on=signals[0],
            us_risk_on=signals[1],
            enable_multi_index_risk=True,
            gating_mode="split",
        )
        assert_equal(decision.allowed_groups, allowed_groups, f"split matrix {signals}")


def test_mixed_holdings_and_empty_candidates():
    holdings = {
        "069500": 10,  # domestic_equity
        "133690": 5,  # foreign_investment
    }
    decision = build_gating_decision(
        [],
        holdings,
        kospi_risk_on=True,
        us_risk_on=False,
        enable_multi_index_risk=True,
        gating_mode="split",
    )
    assert_equal(decision.eligible_ranked, [], "empty eligible candidates")
    assert_equal(decision.forced_exit_tickers, {"133690"}, "US-off forced exit")

    orders = build_rebalance_orders(
        current_holdings=holdings,
        target_tickers=[],
        latest_prices={"069500": 10_000, "133690": 100_000},
        available_cash=0,
        slippage=0,
        allow_empty_target_sell=False,
        forced_exit_tickers=decision.forced_exit_tickers,
    )
    assert_equal(len(orders), 1, "only one forced exit order")
    assert_equal(orders[0]["ticker"], "133690", "foreign holding must exit")
    assert_equal(orders[0]["reason"], "ETF_RISK_GATE_EXIT", "forced exit reason")


def test_rank_filter_and_legacy_modes():
    ranked = ["133690", "069500", "411060"]
    split = build_gating_decision(
        ranked,
        {"133690": 5},
        kospi_risk_on=True,
        us_risk_on=False,
        enable_multi_index_risk=True,
        gating_mode="split",
    )
    assert_equal(split.eligible_ranked, ["069500", "411060"], "split rank filter")

    hybrid = build_gating_decision(
        ranked,
        {"133690": 5},
        kospi_risk_on=True,
        us_risk_on=False,
        enable_multi_index_risk=True,
        gating_mode="hybrid",
    )
    assert_equal(hybrid.eligible_ranked, ranked, "hybrid KOSPI-on compatibility")
    assert_equal(hybrid.forced_exit_tickers, set(), "hybrid must not force exit")

    disabled = build_gating_decision(
        ranked,
        {"133690": 5},
        kospi_risk_on=True,
        us_risk_on=False,
        enable_multi_index_risk=False,
        gating_mode="split",
    )
    assert_equal(disabled.eligible_ranked, ranked, "disabled multi-index compatibility")


def test_forced_exit_with_target_weight_rebalance():
    orders = build_rebalance_orders(
        current_holdings={"069500": 10, "133690": 5},
        target_tickers=[],
        latest_prices={"069500": 10_000, "133690": 100_000},
        available_cash=0,
        slippage=0,
        allow_empty_target_sell=False,
        forced_exit_tickers={"133690"},
        target_weight_rebalance=True,
    )
    assert_equal(len(orders), 1, "target-weight selective forced exit")
    assert_equal(orders[0]["ticker"], "133690", "target-weight foreign exit")


def test_risk_off_hold_policy():
    decision = build_gating_decision(
        ["069500", "133690", "411060"],
        {"069500": 10, "133690": 5},
        kospi_risk_on=False,
        us_risk_on=False,
        enable_multi_index_risk=True,
        gating_mode="split",
        liquidate_on_risk_off=False,
    )
    assert_equal(decision.forced_exit_tickers, set(), "risk-off hold must preserve holdings")


def main():
    test_signal_matrix()
    test_mixed_holdings_and_empty_candidates()
    test_rank_filter_and_legacy_modes()
    test_forced_exit_with_target_weight_rebalance()
    test_risk_off_hold_policy()
    print("split gating regression checks passed")


if __name__ == "__main__":
    main()
