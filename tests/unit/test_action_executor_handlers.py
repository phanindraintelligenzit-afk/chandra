"""Unit tests for the auto-remediation handlers added for the remaining
KRA detectors (COST-003, COMP-005, COMP-008, COMP-009, REL-002, SEC-009).

Same shape as ``test_action_executor_node.py``: exercise the registry
dispatch through ``action_executor_node`` (dry-run + real-run via moto)
plus the new ARN parsers in isolation.
"""

from __future__ import annotations

import boto3
import pytest
from src.chandra.briefing.schemas import ProposedWrite
from src.chandra.graphs.action_nodes import action_executor_node
from src.chandra.graphs.action_nodes.action_executor import (
    _eip_alloc_from_arn,
    _identity_arn,
    _kms_key_id_from_arn,
    _region_scope_from_arn,
)
from src.chandra.graphs.state import ChandraState


def _write(detector_id: str, target_arn: str, region: str = "us-east-1") -> ProposedWrite:
    return ProposedWrite(
        action=f"remediate_{detector_id}",
        target_arn=target_arn,
        region=region,
        payload={"recommendation": "fix it"},
        requested_by="decision_router",
        justification="auto",
        risk_level="low",
    )


def _state(auto_fixed: list[ProposedWrite], **extra: object) -> ChandraState:
    state: ChandraState = {"run_id": "test-run", "account_id": "123456789012"}
    state["auto_fixed"] = auto_fixed
    state.update(extra)  # type: ignore[typeddict-unknown-key]
    return state


# ---------------------------------------------------------------------------
# New ARN parsers
# ---------------------------------------------------------------------------


class TestNewArnParsers:
    def test_kms_key(self) -> None:
        arn = "arn:aws:kms:us-east-1:123:key/1234abcd-12ab-34cd-56ef-1234567890ab"
        assert _kms_key_id_from_arn(arn) == "1234abcd-12ab-34cd-56ef-1234567890ab"

    def test_kms_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid KMS key ARN"):
            _kms_key_id_from_arn("arn:aws:kms:us-east-1:123:alias/foo")

    def test_eip_alloc(self) -> None:
        arn = "arn:aws:ec2:us-east-1:123:address/eipalloc-0abc123"
        assert _eip_alloc_from_arn(arn) == "eipalloc-0abc123"

    def test_eip_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid Elastic IP ARN"):
            _eip_alloc_from_arn("arn:aws:ec2:us-east-1:123:instance/i-1")

    def test_region_scope(self) -> None:
        arn = "arn:aws:ec2:eu-west-1:123:ebs-encryption-by-default"
        assert _region_scope_from_arn(arn) == "eu-west-1"

    def test_region_scope_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid region-scoped ARN"):
            _region_scope_from_arn("arn:aws:ec2")

    def test_identity_passthrough(self) -> None:
        arn = "arn:aws:rds:us-east-1:123:db:mydb"
        assert _identity_arn(arn) == arn

    def test_identity_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid ARN"):
            _identity_arn("not-an-arn")


# ---------------------------------------------------------------------------
# Dry-run dispatch — every new detector routes through the registry
# ---------------------------------------------------------------------------


class TestDryRunDispatch:
    @pytest.mark.parametrize(
        ("detector_id", "arn"),
        [
            ("COST-003-unused-eip", "arn:aws:ec2:us-east-1:123:address/eipalloc-1"),
            ("COMP-005-s3-default-enc", "arn:aws:s3:::my-bucket"),
            ("COMP-008-ebs-default-enc-off", "arn:aws:ec2:us-east-1:123:ebs-encryption-by-default"),
            ("COMP-009-missing-tags", "arn:aws:rds:us-east-1:123:db:mydb"),
            ("REL-002-s3-versioning", "arn:aws:s3:::my-bucket"),
            ("SEC-009-kms-rotation", "arn:aws:kms:us-east-1:123:key/abcd-1234"),
        ],
    )
    def test_dispatches_dry_run(self, detector_id: str, arn: str) -> None:
        result = action_executor_node(_state([_write(detector_id, arn)], dry_run=True))
        r = result["action_results"][0]
        assert r.status == "dry_run", f"{detector_id} did not dispatch"
        assert r.dry_run is True


