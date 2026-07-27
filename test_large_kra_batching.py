import os
import json
from digitalworker_agents.aws_execution_agent import ExecutionAgents
import sys
import uuid

def run_test():
    # Setup test credentials
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    
    agent = ExecutionAgents(max_iterations=6)
    agent.job_id = f"test-kra-{uuid.uuid4().hex[:6]}"

    action_payload = {
        "actionName": "Deploy Complete Web Application Infrastructure",
        "actionDescription": (
            "Create a VPC with public and private subnets, an internet gateway, route tables, "
            "an application load balancer (ALB), an auto scaling group (ASG) of EC2 instances, "
            "an RDS PostgreSQL database, and a Redis cluster. Set up all required security groups "
            "and IAM roles for full application operation."
        ),
        "service": "Custom AWS Infrastructure",
        "kraCode": "KRA-TEST-BATCHING",
        "steps": [
            "1. Create a VPC, subnets, IGW, and route tables for networking.",
            "2. Create Security Groups for the ALB, EC2 instances, RDS, and ElastiCache.",
            "3. Create an IAM Role for the EC2 instances.",
            "4. Deploy an RDS PostgreSQL instance in the private subnets.",
            "5. Deploy an ElastiCache Redis cluster.",
            "6. Set up an ALB, Target Group, and Auto Scaling Group for the web tier.",
            "7. Output the ALB DNS name, RDS endpoint, and Redis endpoint."
        ],
    }
    
    print(f"=== STARTING TEST FOR JOB {agent.job_id} ===")
    
    # 1. Run the pipeline (this should generate the plan and hit Human Approval Center)
    response = agent.RunPipeline(
        action=action_payload,
        reference_folder="",
        command_timeout=300,
    )
    
    print("\n\n=== PIPELINE RESPONSE ===")
    print(json.dumps(response.model_dump(), indent=2))
    
    if response.status == "needs_clarification":
        print("\n\n=== PIPELINE PAUSED FOR HUMAN APPROVAL ===")
        
        # In a real scenario, the UI would capture the approval payload and wait for the user to click "Approve".
        # Then it would resume the pipeline with answers=["APPROVED"].
        
        print(f"\nResuming job {agent.job_id}...")
        
        resume_response = agent.RunPipeline(
            action=action_payload,
            thread_id=response.thread_id,
            answers=[{"status": "APPROVED", "message": "Looks good, proceed to apply."}],
            command_timeout=300,
        )
        
        print("\n\n=== RESUMED PIPELINE RESPONSE ===")
        print(json.dumps(resume_response.model_dump(), indent=2))
    else:
        print("\n\n=== PIPELINE FAILED TO PAUSE FOR HUMAN APPROVAL ===")
        print("Final Status:", response.status)

if __name__ == "__main__":
    run_test()
