"""Rollback snapshots and idempotency verification (PRD §26.9)."""

from __future__ import annotations

import json
from pathlib import Path

from src.chandra.execution.recovery import (
    STATE_FILENAME,
    IdempotencyVerdict,
    plan_recovery,
    restore_state,
    snapshot_state,
    verify_idempotency,
)

_STATE = {
    "version": 4,
    "resources": [
        {"type": "aws_s3_bucket", "instances": [{"attributes": {"id": "b1"}}]},
        {"type": "aws_s3_bucket_versioning", "instances": [{"attributes": {"id": "v1"}}]},
    ],
}


def _write_state(workdir: Path, payload: dict | None = None) -> Path:
    path = workdir / STATE_FILENAME
    path.write_text(json.dumps(payload or _STATE), encoding="utf-8")
    return path


class TestSnapshots:
    def test_snapshot_captures_existing_state(self, tmp_path: Path) -> None:
        _write_state(tmp_path)
        snapshot = snapshot_state(tmp_path)
        assert snapshot.existed is True
        assert snapshot.available is True
        assert snapshot.resource_count == 2

    def test_absent_state_is_recorded_not_an_error(self, tmp_path: Path) -> None:
        """A first apply has no prior state; 'nothing' is what to restore to."""
        snapshot = snapshot_state(tmp_path)
        assert snapshot.existed is False
        assert snapshot.available is False
        assert snapshot.error == ""

    def test_snapshot_failure_never_raises(self, tmp_path: Path) -> None:
        """A snapshot problem must not block an apply that cleared both gates."""
        missing = tmp_path / "does-not-exist"
        snapshot = snapshot_state(missing)
        assert snapshot.existed is False

    def test_restore_puts_the_original_state_back(self, tmp_path: Path) -> None:
        _write_state(tmp_path)
        snapshot = snapshot_state(tmp_path)
        (tmp_path / STATE_FILENAME).write_text('{"version": 4, "resources": []}', encoding="utf-8")

        assert restore_state(snapshot, tmp_path) is True
        restored = json.loads((tmp_path / STATE_FILENAME).read_text(encoding="utf-8"))
        assert len(restored["resources"]) == 2

    def test_restore_without_a_snapshot_reports_failure(self, tmp_path: Path) -> None:
        snapshot = snapshot_state(tmp_path)  # no prior state
        assert restore_state(snapshot, tmp_path) is False

    def test_unreadable_state_counts_zero_rather_than_raising(self, tmp_path: Path) -> None:
        (tmp_path / STATE_FILENAME).write_text("not json", encoding="utf-8")
        snapshot = snapshot_state(tmp_path)
        assert snapshot.available is True
        assert snapshot.resource_count == 0

    def test_snapshot_serialises_for_the_audit_trail(self, tmp_path: Path) -> None:
        _write_state(tmp_path)
        payload = snapshot_state(tmp_path).as_dict()
        assert payload["resource_count"] == 2
        assert payload["available"] is True
        assert "taken_at" in payload


class TestIdempotency:
    def test_no_pending_changes_means_the_mutation_already_landed(self, tmp_path: Path) -> None:
        """Exit 0 from plan -detailed-exitcode: reality already matches. Retrying
        would risk doubling the change."""
        verdict = verify_idempotency(tmp_path, runner=lambda w: (0, "No changes."))
        assert verdict.already_applied is True
        assert verdict.safe_to_retry is False

    def test_pending_changes_mean_a_retry_is_safe(self, tmp_path: Path) -> None:
        verdict = verify_idempotency(tmp_path, runner=lambda w: (2, "1 to add"))
        assert verdict.safe_to_retry is True
        assert verdict.already_applied is False

    def test_an_unanswerable_state_refuses_the_retry(self, tmp_path: Path) -> None:
        """A retry that might double a mutation is worse than an escalation."""
        verdict = verify_idempotency(tmp_path, runner=lambda w: (1, "error: credentials"))
        assert verdict.safe_to_retry is False
        assert verdict.already_applied is False

    def test_a_crashing_check_refuses_the_retry(self, tmp_path: Path) -> None:
        def _boom(workdir: Path) -> tuple[int, str]:
            raise OSError("terraform not found")

        verdict = verify_idempotency(tmp_path, runner=_boom)
        assert verdict.safe_to_retry is False
        assert "Could not determine" in verdict.reason

    def test_evidence_is_captured_for_the_audit_trail(self, tmp_path: Path) -> None:
        verdict = verify_idempotency(
            tmp_path, expected_resources=["aws_s3_bucket.b"], runner=lambda w: (2, "1 to add")
        )
        assert verdict.evidence["plan_exit_code"] == 2
        assert verdict.evidence["expected_resources"] == ["aws_s3_bucket.b"]
        assert verdict.as_dict()["safe_to_retry"] is True


class TestRecoveryPlanning:
    def test_success_needs_no_recovery(self, tmp_path: Path) -> None:
        snapshot = snapshot_state(tmp_path)
        verdict = IdempotencyVerdict(safe_to_retry=True, reason="")
        assert plan_recovery(snapshot, verdict, apply_succeeded=True)["action"] == "none"

    def test_already_applied_recommends_verification_not_retry(self, tmp_path: Path) -> None:
        snapshot = snapshot_state(tmp_path)
        verdict = IdempotencyVerdict(safe_to_retry=False, reason="", already_applied=True)
        recovery = plan_recovery(snapshot, verdict, apply_succeeded=False)
        assert recovery["action"] == "verify"
        assert recovery["escalate"] is True

    def test_nothing_landed_recommends_retry(self, tmp_path: Path) -> None:
        snapshot = snapshot_state(tmp_path)
        verdict = IdempotencyVerdict(safe_to_retry=True, reason="")
        recovery = plan_recovery(snapshot, verdict, apply_succeeded=False)
        assert recovery["action"] == "retry"
        assert recovery["escalate"] is False

    def test_unknown_state_escalates_and_never_destroys(self, tmp_path: Path) -> None:
        """§26.9 says revert only 'when safe'. An unknown AWS state is not safe,
        so the recommendation is escalation — no automatic destroy anywhere."""
        _write_state(tmp_path)
        snapshot = snapshot_state(tmp_path)
        verdict = IdempotencyVerdict(safe_to_retry=False, reason="unknown")
        recovery = plan_recovery(snapshot, verdict, apply_succeeded=False)
        assert recovery["action"] == "escalate"
        assert recovery["escalate"] is True
        assert recovery["snapshot"]["available"] is True
        assert "destroy" not in json.dumps(recovery).lower()

    def test_escalation_says_so_when_no_snapshot_exists(self, tmp_path: Path) -> None:
        snapshot = snapshot_state(tmp_path)
        verdict = IdempotencyVerdict(safe_to_retry=False, reason="unknown")
        recovery = plan_recovery(snapshot, verdict, apply_succeeded=False)
        assert "no state snapshot" in recovery["detail"].lower()
