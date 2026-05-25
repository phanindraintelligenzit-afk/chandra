"""Build the compiled LangGraph for Chandra."""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from chandra.config import settings
from chandra.graphs.nodes import (
    analyze,
    approval_node,
    compose_briefing,
    fanout_observers,
    observe_compliance,
    observe_cost,
    observe_performance,
    observe_reliability,
    observe_security,
    onboard_account,
    persist,
)
from chandra.graphs.state import ChandraState
from chandra.logging import get_logger

logger = get_logger(__name__)


def _build_checkpointer() -> Any:
    """Return a checkpointer.

    Prefers Postgres (production); falls back to in-memory if the Postgres
    checkpoint library is unavailable in the runtime environment. The
    fallback is logged at WARNING so it never silently regresses.
    """
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError:
        logger.warning("checkpointer.postgres_unavailable_fallback_to_memory")
        return MemorySaver()

    try:
        checkpointer = PostgresSaver.from_conn_string(settings.postgres_url)
        checkpointer.setup()
        return checkpointer
    except Exception as exc:
        logger.warning(
            "checkpointer.postgres_setup_failed_fallback_to_memory",
            error=str(exc),
        )
        return MemorySaver()


def build_graph(checkpointer: Any | None = None) -> Any:
    """Compile the Chandra observation graph.

    Parameters
    ----------
    checkpointer:
        Optional. Pass ``None`` to use the default Postgres checkpointer.
        Pass an explicit :class:`MemorySaver` in tests.
    """
    graph: StateGraph[ChandraState] = StateGraph(ChandraState)

    graph.add_node("onboard_account", onboard_account)
    graph.add_node("observe_cost", observe_cost)
    graph.add_node("observe_security", observe_security)
    graph.add_node("observe_compliance", observe_compliance)
    graph.add_node("observe_performance", observe_performance)
    graph.add_node("observe_reliability", observe_reliability)
    graph.add_node("analyze", analyze)
    graph.add_node("compose_briefing", compose_briefing)
    graph.add_node("approval_node", approval_node)
    graph.add_node("persist", persist)

    graph.add_edge(START, "onboard_account")
    graph.add_conditional_edges(
        "onboard_account",
        fanout_observers,
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

    graph.add_edge("analyze", "compose_briefing")

    def route_to_approval(state: ChandraState) -> str:
        pending = state.get("pending_writes", []) or []
        return "approval_node" if pending else "persist"

    graph.add_conditional_edges(
        "compose_briefing",
        route_to_approval,
        ["approval_node", "persist"],
    )
    graph.add_edge("approval_node", "persist")
    graph.add_edge("persist", END)

    saver = checkpointer if checkpointer is not None else _build_checkpointer()
    return graph.compile(checkpointer=saver)
