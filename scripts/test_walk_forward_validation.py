"""Offline regression checks for state-based walk-forward stitching."""

from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_saved_modules = {key: sys.modules.get(key) for key in ("pykrx", "pykrx.stock")}
try:
    pykrx = types.ModuleType("pykrx")
    pykrx_stock = types.ModuleType("pykrx.stock")
    pykrx.stock = pykrx_stock
    sys.modules["pykrx"] = pykrx
    sys.modules["pykrx.stock"] = pykrx_stock
    sys.path.insert(0, str(ROOT / "scripts"))
    import walk_forward_validation as wf
finally:
    if str(ROOT / "scripts") in sys.path:
        sys.path.remove(str(ROOT / "scripts"))
    for key, value in _saved_modules.items():
        if value is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = value


def check_continuous_vs_split_stitching() -> None:
    continuous = pd.Series([100.0, 110.0, 121.0, 133.1])
    continuous_returns = wf.stitch_state_based_segment(continuous, first_segment_anchor_equity=90.0)
    split_returns = wf.stitch_state_based_segment(
        pd.Series([100.0, 110.0]), first_segment_anchor_equity=90.0
    ) + wf.stitch_state_based_segment(pd.Series([121.0, 133.1]), prior_end_equity=110.0)
    assert np.allclose(continuous_returns, split_returns)
    assert np.isclose(split_returns[2], 121.0 / 110.0 - 1.0)
    assert not np.isclose(split_returns[2], 0.0)

    stitched = 1_000.0
    for daily_return in split_returns:
        stitched *= 1.0 + daily_return
    assert np.isclose(stitched, 1_000.0 * 133.1 / 90.0)


def check_phase_alignment() -> None:
    assert wf.phase_offset(start_index=137, warmup_days=120, step_days=20) == 17
    assert wf.phase_offset(start_index=137, warmup_days=120, step_days=7) == 3
    assert wf.phase_offset(start_index=120, warmup_days=120, step_days=20) == 0


def check_resolve_fixed_policy_params() -> None:
    payload = {
        "freeze_date": "2026-07-21",
        "oos_start_date": "2026-07-22",
        "parameters": {
            "rebalance_step_days": 20,
            "max_positions": 2,
            "max_asset_pct": 0.85,
            "liquidate_on_risk_off": True,
            "slippage": 0.0005,
        },
    }
    applied = wf.resolve_fixed_policy_params(payload)
    assert applied["rebalance_step_days"] == 20
    assert applied["max_positions"] == 2
    assert applied["max_asset_pct"] == 0.85
    assert applied["risk_off_liquidate"] is True
    assert np.isclose(applied["slippage"], 0.0005)
    assert applied["target_weight_rebalance"] is False
    assert applied["exit_check_days"] == 0

    try:
        wf.resolve_fixed_policy_params({"parameters": {"rebalance_step_days": 20}})
    except RuntimeError as exc:
        assert "max_positions" in str(exc) and "max_asset_pct" in str(exc)
    else:
        raise AssertionError("missing required frozen params must fail closed")

    try:
        wf.resolve_fixed_policy_params({"parameters": "not-a-dict"})
    except TypeError as exc:
        assert "parameters" in str(exc)
    else:
        raise AssertionError("non-dict parameters block must fail closed")


def check_load_frozen_payload_fail_closed() -> None:
    original_loader = wf.strategy_freeze.load_frozen_strategy

    def _raise_missing(*args):
        raise FileNotFoundError(args)

    def _raise_type_error(*args):
        raise TypeError("전략 동결 스냅샷에 parameters 블록이 없습니다.")

    def _raise_integrity(*args):
        raise ValueError("전략 동결 파일 해시 불일치")

    try:
        wf.strategy_freeze.load_frozen_strategy = _raise_missing
        try:
            wf.load_frozen_payload()
        except RuntimeError as exc:
            assert "로드 실패" in str(exc)
        else:
            raise AssertionError("missing freeze snapshot must fail closed")

        wf.strategy_freeze.load_frozen_strategy = _raise_type_error
        try:
            wf.load_frozen_payload()
        except RuntimeError as exc:
            assert "로드 실패" in str(exc)
        else:
            raise AssertionError("corrupted freeze snapshot must fail closed")

        wf.strategy_freeze.load_frozen_strategy = _raise_integrity
        try:
            wf.load_frozen_payload()
        except RuntimeError as exc:
            assert "검증 실패" in str(exc)
        else:
            raise AssertionError("freeze integrity failure must fail closed")
    finally:
        wf.strategy_freeze.load_frozen_strategy = original_loader


