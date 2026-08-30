"""Offline checks for strict approval CLI isolation and fail-closed output."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run_etf_backtest as backtest


def check_risk_off_output_creation() -> None:
    import pykrx_utils

    original_output = backtest.OUTPUT_DIR
    original_mode = backtest.RUN_MODE
    original_start = backtest.START
    original_end = backtest.END
    original_stock = backtest.get_stock
    original_auth = pykrx_utils.check_krx_auth_status
    original_runner = backtest.run_risk_off_compare_mode
    with tempfile.TemporaryDirectory() as temporary_directory:
        output_dir = Path(temporary_directory) / "outputs_etf_only"
        try:
            backtest.OUTPUT_DIR = output_dir
            backtest.get_stock = lambda: object()
            pykrx_utils.check_krx_auth_status = lambda: "ok"

            def fake_risk_off_compare() -> None:
                if not output_dir.exists():
                    raise AssertionError("risk-off mode dispatched before standard output creation")
                (output_dir / "risk_off_comparison.csv").write_text("date,equity\n", encoding="utf-8")

            backtest.run_risk_off_compare_mode = fake_risk_off_compare
            old_argv = sys.argv
            sys.argv = ["run_etf_backtest.py", "--mode", "risk_off_compare"]
            try:
                backtest.main()
            finally:
                sys.argv = old_argv
            if not (output_dir / "risk_off_comparison.csv").exists():
                raise AssertionError("risk-off mode did not write its expected artifact")
        finally:
            backtest.OUTPUT_DIR = original_output
            backtest.RUN_MODE = original_mode
            backtest.START = original_start
            backtest.END = original_end
            backtest.get_stock = original_stock
            pykrx_utils.check_krx_auth_status = original_auth
            backtest.run_risk_off_compare_mode = original_runner


def main() -> None:
    check_risk_off_output_creation()
    try:
        backtest._validate_approval_output_dir(Path("outputs_etf_only"))
    except ValueError:
        pass
    else:
        raise AssertionError("strict approval accepted the standard output directory")
    standard_dir = ROOT / "outputs_etf_only"
    standard_before = (
        standard_dir.exists(),
        tuple(sorted(path.name for path in standard_dir.iterdir())) if standard_dir.exists() else (),
        standard_dir.stat().st_mtime_ns if standard_dir.exists() else None,
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        output_dir = Path(temporary_directory) / "approval"
        output_dir.mkdir(parents=True)
        for stale_name in (
            "approval_equity_curve.csv",
            "approval_trades.csv",
            "approval_performance.json",
            "performance.json",
        ):
            (output_dir / stale_name).write_text("stale approved artifact", encoding="utf-8")
        environment = os.environ.copy()
        environment.pop("KRX_ID", None)
        environment.pop("KRX_PW", None)
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "run_etf_backtest.py"),
                "--approval-strict",
                "--approval-output-dir",
                str(output_dir),
                "--corporate-actions-ledger",
                str(ROOT / "data/etf_corporate_actions.csv"),
                "--corporate-actions-manifest",
                str(ROOT / "data/etf_corporate_actions_manifest.json"),
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(
                f"strict blocked CLI failed: stdout={completed.stdout!r}, stderr={completed.stderr!r}"
            )
        if "로그인 시도" in completed.stdout or "로그인 시도" in completed.stderr:
            raise AssertionError("blocked strict run authenticated before ledger preflight")

        expected = {
            "approval_report.json",
            "approval_blockers.csv",
            "reproducibility.json",
        }
        actual = {path.name for path in output_dir.iterdir()}
        if actual != expected:
            raise AssertionError(f"blocked strict output set changed: expected={expected}, actual={actual}")
        report = json.loads((output_dir / "approval_report.json").read_text(encoding="utf-8"))
        if report["status"] != "BLOCKED" or report["approval_valid"]:
            raise AssertionError(f"incomplete template was not blocked: {report}")
        codes = {blocker["code"] for blocker in report["blockers"]}
        if not {"EMPTY_LEDGER", "INCOMPLETE_COVERAGE"}.issubset(codes):
            raise AssertionError(f"expected deterministic template blockers, got={codes}")

        approved_dir = Path(temporary_directory) / "approved"
        backtest._write_approval_result(
            approved_dir,
            pd.DataFrame({"date": pd.to_datetime(["2024-01-02", "2024-01-03"]), "equity": [100.0, 101.0]}),
            pd.DataFrame(),
            backtest.ApprovalReport("APPROVED", (), 0, "fixture"),
            ledger_path="fixture.csv",
            manifest_path="fixture.json",
        )
        if standard_dir.exists() != standard_before[0]:
            raise AssertionError("strict approved output changed standard directory existence")

    standard_after = (
        standard_dir.exists(),
        tuple(sorted(path.name for path in standard_dir.iterdir())) if standard_dir.exists() else (),
        standard_dir.stat().st_mtime_ns if standard_dir.exists() else None,
    )
    if standard_after != standard_before:
        raise AssertionError(
            f"strict blocked run changed standard outputs: before={standard_before}, "
            f"after={standard_after}"
        )

    print("approval output checks passed")


if __name__ == "__main__":
    main()
