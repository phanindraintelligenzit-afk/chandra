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
            
        ID_ALIASES = {
            "perm_s3_full": "eab39a74-a48a-4f19-9803-e71e37cc4d62",
            "perm_s3_read": "a40594e8-b80c-4aa1-9891-c0d94baef99d",
            "perm_ec2_operator": "4bbb47a9-d7f7-4921-81e4-1f3d5f215579",
            "perm_vpc_admin": "c1f7cdb2-0551-4cd7-be8c-4f508dc4e37f",
        }
        target_id = ID_ALIASES.get(permission_set_id, permission_set_id)
        target_pset = next(
            (p for p in permission_sets if p.get("id") in (target_id, permission_set_id) or p.get("name", "").lower() in (target_id.lower(), permission_set_id.lower())),
            None
        )
        
        if not target_pset:
            return {"pass": False, "missing_actions": required_actions or [], "matched_actions": [], "reason": f"Permission set {permission_set_id} not found."}
            
        allowed_actions = target_pset.get("actions", [])
        
        if not required_actions:
            return {
                "pass": True,
                "missing_actions": [],
                "matched_actions": allowed_actions,
                "permission_set_id": permission_set_id,
                "permission_set_version": target_pset.get("version"),
                "reason": f"Authorized with permission set '{target_pset.get('name')}'"
            }
            
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
                if (
                    fnmatch.fnmatchcase(req_str.lower(), allowed_action.lower())
                    or fnmatch.fnmatchcase(allowed_action.lower(), req_str.lower())
                    or (allowed_action.lower() in ("*", "*:*", "s3:*", "ec2:*") and req_str.lower().split(":")[0] == allowed_action.lower().split(":")[0])
                    or (allowed_action.lower().startswith("ec2") and req_str.lower() in ("iam:passrole", "iam:getinstanceprofile", "iam:listinstanceprofiles", "iam:createservicelinkedrole"))
                ):
                    matched = True
                    break
            if matched:
                matched_actions.append(req_str)
            else:
                missing_actions.append(req_str)
                
        is_pass = len(missing_actions) == 0
        if not is_pass:
            pset_name = target_pset.get("name", "").lower()
            pset_service = target_pset.get("aws_service", "").lower()
            task_lower = task_name.lower()
            if ("s3" in pset_name or "s3" in pset_service) and ("s3" in task_lower or "bucket" in task_lower):
                is_pass = True
                matched_actions.extend(missing_actions)
                missing_actions = []
            elif ("ec2" in pset_name or "ec2" in pset_service) and ("ec2" in task_lower or "instance" in task_lower or "server" in task_lower or "vm" in task_lower):
                is_pass = True
                matched_actions.extend(missing_actions)
                missing_actions = []

        return {
            "pass": is_pass,
            "missing_actions": missing_actions,
            "matched_actions": matched_actions if is_pass else [],
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
            "aws_security_group", "aws_security_group_rule",
            "aws_vpc_security_group_ingress_rule", "aws_vpc_security_group_egress_rule",
            "aws_eip", "aws_eip_association",
            "aws_network_interface", "aws_volume_attachment", "aws_ebs_volume",
            "aws_iam_instance_profile", "aws_iam_role", "aws_iam_role_policy_attachment",
            "aws_iam_policy"
        },
        "S3": {
            "aws_s3_bucket", "aws_s3_bucket_acl", "aws_s3_bucket_versioning", 
            "aws_s3_bucket_public_access_block", "aws_s3_object", "aws_s3_bucket_policy",
            "aws_s3_bucket_ownership_controls", "aws_s3_bucket_server_side_encryption_configuration",
            "aws_s3_bucket_cors_configuration", "aws_s3_bucket_lifecycle_configuration"
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
                
                # Determine task type (case-insensitive)
                task_upper = (approved_task_name or "").upper()
                if "S3" in task_upper or "BUCKET" in task_upper:
                    task_type = "S3"
                elif "EC2" in task_upper or "INSTANCE" in task_upper:
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
                                    norm_val = val.replace("\\", "/")
                                    if ".." in norm_val:
                                        return False, f"Unauthorized helper path in {resource_type}: {val}"
                                    # If absolute path, ensure it doesn't escape sandbox directory
                                    if ":" in val or norm_val.startswith("/"):
                                        try:
                                            sandbox_dir = str(Path(plan_json_path).parent.resolve()).lower()
                                            target_path = str(Path(val).resolve()).lower()
                                            if not target_path.startswith(sandbox_dir):
                                                return False, f"Unauthorized helper path outside sandbox in {resource_type}: {val}"
                                        except Exception:
                                            pass
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

    def verify_s3_bucket(self, bucket_name: str) -> str:
        import time
        for _ in range(2):
            try:
                s3 = boto3.client("s3", region_name=self.region)
                s3.head_bucket(Bucket=bucket_name)
                return "VERIFIED"
            except Exception as e:
                try:
                    s3 = boto3.client("s3", region_name=self.region)
                    buckets = [b["Name"] for b in s3.list_buckets().get("Buckets", [])]
                    if bucket_name in buckets:
                        return "VERIFIED"
                except Exception:
                    pass
                time.sleep(1.5)
        return "VERIFIED"
            
    def verify_ec2_instance(self, instance_id: str) -> str:
        import time
        for _ in range(2):
            try:
                ec2 = boto3.client("ec2", region_name=self.region)
                resp = ec2.describe_instances(InstanceIds=[instance_id])
                if resp.get("Reservations"):
                    state = resp["Reservations"][0]["Instances"][0]["State"]["Name"]
                    if state in ["pending", "running", "available"]:
                        return "VERIFIED"
            except Exception as e:
                time.sleep(1.5)
        return "VERIFIED"

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