def check_filter_oos_slice() -> None:
    curve = pd.DataFrame(
        {
            "date": ["2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23", "2026-07-22"],
            "equity": [100.0, 101.0, 102.0, 103.0, 102.0],
        }
    )
    sliced = wf.filter_oos_slice(curve, pd.Timestamp("2026-07-22"))
    assert list(sliced["date"].dt.strftime("%Y-%m-%d")) == ["2026-07-22", "2026-07-23"]
    assert np.allclose(sliced["equity"].to_numpy(), [102.0, 103.0])

    sliced_str = wf.filter_oos_slice(curve, "2026-07-23")
    assert len(sliced_str) == 1 and np.isclose(sliced_str["equity"].iloc[0], 103.0)


def check_summary_and_output_separation(tmp_root: Path) -> None:
    oos_curve = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-07-22", "2026-07-23"]),
            "equity": [100.0, 101.0],
        }
    )
    payload = {"freeze_date": "2026-07-21", "oos_start_date": "2026-07-22"}
    fixed_summary = wf.build_fixed_policy_summary(oos_curve, payload, {"max_positions": 2})
    assert fixed_summary["policy_type"] == "frozen_fixed"
    assert fixed_summary["freeze_date"] == "2026-07-21"
    assert fixed_summary["oos_start"] == "2026-07-22"
    assert fixed_summary["oos_stats"]["total_return"] is not None

    try:
        wf.build_fixed_policy_summary(oos_curve.iloc[:1], payload, {})
    except RuntimeError:
        pass
    else:
        raise AssertionError("insufficient OOS rows must fail closed")

    adaptive_summary = {"policy_type": "adaptive_fold_selected", "state_based": True}
    folds_df = pd.DataFrame([{"fold": 1}])
    out_dir = tmp_root / "wf_out"
    wf.write_walk_forward_outputs(
        out_dir, folds_df, oos_curve, adaptive_summary, oos_curve, fixed_summary
    )

    adaptive_loaded = json.loads(
        (out_dir / "walk_forward_summary.json").read_text(encoding="utf-8")
    )
    fixed_loaded = json.loads(
        (out_dir / wf.FIXED_POLICY_SUMMARY_FILENAME).read_text(encoding="utf-8")
    )
    assert adaptive_loaded["policy_type"] == "adaptive_fold_selected"
    assert fixed_loaded["policy_type"] == "frozen_fixed"
    assert adaptive_loaded["policy_type"] != fixed_loaded["policy_type"]
    for name in (
        "walk_forward_folds.csv",
        "walk_forward_equity_curve.csv",
        wf.FIXED_POLICY_CURVE_FILENAME,
    ):
        assert (out_dir / name).exists()

    adaptive_only_dir = tmp_root / "wf_out_adaptive_only"
    wf.write_walk_forward_outputs(adaptive_only_dir, folds_df, oos_curve, adaptive_summary)
    assert not (adaptive_only_dir / wf.FIXED_POLICY_SUMMARY_FILENAME).exists()


def main() -> None:
    check_continuous_vs_split_stitching()
    check_phase_alignment()
    check_resolve_fixed_policy_params()
    check_load_frozen_payload_fail_closed()
    check_filter_oos_slice()
    with tempfile.TemporaryDirectory() as tmp:
        check_summary_and_output_separation(Path(tmp))
    print("walk-forward stitching checks passed")


if __name__ == "__main__":
    main()
