"""Offline regression checks for state-based walk-forward stitching."""

from __future__ import annotations

import sys
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


def main() -> None:
    check_continuous_vs_split_stitching()
    check_phase_alignment()
    print("walk-forward stitching checks passed")


if __name__ == "__main__":
    main()
