"""Build the compiled LangGraph for Chandra."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from src.chandra.graphs.action_nodes import (
    _route_kra_workers,
    action_executor_node,
    analyze,
    approval_node,
    decision_router,
    escalation_node,
    ingest_observations,
    kra_supervisor,
    observe_compliance,
    observe_cost,
    observe_performance,
    observe_reliability,
    observe_security,
    onboard_account,
    persist,
)
from src.chandra.graphs.checkpointer import build_checkpointer
from src.chandra.graphs.state import ChandraState
from src.chandra.logging import get_logger

logger = get_logger(__name__)


def build_graph(checkpointer: Any | None = None) -> Any:
    """Compile the Chandra observation graph.

    Parameters
    ----------
    checkpointer:
        Optional. Pass ``None`` to use the default Postgres checkpointer.
        Pass an explicit :class:`MemorySaver` in tests.
    """
    graph: StateGraph[ChandraState] = StateGraph(ChandraState)
    graph.add_node("escalation", escalation_node)

    graph.add_node("onboard_account", onboard_account)
    graph.add_node("ingest_observations", ingest_observations)
    graph.add_node("kra_supervisor", kra_supervisor)
    graph.add_node("observe_cost", observe_cost)
    graph.add_node("observe_security", observe_security)
    graph.add_node("observe_compliance", observe_compliance)
    graph.add_node("observe_performance", observe_performance)
    graph.add_node("observe_reliability", observe_reliability)
    graph.add_node("analyze", analyze)
    graph.add_node("decision_router", decision_router)
    graph.add_node("approval_node", approval_node)
    graph.add_node("persist", persist)
    graph.add_node("action_executor", action_executor_node)

    graph.add_edge(START, "onboard_account")
    graph.add_edge("onboard_account", "ingest_observations")
    graph.add_edge("ingest_observations", "kra_supervisor")
    graph.add_conditional_edges(
        "kra_supervisor",
        _route_kra_workers,
        [
            "observe_cost",
            "observe_security",
            "observe_compliance",
            "observe_performance",
            "observe_reliability",
        ],
    )

    # All five observers join into analyze.
    for kra in ("cost", "security", "compliance", "performance", "reliability"):
        graph.add_edge(f"observe_{kra}", "analyze")

    # Sequential post-analyze chain: decision_router classifies findings,
    # action_executor consumes decision_router's auto_fixed output, escalation
    # publishes pending_writes to SNS, compose_briefing renders the briefing.
    # The compose_briefing -> approval_node/persist conditional below uses
    # pending_writes to decide whether to interrupt for human approval.
    graph.add_edge("analyze", "decision_router")
    graph.add_edge("decision_router", "action_executor")
    graph.add_edge("action_executor", "escalation")

    def route_to_approval(state: ChandraState) -> str:
        pending = state.get("pending_writes", []) or []
        return "approval_node" if pending else "persist"

    graph.add_conditional_edges(
        "escalation",
        route_to_approval,
        ["approval_node", "persist"],
    )
    graph.add_edge("approval_node", "persist")
    graph.add_edge("persist", END)

    saver = checkpointer if checkpointer is not None else build_checkpointer()
    # ``interrupt_before`` on ``approval_node`` actually halts the graph at
    # the human-in-the-loop gate. The function-level ``interrupt(...)`` in
    # approval_node alone is NOT enough in LangGraph 1.x — the runtime
    # treats an interrupted node as "complete" for routing and continues
    # to downstream nodes. ``interrupt_before`` is what makes the gate
    # real. On resume with ``Command(resume=decisions)``, the function
    # re-runs from the top and ``interrupt(...)`` returns ``decisions``.
    return graph.compile(
        checkpointer=saver,
        interrupt_before=["approval_node"],
    )
