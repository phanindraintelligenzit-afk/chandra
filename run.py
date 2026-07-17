"""Ad-hoc local runner for the Chandra LangGraph pipeline.

Streams the compiled graph against a hard-coded synthetic account so
developers can iterate without touching the Typer CLI. Supports an
auto-approve mode (``CHANDRA_AUTO_APPROVE=1``) that resumes the graph
past the human-in-the-loop gate with one ``approve`` decision per
pending write — useful for local end-to-end smoke tests, not for prod.

In production the FE approval center resumes the graph; ``run.py`` is a
developer scratchpad, not the production entry point. Use
``chandra run --account $SYNTHETIC_ACCOUNT_ID`` for the real path.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
import boto3
from datetime import datetime, timezone

from dotenv import load_dotenv
from langchain_aws import ChatBedrockConverse  # noqa: F401  (forces import ordering)
# from langchain_openai import ChatOpenAI  # noqa: F401
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from src.chandra.graphs.chandra_graph import build_graph

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    force=True,
    handlers=[
        logging.FileHandler("logs.txt", mode="w", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("chandra_runner")

load_dotenv()

AUTO_APPROVE = os.environ.get("CHANDRA_AUTO_APPROVE", "").lower() in {"1", "true", "yes"}


def default_serializer(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return str(obj)


def _stream_and_log(graph, payload, config, label: str) -> bool:
    """Stream a graph invocation, logging each step. Returns True if the
    stream yielded the end-of-graph (no interrupt), False if it paused
    for human input (i.e. ``graph.get_state(config).next`` is set after)."""
    logger.info("=" * 80)
    logger.info(f"STREAM: {label}")
    logger.info("=" * 80)
    for step in graph.stream(payload, config):
        for node_name, state_update in step.items():
            logger.info(f"\n[ RUNNING NODE ] --> {node_name}")
            logger.info(f"Output state from ({node_name}):")
            formatted_output = json.dumps(state_update, indent=2, default=default_serializer)
            logger.info(f"\n{formatted_output}")
            logger.info("-" * 80)
    state = graph.get_state(config)
    return not bool(state.next)


def main() -> None:
    graph = build_graph(checkpointer=MemorySaver())
    run_id = str(uuid.uuid4())

    try:
        sts = boto3.client("sts")
        account_id = sts.get_caller_identity()["Account"]
    except Exception as e:
        logger.error(f"Failed to fetch AWS account ID from credentials: {e}")
        account_id = "UNKNOWN"

    inputs = {
        "run_id": run_id,
        "account_id": account_id,
        "regions": [os.getenv("AWS_DEFAULT_REGION", "us-east-1")],
        "dry_run": False,
        "raw_findings": {},
        "errors": [],
        "selected_kras": ["cost", "security", "compliance", "performance", "reliability"],
    }
    config = {"configurable": {"thread_id": run_id}}

    logger.info("=" * 80)
    logger.info(f"STARTING PIPELINE RUN: {run_id}")
    logger.info(f"AUTO_APPROVE: {AUTO_APPROVE}")
    logger.info("=" * 80)
    logger.info(f"Initial State:\n{json.dumps(inputs, indent=2)}")
    logger.info("=" * 80)

    completed = _stream_and_log(graph, inputs, config, label="initial")

    # If the graph paused, handle the approval gate.
    if not completed:
        state = graph.get_state(config)
        pending = (state.values or {}).get("pending_writes", []) or []
        logger.info(
            f"\n[ PAUSED ] at {state.next!r} — {len(pending)} pending write(s) "
            f"awaiting human approval."
        )
        if not AUTO_APPROVE:
            logger.info(
                "Set CHANDRA_AUTO_APPROVE=1 to auto-approve locally, or feed "
                "decisions via the FE approval center."
            )
            return
        
        now = datetime.now(timezone.utc)
        decisions = [
            {
                "decision": "approve",
                "reviewer": "auto-approver@run.py",
                "reason": f"CHANDRA_AUTO_APPROVE=1 at {now.isoformat()}",
                "decided_at": now.isoformat(),
            }
            for _ in pending
        ]
        logger.info(f"Auto-approving {len(decisions)} write(s).")
        _stream_and_log(graph, Command(resume=decisions), config, label="resume")

    logger.info("\nPIPELINE COMPLETED SUCCESSFULLY!")


if __name__ == "__main__":
    main()
