"""
Tests for Action Executor Node

NOTE: the ``action_type`` strings in this file (e.g. ``"remediate_s3"``) are
illustrative legacy values. The real ``decision_router`` produces
``action_type="remediate_SEC-001-public-s3"`` etc. — see
``tests/unit/test_action_executor_node.py`` for the up-to-date node tests that
exercise the LangGraph node and the detector-id -> handler registry.
"""

import pytest

from src.chandra.graphs.nodes.action_executor import ActionExecutor


def test_dry_run_mode():
    """Test that dry_run mode doesn't make actual changes."""
    executor = ActionExecutor(dry_run=True)

    state = {
        "action_type": "remediate_s3",
        "resource_id": "test-bucket",
        "region": "us-east-1",
        "problem_type": "public_s3"
    }

    result = executor.run(state)

    assert result["status"] == "dry_run"
    assert result["action_executed"] is False
    assert "DRY RUN" in result["message"]


def test_executor_initialization():
    """Test executor initializes correctly."""
    executor = ActionExecutor(dry_run=False)
    assert executor.dry_run is False


def test_unknown_problem_type():
    """Test handling of unknown problem type."""
    executor = ActionExecutor(dry_run=False)

    state = {
        "action_type": "unknown",
        "resource_id": "test-resource",
        "region": "us-east-1",
        "problem_type": "unknown_problem"
    }

    result = executor.run(state)

    assert result["status"] == "failure"
    assert result["action_executed"] is False
    assert "error" in result
