import json
import uuid
from typing import Annotated, Any, TypedDict
import os
from src.chandra.config import settings
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
# from langchain_aws import ChatBedrockConverse
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver, logger
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(override=True)

# ── Import all tools from tools.py ───────────────────────────────────────────
from copilot_agents.call_tools import (
    get_aws_cost_summary,
    get_cloudtrail_api_logs,
    get_cpu_metrics,
    get_recent_resource_changes,
    get_xray_traces,
)

# ── Register all tools ────────────────────────────────────────────────────────
tools = [
    get_cpu_metrics,
    get_xray_traces,
    get_recent_resource_changes,
    get_cloudtrail_api_logs,
    get_aws_cost_summary,
]
tools_by_name = {t.name: t for t in tools}


def _build_checkpointer() -> Any:
    """Return a checkpointer using a three-tier fallback strategy.

    Tier 1: Postgres (production)
    Tier 2: SQLite (local disk inside 'database/' folder)
    Tier 3: In-memory MemorySaver fallback
    """
    # ── Tier 1: Try Postgres ──
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg_pool import ConnectionPool
        import psycopg
        if hasattr(settings, "postgres_url") and settings.postgres_url:
            # Convert SQLAlchemy URL format to psycopg native format
            # postgresql+psycopg://... → postgresql://...
            conn_string = settings.postgres_url.replace("postgresql+psycopg://", "postgresql://")
            
            with psycopg.connect(conn_string, autocommit=True) as conn:
                PostgresSaver(conn).setup()
                
            pool = ConnectionPool(conn_string, max_size=10)
            checkpointer = PostgresSaver(pool)
            logger.info("checkpointer.postgres_setup_success")
            return checkpointer
        else:
            logger.warning("checkpointer.postgres_url_missing")
    except ImportError:
        logger.warning("checkpointer.postgres_unavailable")
    except Exception as exc:
        logger.warning(
            "checkpointer.postgres_setup_failed_fallback_to_sqlite",
            exc_info=exc,
        )

    # ── Tier 2: Try SQLite ──
    try:
        import sqlite3
        from langgraph.checkpoint.sqlite import SqliteSaver
        
        # Ensure the database directory exists
        db_dir = Path("database")
        db_dir.mkdir(parents=True, exist_ok=True)
        db_path = db_dir / "checkpoints.sqlite"
        
        # Initialize SQLite connection and checkpointer
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        checkpointer = SqliteSaver(conn)
        checkpointer.setup()
        logger.info(f"checkpointer.sqlite_setup_success at {db_path}")
        return checkpointer
    except ImportError:
        logger.warning("checkpointer.sqlite_unavailable")
    except Exception as exc:
        logger.warning(
            "checkpointer.sqlite_setup_failed_fallback_to_memory",
            exc_info=exc,
        )

    # ── Tier 3: MemorySaver ──
    logger.warning("checkpointer.fallback_to_memory_saver")
    return MemorySaver()

# ── Graph state ───────────────────────────────────────────────────────────────
class AgentState(TypedDict):
    messages: Annotated[list, add_messages]


# LLM is initialized inside build_graph() to avoid crashing at import time
# if MODEL_NAME is missing or Bedrock credentials are invalid.

SYSTEM_PROMPT = (
    "You are a cloud watcher agent that monitors AWS cloud infrastructure. "
    "You have access to tools for CPU metrics, X-Ray traces, resource change "
    "history, CloudTrail audit logs, and cost summaries. Use them as needed to "
    "answer the user's question thoroughly. Just give very short summary one line."
)



def execute_tools(state: AgentState) -> AgentState:
    """Execute every tool call requested by the last AI message."""
    last_message = state["messages"][-1]
    tool_messages = []

    for tool_call in last_message.tool_calls:
        tool_fn = tools_by_name[tool_call["name"]]
        result = tool_fn.invoke(tool_call["args"])
        tool_messages.append(
            ToolMessage(
                content=json.dumps(result, default=str),
                tool_call_id=tool_call["id"],
            )
        )

    return {"messages": tool_messages}


# ── Routing logic ─────────────────────────────────────────────────────────────
def should_continue(state: AgentState) -> str:
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "execute_tools"
    return END


# ── Build the graph ───────────────────────────────────────────────────────────
def build_graph():
    # Initialize LLM here (not at module level) so import errors don't crash FastAPI
    # llm = ChatBedrockConverse(model=os.getenv("MODEL_NAME"))
    llm = ChatOpenAI(
        base_url=os.getenv("OPENAI_API_BASE"),
        api_key=os.getenv("OPENAI_API_KEY"),
        model=os.getenv("OPENAI_MODEL_NAME"),
    )
    llm_with_tools = llm.bind_tools(tools)

    # Define call_llm as a closure so it captures the local llm_with_tools
    def call_llm_node(state: AgentState) -> AgentState:
        """Send messages to the LLM (with tools available)."""
        messages = [SystemMessage(content=SYSTEM_PROMPT)] + state["messages"]
        response = llm_with_tools.invoke(messages)
        return {"messages": [response]}

    checkpointer = _build_checkpointer()

    graph = StateGraph(AgentState)

    graph.add_node("call_llm", call_llm_node)
    graph.add_node("execute_tools", execute_tools)

    graph.set_entry_point("call_llm")

    graph.add_conditional_edges(
        "call_llm",
        should_continue,
        {"execute_tools": "execute_tools", END: END},
    )

    graph.add_edge("execute_tools", "call_llm")

    return graph.compile(checkpointer=checkpointer)  # ← pass checkpointer here


# ── Helper: single turn ───────────────────────────────────────────────────────
def chat(agent, thread_id: str, user_message: str) -> str:
    """
    Send one user message and return the agent's reply.
    The same thread_id preserves conversation history across turns.
    """
    config = {"configurable": {"thread_id": thread_id}}
    result = agent.invoke(
        {"messages": [HumanMessage(content=user_message)]},
        config=config,
    )
    return result["messages"][-1].content


# def main():
#     agent = build_graph()
#     thread_id = str(uuid.uuid4())
#     config = {"configurable": {"thread_id": thread_id}}

#     def run(query: str) -> str:
#         final_state = agent.invoke(
#             {"messages": [HumanMessage(content=query)]},
#             config=config,
#         )
#         return final_state["messages"][-1].content

#     # ── Test queries ──────────────────────────────────────────────────────────

#     # 1. Basic CPU fetch — last 20 hours, 30-min buckets
    # q1 = (
    #     "Give observations on CPU utilization for ec2 instance over last 20 hours "
    #     "with 30 mins timeline in detail for instanceId i-0327bf109dc40d412"
    # )

#     # 2. Memory test — relies on the answer from q1
#     q2 = "What was the peak CPU value you just reported and at what time?"

#     # 3. Shorter window — last 3 hours, 10-min buckets (period=600)
#     q3 = (
#         "Check CPU for instanceId i-0327bf109dc40d412 over the last 3 hours "
#         "with a 10 minute interval"
#     )

#     q1_response = run(q1)
#     print(q1_response)

#     q2_response = run(q2)
#     print(q2_response)


# if __name__ == "__main__":
#     main()