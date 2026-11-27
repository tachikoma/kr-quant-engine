"""Forward Shadow MVP for the frozen ETF momentum strategy (v2 Baseline).

This package is an independent, read-only-by-default shadow track. It reuses the
frozen strategy helpers in :mod:`etf_shared` and the OHLCV capacity model in
:mod:`etf_execution`, but never places real orders and never mutates the live
operational checkout or Track A worktrees.

All runtime artifacts live under an independent runtime root (default:
``shadow_runtime/``). See ``docs/forward_shadow.md`` for the design and the
unapproved operational parameters that remain pending approval.
"""

from __future__ import annotations

__version__ = "0.2.0"
