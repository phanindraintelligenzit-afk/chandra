import sys
import os
import logging
from unittest.mock import patch
from digitalworker_agents.aws_execution_agent import ExecutionAgents

# Set to bedrock so generation works
os.environ["LLM_PROVIDER"] = "bedrock"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

tasks = [
    {
        "name": "Create S3 bucket",
        "desc": "Provision a new S3 bucket named 'chandra-test-bucket' in us-east-1",
        "service": "S3"
    },
    {
        "name": "Create EC2 Instance",
        "desc": "Provision a t3.micro EC2 instance named 'chandra-test-ec2' in us-east-1",
        "service": "EC2"
    },
    {
        "name": "Create VPC",
        "desc": "Provision a VPC with 1 public and 1 private subnet in us-east-1",
        "service": "VPC"
    },
    {
        "name": "Create Lambda Function",
        "desc": "Provision a Python 3.9 Lambda function named 'chandra-test-lambda'",
        "service": "Lambda"
    }
]

permission_set = "arn:aws:sso:::permissionSet/ssoins-123/ps-123"

def mock_execute_terraform_node(self, state):
    # Mocking actual terraform apply to prevent creating expensive real resources
    logger.info("MOCKED _execute_terraform_node: Skipping real AWS terraform apply.")
    return {"validate_passed": True, "sandbox_path": state.get("sandbox_path", "")}

def run_tests():
    with patch.object(ExecutionAgents, '_execute_terraform_node', mock_execute_terraform_node):
        for task in tasks:
            print(f"\n{'='*50}\nTesting {task['name']}\n{'='*50}")
            agent = ExecutionAgents(max_iterations=1, job_id=f"test_{task['service'].lower()}")
            
            action_dict = {
                "actionName": task["name"],
                "actionDescription": task["desc"],
                "service": task["service"],
                "region": "us-east-1",
                "isAwsTask": True,
                "action_type": "AWS_TASK"
            }
            
            response = agent.RunPipeline(
                action=action_dict,
                aws_permissions=[permission_set],
                command_timeout=300
            )
            
            print(f"[{task['name']}] Status Code: {response.statusCode}")
            print(f"[{task['name']}] Status: {response.status}")
            print(f"[{task['name']}] Message: {response.summary or response.exception}")

if __name__ == "__main__":
    run_tests()
