"""Freeze-derived effective configuration and the Shadow manifest.

The frozen payload in ``strategy_freeze.json`` does not spell out every value
that affects execution (momentum weight, capacity participation, carry limit,
rebalance anchor, initial capital).  This module records those values in a
separate manifest and fails closed when either the code or the freeze drifts.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from strategy_freeze import load_frozen_strategy

ROOT = Path(__file__).resolve().parents[1]

MANIFEST_NAME = "manifest.json"
SCHEMA_VERSION = 1

# Files whose bytes define the behaviour of the shadow track.  A change to any
# of them invalidates an existing manifest (fail-closed on code drift).
CODE_FILES = (
    "forward_shadow/__init__.py",
    "forward_shadow/config.py",
    "forward_shadow/store.py",
    "forward_shadow/market.py",
    "forward_shadow/engine.py",
    "forward_shadow/report.py",
    "forward_shadow/runner.py",
    "run_forward_shadow.py",
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


# Environment variables that can silently change the behaviour of the reused
# etf_shared helpers.  They are snapshotted into the manifest and re-checked on
# every run so an inherited .env cannot alter a supposedly frozen signal.
STRATEGY_ENV_KEYS = (
    "ETF_MOMENTUM_WEIGHT_60",
    "ETF_BASE_SLIPPAGE",
    "ETF_SPREAD_PCT",
    "MAX_ASSET_PCT",
    "MIN_AVG_TRADING_VALUE",
    "MIN_LISTING_DAYS",
    "MAX_PREMIUM_DISCOUNT",
    "ETF_RETURN_BASIS",
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
            # Not in strategy_freeze.json; pinned to the frozen code default.
            "momentum_weight_60": 0.55,
            "momentum_weight_120": 0.45,
            # etf_execution.py defaults (diagnostic capacity model).
            "capacity_participation_rate": 0.05,
            "capacity_max_carry_days": 1,
            # etf_shared.py constants.
            "buy_fee_pct": 0.00015,
            "sell_fee_pct": 0.00015,
            "sell_tax_pct": 0.154,
            "spread_half_pct": float(params["spread_pct"]) / 2.0,
            # Research-only phase offset is explicitly disabled.
            "rebalance_phase_offset": 0,
            "warmup_days": max(120, int(params["market_ma_days"]) + int(params["market_slope_days"])),
        }
    )
    return params


def build_manifest(
    *,
    initial_capital: float,
    initial_holdings: dict[str, int] | None = None,
    observation_start_date: str,
    decision_offset_days: int = 0,
    root: Path = ROOT,
) -> dict[str, Any]:
    """Create a manifest from the current freeze and code state."""
    frozen = load_frozen_strategy()
    files = code_fingerprint(root)
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
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
            "interval_trading_days": 20,
            "decision_offset_days": int(decision_offset_days),
            "phase_offset": 0,
            "note": (
                "strategy_freeze.json does not pin the rebalance anchor/phase. "
                "Phase 0 is the frozen research baseline; the operational "
                "rebalancing base date has not been approved. This value is a "
                "test-only placeholder and must not be treated as an approved "
                "operational schedule."
            ),
        },
        "initial_capital": float(initial_capital),
        "initial_holdings": dict(initial_holdings or {}),
        "observation_start_date": str(observation_start_date),
        "approval_status": "UNAPPROVED_TEST_VALUES",
        "virtual_fill_model": {
            "engine": "etf_execution.decide_execution",
            "price_basis": "next trading day open",
            "spread_application": "open * (1 +/- spread_pct/2)",
            "capacity": "floor(volume * participation_rate)",
            "carry": "max_carry_days=1, retry on next eligible trading date",
            "cost_order": "spread -> slippage -> fee -> dividend tax",
            "limitation": (
                "Daily total volume with a 5% participation cap is a diagnostic "
                "approximation, not a claim of executable fill accuracy."
            ),
        },
        "data_sources": {
            "etf_ohlcv": "pykrx ETF OHLCV (NAV when available)",
            "index": "KOSPI index via pykrx; missing data fails closed",
            "corporate_actions": "data/etf_corporate_actions.csv (EMPTY template)",
            "distributions": "data/etf_distributions.csv (EMPTY template)",
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
    }
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

    frozen = load_frozen_strategy()
    if manifest.get("freeze_sha256") != frozen["sha256"]:
        problems.append(
            f"freeze_sha256 drift: manifest={manifest.get('freeze_sha256')}, "
            f"current={frozen['sha256']}"
        )

    expected_params = effective_parameters_from_freeze(frozen)
    if manifest.get("effective_parameters") != expected_params:
        problems.append("effective_parameters differ from the frozen payload/code defaults")

    for key in ("universe", "ticker_groups", "taxable_tickers", "group_risk_override"):
        expected = frozen[key]
        if isinstance(expected, list):
            expected = [str(v) for v in expected]
        elif isinstance(expected, dict):
            expected = {str(k): str(v) for k, v in expected.items()}
        else:
            expected = {str(v) for v in expected}
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
    if anchor.get("phase_offset") != 0:
        problems.append("non-zero rebalance phase_offset is research-only and not approved")
    if anchor.get("status") != "UNAPPROVED_TEST_VALUE":
        problems.append("rebalance anchor must remain explicitly unapproved")
    return problems


@dataclass(frozen=True)
class EffectiveConfig:
    """Runtime view of the manifest parameters (no environment overrides)."""

    manifest: dict[str, Any]

    @property
    def params(self) -> dict[str, Any]:
        return self.manifest["effective_parameters"]

    @property
    def initial_capital(self) -> float:
        return float(self.manifest["initial_capital"])

    @property
    def initial_holdings(self) -> dict[str, int]:
        return {str(k): int(v) for k, v in self.manifest["initial_holdings"].items()}

    @property
    def observation_start(self) -> str:
        return str(self.manifest["observation_start_date"])

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
    def ticker_groups(self) -> dict[str, str]:
        return dict(self.manifest.get("ticker_groups", {}))

    @property
    def taxable_tickers(self) -> set[str]:
        return {str(t) for t in self.manifest.get("taxable_tickers", [])}

    @property
    def group_risk_override(self) -> set[str]:
        return {str(g) for g in self.manifest.get("group_risk_override", [])}
