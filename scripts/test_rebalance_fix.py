import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from etf_shared import build_rebalance_orders

def assert_condition(cond, msg):
    if not cond:
        print(f"Assertion failed: {msg}")
        sys.exit(1)

def main():
    # Incident scenario: held 091160(3주) + cash 5,850,000, targets=[091160, 102110]
    current_holdings = {"091160": 3}
    target_tickers = ["091160", "102110"]
    available_cash = 5_850_000
    latest_prices = {"091160": 10000, "102110": 9000}
    slippage = 0.0005
    max_asset_pct = 0.50

    orders = build_rebalance_orders(
        current_holdings=current_holdings,
        target_tickers=target_tickers,
        latest_prices=latest_prices,
        available_cash=available_cash,
        slippage=slippage,
        max_asset_pct=max_asset_pct,
    )
    buy_orders = [o for o in orders if o.get("side") == "BUY"]
    sell_orders = [o for o in orders if o.get("side") == "SELL"]

    assert_condition(len(buy_orders) == 2, f"Expected 2 buy orders, got {len(buy_orders)}: {[o['ticker'] for o in buy_orders]}")
    qty_091160 = next(o["qty"] for o in buy_orders if o["ticker"] == "091160")
    assert_condition(qty_091160 > 10, f"091160 order qty {qty_091160} <= 10")
    # Compare estimated values — should be within 10% of each other
    est_vals = [o["estimated_value"] for o in buy_orders]
    diff_ratio = abs(est_vals[0] - est_vals[1]) / max(est_vals[0], est_vals[1])
    assert_condition(diff_ratio <= 0.10, f"Buy order values differ >10%: {est_vals[0]:,.0f} vs {est_vals[1]:,.0f} (ratio={diff_ratio:.3f})")
    print(f"  Scenario 1: buy_orders={[(o['ticker'], o['qty'], o['estimated_value']) for o in buy_orders]}")
    print(f"  sell_orders={[(o['ticker'], o['qty'], o['estimated_value']) for o in sell_orders]}")
    print("  ✅ PASS")

    # Edge case 1: empty holdings
    orders = build_rebalance_orders(
        current_holdings={},
        target_tickers=["091160", "102110"],
        latest_prices=latest_prices,
        available_cash=available_cash,
        slippage=slippage,
        max_asset_pct=max_asset_pct,
    )
    sell_orders = [o for o in orders if o.get("side") == "SELL"]
    assert_condition(len(sell_orders) == 0, f"Expected no sell orders for empty holdings, got {len(sell_orders)}")
    print("  Edge 1 (empty holdings): ✅")

    # Edge case 2: both targets already held — should still generate buy orders (equal-budget fill)
    orders = build_rebalance_orders(
        current_holdings={"091160": 3, "102110": 2},
        target_tickers=["091160", "102110"],
        latest_prices=latest_prices,
        available_cash=available_cash,
        slippage=slippage,
        max_asset_pct=max_asset_pct,
    )
    buy_orders = [o for o in orders if o.get("side") == "BUY"]
    assert_condition(len(buy_orders) == 2, f"Expected 2 buy orders when both targets held (equal-budget fill), got {len(buy_orders)}")
    # Verify equal-budget allocation
    est_vals = [o["estimated_value"] for o in buy_orders]
    diff_ratio = abs(est_vals[0] - est_vals[1]) / max(est_vals[0], est_vals[1])
    assert_condition(diff_ratio <= 0.10, f"Values differ >10%: {est_vals[0]:,.0f} vs {est_vals[1]:,.0f}")
    print(f"  Edge 2 (both held — equal budget): buy_orders={[(o['ticker'], o['qty'], o['estimated_value']) for o in buy_orders]} ✅")

    # Edge case 3: insufficient cash
    orders = build_rebalance_orders(
        current_holdings={},
        target_tickers=["091160"],
        latest_prices=latest_prices,
        available_cash=1000,
        slippage=slippage,
        max_asset_pct=max_asset_pct,
    )
    buy_orders = [o for o in orders if o.get("side") == "BUY"]
    assert_condition(len(buy_orders) == 0, f"Expected no buy orders with insufficient cash, got {len(buy_orders)}")
    print("  Edge 3 (insufficient cash): ✅")

    # Edge case 4: max_asset_pct cap
    orders = build_rebalance_orders(
        current_holdings={},
        target_tickers=["091160"],
        latest_prices=latest_prices,
        available_cash=10_000_000,
        slippage=slippage,
        max_asset_pct=0.001,
    )
    buy_orders = [o for o in orders if o.get("side") == "BUY"]
    assert_condition(len(buy_orders) == 0, f"Expected no buy orders due to max_asset_pct cap, got {len(buy_orders)}")
    print("  Edge 4 (max_asset_pct cap): ✅")

    # Edge case 5: single target
    orders = build_rebalance_orders(
        current_holdings={},
        target_tickers=["091160"],
        latest_prices=latest_prices,
        available_cash=available_cash,
        slippage=slippage,
        max_asset_pct=max_asset_pct,
    )
    buy_orders = [o for o in orders if o.get("side") == "BUY"]
    assert_condition(len(buy_orders) == 1, f"Expected one buy order for single target, got {len(buy_orders)}")
    print("  Edge 5 (single target): ✅")

    print("\nALL TESTS PASSED")
    sys.exit(0)

if __name__ == "__main__":
    main()
