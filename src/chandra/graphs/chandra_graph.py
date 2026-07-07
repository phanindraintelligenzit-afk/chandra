"""Build the compiled LangGraph for Chandra."""

from __future__ import annotations

from typing import Any, cast

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from src.chandra.config import settings
from src.chandra.graphs.action_nodes import (
    _route_kra_workers,
    action_executor_node,
    analyze,
    approval_node,
    compose_briefing,
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
from src.chandra.graphs.state import ChandraState
from src.chandra.logging import get_logger

logger = get_logger(__name__)

# LangGraph 1.x emits "Deserializing unregistered type
# src.chandra.briefing.schemas.*" deprecation warnings on every
# checkpoint round-trip of a pydantic model. Registering the module
# via ``JsonPlusSerializer(allowed_msgpack_modules=...)`` silences
# them, but in 1.2.0 it also changes deserialization to return plain
# dicts, which breaks pydantic field access in ``persist`` and other
# post-checkpoint nodes. So we accept the warnings as forward-looking
# deprecation noise. The future-strict mode (``LANGGRAPH_STRICT_MSGPACK=true``)
# will force a real fix: either a custom serde that preserves pydantic
# types, or migrating state fields to plain dicts.


def _build_checkpointer() -> Any:
    """Return a checkpointer.

    Prefers Postgres (production); falls back to in-memory if the Postgres
    checkpoint library is unavailable in the runtime environment. The
    fallback is logged at WARNING so it never silently regresses.
    """
    try:
        from langgraph.checkpoint.postgres import (  # noqa: PLC0415  # lazy: optional dep
            PostgresSaver,
        )
    except ImportError:
        logger.warning("checkpointer.postgres_unavailable_fallback_to_memory")
        return MemorySaver()

    try:
        # Convert SQLAlchemy URL format to psycopg native format
        # postgresql+psycopg://... → postgresql://...
        import psycopg  # noqa: PLC0415  # lazy: optional dep
        from psycopg.rows import dict_row  # noqa: PLC0415  # lazy: optional dep
        from psycopg_pool import (  # noqa: PLC0415  # lazy: optional dep
            ConnectionPool,
        )

        conn_string = settings.postgres_url.replace("postgresql+psycopg://", "postgresql://")

        with psycopg.connect(conn_string, autocommit=True, row_factory=dict_row) as conn:
            PostgresSaver(conn).setup()

        # kwargs row_factory yields dict rows at runtime; ConnectionPool's
        # type parameter cannot express that, hence the cast.
        pool = ConnectionPool(conn_string, max_size=10, kwargs={"row_factory": dict_row})
        checkpointer = PostgresSaver(cast(Any, pool))
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
    graph.add_node("compose_briefing", compose_briefing)
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
    graph.add_edge("escalation", "compose_briefing")

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
