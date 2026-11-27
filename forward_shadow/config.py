"""Freeze-derived effective configuration and the Shadow manifest.

The frozen payload in ``strategy_freeze.json`` does not spell out every value
that affects execution (momentum weight, capacity participation, carry limit,
rebalance anchor, initial capital).  This module records those values in a
separate manifest and fails closed when either the code, the freeze or the
*effective* runtime configuration drifts.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import etf_execution
import etf_shared
from config_utils import parse_fraction_env, parse_pct_env
from forward_shadow.trading_calendar import CALENDAR_STATUS, COVERAGE_END, COVERAGE_START
from strategy_freeze import load_frozen_strategy

ROOT = Path(__file__).resolve().parents[1]

MANIFEST_NAME = "manifest.json"
SCHEMA_VERSION = 2
RUNTIME_KINDS = ("TEST", "FORWARD")
APPROVED = "APPROVED"
UNAPPROVED = "UNAPPROVED_TEST_VALUES"
UNAPPROVED_FIELD = "UNAPPROVED_TEST_VALUE"
APPROVAL_FIELDS = ("status", "approved_at", "approver", "decision_ref")
# A day's data counts as confirmed from this KST time (after-hours trading has
# ended and closing NAV is published).  Not an approved operational value.
CONFIRM_CUTOFF_KST = "18:00"
# The forward observation window of day D closes when D+1 can start trading.
WINDOW_CLOSE_KST = "09:00"

# Values pinned by the frozen code but absent from strategy_freeze.json.
MOMENTUM_WEIGHT_60 = 0.55
BUY_FEE_PCT = 0.00015
SELL_FEE_PCT = 0.00015
SELL_TAX_PCT = 0.154
CAPACITY_PARTICIPATION = 0.05
CAPACITY_MAX_CARRY_DAYS = 1
KOSPI_INDEX_CODE = "1001"

# Files whose bytes define the behaviour of the shadow track.  A change to any
# of them invalidates an existing manifest (fail-closed on code drift).
CODE_FILES = (
    "forward_shadow/__init__.py",
    "forward_shadow/config.py",
    "forward_shadow/store.py",
    "forward_shadow/market.py",
    "forward_shadow/warmup.py",
    "forward_shadow/trading_calendar.py",
    "forward_shadow/engine.py",
    "forward_shadow/report.py",
    "forward_shadow/runner.py",
    "run_forward_shadow.py",
    "config_utils.py",
    "etf_distributions.py",
    "etf_shared.py",
    "etf_execution.py",
    "pykrx_utils.py",
    "strategy_freeze.py",
    "run_etf_backtest.py",
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def code_fingerprint(root: Path = ROOT) -> dict[str, str]:
    """Return ``{relative_path: sha256}`` for every behaviour-defining file."""
    result: dict[str, str] = {}
    for rel in CODE_FILES:
        path = root / rel
        if not path.exists():
            raise FileNotFoundError(f"Shadow code file missing: {path}")
        result[rel] = _sha256_bytes(path.read_bytes())
    return result


def aggregate_sha256(parts: dict[str, str]) -> str:
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(canonical)


# Environment variables that can change the behaviour of the reused helpers.
# They are snapshotted into the manifest and re-checked on every run so an
# inherited .env cannot alter a supposedly frozen signal.
STRATEGY_ENV_KEYS = (
    "ETF_LIST",
    "ETF_UNIVERSE_MODE",
    "TAXABLE_ETF_TICKERS",
    "ETF_DEVIATION_THRESHOLD_BY_GROUP",
    "ETF_DEVIATION_THRESHOLD_BY_TICKER",
    "ETF_MOMENTUM_WEIGHT_60",
    "ETF_BASE_SLIPPAGE",
    "ETF_SPREAD_PCT",
    "MAX_ASSET_PCT",
    "MIN_AVG_TRADING_VALUE",
    "MIN_LISTING_DAYS",
    "MAX_PREMIUM_DISCOUNT",
    "ETF_RETURN_BASIS",
    "ETF_DISTRIBUTIONS_FILE",
    "ETF_DISTRIBUTION_TAX_PCT",
    "TARGET_WEIGHT_REBALANCE",
    "REBALANCE_BAND_PCT",
    "TRIM_OVERWEIGHT_POSITIONS",
    "LIQUIDATE_ON_RISK_OFF",
    "ENABLE_MULTI_INDEX_RISK",
    "MULTI_INDEX_GATING_MODE",
    "US_RISK_PROXY",
    "REBALANCE_STEP_DAYS",
)


def strategy_env_snapshot() -> dict[str, str | None]:
    return {key: os.environ.get(key) for key in STRATEGY_ENV_KEYS}


def effective_config_problems(frozen: dict[str, Any] | None = None) -> list[str]:
    """Compare the values the shadow will actually use against the freeze.

    This inspects the live module objects (which absorbed the environment at
    import time), so it catches drift that already exists at initialization.
    """
    frozen = frozen or load_frozen_strategy()
    params = frozen["parameters"]
    universe = [str(t) for t in frozen["universe"]]
    groups = {str(k): str(v) for k, v in frozen["ticker_groups"].items()}
    problems: list[str] = []

    def expect(name: str, actual: Any, wanted: Any) -> None:
        if actual != wanted:
            problems.append(f"{name}: effective={actual!r} freeze={wanted!r}")

    expect("universe_mode", etf_shared.UNIVERSE_MODE, frozen["universe_mode"])
    expect("ETF_LIST", list(etf_shared.ETF_LIST), universe)
    expect(
        "ticker_groups",
        {t: etf_shared.get_etf_group(t) for t in universe},
        {t: groups[t] for t in universe},
    )
    expect("ETF_TICKER_GROUPS", dict(etf_shared.ETF_TICKER_GROUPS), groups)
    expect(
        "GROUP_RISK_OVERRIDE",
        sorted(etf_shared.GROUP_RISK_OVERRIDE),
        sorted(frozen["group_risk_override"]),
    )
    expect(
        "TAXABLE_ETF_TICKERS",
        sorted(etf_shared.TAXABLE_ETF_TICKERS),
        sorted(str(t) for t in frozen["taxable_tickers"]),
    )
    expect(
        "deviation_threshold_by_group",
        dict(etf_shared.ETF_DEVIATION_THRESHOLD_BY_GROUP),
        params["deviation_threshold_by_group"],
    )
    expect(
        "deviation_threshold_by_ticker",
        dict(etf_shared.ETF_DEVIATION_THRESHOLD_BY_TICKER),
        params["deviation_threshold_by_ticker"],
    )
    by_ticker = params["deviation_threshold_by_ticker"]
    by_group = params["deviation_threshold_by_group"]
    expect(
        "deviation_threshold_per_ticker",
        {t: etf_shared.get_deviation_threshold(t) for t in universe},
        {
            t: by_ticker.get(
                t, by_group.get(groups[t], params["max_premium_discount"])
            )
            for t in universe
        },
    )
    for name, actual, key in (
        ("MAX_PREMIUM_DISCOUNT", etf_shared.MAX_PREMIUM_DISCOUNT, "max_premium_discount"),
        ("MIN_AVG_TRADING_VALUE", etf_shared.MIN_AVG_TRADING_VALUE, "min_avg_trading_value"),
        ("MIN_LISTING_DAYS", etf_shared.MIN_LISTING_DAYS, "min_listing_days"),
        ("MARKET_MA_DAYS", etf_shared.MARKET_MA_DAYS, "market_ma_days"),
        ("MARKET_SLOPE_DAYS", etf_shared.MARKET_SLOPE_DAYS, "market_slope_days"),
        ("ETF_MAX_POSITIONS", etf_shared.ETF_MAX_POSITIONS, "max_positions"),
        ("ETF_SELL_RANK_BUFFER", etf_shared.ETF_SELL_RANK_BUFFER, "sell_rank_buffer"),
        ("REBALANCE_STEP_DAYS", etf_shared.REBALANCE_STEP_DAYS, "rebalance_step_days"),
    ):
        expect(name, actual, params[key])

    expect(
        "ETF_MOMENTUM_WEIGHT_60",
        parse_fraction_env("ETF_MOMENTUM_WEIGHT_60", MOMENTUM_WEIGHT_60),
        MOMENTUM_WEIGHT_60,
    )
    raw_weight = os.environ.get("ETF_MOMENTUM_WEIGHT_60")
    if raw_weight is not None:
        try:
            expect("ETF_MOMENTUM_WEIGHT_60(raw)", float(raw_weight), MOMENTUM_WEIGHT_60)
        except ValueError:
            problems.append(f"ETF_MOMENTUM_WEIGHT_60: invalid value {raw_weight!r}")
    expect("BUY_FEE_PCT", etf_shared.BUY_FEE_PCT, BUY_FEE_PCT)
    expect("SELL_FEE_PCT", etf_shared.SELL_FEE_PCT, SELL_FEE_PCT)
    expect("ETF_TAXABLE_SELL_TAX_PCT", etf_shared.ETF_TAXABLE_SELL_TAX_PCT, SELL_TAX_PCT)
    expect(
        "capacity_participation",
        etf_execution.DEFAULT_MAX_PARTICIPATION,
        CAPACITY_PARTICIPATION,
    )
    expect(
        "capacity_max_carry_days",
        etf_execution.DEFAULT_MAX_CARRY_DAYS,
        CAPACITY_MAX_CARRY_DAYS,
    )
    expect("KOSPI_INDEX_CODE", etf_shared.KOSPI_INDEX_CODE, KOSPI_INDEX_CODE)

    # Preprocessing: price-return basis and the pinned distribution file.
    expect(
        "ETF_RETURN_BASIS",
        os.environ.get("ETF_RETURN_BASIS", "price").strip().lower(),
        params["return_basis"],
    )
    expect(
        "ETF_DISTRIBUTION_TAX_PCT",
        parse_pct_env("ETF_DISTRIBUTION_TAX_PCT", 0.0),
        params["distribution_tax_pct"],
    )
    configured = os.environ.get("ETF_DISTRIBUTIONS_FILE", params["distributions_file"])
    expect("distributions_file", configured, params["distributions_file"])
    distributions = ROOT / params["distributions_file"]
    if not distributions.exists():
        problems.append(f"distributions file missing: {distributions}")
    else:
        expect(
            "distributions_sha256",
            _sha256_bytes(distributions.read_bytes()),
            params["distributions_sha256"],
        )
        with distributions.open(encoding="utf-8") as handle:
            if sum(1 for _ in csv.DictReader(handle)):
                problems.append("distribution rows present: the shadow does not credit them")

    # Freeze features the shadow does not implement must remain disabled.
    for key, wanted in (
        ("exit_check_days", 0),
        ("trailing_stop_pct", 0.0),
        ("portfolio_trailing_stop_pct", 0.0),
        ("enable_multi_index_risk", False),
        ("use_market_filter", True),
    ):
        expect(f"unsupported:{key}", params[key], wanted)
    return problems


def manifest_digest(manifest: dict[str, Any]) -> str:
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_bytes(canonical.encode("utf-8"))


def effective_parameters_from_freeze(frozen: dict[str, Any]) -> dict[str, Any]:
    """Build the full effective parameter set from the frozen payload.

    Values not present in the freeze payload are taken from the *frozen code*
    defaults and recorded explicitly here.  Environment variables are never
    consulted: the shadow track must not silently inherit a different config.
    """
    params = dict(frozen["parameters"])
    params.update(
        {
            "momentum_weight_60": MOMENTUM_WEIGHT_60,
            "momentum_weight_120": 0.45,
            "capacity_participation_rate": CAPACITY_PARTICIPATION,
            "capacity_max_carry_days": CAPACITY_MAX_CARRY_DAYS,
            "buy_fee_pct": BUY_FEE_PCT,
            "sell_fee_pct": SELL_FEE_PCT,
            "sell_tax_pct": SELL_TAX_PCT,
            "spread_half_pct": float(params["spread_pct"]) / 2.0,
            # Research-only phase offset is explicitly disabled.
            "rebalance_phase_offset": 0,
            "warmup_days": max(120, int(params["market_ma_days"]) + int(params["market_slope_days"])),
        }
    )
    return params


def approval_settings(manifest: dict[str, Any]) -> dict[str, Any]:
    """Operational fields bound to the approval record and manifest hash."""
    anchor = manifest.get("rebalance_anchor", {})
    window = manifest.get("observation_window", {})
    return {
        "runtime_kind": manifest.get("runtime_kind"),
        "initial_capital": manifest.get("initial_capital"),
        "observation_start_date": manifest.get("observation_start_date"),
        "anchor_date": anchor.get("anchor_date"),
        "interval_trading_days": anchor.get("interval_trading_days"),
        "decision_offset_days": anchor.get("decision_offset_days"),
        "phase_offset": anchor.get("phase_offset"),
        "confirm_cutoff_kst": window.get("confirm_cutoff_kst"),
        "window_close_kst_next_trading_day": window.get("window_close_kst_next_trading_day"),
    }


def approval_problems(manifest: dict[str, Any]) -> list[str]:
    """Validate the approval record against the operational settings."""
    problems: list[str] = []
    approval = manifest.get("approval")
    if not isinstance(approval, dict):
        return ["approval record missing"]
    for field in APPROVAL_FIELDS:
        if not isinstance(approval.get(field), str) or not approval[field].strip():
            problems.append(f"approval.{field} missing or empty")
    if approval.get("status") != APPROVED:
        problems.append(f"approval.status must be {APPROVED!r}")
    if approval.get("settings") != approval_settings(manifest):
        problems.append("approval.settings do not match the manifest operational settings")
    if manifest.get("test_only_forward"):
        problems.append("an approved runtime cannot carry the test-only flag")
    return problems


def build_manifest(
    *,
    initial_capital: float,
    observation_start_date: str,
    runtime_kind: str = "TEST",
    warmup: dict[str, Any] | None = None,
    anchor_date: str | None = None,
    decision_offset_days: int = 0,
    approval: dict[str, Any] | None = None,
    test_only_forward: bool = False,
    root: Path = ROOT,
) -> dict[str, Any]:
    """Create a manifest from the current freeze, code and effective config."""
    if runtime_kind not in RUNTIME_KINDS:
        raise ValueError(f"runtime_kind must be one of {RUNTIME_KINDS}")
    frozen = load_frozen_strategy()
    problems = effective_config_problems(frozen)
    if problems:
        raise RuntimeError(
            "effective configuration differs from the freeze: " + "; ".join(problems)
        )
    files = code_fingerprint(root)
    interval = int(frozen["parameters"]["rebalance_step_days"])
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "runtime_kind": runtime_kind,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "code_sha256": aggregate_sha256(files),
        "code_files": files,
        "freeze_sha256": frozen["sha256"],
        "freeze_date": frozen["freeze_date"],
        "oos_start_date": frozen["oos_start_date"],
        "universe": [str(t) for t in frozen["universe"]],
        "ticker_groups": {str(k): str(v) for k, v in frozen["ticker_groups"].items()},
        "taxable_tickers": [str(t) for t in frozen["taxable_tickers"]],
        "group_risk_override": [str(g) for g in frozen["group_risk_override"]],
        "effective_parameters": effective_parameters_from_freeze(frozen),
        "rebalance_anchor": {
            "status": "UNAPPROVED_TEST_VALUE",
            "anchor_date": str(anchor_date or observation_start_date),
            "interval_trading_days": interval,
            "decision_offset_days": int(decision_offset_days),
            "phase_offset": 0,
            "note": (
                "strategy_freeze.json does not pin the rebalance anchor. Research "
                "phase 0 counts from the backtest data start; the live runner counts "
                "from its last executed rebalance; neither is approved for this "
                "track. This anchor is a test-only placeholder."
            ),
        },
        "observation_window": {
            "confirm_cutoff_kst": CONFIRM_CUTOFF_KST,
            "window_close_kst_next_trading_day": WINDOW_CLOSE_KST,
            "status": "UNAPPROVED_TEST_VALUE",
        },
        "calendar": {
            "coverage_start": str(COVERAGE_START),
            "coverage_end": str(COVERAGE_END),
            "status": CALENDAR_STATUS,
        },
        "warmup": warmup,
        "initial_capital": float(initial_capital),
        "initial_holdings": {},
        "observation_start_date": str(observation_start_date),
        "approval_status": "UNAPPROVED_TEST_VALUES",
        "virtual_fill_model": {
            "engine": "etf_execution.decide_execution",
            "price_basis": "next trading day open",
            "spread_application": "open * (1 +/- spread_pct/2)",
            "capacity": "floor(volume * participation_rate)",
            "carry": "PARTIAL_CARRY only, one retry on the next trading day",
            "cost_order": "spread -> slippage -> fee -> dividend tax",
            "limitation": (
                "Daily total volume with a 5% participation cap is a diagnostic "
                "approximation, not a claim of executable fill accuracy. Daily "
                "volume is known only after the close."
            ),
        },
        "data_sources": {
            "etf_ohlcv": "pykrx ETF OHLCV with NAV (NAV mandatory)",
            "index": "KOSPI index via pykrx; missing data fails closed",
            "corporate_actions": "data/etf_corporate_actions.csv (EMPTY template)",
            "distributions": "data/etf_distributions.csv (EMPTY template, hash pinned)",
            "pit_classification": "unavailable (Strict Approval blocker PIT-CLASS-TAX)",
            "independent_price_source": "unavailable (Strict Approval blocker INDEPENDENT-SOURCE)",
        },
        "known_limitations": [
            "PIT-CLASS-TAX: historical classification/tax effective-dated source missing",
            "CA-COVERAGE: no official corporate-action coverage/manifest",
            "DISTRIBUTION: zero distribution history; total-return not approved",
            "INDEPENDENT-SOURCE: no structured price source outside KRX",
            "OOS: 46-day freeze monitoring is not an independent OOS track",
        ],
        "strategy_env_snapshot": strategy_env_snapshot(),
        "test_only_forward": bool(test_only_forward),
        "approval": None,
    }
    if approval is not None:
        if runtime_kind != "FORWARD":
            raise ValueError("an approval record is only valid for a FORWARD runtime")
        if approval.get("status") != APPROVED:
            raise ValueError(f"approval.status must be {APPROVED!r}")
        missing = [
            field
            for field in APPROVAL_FIELDS
            if not isinstance(approval.get(field), str) or not approval[field].strip()
        ]
        if missing:
            raise ValueError(f"approval record missing fields: {', '.join(missing)}")
        manifest["approval_status"] = APPROVED
        manifest["rebalance_anchor"]["status"] = APPROVED
        manifest["observation_window"]["status"] = APPROVED
        manifest["approval"] = {**approval, "settings": approval_settings(manifest)}
    manifest["manifest_sha256"] = manifest_digest(manifest)
    return manifest


def verify_manifest(manifest: dict[str, Any], root: Path = ROOT) -> list[str]:
    """Return a list of drift problems; empty means the manifest is valid."""
    problems: list[str] = []
    if manifest.get("manifest_sha256") != manifest_digest(manifest):
        problems.append("manifest_sha256 mismatch: manifest was modified after initialization")
    if manifest.get("strategy_env_snapshot") != strategy_env_snapshot():
        current = strategy_env_snapshot()
        recorded = manifest.get("strategy_env_snapshot", {})
        changed = sorted(k for k in current if current[k] != recorded.get(k))
        problems.append(f"strategy environment drift: {', '.join(changed)}")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        problems.append(
            f"schema_version: expected {SCHEMA_VERSION}, got {manifest.get('schema_version')!r}"
        )
    if manifest.get("runtime_kind") not in RUNTIME_KINDS:
        problems.append(f"runtime_kind invalid: {manifest.get('runtime_kind')!r}")

    frozen = load_frozen_strategy()
    if manifest.get("freeze_sha256") != frozen["sha256"]:
        problems.append(
            f"freeze_sha256 drift: manifest={manifest.get('freeze_sha256')}, "
            f"current={frozen['sha256']}"
        )
    problems.extend(effective_config_problems(frozen))

    expected_params = effective_parameters_from_freeze(frozen)
    if manifest.get("effective_parameters") != expected_params:
        problems.append("effective_parameters differ from the frozen payload/code defaults")

    for key in ("universe", "ticker_groups", "taxable_tickers", "group_risk_override"):
        expected = frozen[key]
        if isinstance(expected, list):
            expected = [str(v) for v in expected]
        elif isinstance(expected, dict):
            expected = {str(k): str(v) for k, v in expected.items()}
        if manifest.get(key) != expected:
            problems.append(f"{key} differs from the frozen payload")

    current_files = code_fingerprint(root)
    if manifest.get("code_files") != current_files:
        changed = sorted(
            name
            for name in set(current_files) | set(manifest.get("code_files", {}))
            if current_files.get(name) != manifest.get("code_files", {}).get(name)
        )
        problems.append(f"code drift detected in: {', '.join(changed)}")
    if manifest.get("code_sha256") != aggregate_sha256(current_files):
        problems.append("code_sha256 does not match current code fingerprint")

    anchor = manifest.get("rebalance_anchor", {})
    window = manifest.get("observation_window", {})
    if anchor.get("phase_offset") != 0:
        problems.append("non-zero rebalance phase_offset is research-only and not approved")

    if manifest.get("approval_status") == APPROVED:
        problems.extend(approval_problems(manifest))
        if manifest.get("runtime_kind") != "FORWARD":
            problems.append("an approved manifest requires runtime_kind=FORWARD")
        if anchor.get("status") != APPROVED:
            problems.append("approved manifest requires an APPROVED rebalance anchor")
        if window.get("status") != APPROVED:
            problems.append("approved manifest requires an APPROVED observation window")
    else:
        if manifest.get("approval_status") != UNAPPROVED:
            problems.append("approval_status must be UNAPPROVED_TEST_VALUES or APPROVED")
        if anchor.get("status") != UNAPPROVED_FIELD:
            problems.append("rebalance anchor must remain explicitly unapproved")
        if window.get("status") != UNAPPROVED_FIELD:
            problems.append("observation window must remain explicitly unapproved")
        if manifest.get("runtime_kind") == "FORWARD" and not manifest.get("test_only_forward"):
            problems.append("FORWARD runtime requires an approved manifest")
    return problems


@dataclass(frozen=True)
class EffectiveConfig:
    """Runtime view of the manifest parameters (no environment overrides)."""

    manifest: dict[str, Any]

    @property
    def params(self) -> dict[str, Any]:
        return self.manifest["effective_parameters"]

    @property
    def runtime_kind(self) -> str:
        return str(self.manifest["runtime_kind"])

    @property
    def observation_start(self) -> str:
        return str(self.manifest["observation_start_date"])

    @property
    def anchor_date(self) -> str:
        return str(self.manifest["rebalance_anchor"]["anchor_date"])

    @property
    def decision_offset(self) -> int:
        return int(self.manifest["rebalance_anchor"]["decision_offset_days"])

    @property
    def interval(self) -> int:
        return int(self.manifest["rebalance_anchor"]["interval_trading_days"])

    @property
    def universe(self) -> list[str]:
        return [str(t) for t in self.manifest.get("universe", [])]

    @property
    def taxable_tickers(self) -> set[str]:
        return {str(t) for t in self.manifest.get("taxable_tickers", [])}
