"""P0-D: 팩터 기여도 및 Random Ranking Baseline 검증(연구용).

동결 전략의 종목 선정(모멘텀)이 동일 필터를 통과한 무작위 순위보다 우수한지 검증한다.
기존 필터·시장 게이트·주문/비용 엔진은 그대로 두고 **순위만** 무작위화한다.

- Random Ranking: `rank_etfs()`가 반환한 적격 종목의 순서만 seed·결정일 기반으로 섞는다.
- 팩터 제거: KOSPI 필터·그룹 override·trend_ok·모멘텀 기간(60/120만) 1개씩.
- 벤치마크: KODEX200, 동일 유니버스 균등비중, 정책 포트폴리오, 현금.
- 하위 기간/비용: full/ex-2026/IS/OOS, base(5bp/1x)와 stress(50bp/3x).

실행::

    uv run python scripts/p0d_factor_random_baseline.py
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import benchmark_comparison as bench
import factorial_ablation as ablation
import p0b_execution_stress as p0b
import p0c_regime_drawdown as p0c
import p1a1_risk_overlay as p1a1

import etf_shared
import run_etf_backtest as rtb

OUTPUT_DIR = ROOT / "outputs_p0d"
AUM = float(rtb.INITIAL_CASH)
CAP = 0.85
SEEDS: list[int] = list(range(100))  # 성과 확인 전에 고정
COST_CASES = (
    ("base", 0.0005, 1.0),
    ("stress_50bp_3x", 0.005, 3.0),
)
EXCLUDE_2026_END = pd.Timestamp("2025-12-31")
BASELINE_MOMENTUM_WEIGHT = 0.55
BENCH_TICKERS = {"kodex200": "069500", "us": "360750", "gold": "411060"}
REQUIRED_METRICS = (
    "cagr",
    "mdd",
    "sharpe",
    "calmar",
    "final",
    "total_cost",
    "turnover",
    "avg_cash_weight",
    "ex2026_cagr",
    "ex2026_mdd",
    "is_cagr",
    "oos_cagr",
    "trade_count",
)
CODE_FILES = (
    "scripts/p0d_factor_random_baseline.py",
    "scripts/p0b_execution_stress.py",
    "scripts/p1a1_risk_overlay.py",
    "scripts/p0c_regime_drawdown.py",
    "run_etf_backtest.py",
    "etf_shared.py",
)


def code_digest() -> str:
    payload = []
    for name in CODE_FILES:
        path = ROOT / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"
        payload.append(f"{name}:{digest}")
    return hashlib.sha256("|".join(payload).encode("utf-8")).hexdigest()


def binomial_cdf(k: int, n: int, p: float) -> float:
    return sum(math.comb(n, i) * (p**i) * ((1 - p) ** (n - i)) for i in range(k + 1))


def upper_one_sided_cp(k: int, n: int, confidence: float = 0.95) -> float:
    """k/n의 단측 Clopper-Pearson 상한(정확 이항). 0/100 → 1-0.05^(1/100)=2.95%."""
    if k >= n:
        return 1.0
    alpha = 1.0 - confidence
    low, high = 0.0, 1.0
    for _ in range(200):
        mid = (low + high) / 2.0
        if binomial_cdf(k, n, mid) > alpha:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"


# --------------------------------------------------------------------------- #
# 무작위 ranker (적격 집합·필터는 기존 rank_etfs 그대로)
# --------------------------------------------------------------------------- #
def make_random_ranker(seed: int, original):
    def rank(snapshot: pd.DataFrame) -> pd.DataFrame:
        ranked = original(snapshot)
        if ranked.empty:
            return ranked
        date_key = (
            pd.Timestamp(snapshot["date"].iloc[0]).strftime("%Y-%m-%d")
            if "date" in snapshot.columns
            else "na"
        )
        tickers = sorted(ranked["ticker"].astype(str).tolist())
        rng = random.Random(
            int(hashlib.sha256(f"{seed}:{date_key}".encode()).hexdigest()[:16], 16)
        )
        rng.shuffle(tickers)
        return ranked.set_index("ticker").loc[tickers].reset_index()

    return rank


@contextlib.contextmanager
def patched_state(
    *,
    ranker=None,
    group_override: tuple[str, ...] | None = None,
    momentum_weight: float | None = None,
):
    original_rank = rtb.rank_etfs
    original_override = set(etf_shared.GROUP_RISK_OVERRIDE)
    original_weight = os.environ.get("ETF_MOMENTUM_WEIGHT_60")
    try:
        if ranker is not None:
            rtb.rank_etfs = ranker
        if group_override is not None:
            etf_shared.GROUP_RISK_OVERRIDE.clear()
            etf_shared.GROUP_RISK_OVERRIDE.update(group_override)
        if momentum_weight is not None:
            os.environ["ETF_MOMENTUM_WEIGHT_60"] = str(momentum_weight)
        yield
    finally:
        rtb.rank_etfs = original_rank
        etf_shared.GROUP_RISK_OVERRIDE.clear()
        etf_shared.GROUP_RISK_OVERRIDE.update(original_override)
        if original_weight is None:
            os.environ.pop("ETF_MOMENTUM_WEIGHT_60", None)
        else:
            os.environ["ETF_MOMENTUM_WEIGHT_60"] = original_weight


def run(
    price: pd.DataFrame,
    index_df: pd.DataFrame,
    dates: list[pd.Timestamp],
    *,
    slippage: float,
    fee_multiplier: float,
    ranker=None,
    use_market_filter: bool = True,
    group_override: tuple[str, ...] | None = None,
    momentum_weight: float | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    with patched_state(
        ranker=ranker,
        group_override=group_override,
        momentum_weight=momentum_weight,
    ), p0b.patched_costs(fee_multiplier):
        curve, trades = rtb.run_etf_strategy(
            AUM,
            dates,
            index_df,
            use_market_filter=use_market_filter,
            max_positions=int(p0b.APPLIED["max_positions"]),
            slippage=slippage,
            risk_off_liquidate=True,
            price_data=price,
            max_asset_pct=CAP,
        )
    p0b.assert_legacy_reconciled(curve, trades, AUM, "p0d run")
    return curve, trades


def metrics(
    curve: pd.DataFrame,
    trades: pd.DataFrame,
    dates: list[pd.Timestamp],
    *,
    slippage: float,
    fee_multiplier: float,
    oos_start: pd.Timestamp,
) -> dict[str, Any]:
    stats = p0b.performance(AUM, dates, curve)
    trading = p0b.trading_stats(curve, trades)
    costs = p0b.cost_breakdown(trades, slippage, fee_multiplier)
    cash = (curve["cash"] / curve["equity"]).replace([np.inf, -np.inf], np.nan)
    equity = p0c.equity_with_initial(curve, dates)
    ex2026 = p1a1.slice_stats(equity, None, EXCLUDE_2026_END)
    is_stats = p1a1.slice_stats(equity, None, oos_start - pd.Timedelta(days=1))
    oos_stats = p1a1.slice_stats(equity, oos_start, None)
    return {
        **stats,
        "trade_count": trading["trade_count"],
        "turnover": trading["turnover"],
        "avg_cash_weight": float(cash.mean()),
        "total_cost": costs["total_run_rate_cost"],
        "ex2026_cagr": ex2026["cagr"],
        "ex2026_mdd": ex2026["mdd"],
        "ex2026_sharpe": ex2026["sharpe"],
        "is_cagr": is_stats["cagr"],
        "is_mdd": is_stats["mdd"],
        "is_total_return": is_stats["total_return"],
        "oos_cagr": oos_stats["cagr"],
        "oos_mdd": oos_stats["mdd"],
        "oos_total_return": oos_stats["total_return"],
        "oos_days": oos_stats["days"],
    }


# --------------------------------------------------------------------------- #
# 분포 요약
# --------------------------------------------------------------------------- #
def summarize(
    frame: pd.DataFrame, baseline: dict[str, Any], label: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    metrics_list = (
        "cagr",
        "mdd",
        "sharpe",
        "calmar",
        "final",
        "total_cost",
        "turnover",
        "avg_cash_weight",
        "ex2026_cagr",
        "ex2026_mdd",
        "is_cagr",
        "oos_cagr",
    )
    rng = np.random.default_rng(0)
    # MDD는 0에 가까울수록(값이 클수록) 좋다. 비용·회전만 낮을수록 좋다.
    lower_is_better = {"total_cost", "turnover"}
    informational = {"avg_cash_weight"}
    for metric in metrics_list:
        values = pd.to_numeric(frame[metric], errors="coerce").dropna().to_numpy()
        if len(values) < 2 or baseline.get(metric) is None:
            continue
        base_value = float(baseline[metric])
        higher_is_better = metric not in lower_is_better
        if higher_is_better:
            percentile = float((values < base_value).mean() * 100)
            share_ge = float((values >= base_value).mean())
        else:
            percentile = float((values > base_value).mean() * 100)
            share_ge = float((values <= base_value).mean())
        boot = np.array(
            [rng.choice(values, size=len(values), replace=True).mean() for _ in range(1000)]
        )
        as_good_count = round(share_ge * len(values))
        rows.append(
            {
                "cost_case": label,
                "metric": metric,
                "informational_only": metric in informational,
                "n": len(values),
                "mean": float(values.mean()),
                "std": float(values.std(ddof=1)),
                "median": float(np.median(values)),
                "p05": float(np.quantile(values, 0.05)),
                "p95": float(np.quantile(values, 0.95)),
                "min": float(values.min()),
                "max": float(values.max()),
                "baseline": base_value,
                "baseline_percentile": percentile,
                "share_at_least_as_good": share_ge,
                "share_binomial_se": float(np.sqrt(share_ge * (1 - share_ge) / len(values))),
                "share_upper_95_one_sided": upper_one_sided_cp(as_good_count, len(values)),
                "mean_bootstrap_ci_low": float(np.quantile(boot, 0.025)),
                "mean_bootstrap_ci_high": float(np.quantile(boot, 0.975)),
                "higher_is_better": higher_is_better,
                "percentile_meaning": (
                    "정보용(순위·우열 해석 금지)"
                    if metric in informational
                    else "baseline보다 나쁜 무작위 비율(높을수록 우수)"
                ),
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# C. 팩터 제거
# --------------------------------------------------------------------------- #
def factor_ablation(
    price: pd.DataFrame,
    index_df: pd.DataFrame,
    dates: list[pd.Timestamp],
    oos_start: pd.Timestamp,
) -> pd.DataFrame:
    def no_trend_ranker(original):
        def rank(snapshot: pd.DataFrame) -> pd.DataFrame:
            frame = snapshot.copy()
            frame["trend_ok"] = True
            return original(frame)

        return rank

    variants: list[tuple[str, dict[str, Any]]] = [
        ("baseline", {"momentum_weight": BASELINE_MOMENTUM_WEIGHT}),
        ("no_kospi_filter", {"use_market_filter": False}),
        ("no_group_override", {"group_override": ()}),
        ("no_trend_filter", {"ranker_factory": no_trend_ranker}),
        ("mom60_only", {"momentum_weight": 1.0}),
        ("mom120_only", {"momentum_weight": 0.0}),
    ]
    rows: list[dict[str, Any]] = []
    original_rank = etf_shared.rank_etfs
    for label, kwargs in variants:
        ranker = kwargs.pop("ranker_factory", None)
        rank = ranker(original_rank) if ranker else None
        curve, trades = run(
            price,
            index_df,
            dates,
            slippage=0.0005,
            fee_multiplier=1.0,
            ranker=rank,
            **kwargs,
        )
        row = {"variant": label, **metrics(
            curve, trades, dates, slippage=0.0005, fee_multiplier=1.0, oos_start=oos_start
        )}
        rows.append(row)
        print(
            f"[p0d] factor {label:18s} cagr={row['cagr']:.3%} mdd={row['mdd']:.2%} "
            f"sharpe={row['sharpe']:.3f}"
        )
    frame = pd.DataFrame(rows)
    base = frame[frame["variant"] == "baseline"].iloc[0]
    frame["cagr_delta_pp"] = (frame["cagr"] - base["cagr"]) * 100
    frame["mdd_delta_pp"] = (frame["mdd"] - base["mdd"]) * 100
    frame["sharpe_delta"] = frame["sharpe"] - base["sharpe"]
    return frame


def factorial_crosscheck(
    price: pd.DataFrame,
    index_df: pd.DataFrame,
    dates: list[pd.Timestamp],
    expected: pd.DataFrame,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    cases = (
        ("no_kospi_filter", False, None),
        ("no_group_override", True, ()),
    )
    for label, use_filter, override in cases:
        original = set(etf_shared.GROUP_RISK_OVERRIDE)
        if override is not None:
            etf_shared.GROUP_RISK_OVERRIDE.clear()
            etf_shared.GROUP_RISK_OVERRIDE.update(override)
        try:
            result = ablation.run_scenario(
                common_dates=dates,
                index_df=index_df,
                price_data=price,
                us_index_df=None,
                label=label,
                use_market_filter=use_filter,
                risk_off_liquidate=True,
                enable_multi_index_risk=False,
                max_positions=int(p0b.APPLIED["max_positions"]),
                rebalance_step_days=int(rtb.REBALANCE_STEP_DAYS),
                momentum_weight_60=BASELINE_MOMENTUM_WEIGHT,
                universe_tickers=list(etf_shared.ETF_LIST),
            )
        finally:
            etf_shared.GROUP_RISK_OVERRIDE.clear()
            etf_shared.GROUP_RISK_OVERRIDE.update(original)
        row = expected[expected["variant"] == label].iloc[0]
        matches = (
            abs(float(result["final_equity"]) - float(row["final"])) < 1e-6
            and abs(float(result["mdd"]) - float(row["mdd"])) < 1e-12
            and int(result["n_trades"]) == int(row["trade_count"])
        )
        if not matches:
            raise AssertionError(f"{label}: factorial_ablation 교차검증 불일치")
        checks.append({"variant": label, "crosscheck": True})
    return checks


# --------------------------------------------------------------------------- #
# D. 벤치마크
# --------------------------------------------------------------------------- #
def build_benchmarks(price: pd.DataFrame, dates: list[pd.Timestamp]) -> pd.DataFrame:
    prices = (
        price.pivot_table(index="date", columns="ticker", values="close")
        .sort_index()
        .reindex(pd.DatetimeIndex(dates))
        .ffill()
    )
    rows: list[dict[str, Any]] = []

    def add(name: str, series: pd.Series, basis: str) -> None:
        series = series.dropna()
        if len(series) < 2:
            return
        stats = bench.calc_stats(series)
        rows.append(
            {
                "benchmark": name,
                "basis": basis,
                "start": series.index[0].strftime("%Y-%m-%d"),
                "end": series.index[-1].strftime("%Y-%m-%d"),
                "cagr": stats["cagr"],
                "mdd": stats["mdd"],
                "sharpe": stats["sharpe"],
                "calmar": stats["calmar"],
                "final": stats["final"],
            }
        )

    kodex = BENCH_TICKERS["kodex200"]
    if kodex in prices.columns:
        close = prices[kodex].dropna()
        add("kodex200_buy_hold", close / close.iloc[0] * AUM, "price, no cost")

    returns = prices.pct_change()
    equal = returns.mean(axis=1, skipna=True).fillna(0.0)
    equity = AUM * (1.0 + equal).cumprod()
    add("universe_equal_weight_daily", equity, "price, daily rebalance, no cost")

    weights = {"069500": 0.5, "360750": 0.3, "411060": 0.2}
    cols = [t for t in weights if t in prices.columns]
    if len(cols) == len(weights):
        sub = prices[cols].dropna()
        if len(sub) > 1:
            # 고정 가중치를 매일 재조정하는 정책 포트폴리오(비용 0 가정).
            rets = sub.pct_change().dropna()
            policy = pd.concat(
                [
                    pd.Series([AUM], index=[sub.index[0]]),
                    AUM * (1.0 + sum(weights[t] * rets[t] for t in cols)).cumprod(),
                ]
            )
            add("policy_50_30_20_daily", policy, "price, daily rebalance, no cost")

    add(
        "cash",
        pd.Series(AUM, index=pd.DatetimeIndex(dates)),
        "0% return",
    )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# 재사용 무결성 검증
# --------------------------------------------------------------------------- #
def validate_reuse(
    frame: pd.DataFrame | None,
    stored: dict[str, Any] | None,
    current: dict[str, Any],
    expected_pairs: set[tuple[int, str]],
    frame_sha256: str,
) -> list[str]:
    """기존 random_runs 재사용 가능 여부. 문제 목록을 반환(빈 리스트면 통과)."""
    reasons: list[str] = []
    if stored is None:
        reasons.append("provenance 파일이 없습니다")
    else:
        for key in (
            "code_digest",
            "data_cache_digest",
            "freeze_sha256",
            "period",
            "cost_cases",
            "momentum_weight_60",
            "max_asset_pct",
            "spread_pct",
            "oos_start_date",
            "seeds",
        ):
            if stored.get(key) != current.get(key):
                reasons.append(f"provenance 불일치: {key}")
        if stored.get("random_runs_sha256") != frame_sha256:
            reasons.append("random_runs SHA256 불일치")
        failures = stored.get("failures")
        if isinstance(failures, bool) or not isinstance(failures, int) or failures != 0:
            reasons.append("원 실행 실패 건수 메타데이터가 정수 0이 아님")
        if stored.get("experiment_complete") is not True:
            reasons.append("experiment_complete가 boolean true가 아님")
    if frame is None or frame.empty:
        reasons.append("random_runs가 비어 있습니다")
        return reasons
    if not {"seed", "cost_case"}.issubset(frame.columns):
        reasons.append("seed/cost_case 컬럼 누락")
        return reasons
    seed_values = pd.to_numeric(frame["seed"], errors="coerce")
    if seed_values.isna().any() or not np.isfinite(seed_values.to_numpy()).all():
        reasons.append("seed에 NaN/Inf 존재")
        return reasons
    if (seed_values % 1 != 0).any():
        reasons.append("seed가 정수가 아님")
        return reasons
    pairs = list(zip(seed_values.astype(int), frame["cost_case"].astype(str), strict=False))
    if len(pairs) != len(expected_pairs):
        reasons.append(f"행 수 불일치: {len(pairs)} != {len(expected_pairs)}")
    if len(set(pairs)) != len(pairs):
        reasons.append("중복된 (seed, cost_case) 행")
    missing = expected_pairs - set(pairs)
    extra = set(pairs) - expected_pairs
    if missing:
        reasons.append(f"누락된 조합 {len(missing)}건")
    if extra:
        reasons.append(f"예상 밖 조합 {len(extra)}건")
    for column in REQUIRED_METRICS:
        if column not in frame.columns:
            reasons.append(f"필수 지표 누락: {column}")
            continue
        values = pd.to_numeric(frame[column], errors="coerce")
        if values.isna().any() or not np.isfinite(values.to_numpy()).all():
            reasons.append(f"{column}에 NaN/Inf 존재")
    return reasons


def resolve_source_provenance(
    preserved: dict[str, Any] | None, stored: dict[str, Any], random_sha: str
) -> tuple[dict[str, Any], int, bool]:
    """재사용 시 보존할 원본 provenance 선택.

    반환: (보존할 provenance, reuse_count, 원본 파일 갱신 필요 여부)
    - 같은 산출물(SHA256 일치)의 최초 full provenance는 유지한다.
    - 다른 산출물의 잔여 파일(이전 full 실행)이면 현재 실행 것으로 교체해 계보 혼선을 막는다.
    """
    reuse_count = 1
    if stored.get("run_mode") == "reused" and isinstance(stored.get("source"), dict):
        reuse_count = int(stored["source"].get("reuse_count", 1)) + 1
    if (
        preserved is not None
        and preserved.get("run_mode") == "full"
        and preserved.get("random_runs_sha256") == random_sha
    ):
        return preserved, reuse_count, False
    return stored, reuse_count, True


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="P0-D factor and random baseline")
    parser.add_argument(
        "--reuse-random-runs",
        action="store_true",
        help="기존 random_runs.csv를 seed·비용 조건 검증 후 재사용(동일 seed 집합)",
    )
    args = parser.parse_args()

    frozen = p0b.configure_frozen_settings(CAP)
    os.environ["ETF_MOMENTUM_WEIGHT_60"] = str(BASELINE_MOMENTUM_WEIGHT)
    p0b.rtb.START, p0b.rtb.END = p0b.DEFAULT_START, p0b.DEFAULT_END
    preflight = p0b.preflight_cache(p0b.DEFAULT_START, p0b.DEFAULT_END)
    digest_before = p0b.data_cache_digest()
    price, index_df, dates = p0b.load_inputs()
    p0b.verify_applied_settings()
    missing = sorted({str(t) for t in etf_shared.ETF_LIST} - set(price["ticker"].astype(str)))
    if missing:
        raise SystemExit(f"[p0d] 필수 종목 누락: {missing}")
    oos_start = pd.Timestamp(frozen["oos_start_date"])

    baselines: dict[str, dict[str, Any]] = {}
    for cost_label, slippage, multiplier in COST_CASES:
        baseline_curve, baseline_trades = run(
            price,
            index_df,
            dates,
            slippage=slippage,
            fee_multiplier=multiplier,
            momentum_weight=BASELINE_MOMENTUM_WEIGHT,
        )
        baselines[cost_label] = metrics(
            baseline_curve,
            baseline_trades,
            dates,
            slippage=slippage,
            fee_multiplier=multiplier,
            oos_start=oos_start,
        )
    baseline = baselines["base"]
    expected = {
        "cagr": 0.2454113778672673,
        "mdd": -0.5121244554166072,
        "sharpe": 1.0391357658469746,
    }
    if any(abs(baseline[k] - v) > 1e-9 for k, v in expected.items()):
        raise SystemExit(f"[p0d] 기준 재현 실패: {baseline}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    random_rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    reuse_path = OUTPUT_DIR / "random_runs.csv"
    provenance_path = OUTPUT_DIR / "run_provenance.json"
    source_provenance_path = OUTPUT_DIR / "run_provenance_source.json"
    expected_pairs = {(seed, label) for label, _, _ in COST_CASES for seed in SEEDS}
    current_provenance = {
        "git_commit": p0b.git_commit(),
        "code_digest": code_digest(),
        "data_cache_digest": digest_before,
        "freeze_sha256": frozen["freeze_sha256"],
        "period": {"start": p0b.DEFAULT_START, "end": p0b.DEFAULT_END},
        "cost_cases": {
            label: [slippage, multiplier] for label, slippage, multiplier in COST_CASES
        },
        "momentum_weight_60": BASELINE_MOMENTUM_WEIGHT,
        "max_asset_pct": CAP,
        "spread_pct": frozen["spread_pct"],
        "oos_start_date": frozen["oos_start_date"],
        "seeds": {"count": len(SEEDS), "range": [min(SEEDS), max(SEEDS)]},
    }
    run_mode = "full"
    source_info: dict[str, Any] | None = None
    random_sha = ""
    if args.reuse_random_runs:
        if not reuse_path.exists():
            raise SystemExit("[p0d] 재사용할 random_runs.csv가 없습니다.")
        if not provenance_path.exists():
            raise SystemExit("[p0d] 재사용에 필요한 run_provenance.json이 없습니다.")
        existing = pd.read_csv(reuse_path, float_precision="round_trip")
        stored = json.loads(provenance_path.read_text(encoding="utf-8"))
        random_sha = file_sha256(reuse_path)
        reasons = validate_reuse(
            existing, stored, current_provenance, expected_pairs, random_sha
        )
        if reasons:
            raise SystemExit("[p0d] 재사용 거부: " + "; ".join(reasons))
        preserved = (
            json.loads(source_provenance_path.read_text(encoding="utf-8"))
            if source_provenance_path.exists()
            else None
        )
        original, reuse_count, needs_write = resolve_source_provenance(
            preserved, stored, random_sha
        )
        if needs_write:
            source_provenance_path.write_text(
                json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        source_info = {
            "run_mode": original.get("run_mode"),
            "created_at": original.get("created_at"),
            "random_runs_sha256": original.get("random_runs_sha256"),
            "git_commit": original.get("git_commit"),
            "provenance_sha256": file_sha256(source_provenance_path),
            "reused_from_provenance_sha256": file_sha256(provenance_path),
            "reuse_count": reuse_count,
            "preserved_at": source_provenance_path.name,
        }
        run_mode = "reused"
        random_rows = existing.to_dict(orient="records")
        print(f"[p0d] 기존 무작위 실행 재사용(검증 통과): {len(random_rows)}건")
    else:
        for cost_label, slippage, multiplier in COST_CASES:
            for seed in SEEDS:
                try:
                    curve, trades = run(
                        price,
                        index_df,
                        dates,
                        slippage=slippage,
                        fee_multiplier=multiplier,
                        ranker=make_random_ranker(seed, rtb.rank_etfs),
                        momentum_weight=BASELINE_MOMENTUM_WEIGHT,
                    )
                    row = metrics(
                        curve,
                        trades,
                        dates,
                        slippage=slippage,
                        fee_multiplier=multiplier,
                        oos_start=oos_start,
                    )
                except Exception as exc:  # noqa: BLE001 - 실패도 기록하고 계속
                    failures.append({"seed": seed, "cost_case": cost_label, "error": str(exc)})
                    continue
                random_rows.append({"seed": seed, "cost_case": cost_label, **row})
                if seed % 20 == 0:
                    print(
                        f"[p0d] seed {seed:3d} {cost_label:13s} cagr={row['cagr']:.3%} "
                        f"mdd={row['mdd']:.2%}"
                    )
    random_frame = pd.DataFrame(random_rows)
    if random_frame.empty:
        raise SystemExit("[p0d] 무작위 실행이 모두 실패했습니다.")
    if run_mode == "full":
        random_frame.to_csv(reuse_path, index=False, encoding="utf-8-sig")
        random_sha = file_sha256(reuse_path)
    pd.DataFrame(failures).to_csv(
        OUTPUT_DIR / "random_failures.csv", index=False, encoding="utf-8-sig"
    )
    experiment_complete = len(failures) == 0 and len(random_frame) == len(expected_pairs)

    summary_rows: list[dict[str, Any]] = []
    for cost_label, _, _ in COST_CASES:
        subset = random_frame[random_frame["cost_case"] == cost_label]
        summary_rows.extend(summarize(subset, baselines[cost_label], cost_label))
    summary = pd.DataFrame(summary_rows)
    summary.to_csv(OUTPUT_DIR / "random_summary.csv", index=False, encoding="utf-8-sig")

    factors = factor_ablation(price, index_df, dates, oos_start)
    factors.to_csv(OUTPUT_DIR / "factor_ablation.csv", index=False, encoding="utf-8-sig")
    crosschecks = factorial_crosscheck(price, index_df, dates, factors)

    benchmarks = build_benchmarks(price, dates)
    benchmarks.to_csv(OUTPUT_DIR / "benchmarks.csv", index=False, encoding="utf-8-sig")

    if p0b.data_cache_digest() != digest_before:
        raise SystemExit("[p0d] 실행 중 data_cache가 변경되었습니다.")

    provenance = {
        **current_provenance,
        "run_mode": run_mode,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "random_runs_sha256": random_sha,
        "random_runs_rows": len(random_frame),
        "failures": len(failures),
        "experiment_complete": experiment_complete,
        "source": source_info,
    }
    provenance_path.write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    metadata = {
        "phase": "P0-D",
        "run_mode": run_mode,
        "experiment_complete": experiment_complete,
        "random_runs_sha256": random_sha,
        "provenance": provenance,
        "period": {"start": p0b.DEFAULT_START, "end": p0b.DEFAULT_END},
        "seeds": {"count": len(SEEDS), "range": [min(SEEDS), max(SEEDS)]},
        "runs": {
            "attempted": len(SEEDS) * len(COST_CASES),
            "succeeded": len(random_frame),
            "failed": len(failures),
        },
        "cost_cases": {label: [slippage, multiplier] for label, slippage, multiplier in COST_CASES},
        "momentum_weight_60": BASELINE_MOMENTUM_WEIGHT,
        "max_asset_pct": CAP,
        "spread_pct": frozen["spread_pct"],
        "freeze_sha256": frozen["freeze_sha256"],
        "oos_start_date": frozen["oos_start_date"],
        "preflight": preflight,
        "git_commit": p0b.git_commit(),
        "data_cache_digest": digest_before,
        "data_cache_unchanged": True,
        "baselines": baselines,
        "baseline": baseline,
        "baseline_matches_p0b": True,
        "factor_crosscheck": crosschecks,
        "artifacts": sorted(path.name for path in OUTPUT_DIR.glob("*")),
    }
    summary_payload = {
        "baseline": baseline,
        "baselines": baselines,
        "seeds": metadata["seeds"],
        "runs": metadata["runs"],
        "random_summary": summary.to_dict(orient="records"),
        "factor_ablation": factors.to_dict(orient="records"),
        "benchmarks": benchmarks.to_dict(orient="records"),
    }
    (OUTPUT_DIR / "p0d_summary.json").write_text(
        json.dumps(summary_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (OUTPUT_DIR / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"[p0d] 완료: {OUTPUT_DIR} (seeds={len(SEEDS)}, failures={len(failures)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
