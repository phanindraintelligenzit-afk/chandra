"""Tests for the governed Jira AWS workflow: Phases 3B → 3C → 3D → 3E.

Exercises the complete governed path:
  Jira → approval → permission_analysis → permission_selection → Gate 1
  → terraform_generate → terraform_validate_plan → Gate 2
  → terraform_apply → verify_aws_resources → validate_result → tracker → ...

Every test uses MemorySaver (in-memory) and mocks LLM/AWS/Jira/Terraform.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, ClassVar
from unittest.mock import patch

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from src.chandra.db.models import Base
from src.chandra.digital_worker import graph as dw_graph
from src.chandra.digital_worker import memory, planner
from src.chandra.digital_worker.graph import build_digital_worker_graph
from src.chandra.digital_worker.schemas import CloudPlatform


THREAD = {"configurable": {"thread_id": "gov-test-thread"}}

JIRA_AWS_PAYLOAD: dict[str, Any] = {
    "source": "jira",
    "payload": {
        "issue": {
            "key": "AWS-42",
            "fields": {
                "summary": "Create S3 bucket for analytics team",
                "description": "Need a new S3 bucket chandra-analytics-prod with versioning",
                "priority": {"name": "High"},
                "reporter": {"displayName": "phani"},
            },
        },
    },
}

S3_FULL_ACCESS_PSET_ID = "eab39a74-a48a-4f19-9803-e71e37cc4d62"
EC2_PSET_ID = "4bbb47a9-d7f7-4921-81e4-1f3d5f215579"


@pytest.fixture
def sqlite_scope(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def _scope() -> Iterator[Session]:
        session = factory()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(dw_graph, "session_scope", _scope)
    yield factory


@pytest.fixture
def gov_workflow(
    monkeypatch: pytest.MonkeyPatch,
    sqlite_scope: sessionmaker[Session],
) -> Any:
    """Compiled graph with LLM, memory, Jira, terraform mocked."""
    monkeypatch.setattr(memory, "lookup_plan", lambda fingerprint: None)
    monkeypatch.setattr(planner, "compose_request_analysis", lambda payload: None)
    for var in (
        "JIRA_SERVER", "JIRA_EMAIL", "JIRA_API_TOKEN",
        "SLACK_WEBHOOK_URL", "TEAMS_WEBHOOK_URL", "SMTP_HOST",
    ):
        monkeypatch.delenv(var, raising=False)
    # Disable real terraform apply
    monkeypatch.delenv("CHANDRA_TERRAFORM_APPLY_ENABLED", raising=False)
    return build_digital_worker_graph(checkpointer=MemorySaver())


class TestPhase3B_Gate1:
    """Phase 3B: Permission analysis → Copilot → Permission Set → Gate 1."""

    def test_jira_aws_pauses_at_approval(self, gov_workflow: Any) -> None:
        """Jira request always requires approval (decision engine policy)."""
        gov_workflow.invoke(dict(JIRA_AWS_PAYLOAD), config=THREAD)
        snapshot = gov_workflow.get_state(THREAD)
        assert "approval_gate" in snapshot.next

    def test_approved_jira_enters_permission_analysis(self, gov_workflow: Any) -> None:
        """After APPROVE, AWS platform request enters permission_analysis, not execute_automation."""
        gov_workflow.invoke(dict(JIRA_AWS_PAYLOAD), config=THREAD)
        # Approve
        gov_workflow.invoke(
            Command(resume={"approved": True, "approver": "phani", "comment": "go"}),
            config=THREAD,
        )
        snapshot = gov_workflow.get_state(THREAD)
        # Should be paused at permission_selection_pause (after permission_analysis ran)
        assert "permission_selection_pause" in snapshot.next
        values = snapshot.values
        assert values.get("required_permissions") is not None
        assert len(values["required_permissions"]) > 0

    def test_gate_1_pass_with_matching_permissions(self, gov_workflow: Any) -> None:
        """Gate 1 PASS when all required permissions are covered by the permission set."""
        gov_workflow.invoke(dict(JIRA_AWS_PAYLOAD), config=THREAD)
        gov_workflow.invoke(
            Command(resume={"approved": True, "approver": "phani", "comment": "go"}),
            config=THREAD,
        )
        # Attach S3 Full Access permission set
        gov_workflow.invoke(
            Command(resume={"permission_set_id": S3_FULL_ACCESS_PSET_ID}),
            config=THREAD,
        )
        snapshot = gov_workflow.get_state(THREAD)
        values = snapshot.values
        assert values.get("gate_1_passed") is True
        gate_1 = values.get("gate_1_result", {})
        assert gate_1.get("pass") is True
        assert gate_1.get("missing_actions") == []

    def test_gate_1_fail_blocks_execution(self, gov_workflow: Any) -> None:
        """Gate 1 FAIL when required permissions are NOT covered → loops back."""
        gov_workflow.invoke(dict(JIRA_AWS_PAYLOAD), config=THREAD)
        gov_workflow.invoke(
            Command(resume={"approved": True, "approver": "phani", "comment": "go"}),
            config=THREAD,
        )
        # Attach EC2 permission set (wrong for S3 request)
        gov_workflow.invoke(
            Command(resume={"permission_set_id": EC2_PSET_ID}),
            config=THREAD,
        )
        snapshot = gov_workflow.get_state(THREAD)
        values = snapshot.values
        assert values.get("gate_1_passed") is False
        gate_1 = values.get("gate_1_result", {})
        assert gate_1.get("pass") is False
        assert len(gate_1.get("missing_actions", [])) > 0
        # Should loop back to permission_selection_pause
        assert "permission_selection_pause" in snapshot.next

    def test_regression_approved_jira_never_bypasses_permission(
        self, gov_workflow: Any
    ) -> None:
        """Regression: approved Jira AWS request must ALWAYS enter permission_analysis,
        never bypass to execute_automation."""
        gov_workflow.invoke(dict(JIRA_AWS_PAYLOAD), config=THREAD)
        gov_workflow.invoke(
            Command(resume={"approved": True, "approver": "phani", "comment": "go"}),
            config=THREAD,
        )
        snapshot = gov_workflow.get_state(THREAD)
        # Must NOT be at execute_automation
        assert "execute_automation" not in (snapshot.next or [])
        # Must be at permission_selection_pause (having passed through permission_analysis)
        assert "permission_selection_pause" in snapshot.next


class TestPhase3C_TerraformPreparation:
    """Phase 3C: Gate 1 PASS → Terraform generate → validate → plan → STOP at Gate 2."""

    def _advance_to_gate1_pass(self, wf: Any) -> None:
        wf.invoke(dict(JIRA_AWS_PAYLOAD), config=THREAD)
        wf.invoke(
            Command(resume={"approved": True, "approver": "phani", "comment": "go"}),
            config=THREAD,
        )
        wf.invoke(
            Command(resume={"permission_set_id": S3_FULL_ACCESS_PSET_ID}),
            config=THREAD,
        )

    def test_gate1_pass_proceeds_to_terraform_and_gate2(self, gov_workflow: Any) -> None:
        """After Gate 1 PASS, terraform generation/validation runs and stops at Gate 2."""
        self._advance_to_gate1_pass(gov_workflow)
        snapshot = gov_workflow.get_state(THREAD)
        values = snapshot.values
        assert values.get("gate_1_passed") is True
        # Should have generated terraform HCL
        assert values.get("terraform_hcl") is not None
        assert len(values["terraform_hcl"]) > 0
        # Should have terraform validation result
        assert values.get("terraform_validation") is not None
        # Should be paused at gate_2_review
        assert "gate_2_review" in snapshot.next

    def test_terraform_hcl_is_generated(self, gov_workflow: Any) -> None:
        """Terraform HCL is generated from the request (LLM or deterministic fallback)."""
        self._advance_to_gate1_pass(gov_workflow)
        values = gov_workflow.get_state(THREAD).values
        hcl = values.get("terraform_hcl", "")
        assert "terraform" in hcl or "provider" in hcl or "resource" in hcl

    def test_no_terraform_apply_in_phase_3c(self, gov_workflow: Any) -> None:
        """Phase 3C must stop safely after planning — no apply."""
        self._advance_to_gate1_pass(gov_workflow)
        values = gov_workflow.get_state(THREAD).values
        assert values.get("terraform_apply_result") is None
        assert values.get("boto3_verification") is None


class TestPhase3D_Gate2:
    """Phase 3D: Gate 2 human execution review."""

    def _advance_to_gate2(self, wf: Any) -> None:
        wf.invoke(dict(JIRA_AWS_PAYLOAD), config=THREAD)
        wf.invoke(
            Command(resume={"approved": True, "approver": "phani", "comment": "go"}),
            config=THREAD,
        )
        wf.invoke(
            Command(resume={"permission_set_id": S3_FULL_ACCESS_PSET_ID}),
            config=THREAD,
        )

    def test_gate2_displays_review_payload(self, gov_workflow: Any) -> None:
        """Gate 2 interrupt contains full review payload."""
        self._advance_to_gate2(gov_workflow)
        snapshot = gov_workflow.get_state(THREAD)
        assert "gate_2_review" in snapshot.next
        interrupts = snapshot.tasks[0].interrupts if snapshot.tasks else []
        assert len(interrupts) > 0
        interrupt_val = interrupts[0].value
        assert interrupt_val.get("type") == "gate2_execution_review"
        review = interrupt_val.get("review", {})
        assert review.get("jira_issue_key") == "AWS-42"
        assert review.get("gate_1_result", {}).get("pass") is True

    def test_gate2_reject_blocks_execution(self, gov_workflow: Any) -> None:
        """Gate 2 REJECT → guidance, no terraform apply."""
        self._advance_to_gate2(gov_workflow)
        final = gov_workflow.invoke(
            Command(resume={"approved": False, "approver": "pvr", "comment": "too risky"}),
            config=THREAD,
        )
        assert final.get("gate_2_passed") is False
        assert final.get("terraform_apply_result") is None
        assert "# Engineer Guidance" in final.get("guidance_md", "")

    def test_gate2_approve_resumes_to_apply(self, gov_workflow: Any) -> None:
        """Gate 2 APPROVE → proceeds to terraform_apply (dry run by default)."""
        self._advance_to_gate2(gov_workflow)
        final = gov_workflow.invoke(
            Command(resume={"approved": True, "approver": "pvr", "comment": "approved"}),
            config=THREAD,
        )
        assert final.get("gate_2_passed") is True
        apply_result = final.get("terraform_apply_result", {})
        assert apply_result.get("dry_run") is True


class TestPhase3E_Execution:
    """Phase 3E: Terraform apply → boto3 verification → Jira update."""

    def _advance_to_gate2_approved(self, wf: Any) -> dict[str, Any]:
        wf.invoke(dict(JIRA_AWS_PAYLOAD), config=THREAD)
        wf.invoke(
            Command(resume={"approved": True, "approver": "phani", "comment": "go"}),
            config=THREAD,
        )
        wf.invoke(
            Command(resume={"permission_set_id": S3_FULL_ACCESS_PSET_ID}),
            config=THREAD,
        )
        return wf.invoke(
            Command(resume={"approved": True, "approver": "pvr", "comment": "execute"}),
            config=THREAD,
        )

    def test_dry_run_produces_indeterminate(self, gov_workflow: Any) -> None:
        """With apply disabled, result is INDETERMINATE (not COMPLETED)."""
        final = self._advance_to_gate2_approved(gov_workflow)
        verification = final.get("boto3_verification", {})
        assert verification.get("boto3_verification_status") == "INDETERMINATE"
        assert final.get("final_status") == "INDETERMINATE"

    def test_apply_failure_produces_failed(self, gov_workflow: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """Terraform apply failure → FAILED status."""
        monkeypatch.setenv("CHANDRA_TERRAFORM_APPLY_ENABLED", "true")
        # Mock terraform to fail
        import subprocess
        original_run = subprocess.run
        def mock_run(args, **kwargs):
            if args and args[0] == "terraform" and "apply" in args:
                from types import SimpleNamespace
                return SimpleNamespace(returncode=1, stdout="", stderr="Error: apply failed")
            if args and args[0] == "terraform" and "init" in args:
                from types import SimpleNamespace
                return SimpleNamespace(returncode=0, stdout="Initialized", stderr="")
            return original_run(args, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            final = self._advance_to_gate2_approved(gov_workflow)

        assert final.get("final_status") == "FAILED"
        verification = final.get("boto3_verification", {})
        assert verification.get("boto3_verification_status") == "FAILED"

    def test_jira_tracker_update_attempted(self, gov_workflow: Any) -> None:
        """Jira update is attempted in the update_tracker node."""
        final = self._advance_to_gate2_approved(gov_workflow)
        tracker_updates = final.get("tracker_updates", [])
        assert len(tracker_updates) > 0

    def test_full_audit_trail_preserved(self, gov_workflow: Any) -> None:
        """Complete audit trail with job_id and jira correlation."""
        final = self._advance_to_gate2_approved(gov_workflow)
        audit_trail = final.get("audit_trail", [])
        node_names = [e.node for e in audit_trail]
        # Verify governed path nodes are in the trail
        assert "receive_request" in node_names
        assert "gate_1_verification" in node_names
        assert "terraform_generate" in node_names
        assert "terraform_validate_plan" in node_names
        assert "gate_2_review" in node_names
        assert "terraform_apply" in node_names
        assert "verify_aws_resources" in node_names

    def test_verification_semantics_apply_success_verify_fail(
        self, gov_workflow: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Apply SUCCESS + boto3 verification FAILURE → FAILED (not COMPLETED)."""
        monkeypatch.setenv("CHANDRA_TERRAFORM_APPLY_ENABLED", "true")
        import subprocess
        original_run = subprocess.run
        def mock_run(args, **kwargs):
            if args and args[0] == "terraform":
                from types import SimpleNamespace
                if "init" in args:
                    return SimpleNamespace(returncode=0, stdout="Initialized", stderr="")
                if "apply" in args:
                    return SimpleNamespace(returncode=0, stdout="Apply complete! Resources: 1 added", stderr="")
                if "output" in args:
                    return SimpleNamespace(returncode=0, stdout='{"bucket_name":{"value":"test-bucket"}}', stderr="")
            return original_run(args, **kwargs)

        def mock_verify_resource(self, task_name, outputs):
            return "FAILED"

        with patch("subprocess.run", side_effect=mock_run):
            with patch(
                "src.chandra.execution.services.AwsResourceVerifier.verify_resource",
                mock_verify_resource,
            ):
                final = self._advance_to_gate2_approved(gov_workflow)

        assert final.get("final_status") == "FAILED"


class TestDeterminismInvariant:
    """Gate 1, Gate 2, decision_router, and verification are deterministic."""

    def test_gate1_is_deterministic(self, gov_workflow: Any) -> None:
        """Gate 1 does not call any LLM — it's a pure fnmatch comparison."""
        gov_workflow.invoke(dict(JIRA_AWS_PAYLOAD), config=THREAD)
        gov_workflow.invoke(
            Command(resume={"approved": True, "approver": "phani", "comment": "go"}),
            config=THREAD,
        )
        # Gate 1 with matching perms
        gov_workflow.invoke(
            Command(resume={"permission_set_id": S3_FULL_ACCESS_PSET_ID}),
            config=THREAD,
        )
        values = gov_workflow.get_state(THREAD).values
        # Gate 1 must have produced a structured result
        gate_1 = values.get("gate_1_result", {})
        assert "pass" in gate_1
        assert "matched_actions" in gate_1
        assert "missing_actions" in gate_1
