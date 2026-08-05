import json
import os
import tempfile
import pytest
from src.chandra.execution.services import TerraformPlanPolicyValidator

@pytest.fixture
def validator():
    # Use a dummy permissions file path to avoid file load errors, since we're testing the plan validation part
    return TerraformPlanPolicyValidator()

def write_plan(resources, actions=None):
    if actions is None:
        actions = ["create"]
    
    changes = []
    for r in resources:
        changes.append({
            "type": r,
            "change": {
                "actions": actions
            }
        })
    plan_data = {"resource_changes": changes}
    
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(plan_data, f)
    return path

def test_ec2_with_legitimate_dependencies(validator):
    plan_path = write_plan([
        "aws_instance", 
        "aws_key_pair", 
        "tls_private_key", 
        "local_file", 
        "aws_security_group",
        "random_id"
    ])
    try:
        is_valid, reason = validator.validate_plan(plan_path, "EC2_OPERATOR", "CREATE EC2 INSTANCE")
        assert is_valid is True, f"Failed: {reason}"
    finally:
        os.remove(plan_path)

def test_ec2_with_unrelated_resource_fails(validator):
    plan_path = write_plan(["aws_instance", "aws_db_instance"])
    try:
        is_valid, reason = validator.validate_plan(plan_path, "EC2_OPERATOR", "CREATE EC2 INSTANCE")
        assert is_valid is False
        assert "Unrelated resource aws_db_instance detected" in reason
    finally:
        os.remove(plan_path)

def test_ec2_with_unauthorized_iam_fails(validator):
    plan_path = write_plan(["aws_instance", "aws_iam_role"])
    try:
        is_valid, reason = validator.validate_plan(plan_path, "EC2_OPERATOR", "CREATE EC2 INSTANCE")
        assert is_valid is False
        assert "Unrelated resource aws_iam_role detected" in reason
    finally:
        os.remove(plan_path)

def test_s3_legitimate(validator):
    plan_path = write_plan(["aws_s3_bucket", "aws_s3_bucket_policy", "aws_s3_bucket_public_access_block"])
    try:
        is_valid, reason = validator.validate_plan(plan_path, "S3_FULL_ACCESS", "CREATE S3 BUCKET")
        assert is_valid is True, f"Failed: {reason}"
    finally:
        os.remove(plan_path)

def test_s3_unrelated(validator):
    plan_path = write_plan(["aws_s3_bucket", "aws_instance"])
    try:
        is_valid, reason = validator.validate_plan(plan_path, "S3_FULL_ACCESS", "CREATE S3 BUCKET")
        assert is_valid is False
        assert "Unrelated resource aws_instance detected" in reason
    finally:
        os.remove(plan_path)

def test_destructive_plan_blocked(validator):
    plan_path = write_plan(["aws_instance"], actions=["delete"])
    try:
        is_valid, reason = validator.validate_plan(plan_path, "EC2_OPERATOR", "CREATE EC2 INSTANCE")
        assert is_valid is False
        assert "Unauthorized delete action" in reason
    finally:
        os.remove(plan_path)

def test_destructive_plan_allowed_if_task_is_delete(validator):
    plan_path = write_plan(["aws_instance"], actions=["delete"])
    try:
        is_valid, reason = validator.validate_plan(plan_path, "EC2_OPERATOR", "DELETE EC2 INSTANCE")
        assert is_valid is True, f"Failed: {reason}"
    finally:
        os.remove(plan_path)
