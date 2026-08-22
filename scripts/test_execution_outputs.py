"""Offline regression checks for Phase 3B2 capacity output orchestration."""

from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

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
    import run_etf_backtest as backtest
finally:
    for key, value in _saved_modules.items():
        if value is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = value


def synthetic_runner(**kwargs):
    aum = int(kwargs["initial_cash"])
    assert kwargs["execution_mode"] == "ohlcv_capacity"
    assert kwargs["return_final_state"] is True
    result = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "equity": [float(aum), float(aum - 100 + 100)],
            "cash": [float(aum), float(aum - 100)],
            "market_value": [0.0, 100.0],
            "distribution_cash": [0.0, 0.0],
        }
    )
    trades = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2024-01-03"),
                "ticker": "000001",
                "side": "BUY",
                "reason": "ETF_REBALANCE",
                "execution_order_id": f"order-{aum}",
                "qty": 49,
                "price": 20.0,
                "net_value": 100.0,
                "cash_flow": -100.0,
                "estimated_tax": 0.0,
                "cash_after": float(aum - 100),
            }
        ]
    )
    final_state = {
        "cash": float(aum - 100),
        "holdings": {"000001": 49},
        "execution_mode": "ohlcv_capacity",
        "pending_execution_carries": (),
        "execution_diagnostics": [
            {
                "date": "2024-01-03",
                "ticker": "000001",
                "side": "BUY",
                "decision": "CARRY_CANCELLED",
                "execution_order_id": f"order-{aum}",
                "terminal_applied": True,
                "requested_qty": 98,
                "filled_qty": 49,
                "remaining_qty": 49,
                "capacity_qty": 100,
                "bar_volume": 200,
                "bar_value": 20_000,
                "close_volume_notional_estimate": 20_000,
                "participation_rate": 0.05,
                "carry_age": 0,
                "max_carry_days": 1,
                "reason": "repriced carry BUY remainder cancelled by current cash",
                "diagnostic_labels": [
                    "OHLCV_CAPACITY_SCENARIO",
                    "CASH_LIMITED_CARRY_CANCEL",
                ],
                "origin_date": "2024-01-02",
                "due_date": None,
                "order_reason": "ETF_EXECUTION_CARRY",
                "execution_mode": "ohlcv_capacity",
            }
        ],
    }
    return result, trades, final_state


def empty_execution_runner(**kwargs):
    aum = int(kwargs["initial_cash"])
    result = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "equity": [float(aum), float(aum)],
            "distribution_cash": [0.0, 0.0],
        }
    )
    return (
        result,
        pd.DataFrame(),
        {
            "cash": float(aum),
            "holdings": {},
            "execution_mode": "ohlcv_capacity",
            "pending_execution_carries": (),
            "execution_diagnostics": [],
        },
    )


