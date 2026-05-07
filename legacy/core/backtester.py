import pandas as pd
from legacy.core.strategy import add_price_features, score_universe, get_rebalance_dates


def _apply_buy_cost(value, cfg):
    c = cfg["costs"]
    return value * (1 + c["buy_fee_pct"] + c["slippage_pct"])


def _apply_sell_cost(value, cfg):
    c = cfg["costs"]
    return value * (1 - c["sell_fee_pct"] - c["sell_tax_pct"] - c["slippage_pct"])


def market_is_risk_on(index_df: pd.DataFrame, dt: pd.Timestamp, cfg: dict) -> bool:
    if not cfg["risk"]["market_filter"]:
        return True
    m = cfg["risk"]["market_ma_days"]
    idx = index_df[index_df["date"] <= dt].copy()
    if len(idx) < m:
        return True
    idx["ma"] = idx["close"].rolling(m).mean()
    last = idx.iloc[-1]
    return bool(last["close"] >= last["ma"])


def run_backtest(price: pd.DataFrame, fundamentals_by_date: dict, index_df: pd.DataFrame, cfg: dict):
    price = add_price_features(price)
    all_dates = sorted(price["date"].unique())
    rebalance_dates = get_rebalance_dates(price, cfg["strategy"]["rebalance_frequency"])

    # 다음 거래일 시가 체결을 위해 date -> next_date 매핑
    next_date = {all_dates[i]: all_dates[i + 1] for i in range(len(all_dates) - 1)}

    cash = float(cfg["strategy"]["initial_cash"])
    holdings = {}  # 티커 -> 수량
    avg_price = {}
    trades = []
    equity_rows = []

    for dt in all_dates:
        day = price[price["date"] == dt]
        close_map = day.set_index("ticker")["close"].to_dict()

        # 매일 손절: 종가 기준 발생, 다음날 시가 매도 단순화
        for ticker in list(holdings.keys()):
            if ticker not in close_map:
                continue
            pnl = close_map[ticker] / avg_price[ticker] - 1
            if pnl <= cfg["risk"]["stop_loss_pct"]:
                proceeds = _apply_sell_cost(close_map[ticker] * holdings[ticker], cfg)
                cash += proceeds
                trades.append({
                    "date": dt, "ticker": ticker, "side": "SELL",
                    "price": close_map[ticker], "qty": holdings[ticker], "reason": "STOP_LOSS"
                })
                del holdings[ticker]
                del avg_price[ticker]

        if dt in rebalance_dates and dt in next_date:
            execute_dt = next_date[dt]
            execute_day = price[price["date"] == execute_dt]
            open_map = execute_day.set_index("ticker")["open"].to_dict()

            risk_on = market_is_risk_on(index_df, pd.Timestamp(dt), cfg)

            snapshot = price[price["date"] <= dt].groupby("ticker").tail(1)
            # 가장 가까운 과거 펀더멘털 스냅샷 사용
            available_fund_dates = [d for d in fundamentals_by_date.keys() if d <= pd.Timestamp(dt)]
            if available_fund_dates:
                fdt = max(available_fund_dates)
                fundamental = fundamentals_by_date[fdt]
            else:
                fundamental = pd.DataFrame({"ticker": snapshot["ticker"]})

            if risk_on:
                scored = score_universe(snapshot, fundamental, cfg)
                targets = scored.head(cfg["strategy"]["max_positions"])["ticker"].tolist()
            else:
                targets = []

            # 리밸런싱 매도
            for ticker in list(holdings.keys()):
                if ticker not in targets and ticker in open_map:
                    proceeds = _apply_sell_cost(open_map[ticker] * holdings[ticker], cfg)
                    cash += proceeds
                    trades.append({
                        "date": execute_dt, "ticker": ticker, "side": "SELL",
                        "price": open_map[ticker], "qty": holdings[ticker], "reason": "REBALANCE"
                    })
                    del holdings[ticker]
                    del avg_price[ticker]

            # 포트폴리오 가치 계산
            portfolio_value = cash
            for ticker, qty in holdings.items():
                portfolio_value += qty * open_map.get(ticker, close_map.get(ticker, 0))

            if targets:
                target_value = portfolio_value / len(targets)
                for ticker in targets:
                    if ticker not in open_map or open_map[ticker] <= 0:
                        continue
                    current_value = holdings.get(ticker, 0) * open_map[ticker]
                    buy_value = target_value - current_value
                    if buy_value <= 0:
                        continue

                    qty = int(buy_value // open_map[ticker])
                    if qty <= 0:
                        continue

                    cost = _apply_buy_cost(open_map[ticker] * qty, cfg)
                    if cost > cash:
                        qty = int(cash / _apply_buy_cost(open_map[ticker], cfg))
                        cost = _apply_buy_cost(open_map[ticker] * qty, cfg)

                    if qty > 0 and cost <= cash:
                        old_qty = holdings.get(ticker, 0)
                        old_avg = avg_price.get(ticker, 0)
                        new_qty = old_qty + qty
                        avg_price[ticker] = (old_qty * old_avg + qty * open_map[ticker]) / new_qty
                        holdings[ticker] = new_qty
                        cash -= cost
                        trades.append({
                            "date": execute_dt, "ticker": ticker, "side": "BUY",
                            "price": open_map[ticker], "qty": qty, "reason": "REBALANCE"
                        })

        equity = cash + sum(holdings[t] * close_map.get(t, avg_price[t]) for t in holdings)
        equity_rows.append({
            "date": dt,
            "equity": equity,
            "cash": cash,
            "positions": len(holdings),
            "holdings": ",".join(sorted(holdings.keys())),
        })

    return pd.DataFrame(equity_rows), pd.DataFrame(trades)
