import numpy as np
import pandas as pd


def zscore(s: pd.Series) -> pd.Series:
    s = s.replace([np.inf, -np.inf], np.nan)
    if s.notna().sum() < 2:
        return pd.Series(0, index=s.index)
    filled = s.fillna(s.median())
    std = filled.std(ddof=0)
    if std == 0 or np.isnan(std):
        return pd.Series(0, index=s.index)
    return (filled - filled.mean()) / std


def add_price_features(price: pd.DataFrame) -> pd.DataFrame:
    df = price.sort_values(["ticker", "date"]).copy()
    if "trading_value" not in df.columns and {"close", "volume"}.issubset(df.columns):
        df["trading_value"] = df["close"] * df["volume"]

    g = df.groupby("ticker")

    df["ret_60"] = g["close"].pct_change(60)
    df["ret_120"] = g["close"].pct_change(120)
    df["ma20"] = g["close"].transform(lambda x: x.rolling(20).mean())
    df["ma60"] = g["close"].transform(lambda x: x.rolling(60).mean())
    df["avg_trading_value_20"] = g["trading_value"].transform(lambda x: x.rolling(20).mean())
    df["trend_ok"] = (df["close"] > df["ma20"]) & (df["ma20"] > df["ma60"])
    return df


def score_universe(snapshot: pd.DataFrame, fundamental: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    df = snapshot.merge(fundamental, on="ticker", how="left")

    f = cfg["filters"]
    df = df[df["close"] >= f["min_price"]]
    df = df[df["avg_trading_value_20"] >= f["min_avg_trading_value_20d"]]

    # 펀더멘털 이상치 필터
    if "per" in df:
        df = df[(df["per"].isna()) | (df["per"] <= f["max_per"])]
    if "pbr" in df:
        df = df[(df["pbr"].isna()) | (df["pbr"] >= f["min_pbr"])]

    # 가치: 낮은 PER/PBR 선호
    df["value_score"] = -zscore(df.get("per", pd.Series(index=df.index, dtype=float))) \
                        -zscore(df.get("pbr", pd.Series(index=df.index, dtype=float)))

    # 퀄리티: EPS/BPS가 양수인 종목 선호. KRX 펀더멘털만으로는 ROE가 없으므로 근사.
    eps = df.get("eps", pd.Series(index=df.index, dtype=float))
    bps = df.get("bps", pd.Series(index=df.index, dtype=float))
    roe_proxy = eps / bps.replace(0, np.nan)
    df["quality_score"] = zscore(roe_proxy)

    # 배당
    df["dividend_score"] = zscore(df.get("div", pd.Series(index=df.index, dtype=float)))

    # 모멘텀
    df["momentum_score"] = zscore(df["ret_60"]) * 0.6 + zscore(df["ret_120"]) * 0.4

    w = cfg["ranking_weights"]
    df["total_score"] = (
        w["value"] * df["value_score"]
        + w["quality"] * df["quality_score"]
        + w["dividend"] * df["dividend_score"]
        + w["momentum"] * df["momentum_score"]
    )

    df = df[df["trend_ok"] == True]
    return df.sort_values("total_score", ascending=False)


def get_rebalance_dates(price: pd.DataFrame, freq: str = "M") -> list[pd.Timestamp]:
    dates = pd.Series(sorted(price["date"].unique()))
    if freq == "M":
        return list(dates.groupby(dates.dt.to_period("M")).max())
    if freq == "W":
        return list(dates.groupby(dates.dt.to_period("W")).max())
    raise ValueError("freq must be M or W")