def check_cli_defaults_and_output_contract() -> None:
    original_argv = sys.argv
    try:
        sys.argv = [
            "run_etf_backtest.py",
            "--execution-mode",
            "ohlcv_capacity",
            "--execution-participation-rate",
            "0.1",
            "--execution-aum",
            "1,2,3",
            "--execution-output-dir",
            "tmp_execution",
        ]
        args = backtest._parse_cli_args()
        assert args.execution_mode == "ohlcv_capacity"
        assert args.execution_participation_rate == 0.1
        assert args.execution_aum == "1,2,3"
        assert args.execution_output_dir == "tmp_execution"
    finally:
        sys.argv = original_argv

    with tempfile.TemporaryDirectory() as raw_directory:
        output_dir = Path(raw_directory) / "execution"
        calls: list[int] = []

        def runner(**kwargs):
            calls.append(int(kwargs["initial_cash"]))
            return synthetic_runner(**kwargs)

        artifacts = backtest.run_execution_capacity_scenarios(
            aums=[10_000_000, 100_000_000, 1_000_000_000],
            participation_rate=0.05,
            output_dir=output_dir,
            strategy_runner=runner,
        )
        assert calls == [10_000_000, 100_000_000, 1_000_000_000]
        assert {path.name for path in artifacts.values()} == {
            "execution",
            "execution_metadata.json",
            "execution_summary.csv",
            "execution_diagnostics.csv",
            "execution_trades.csv",
            "execution_reconciliation.csv",
        }
        summary = pd.read_csv(artifacts["summary"])
        diagnostics = pd.read_csv(artifacts["diagnostics"])
        trades = pd.read_csv(artifacts["trades"])
        reconciliation = pd.read_csv(artifacts["reconciliation"])
        assert len(summary) == len(diagnostics) == len(trades) == len(reconciliation) == 3
        assert set(summary["aum"]) == {10_000_000, 100_000_000, 1_000_000_000}
        assert set(summary["diagnostic_only"]) == {True}
        assert set(summary["executable_fill_claim"]) == {False}
        assert set(summary["filled_qty_total"]) == {49}
        assert set(summary["actual_trade_filled_qty_total"]) == {49}
        assert int(trades["filled_qty"].sum()) == int(diagnostics["filled_qty"].sum()) == 49 * 3
        assert diagnostics["execution_order_id"].nunique() == 3
        assert set(trades["execution_order_id"]) == set(diagnostics["execution_order_id"])
        diagnostic_header = (
            (output_dir / "execution_diagnostics.csv")
            .read_text(encoding="utf-8-sig")
            .splitlines()[0]
            .split(",")
        )
        assert diagnostic_header.count("execution_mode") == 1
        assert set(reconciliation["reconciled"]) == {True}
        assert {"requested_qty", "filled_qty", "capacity_qty", "reason"}.issubset(
            diagnostics.columns
        )
        metadata = json.loads(artifacts["metadata"].read_text(encoding="utf-8"))
        assert metadata["diagnostic_only"] is True
        assert metadata["executable_fill_claim"] is False
        assert metadata["orderbook_used"] is False
        assert metadata["aums"] == [10_000_000, 100_000_000, 1_000_000_000]
        assert metadata["carry_policy"] == "exactly one following trading date"
        assert not (output_dir / "outputs_etf_only").exists()

        (output_dir / "execution_summary.csv").write_text("stale", encoding="utf-8")
        backtest.run_execution_capacity_scenarios(
            aums=[10_000_000], output_dir=output_dir, strategy_runner=runner
        )
        assert (
            (output_dir / "execution_summary.csv")
            .read_text(encoding="utf-8-sig")
            .startswith("scenario_id")
        )

        (output_dir / "unowned-stale.txt").write_text("reject", encoding="utf-8")
        try:
            backtest.run_execution_capacity_scenarios(
                aums=[10_000_000], output_dir=output_dir, strategy_runner=runner
            )
        except ValueError as exc:
            assert "unowned stale entries" in str(exc)
        else:
            raise AssertionError("unowned stale output entry was accepted")


def check_empty_diagnostics_contract() -> None:
    with tempfile.TemporaryDirectory() as raw_directory:
        output_dir = Path(raw_directory) / "empty-execution"
        artifacts = backtest.run_execution_capacity_scenarios(
            aums=[10_000_000], output_dir=output_dir, strategy_runner=empty_execution_runner
        )
        diagnostics = pd.read_csv(artifacts["diagnostics"])
        trades = pd.read_csv(artifacts["trades"])
        summary = pd.read_csv(artifacts["summary"])
        assert diagnostics.empty
        assert trades.empty
        assert int(summary.loc[0, "diagnostic_count"]) == 0
        assert int(summary.loc[0, "trade_count"]) == 0


def check_atomic_commit_rollback() -> None:
    artifact_names = (
        "execution_summary.csv",
        "execution_diagnostics.csv",
        "execution_trades.csv",
        "execution_reconciliation.csv",
        "execution_metadata.json",
    )
    with tempfile.TemporaryDirectory() as raw_directory:
        output_dir = Path(raw_directory) / "atomic-execution"
        backtest.run_execution_capacity_scenarios(
            aums=[10_000_000], output_dir=output_dir, strategy_runner=synthetic_runner
        )
        prior_report = {name: (output_dir / name).read_bytes() for name in artifact_names}

        original_replace = backtest.os.replace
        replace_calls = 0

        def fail_mid_commit(source, destination):
            nonlocal replace_calls
            replace_calls += 1
            if replace_calls == 2:
                raise OSError("injected directory swap failure")
            return original_replace(source, destination)

        backtest.os.replace = fail_mid_commit
        try:
            try:
                backtest.run_execution_capacity_scenarios(
                    aums=[20_000_000], output_dir=output_dir, strategy_runner=synthetic_runner
                )
            except OSError as exc:
                assert "injected directory swap failure" in str(exc)
            else:
                raise AssertionError("injected commit failure was not raised")
        finally:
            backtest.os.replace = original_replace

        assert replace_calls == 3
        assert {name: (output_dir / name).read_bytes() for name in artifact_names} == prior_report
        assert not list(output_dir.parent.glob(f".{output_dir.name}.staging-*"))
        assert not list(output_dir.parent.glob(f".{output_dir.name}.backup-*"))

        backtest.run_execution_capacity_scenarios(
            aums=[20_000_000], output_dir=output_dir, strategy_runner=synthetic_runner
        )
        assert int(pd.read_csv(output_dir / "execution_summary.csv").loc[0, "aum"]) == 20_000_000


