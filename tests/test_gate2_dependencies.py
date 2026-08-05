import json
import os
import tempfile
import pytest
from src.chandra.execution.services import TerraformPlanPolicyValidator

@pytest.fixture
def validator():
    return TerraformPlanPolicyValidator()

def write_plan(resources_with_after, actions=None):
    if actions is None:
        actions = ["create"]
    
    changes = []
    for r in resources_with_after:
        if isinstance(r, str):
            r_type = r
            after = {}
        else:
            r_type, after = r

        changes.append({
            "type": r_type,
            "change": {
                "actions": actions,
                "after": after
            }
        })
    plan_data = {"resource_changes": changes}
    
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(plan_data, f)
    return path

def test_ec2_with_legitimate_dependencies(validator):
    # EC2 + random_id + aws_instance -> PASS
    plan_path = write_plan([
        "aws_instance", 
        "aws_key_pair", 
        "aws_security_group",
        "random_id",
        ("local_file", {"filename": "key.pem"})
    ])
    try:
        is_valid, reason = validator.validate_plan(plan_path, "EC2_OPERATOR", "CREATE EC2 INSTANCE")
        assert is_valid is True, f"Failed: {reason}"
    finally:
        os.remove(plan_path)

def test_s3_legitimate(validator):
    # S3 + random_id + aws_s3_bucket -> PASS
    plan_path = write_plan(["aws_s3_bucket", "aws_s3_bucket_policy", "random_id"])
    try:
        is_valid, reason = validator.validate_plan(plan_path, "S3_FULL_ACCESS", "CREATE S3 BUCKET")
        assert is_valid is True, f"Failed: {reason}"
    finally:
        os.remove(plan_path)

def test_ec2_with_unrelated_resource_fails(validator):
    # EC2 + aws_db_instance -> BLOCK
    plan_path = write_plan(["aws_instance", "aws_db_instance"])
    try:
        is_valid, reason = validator.validate_plan(plan_path, "EC2_OPERATOR", "CREATE EC2 INSTANCE")
        assert is_valid is False
        assert "Unrelated resource aws_db_instance detected" in reason
    finally:
        os.remove(plan_path)

def test_ec2_with_unauthorized_iam_fails(validator):
    # EC2 + unauthorized aws_iam_role -> BLOCK
    plan_path = write_plan(["aws_instance", "aws_iam_role"])
    try:
        is_valid, reason = validator.validate_plan(plan_path, "EC2_OPERATOR", "CREATE EC2 INSTANCE")
        assert is_valid is False
        assert "Unrelated resource aws_iam_role detected" in reason
    finally:
        os.remove(plan_path)

def test_unknown_provider_fails(validator):
    # unknown Terraform provider/resource -> BLOCK
    plan_path = write_plan(["aws_instance", "azurerm_virtual_machine"])
    try:
        is_valid, reason = validator.validate_plan(plan_path, "EC2_OPERATOR", "CREATE EC2 INSTANCE")
        assert is_valid is False
        assert "Unrelated resource azurerm_virtual_machine detected" in reason
    finally:
        os.remove(plan_path)

def test_helper_does_not_mask_unauthorized(validator):
    # helper resource must not make an unauthorized AWS resource acceptable
    plan_path = write_plan(["aws_instance", "random_id", "aws_db_instance"])
    try:
        is_valid, reason = validator.validate_plan(plan_path, "EC2_OPERATOR", "CREATE EC2 INSTANCE")
        assert is_valid is False
        assert "Unrelated resource aws_db_instance detected" in reason
    finally:
        os.remove(plan_path)

def test_helper_path_containing_dotdot_fails(validator):
    # helper file path containing ../ -> BLOCK
    plan_path = write_plan([
        "aws_instance",
        ("local_file", {"filename": "../key.pem"})
    ])
    try:
        is_valid, reason = validator.validate_plan(plan_path, "EC2_OPERATOR", "CREATE EC2 INSTANCE")
        assert is_valid is False
        assert "Unauthorized helper path in local_file: ../key.pem" in reason
    finally:
        os.remove(plan_path)

def test_helper_absolute_path_fails(validator):
    # helper absolute path outside sandbox -> BLOCK
    plan_path = write_plan([
        "aws_instance",
        ("local_sensitive_file", {"filename": "/etc/shadow"})
    ])
    try:
        is_valid, reason = validator.validate_plan(plan_path, "EC2_OPERATOR", "CREATE EC2 INSTANCE")
        assert is_valid is False
        assert "Unauthorized helper path in local_sensitive_file: /etc/shadow" in reason
    finally:
        os.remove(plan_path)

def test_pure_delete_plan_blocked(validator):
    # pure destructive AWS plan remains BLOCKED for a CREATE task
    plan_path = write_plan(["aws_instance"], actions=["delete"])
    try:
        is_valid, reason = validator.validate_plan(plan_path, "EC2_OPERATOR", "CREATE EC2 INSTANCE")
        assert is_valid is False
        assert "Unauthorized delete action" in reason
    finally:
        os.remove(plan_path)

def test_replacement_plan_allowed(validator):
    # a replacement (create + delete) is allowed for a CREATE/UPDATE task
    plan_path1 = write_plan(["aws_instance"], actions=["create", "delete"])
    plan_path2 = write_plan(["aws_instance"], actions=["delete", "create"])
    try:
        is_valid1, _ = validator.validate_plan(plan_path1, "EC2_OPERATOR", "CREATE EC2 INSTANCE")
        is_valid2, _ = validator.validate_plan(plan_path2, "EC2_OPERATOR", "CREATE EC2 INSTANCE")
        assert is_valid1 is True
        assert is_valid2 is True
    finally:
        os.remove(plan_path1)
        os.remove(plan_path2)

def test_pure_delete_plan_allowed_if_task_is_delete(validator):
    plan_path = write_plan(["aws_instance"], actions=["delete"])
    try:
        is_valid, reason = validator.validate_plan(plan_path, "EC2_OPERATOR", "DELETE EC2 INSTANCE")
        assert is_valid is True, f"Failed: {reason}"
    finally:
        os.remove(plan_path)
