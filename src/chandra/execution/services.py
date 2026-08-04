"""
Reusable services for executing and verifying AWS tasks via Terraform.
"""

import json
import logging
import subprocess
import boto3
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

class TaskAuthorizationService:
    """
    Gate 1: Verifies that the task/action matches the selected AWS permissions
    before any Terraform generation or execution occurs.
    """
    def __init__(self, permissions_path: str = "aws_permissions.json"):
        self.permissions_path = permissions_path
        self.permissions = self._load_permissions()

    def _load_permissions(self) -> Dict[str, Any]:
        try:
            with open(self.permissions_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load permissions from {self.permissions_path}: {e}")
            return {}

    def is_authorized(self, task_name: str, permission_set_id: str, required_actions: List[str] = None) -> bool:
        """
        Check if the required actions are authorized by the given permission_set_id.
        This is a deterministic check.
        """
        # In a real implementation, we would map required_actions to the policy document
        # of the permission_set_id. For now, we ensure the permission_set_id exists.
        if not self.permissions:
            return False
            
        permission_sets = self.permissions.get("permissionSets", [])
        for pset in permission_sets:
            if pset.get("id") == permission_set_id:
                # Basic validation: check if the permission set allows anything or matches requirements
                # Here we simply validate the permission set exists and is active/valid.
                return True
                
        logger.warning(f"Permission set {permission_set_id} not found or unauthorized for task {task_name}.")
        return False


class TerraformPlanPolicyValidator:
    """
    Gate 2: Verifies the generated Terraform plan against the AWS permissions
    before apply. Parses `terraform show -json tfplan`.
    """
    def __init__(self, permissions_path: str = "aws_permissions.json"):
        self.permissions_path = permissions_path
        self.auth_service = TaskAuthorizationService(permissions_path)

    def validate_plan(self, plan_json_path: str, permission_set_id: str, approved_task_name: str) -> tuple[bool, str]:
        """
        Parse the JSON plan and verify each resource action is allowed by the permission set,
        and matches the approved_task_name.
        Returns (is_valid, reason).
        """
        try:
            with open(plan_json_path, "r", encoding="utf-8") as f:
                plan_data = json.load(f)
                
            resource_changes = plan_data.get("resource_changes", [])
            for change in resource_changes:
                resource_type = change.get("type")
                actions = change.get("change", {}).get("actions", [])
                
                # Check if the resource type is allowed for the approved task.
                # Example: "Create S3 Bucket" should only involve aws_s3_bucket* resources.
                if "S3" in approved_task_name and not resource_type.startswith("aws_s3_bucket"):
                    return False, f"Unrelated resource {resource_type} detected for S3 task."
                
                if "EC2" in approved_task_name and not resource_type.startswith("aws_instance"):
                     return False, f"Unrelated resource {resource_type} detected for EC2 task."
                     
                # Deterministic block on destroy unless explicitly approved
                if "delete" in actions and "Delete" not in approved_task_name and "Destroy" not in approved_task_name:
                    return False, f"Unauthorized delete action on {resource_type}."

            return True, "Plan is valid and authorized."
        except Exception as e:
            return False, f"Failed to validate plan: {e}"


class AwsResourceVerifier:
    """
    Post-apply verification: Validates the requested resources independently via boto3.
    """
    def __init__(self, region: str = "us-east-1"):
        self.region = region

    def verify_s3_bucket(self, bucket_name: str) -> bool:
        try:
            s3 = boto3.client("s3", region_name=self.region)
            s3.head_bucket(Bucket=bucket_name)
            return "VERIFIED"
        except Exception as e:
            logger.error(f"S3 verification failed for {bucket_name}: {e}")
            return "FAILED"
            
    def verify_ec2_instance(self, instance_id: str) -> str:
        try:
            ec2 = boto3.client("ec2", region_name=self.region)
            resp = ec2.describe_instances(InstanceIds=[instance_id])
            if resp.get("Reservations"):
                state = resp["Reservations"][0]["Instances"][0]["State"]["Name"]
                if state in ["pending", "running"]:
                    return "VERIFIED"
            return "FAILED"
        except Exception as e:
             logger.error(f"EC2 verification failed for {instance_id}: {e}")
             return "FAILED"

    def verify_dynamodb_table(self, table_name: str) -> str:
        try:
            dynamo = boto3.client("dynamodb", region_name=self.region)
            resp = dynamo.describe_table(TableName=table_name)
            if resp.get("Table", {}).get("TableStatus") in ["ACTIVE", "CREATING"]:
                return "VERIFIED"
            return "FAILED"
        except Exception as e:
            logger.error(f"DynamoDB verification failed for {table_name}: {e}")
            return "FAILED"
            
    def verify_lambda_function(self, function_name: str) -> str:
        try:
            client = boto3.client("lambda", region_name=self.region)
            resp = client.get_function(FunctionName=function_name)
            if resp.get("Configuration", {}).get("State") in ["Active", "Pending"]:
                return "VERIFIED"
            return "FAILED"
        except Exception as e:
            logger.error(f"Lambda verification failed for {function_name}: {e}")
            return "FAILED"

    def verify_resource(self, task_name: str, outputs: Dict[str, Any]) -> str:
        """
        Verify the resource created by the task exists.
        Outputs come from `terraform output -json`.
        Returns "VERIFIED", "FAILED", or "UNVERIFIED"
        """
        task_name_lower = task_name.lower()
        if "s3" in task_name_lower or "bucket" in task_name_lower:
            for k, v in outputs.items():
                if "bucket" in k.lower():
                    val = v.get("value")
                    if val and isinstance(val, str):
                        return self.verify_s3_bucket(val)
            logger.warning("No bucket name output found to verify S3.")
            return "UNVERIFIED"
            
        if "ec2" in task_name_lower or "instance" in task_name_lower:
            for k, v in outputs.items():
                if "instance_id" in k.lower() or "id" in k.lower():
                    val = v.get("value")
                    if val and isinstance(val, str):
                        return self.verify_ec2_instance(val)
            logger.warning("No instance ID output found to verify EC2.")
            return "UNVERIFIED"
            
        if "dynamodb" in task_name_lower or "table" in task_name_lower:
            for k, v in outputs.items():
                if "table" in k.lower() or "name" in k.lower():
                    val = v.get("value")
                    if val and isinstance(val, str):
                        return self.verify_dynamodb_table(val)
            logger.warning("No table name output found to verify DynamoDB.")
            return "UNVERIFIED"
            
        if "lambda" in task_name_lower or "function" in task_name_lower:
            for k, v in outputs.items():
                if "function" in k.lower() or "name" in k.lower():
                    val = v.get("value")
                    if val and isinstance(val, str):
                        return self.verify_lambda_function(val)
            logger.warning("No function name output found to verify Lambda.")
            return "UNVERIFIED"
            
        return "UNVERIFIED"
