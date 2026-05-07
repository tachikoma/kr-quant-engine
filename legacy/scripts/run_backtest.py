import argparse
import os
import sys
from pathlib import Path

import yaml
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from legacy.core.data_loader import get_tickers, load_or_fetch_ohlcv, load_or_fetch_fundamentals, fetch_index_ohlcv
from legacy.core.backtester import run_backtest
from legacy.core.performance import save_outputs


def load_dotenv(dotenv_path: str | Path | None = None) -> None:
    if dotenv_path is None:
        dotenv_path = Path(__file__).resolve().parent / ".env"
    path = Path(dotenv_path)
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


def main():
    load_dotenv()
    default_config = PROJECT_ROOT / "legacy" / "config" / "config.yaml"
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="YYYYMMDD")
    parser.add_argument("--end", required=True, help="YYYYMMDD")
    parser.add_argument("--market", default="KOSPI", choices=["KOSPI", "KOSDAQ"])
    parser.add_argument("--max-tickers", type=int, default=200)
    parser.add_argument("--config", default=str(default_config))
    parser.add_argument("--out", default="outputs")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    tickers = get_tickers(args.end, market=args.market)
    tickers = tickers[:args.max_tickers]

    price = load_or_fetch_ohlcv(
        args.start,
        args.end,
        tickers,
        cache_dir=cfg["data"]["cache_dir"],
    )

    # 월말 리밸런싱 기준일을 먼저 만들고 그 날짜의 펀더멘털을 수집
    rebalance_dates = pd.Series(sorted(price["date"].unique())).groupby(
        pd.Series(sorted(price["date"].unique())).dt.to_period("M")
    ).max().tolist()

    fundamentals = load_or_fetch_fundamentals(
        rebalance_dates,
        market=args.market,
        cache_dir=cfg["data"]["cache_dir"],
    )

    index_code = "1001" if args.market == "KOSPI" else "2001"
    index_df = fetch_index_ohlcv(args.start, args.end, index_code=index_code)

    equity_curve, trades = run_backtest(price, fundamentals, index_df, cfg)
    stats = save_outputs(equity_curve, trades, args.out)

    print("=== Performance ===")
    for k, v in stats.items():
        if isinstance(v, float):
            print(f"{k}: {v:.4f}")
        else:
            print(f"{k}: {v}")


if __name__ == "__main__":
    main()
