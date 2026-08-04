from src.chandra.config import settings
from digitalworker_agents.aws_execution_agent import ExecutionAgents
import uuid
from typing import Any

print("Testing ExecutionAgents with Bedrock...")
print(f"Provider in settings: {settings.llm_provider}")

job_id = str(uuid.uuid4())
try:
    agent = ExecutionAgents(job_id=job_id, max_iterations=1)
    llm = agent.Llm
    print(f"Agent instantiated successfully.")
    print(f"LLM Class inside Agent: {llm.__class__.__name__}")
    print(f"LLM Model ID inside Agent: {getattr(llm, 'model_id', getattr(llm, 'model_name', 'unknown'))}")
except Exception as e:
    print(f"Failed to instantiate agent: {e}")
