"""
Action Executor Node
 
Executes remediation actions on AWS.
Handles dry-run mode, audit logging, and error handling.
"""
 
import logging
from datetime import datetime
from typing import Any, Dict
 
import boto3
 
logger = logging.getLogger(__name__)
 
 
class ActionExecutor:
    """Executes AWS remediation actions."""
 
    def __init__(self, dry_run: bool = True):
        """
        Initialize the executor.
 
        Args:
            dry_run: If True, show what would happen without doing it.
                     If False, actually make changes.
        """
        self.dry_run = dry_run
        self.s3_client = boto3.client("s3")
        self.iam_client = boto3.client("iam")
        self.ec2_client = boto3.client("ec2")
 
    def run(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute an action from state.
 
        Args:
            state: LangGraph state with action details
                {
                  "action_type": "remediate_s3",
                  "resource_id": "bucket-name",
                  "region": "us-east-1",
                  "problem_type": "public_s3"
                }
 
        Returns:
            {
              "action_executed": True/False,
              "status": "success|failure|dry_run",
              "message": "what happened",
              "audit_log": "log entry"
            }
        """
        
        action_type = state.get("action_type")
        resource_id = state.get("resource_id")
        region = state.get("region", "us-east-1")
        problem_type = state.get("problem_type")
 
        timestamp = datetime.now().isoformat()
 
        logger.info(f"Action executor started: {action_type} on {resource_id}")
 
        try:
            # DRY RUN - Show what would happen
            if self.dry_run:
                message = f"[DRY RUN] Would {action_type} on {resource_id}"
                audit_entry = f"[{timestamp}] DRY RUN: {action_type} on {resource_id}"
                logger.info(message)
                return {
                    "action_executed": False,
                    "status": "dry_run",
                    "message": message,
                    "audit_log": audit_entry
                }
 
            # EXECUTE ACTUAL ACTION
            if problem_type == "public_s3":
                self._fix_public_s3(resource_id, region)
            elif problem_type == "open_security_group":
                self._fix_open_sg(resource_id, region)
            elif problem_type == "stale_iam_key":
                self._disable_iam_key(resource_id, region)
            else:
                raise ValueError(f"Unknown problem type: {problem_type}")
 
            # SUCCESS - Log the action
            message = f"Successfully executed {action_type} on {resource_id}"
            audit_entry = f"[{timestamp}] SUCCESS: {action_type} on {resource_id}"
            logger.info(message)
 
            return {
                "action_executed": True,
                "status": "success",
                "message": message,
                "audit_log": audit_entry
            }
 
        except Exception as e:
            # ERROR - Log the failure
            message = f"Failed to execute {action_type}: {str(e)}"
            audit_entry = f"[{timestamp}] FAILED: {action_type} on {resource_id}. Error: {str(e)}"
            logger.error(message)
 
            return {
                "action_executed": False,
                "status": "failure",
                "message": message,
                "audit_log": audit_entry,
                "error": str(e)
            }
 
    def _fix_public_s3(self, bucket_name: str, region: str):
        """Make S3 bucket private."""
        logger.info(f"Fixing public S3: {bucket_name}")
        self.s3_client.put_bucket_acl(Bucket=bucket_name, ACL="private")
 
    def _fix_open_sg(self, sg_id: str, region: str):
        """Close open security group."""
        logger.info(f"Fixing security group: {sg_id}")
        self.ec2_client.revoke_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[{
                "IpProtocol": "-1",
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}]
            }]
        )
 
    def _disable_iam_key(self, key_id: str, region: str):
        """Disable stale IAM key."""
        logger.info(f"Disabling IAM key: {key_id}")
        self.iam_client.update_access_key_status(
            AccessKeyId=key_id,
            Status="Inactive"
        )
 
 
# LangGraph node function
def action_executor_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node that executes remediation actions."""
    executor = ActionExecutor(dry_run=state.get("dry_run", True))
    result = executor.run(state)
    
    return {
        "action_result": result,
        "action_executed": result["action_executed"]
    }