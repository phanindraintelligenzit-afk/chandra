"""Tests for the approval node and pending_writes workflow."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from chandra.briefing.schemas import ApprovalDecision, ProposedWrite
from chandra.graphs.nodes import approval_node
from chandra.graphs.state import ChandraState


def test_approval_node_empty_pending_writes() -> None:
    """Empty pending writes should return empty dict (no state change)."""
    state: ChandraState = {
        "run_id": "test-run-1",
        "account_id": "123456789012",
        "pending_writes": [],
    }

    result = approval_node(state)

    assert result == {}


def test_approval_node_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that approval_node routes to interrupt when pending writes exist."""
    from unittest.mock import Mock

    proposed = ProposedWrite(
        action="EnableVersioning",
        target_arn="arn:aws:s3:::my-bucket",
        payload={"VersioningConfiguration": {"Status": "Enabled"}},
        requested_by="system",
        justification="Enable versioning for data protection",
    )

    state: ChandraState = {
        "run_id": "test-run-1",
        "account_id": "123456789012",
        "pending_writes": [proposed],
    }

    # Mock interrupt to avoid needing a runnable context
    mock_interrupt = Mock(return_value=[{"decision": "approve", "reviewer": "admin", "reason": "OK", "decided_at": datetime.now(timezone.utc).isoformat()}])
    monkeypatch.setattr("chandra.graphs.nodes.interrupt", mock_interrupt)

    result = approval_node(state)

    # Verify that interrupt was called with the pending writes
    mock_interrupt.assert_called_once()
    call_args = mock_interrupt.call_args[0][0]
    assert "pending_writes" in call_args
    assert len(call_args["pending_writes"]) == 1
    assert call_args["pending_writes"][0]["action"] == "EnableVersioning"


def test_approval_decision_creation() -> None:
    """Test creating approval decisions."""
    now = datetime.now(timezone.utc)
    decision = ApprovalDecision(
        decision="approve",
        reviewer="admin@example.com",
        reason="Configuration aligns with security policy",
        decided_at=now,
    )

    assert decision.decision == "approve"
    assert decision.reviewer == "admin@example.com"
    assert decision.decided_at == now


def test_proposed_write_validation() -> None:
    """Test ProposedWrite model validation."""
    write = ProposedWrite(
        action="ModifyBucket",
        target_arn="arn:aws:s3:::test-bucket",
        payload={"BlockPublicAcls": True},
        requested_by="detector-1",
        justification="Block public access per compliance",
    )

    assert write.action == "ModifyBucket"
    assert write.target_arn == "arn:aws:s3:::test-bucket"
    assert write.payload == {"BlockPublicAcls": True}


def test_approval_decision_reject() -> None:
    """Test creating a rejection decision."""
    now = datetime.now(timezone.utc)
    decision = ApprovalDecision(
        decision="reject",
        reviewer="reviewer@example.com",
        reason="Does not align with current policy",
        decided_at=now,
    )

    assert decision.decision == "reject"
    assert "does not align" in decision.reason.lower()
