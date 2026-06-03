import uuid
import json
import logging
from dotenv import load_dotenv
from langgraph.checkpoint.memory import MemorySaver

from src.chandra.graphs.chandra_graph import build_graph

# Configure logging to write to logs.txt
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    force=True,
    handlers=[
        logging.FileHandler("logs.txt", mode="w", encoding="utf-8"),
        logging.StreamHandler() # Also print to console
    ]
)
logger = logging.getLogger("chandra_runner")

# Load environment variables (e.g. AWS credentials, BEDROCK_MODEL_ID, SNS_TOPIC_ARN)
load_dotenv()

def default_serializer(obj):
    return str(obj)

def main():
    # Build the graph using an in-memory checkpointer for quick testing
    graph = build_graph(checkpointer=MemorySaver())

    run_id = str(uuid.uuid4())
    
    # The initial state payload to start the graph
    inputs = {
        "run_id": run_id,
        "account_id": "827295473120",
        "regions": ["us-east-1"],
        "raw_findings": {},
        "errors": [],
    }

    logger.info("=" * 80)
    logger.info(f"STARTING PIPELINE RUN: {run_id}")
    logger.info("=" * 80)
    logger.info(f"Initial State:\n{json.dumps(inputs, indent=2)}")
    logger.info("=" * 80)

    # Use graph.stream() instead of graph.invoke() so we can intercept every step
    for step in graph.stream(
        inputs,
        config={"configurable": {"thread_id": run_id}},
    ):
        for node_name, state_update in step.items():
            logger.info(f"\n[ RUNNING NODE ] --> {node_name}")
            logger.info(f"Output state from ({node_name}):")
            
            formatted_output = json.dumps(state_update, indent=2, default=default_serializer)
            logger.info(f"\n{formatted_output}")
            logger.info("-" * 80)

    logger.info("\nPIPELINE COMPLETED SUCCESSFULLY!")

if __name__ == "__main__":
    main()