def check_overlap_rejection() -> None:
    for protected in (backtest.OUTPUT_DIR, "outputs_approval"):
        try:
            backtest.run_execution_capacity_scenarios(
                aums=[10_000_000],
                output_dir=protected,
                strategy_runner=synthetic_runner,
            )
        except ValueError as exc:
            assert "overlaps protected path" in str(exc)
        else:
            raise AssertionError(f"protected output path was accepted: {protected}")
    for overlapping in (
        backtest.OUTPUT_DIR.parent,
        backtest.OUTPUT_DIR / "nested",
        Path("outputs_approval") / "nested",
    ):
        try:
            backtest._validate_execution_output_dir(overlapping)
        except ValueError:
            pass
        else:
            raise AssertionError(f"parent-child protected path was accepted: {overlapping}")


def check_early_cli_rejection() -> None:
    for kwargs, message in (
        (
            {"approval_strict": True, "run_mode": "single"},
            "approval-strict",
        ),
        (
            {"approval_strict": False, "run_mode": "experiment"},
            "--mode single",
        ),
    ):
        try:
            backtest._validate_execution_cli_contract(
                execution_mode="ohlcv_capacity",
                raw_aums="10000000",
                raw_rate=0.05,
                output_dir=Path(tempfile.gettempdir()) / "execution-contract-test",
                **kwargs,
            )
        except ValueError as exc:
            assert message in str(exc)
        else:
            raise AssertionError("invalid capacity CLI contract was accepted")

    with tempfile.TemporaryDirectory() as raw_directory:
        output_dir = Path(raw_directory) / "existing-reports"
        output_dir.mkdir()
        sentinel = output_dir / "execution_summary.csv"
        sentinel.write_text("existing-report", encoding="utf-8")
        backtest._validate_execution_cli_contract(
            execution_mode="ohlcv_capacity",
            approval_strict=False,
            run_mode="single",
            raw_aums="10000000",
            raw_rate=0.05,
            output_dir=output_dir,
        )
        assert sentinel.read_text(encoding="utf-8") == "existing-report"


def check_fail_close_contract() -> None:
    with tempfile.TemporaryDirectory() as raw_directory:
        output_dir = Path(raw_directory) / "failed-execution"
        output_dir.mkdir()
        sentinel = output_dir / "execution_summary.csv"
        sentinel.write_text("previous-report", encoding="utf-8")

        def pending_runner(**kwargs):
            result, trades, state = synthetic_runner(**kwargs)
            state["pending_execution_carries"] = ({"ticker": "000001"},)
            return result, trades, state

        try:
            backtest.run_execution_capacity_scenarios(
                aums=[10_000_000], output_dir=output_dir, strategy_runner=pending_runner
            )
        except RuntimeError as exc:
            assert "pending execution carry" in str(exc)
        else:
            raise AssertionError("pending carry did not fail closed")
        assert sentinel.read_text(encoding="utf-8") == "previous-report"

        def unreconciled_runner(**kwargs):
            result, trades, state = synthetic_runner(**kwargs)
            state["cash"] += 1.0
            return result, trades, state

        try:
            backtest.run_execution_capacity_scenarios(
                aums=[10_000_000], output_dir=output_dir, strategy_runner=unreconciled_runner
            )
        except RuntimeError as exc:
            assert "reconciliation" in str(exc)
        else:
            raise AssertionError("reconciliation failure did not fail closed")
        assert sentinel.read_text(encoding="utf-8") == "previous-report"


def main() -> None:
    check_cli_defaults_and_output_contract()
    check_empty_diagnostics_contract()
    check_atomic_commit_rollback()
    check_overlap_rejection()
    check_early_cli_rejection()
    check_fail_close_contract()
    print("execution output checks passed")


if __name__ == "__main__":
    main()
