
from chandra.graphs.chandra_graph import build_graph
from langgraph.checkpoint.memory import MemorySaver
import uuid

graph = build_graph(checkpointer=MemorySaver())  # skip Postgres for quick       

run_id = str(uuid.uuid4())
final_state = graph.invoke(
    {
        "run_id": run_id,
        "account_id": "827295473120",
        "regions": ["us-east-1"],
        "raw_findings": {},
        "errors": [],
    },
    config={"configurable": {"thread_id": run_id}},
)
print(final_state)