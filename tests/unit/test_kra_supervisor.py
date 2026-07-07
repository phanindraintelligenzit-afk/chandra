"""Unit tests for KRA supervisor node and routing."""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Send
from src.chandra.graphs.action_nodes import _route_kra_workers, kra_supervisor
from src.chandra.graphs.chandra_graph import build_graph
from src.chandra.graphs.state import ChandraState


class TestKRASupervisor:
    def test_supervisor_returns_empty_dict(self) -> None:
        """Supervisor node returns empty dict (no state mutation)."""
        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            regions=["us-east-1"],
        )
        result = kra_supervisor(state)

        assert result == {}

    def test_route_kra_workers_dispatches_all_five(self) -> None:
        """Router dispatches exactly 5 Send objects, one per KRA."""
        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            regions=["us-east-1"],
        )
        sends = _route_kra_workers(state)

        assert len(sends) == 5
        assert all(isinstance(s, Send) for s in sends)

    def test_route_kra_workers_send_targets(self) -> None:
        """Router targets all 5 KRA worker nodes."""
        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            regions=["us-east-1"],
        )
        sends = _route_kra_workers(state)

        targets = {s.node for s in sends}
        expected_targets = {
            "observe_cost",
            "observe_security",
            "observe_compliance",
            "observe_performance",
            "observe_reliability",
        }
        assert targets == expected_targets

    def test_graph_contains_kra_supervisor_node(self) -> None:
        """Built graph contains kra_supervisor as a registered node."""
        graph = build_graph(checkpointer=MemorySaver())
        node_names = list(graph.nodes.keys())

        assert "kra_supervisor" in node_names
