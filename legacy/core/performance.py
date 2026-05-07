import json
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from pathlib import Path


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    dd = equity / peak - 1
    return float(dd.min())


def performance_stats(equity_curve: pd.DataFrame) -> dict:
    df = equity_curve.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    df["daily_ret"] = df["equity"].pct_change().fillna(0)

    total_return = df["equity"].iloc[-1] / df["equity"].iloc[0] - 1
    days = (df["date"].iloc[-1] - df["date"].iloc[0]).days
    years = max(days / 365.25, 1 / 365.25)
    cagr = (df["equity"].iloc[-1] / df["equity"].iloc[0]) ** (1 / years) - 1

    vol = df["daily_ret"].std() * np.sqrt(252)
    sharpe = np.nan if vol == 0 else (df["daily_ret"].mean() * 252) / vol

    return {
        "start_date": str(df["date"].iloc[0].date()),
        "end_date": str(df["date"].iloc[-1].date()),
        "initial_equity": float(df["equity"].iloc[0]),
        "final_equity": float(df["equity"].iloc[-1]),
        "total_return": float(total_return),
        "cagr": float(cagr),
        "max_drawdown": max_drawdown(df["equity"]),
        "annualized_volatility": float(vol),
        "sharpe_ratio": None if np.isnan(sharpe) else float(sharpe),
    }


def save_outputs(equity_curve: pd.DataFrame, trades: pd.DataFrame, out_dir: str):
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    equity_curve.to_csv(Path(out_dir) / "equity_curve.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(Path(out_dir) / "trades.csv", index=False, encoding="utf-8-sig")

    stats = performance_stats(equity_curve)
    with open(Path(out_dir) / "performance.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    df = equity_curve.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")

    plt.figure(figsize=(12, 6))
    plt.plot(df["date"], df["equity"])
    plt.title("Equity Curve")
    plt.xlabel("Date")
    plt.ylabel("Equity")
    plt.tight_layout()
    plt.savefig(Path(out_dir) / "equity_curve.png", dpi=150)
    plt.close()

    dd = df["equity"] / df["equity"].cummax() - 1
    plt.figure(figsize=(12, 4))
    plt.plot(df["date"], dd)
    plt.title("Drawdown")
    plt.xlabel("Date")
    plt.ylabel("Drawdown")
    plt.tight_layout()
    plt.savefig(Path(out_dir) / "drawdown.png", dpi=150)
    plt.close()

    return stats