# ---------------------------------------------------------------------------
# Real-run remediations (moto)
# ---------------------------------------------------------------------------


def test_comp_005_enables_s3_default_encryption(s3: object) -> None:
    s3.create_bucket(Bucket="enc-bucket")
    write = _write("COMP-005-s3-default-enc", "arn:aws:s3:::enc-bucket")

    result = action_executor_node(_state([write], dry_run=False))
    assert result["action_results"][0].status == "success"
    conf = s3.get_bucket_encryption(Bucket="enc-bucket")
    rules = conf["ServerSideEncryptionConfiguration"]["Rules"]
    assert rules[0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"


def test_rel_002_enables_s3_versioning(s3: object) -> None:
    s3.create_bucket(Bucket="ver-bucket")
    write = _write("REL-002-s3-versioning", "arn:aws:s3:::ver-bucket")

    result = action_executor_node(_state([write], dry_run=False))
    assert result["action_results"][0].status == "success"
    assert s3.get_bucket_versioning(Bucket="ver-bucket").get("Status") == "Enabled"


def test_sec_009_enables_kms_rotation(aws: None) -> None:
    kms = boto3.client("kms", region_name="us-east-1")
    key = kms.create_key()
    key_id = key["KeyMetadata"]["KeyId"]
    arn = f"arn:aws:kms:us-east-1:123456789012:key/{key_id}"
    write = _write("SEC-009-kms-rotation", arn)

    result = action_executor_node(_state([write], dry_run=False))
    assert result["action_results"][0].status == "success"
    assert kms.get_key_rotation_status(KeyId=key_id)["KeyRotationEnabled"] is True


def test_comp_008_enables_ebs_encryption_by_default(ec2: object) -> None:
    assert ec2.get_ebs_encryption_by_default()["EbsEncryptionByDefault"] is False
    write = _write(
        "COMP-008-ebs-default-enc-off",
        "arn:aws:ec2:us-east-1:123:ebs-encryption-by-default",
    )
    result = action_executor_node(_state([write], dry_run=False))
    assert result["action_results"][0].status == "success"
    assert ec2.get_ebs_encryption_by_default()["EbsEncryptionByDefault"] is True


def test_cost_003_releases_unassociated_eip(ec2: object) -> None:
    alloc = ec2.allocate_address(Domain="vpc")
    alloc_id = alloc["AllocationId"]
    arn = f"arn:aws:ec2:us-east-1:123:address/{alloc_id}"
    write = _write("COST-003-unused-eip", arn)

    result = action_executor_node(_state([write], dry_run=False))
    assert result["action_results"][0].status == "success"
    remaining = ec2.describe_addresses().get("Addresses", [])
    assert all(a.get("AllocationId") != alloc_id for a in remaining)


def test_cost_003_refuses_associated_eip(ec2: object) -> None:
    """If the EIP is associated to an instance at remediation time, refuse."""
    images = ec2.describe_images().get("Images", [])
    ami = images[0]["ImageId"] if images else "ami-12345678"
    inst = ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1)
    instance_id = inst["Instances"][0]["InstanceId"]
    alloc = ec2.allocate_address(Domain="vpc")
    alloc_id = alloc["AllocationId"]
    ec2.associate_address(InstanceId=instance_id, AllocationId=alloc_id)

    arn = f"arn:aws:ec2:us-east-1:123:address/{alloc_id}"
    result = action_executor_node(_state([_write("COST-003-unused-eip", arn)], dry_run=False))
    r = result["action_results"][0]
    assert r.status == "failure"
    assert r.error is not None and "in use" in r.error
    # The EIP must still exist — nothing was released.
    remaining = ec2.describe_addresses().get("Addresses", [])
    assert any(a.get("AllocationId") == alloc_id for a in remaining)
