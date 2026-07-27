import asyncio
import json
from digitalworker_agents.aws_execution_agent import ExecutionAgents

async def main():
    agent = ExecutionAgents()
    
    test_cases = [
        {"actionName": "Provision S3 Bucket", "actionDescription": "Create a secure S3 bucket with versioning and SSE."},
        {"actionName": "Provision Custom Resource", "actionDescription": "Create an Amazon OpenSearch Serverless collection (a custom KRA)."}
    ]
    
    for case in test_cases:
        print(f"\n=== Testing {case['actionName']} ===")
        state = {
            "action": case,
            "reference_files": [],
            "existing_files": [],
            "feedback_summary": "",
            "memory_context": "",
            "aws_context": ""
        }
        
        # We invoke _analyze_node. Note: we might need to mock or ensure the LLM is reachable.
        try:
            result = agent._analyze_node(state)
            analysis = result.get("analysis", {})
            print("Detected Services:", analysis.get("aws_services_involved"))
            print("Discovery Commands:", analysis.get("aws_discovery_commands"))
        except Exception as e:
            print("Error:", e)

if __name__ == "__main__":
    asyncio.run(main())
