"""Standalone test for the ``approval_node`` node.

Pipeline position:
    compose_briefing -> approval_node -> persist

Human-in-the-loop checkpoint. If ``state['pending_writes']`` is empty,
the node returns ``{}`` and the graph routes straight to ``persist``.
Otherwise it emits a ``langgraph.interrupt`` carrying the pending writes
as a JSON-safe payload, and on resume it converts the human's reply
into ``ApprovalDecision`` records.

Pure routing logic -- no AWS, no LLM, no DB. Real-env / mock-env toggle
does not change behaviour.
"""

from __future__ import annotations

# --- sys.path bootstrap: lets this script run both as a module
#     (uv run python -m tests.test_nodes.x) and as a script
#     (uv run tests/test_nodes/x.py). ---
import sys as _sys
from pathlib import Path as _Path

_REPO_ROOT = _Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_REPO_ROOT))

from datetime import UTC, datetime
from unittest.mock import patch

from src.chandra.briefing.schemas import ProposedWrite
from src.chandra.graphs.nodes import approval_node

from tests.test_nodes._env import banner, make_state, mode_banner


def _proposed_write(action: str = "EnableVersioning") -> ProposedWrite:
    return ProposedWrite(
        action=action,
        target_arn="arn:aws:s3:::my-bucket",
        region="us-east-1",
        payload={"VersioningConfiguration": {"Status": "Enabled"}},
        requested_by="system",
        justification="Enable versioning for data protection",
    )


def test_approvalnode() -> None:
    """Run ``approval_node`` in both no-op and interrupt paths."""
    mode_banner()
    banner("approval_node -- input state 1 (no pending writes)")
    state_in = make_state(pending_writes=[])
    print(f"  run_id          = {state_in['run_id']!r}")
    print(f"  pending_writes  = {state_in['pending_writes']!r}")

    result = approval_node(state_in)
    print(f"  output          = {result!r}")
    assert result == {}
    print("  [ok] empty pending_writes -> no-op (graph skips approval)")

    banner("approval_node -- input state 2 (with pending write)")
    state_in = make_state(pending_writes=[_proposed_write()])
    print(f"  run_id          = {state_in['run_id']!r}")
    print(f"  pending_writes  = {len(state_in['pending_writes'])} write(s)")
    for w in state_in["pending_writes"]:
        print(f"    - {w.action:24s}  target={w.target_arn}")

    decision_payload = {
        "decision": "approve",
        "reviewer": "alice@example.com",
        "reason": "Looks safe",
        "decided_at": datetime.now(UTC).isoformat(),
    }
    with patch("src.chandra.graphs.nodes.interrupt") as mock_interrupt:
        mock_interrupt.return_value = [decision_payload]
        result = approval_node(state_in)

    banner("approval_node -- output (state update)")
    print(f"  approvals count : {len(result['approvals'])}")
    for a in result["approvals"]:
        print(f"    - decision={a.decision:8s} reviewer={a.reviewer:24s}  reason={a.reason!r}")

    mock_interrupt.assert_called_once()
    call_arg = mock_interrupt.call_args[0][0]
    assert "pending_writes" in call_arg
    assert call_arg["pending_writes"][0]["action"] == "EnableVersioning"
    assert len(result["approvals"]) == 1
    assert result["approvals"][0].decision == "approve"
    print("\n  [ok] approval_node interrupted and translated the human reply")


if __name__ == "__main__":
    test_approvalnode()
