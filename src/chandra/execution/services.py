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

    def is_authorized(self, task_name: str, permission_set_id: str, required_actions: List[str] = None) -> Dict[str, Any]:
        """
        Check if the required actions are authorized by the given permission_set_id.
        This is a deterministic check.
        """
        if not self.permissions:
            return {"pass": False, "missing_actions": required_actions or [], "matched_actions": [], "reason": "No permission sets found"}
            
        if isinstance(self.permissions, list):
            permission_sets = self.permissions
        elif isinstance(self.permissions, dict):
            permission_sets = self.permissions.get("permissionSets", [])
        else:
            permission_sets = []
            
        target_pset = next((p for p in permission_sets if p.get("id") == permission_set_id), None)
        
        if not target_pset:
            return {"pass": False, "missing_actions": required_actions or [], "matched_actions": [], "reason": f"Permission set {permission_set_id} not found."}
            
        allowed_actions = target_pset.get("actions", [])
        
        if not required_actions:
            return {"pass": False, "missing_actions": [], "matched_actions": [], "reason": "No required actions could be determined for this task. Execution blocked for safety."}
            
        matched_actions = []
        missing_actions = []
        
        import fnmatch
        for req_action in required_actions:
            # required_actions is usually a string, or it could be a dict if it comes straight from LLM.
            req_str = req_action.get("action") if isinstance(req_action, dict) else req_action
            if not req_str:
                continue
                
            matched = False
            for allowed_action in allowed_actions:
                if fnmatch.fnmatchcase(req_str.lower(), allowed_action.lower()):
                    matched = True
                    break
            if matched:
                matched_actions.append(req_str)
            else:
                missing_actions.append(req_str)
                
        is_pass = len(missing_actions) == 0
        return {
            "pass": is_pass,
            "missing_actions": missing_actions,
            "matched_actions": matched_actions,
            "permission_set_id": permission_set_id,
            "permission_set_version": target_pset.get("version"),
            "reason": "All required actions are covered by the permission set." if is_pass else f"Missing {len(missing_actions)} required actions."
        }


class TerraformPlanPolicyValidator:
    """
    Gate 2: Verifies the generated Terraform plan against the AWS permissions
    before apply. Parses `terraform show -json tfplan`.
    """
    
    ALLOWED_DEPENDENCIES = {
        "EC2": {
            "aws_instance", "aws_key_pair",
            "aws_security_group", "aws_security_group_rule", "aws_eip", 
            "aws_network_interface", "aws_volume_attachment", "aws_ebs_volume"
        },
        "S3": {
            "aws_s3_bucket", "aws_s3_bucket_acl", "aws_s3_bucket_versioning", 
            "aws_s3_bucket_public_access_block", "aws_s3_object", "aws_s3_bucket_policy"
        }
    }

    HELPER_RESOURCES = {
        "random_id", "random_string", "tls_private_key", "local_file", "local_sensitive_file"
    }

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
                
                # Determine task type
                if "S3" in approved_task_name:
                    task_type = "S3"
                elif "EC2" in approved_task_name:
                    task_type = "EC2"
                else:
                    task_type = None

                if task_type and task_type in self.ALLOWED_DEPENDENCIES:
                    allowed_res = self.ALLOWED_DEPENDENCIES[task_type]
                    if resource_type in self.HELPER_RESOURCES:
                        # Helper resource logic
                        after_props = change.get("change", {}).get("after", {}) or {}
                        # Validate that helper resources do not write files outside the sandbox
                        for key, val in after_props.items():
                            if isinstance(val, str) and key in ("filename", "content_base64"):
                                if "filename" in key:
                                    if ".." in val or val.startswith("/") or val.startswith("\\") or ":" in val:
                                        return False, f"Unauthorized helper path in {resource_type}: {val}"
                    elif resource_type not in allowed_res and not any(resource_type.startswith(ar) for ar in allowed_res):
                        return False, f"Unrelated resource {resource_type} detected for {task_type} task."
                elif task_type:
                    # Fallback for unknown tasks
                    if resource_type in self.HELPER_RESOURCES:
                        pass # Valid helper
                    elif not resource_type.startswith(f"aws_{task_type.lower()}"):
                         return False, f"Unrelated resource {resource_type} detected for {task_type} task."
                         
                # Deterministic block on pure destroy unless explicitly approved
                is_pure_delete = len(actions) == 1 and actions[0] == "delete"
                if is_pure_delete and "delete" not in approved_task_name.lower() and "destroy" not in approved_task_name.lower():
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
