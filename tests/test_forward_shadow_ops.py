"""P2-B-2: operational manifest approval is hash-bound and fail-closed.

Run with::

    uv run python -m unittest tests.test_forward_shadow_ops -v
"""

from __future__ import annotations

import unittest

from forward_shadow.config import (
    APPROVED,
    UNAPPROVED,
    approval_settings,
    build_manifest,
    verify_manifest,
)
from forward_shadow.runner import initialize_runtime, process_date
from forward_shadow.store import RuntimePaths, atomic_write_json, read_json
from tests.shadow_fixtures import OBSERVATION_START, FakeLive, ShadowCase, kst


class OperationalApprovalTest(ShadowCase):
    def approval(self, **overrides):
        record = {
            "status": APPROVED,
            "approved_at": "2026-11-27T12:00:00Z",
            "approver": "ops-approval",
            "decision_ref": "P2-B-2",
        }
        record.update(overrides)
        return record

    def approved_runtime(self, name="approved"):
        root = self.tmp / name
        live = FakeLive(self.tmp / "work", self.etf, self.index)
        initialize_runtime(
            root,
            initial_capital=5_000_000.0,
            observation_start_date=str(OBSERVATION_START),
            runtime_kind="FORWARD",
            provider=live,
            approval=self.approval(),
            clock=lambda: kst(OBSERVATION_START, "10:00"),
        )
        return root, live

    def test_approved_manifest_is_hash_bound(self) -> None:
        root, _ = self.approved_runtime()
        paths = RuntimePaths(root)
        manifest = read_json(paths.manifest)
        self.assertEqual(manifest["approval_status"], APPROVED)
        self.assertEqual(manifest["rebalance_anchor"]["status"], APPROVED)
        self.assertEqual(manifest["observation_window"]["status"], APPROVED)
        self.assertEqual(manifest["approval"]["settings"], approval_settings(manifest))
        self.assertEqual(manifest["approval"]["settings"]["initial_capital"], 5_000_000.0)
        self.assertEqual(manifest["approval"]["settings"]["runtime_kind"], "FORWARD")
        self.assertEqual(verify_manifest(manifest), [])

        manifest["initial_capital"] = 1.0
        atomic_write_json(paths.manifest, manifest)
        problems = verify_manifest(manifest)
        self.assertTrue(any("manifest_sha256" in p for p in problems))
        self.assertTrue(any("approval.settings" in p for p in problems))

    def test_unapproved_forward_init_fails_closed(self) -> None:
        live = FakeLive(self.tmp / "work", self.etf, self.index)
        with self.assertRaisesRegex(RuntimeError, "requires an approved manifest"):
            initialize_runtime(
                self.tmp / "unapproved",
                initial_capital=5_000_000.0,
                observation_start_date=str(OBSERVATION_START),
                runtime_kind="FORWARD",
                provider=live,
                clock=lambda: kst(OBSERVATION_START, "10:00"),
            )

    def test_test_only_forward_flag_is_recorded_and_verified(self) -> None:
        root = self.tmp / "test-only-forward"
        initialize_runtime(
            root,
            initial_capital=5_000_000.0,
            observation_start_date=str(OBSERVATION_START),
            runtime_kind="FORWARD",
            provider=FakeLive(self.tmp / "work2", self.etf, self.index),
            test_only_forward=True,
            clock=lambda: kst(OBSERVATION_START, "10:00"),
        )
        paths = RuntimePaths(root)
        manifest = read_json(paths.manifest)
        self.assertTrue(manifest["test_only_forward"])
        self.assertEqual(manifest["approval_status"], UNAPPROVED)
        self.assertEqual(verify_manifest(manifest), [])
        manifest["test_only_forward"] = False
        problems = verify_manifest(manifest)
        self.assertTrue(any("approved manifest" in p for p in problems))

    def test_approved_forward_observation_is_forward(self) -> None:
        root, live = self.approved_runtime()
        result = process_date(
            self.day(0), live, root, clock=lambda: kst(self.day(0), "18:30")
        )
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["observation_type"], "FORWARD")
        state = read_json(RuntimePaths(root).state)
        self.assertEqual(state["observations"], {self.day(0): "FORWARD"})

    def test_approval_record_requires_all_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing fields"):
            build_manifest(
                initial_capital=5_000_000.0,
                observation_start_date=str(OBSERVATION_START),
                runtime_kind="FORWARD",
                approval=self.approval(approver=""),
            )
        with self.assertRaisesRegex(ValueError, "only valid for a FORWARD"):
            build_manifest(
                initial_capital=5_000_000.0,
                observation_start_date=str(OBSERVATION_START),
                runtime_kind="TEST",
                approval=self.approval(),
            )
        for bad in ({"approver": None}, {"decision_ref": 7}, {"approved_at": 1.5}):
            with self.assertRaisesRegex(ValueError, "missing fields"):
                build_manifest(
                    initial_capital=5_000_000.0,
                    observation_start_date=str(OBSERVATION_START),
                    runtime_kind="FORWARD",
                    approval=self.approval(**bad),
                )


if __name__ == "__main__":
    unittest.main()
