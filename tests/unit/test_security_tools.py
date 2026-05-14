"""Unit tests for ``chandra.tools.security`` — driven by moto."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
import pytest

from chandra.tools import security
from chandra.tools.base import DetectorContext


# ---------------------------------------------------------------------------
# SEC-001 — public S3 buckets
# ---------------------------------------------------------------------------


class TestPublicS3:
    def test_bucket_with_no_public_access_block_is_flagged(
        self, ctx: DetectorContext, s3: Any
    ) -> None:
        s3.create_bucket(Bucket="leaky-bucket")
        findings = security.find_public_s3_buckets(ctx)
        assert len(findings) == 1
        f = findings[0]
        assert f.detector_id == "SEC-001-public-s3"
        assert f.resource_type == "AWS::S3::Bucket"
        assert "leaky-bucket" in f.resource_arn

    def test_bucket_with_full_block_is_not_flagged(
        self, ctx: DetectorContext, s3: Any
    ) -> None:
        s3.create_bucket(Bucket="locked-bucket")
        s3.put_public_access_block(
            Bucket="locked-bucket",
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        findings = security.find_public_s3_buckets(ctx)
        assert findings == []

    def test_bucket_policy_wildcard_is_flagged_critical(
        self, ctx: DetectorContext, s3: Any
    ) -> None:
        s3.create_bucket(Bucket="policy-bucket")
        s3.put_public_access_block(
            Bucket="policy-bucket",
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": "arn:aws:s3:::policy-bucket/*",
                }
            ],
        }
        s3.put_bucket_policy(Bucket="policy-bucket", Policy=json.dumps(policy))
        findings = security.find_public_s3_buckets(ctx)
        flagged = [f for f in findings if "policy-bucket" in f.resource_arn]
        assert len(flagged) == 1
        assert flagged[0].severity == "critical"


# ---------------------------------------------------------------------------
# SEC-002 — open security groups
# ---------------------------------------------------------------------------


class TestOpenSecurityGroups:
    def _make_sg_with_ingress(
        self, ec2: Any, port: int, cidr: str = "0.0.0.0/0"
    ) -> str:
        vpcs = ec2.describe_vpcs()["Vpcs"]
        vpc_id = vpcs[0]["VpcId"] if vpcs else ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
        sg = ec2.create_security_group(
            GroupName=f"open-{port}", Description="test", VpcId=vpc_id
        )
        sg_id = sg["GroupId"]
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": port,
                    "ToPort": port,
                    "IpRanges": [{"CidrIp": cidr}],
                }
            ],
        )
        return sg_id

    def test_open_ssh_is_critical(self, ctx: DetectorContext, ec2: Any) -> None:
        sg_id = self._make_sg_with_ingress(ec2, 22)
        findings = security.find_open_security_groups(ctx)
        matching = [f for f in findings if sg_id in f.resource_arn]
        assert len(matching) == 1
        assert matching[0].severity == "critical"
        assert "22" in matching[0].title

    def test_open_postgres_is_high(self, ctx: DetectorContext, ec2: Any) -> None:
        sg_id = self._make_sg_with_ingress(ec2, 5432)
        findings = security.find_open_security_groups(ctx)
        matching = [f for f in findings if sg_id in f.resource_arn]
        assert len(matching) == 1
        assert matching[0].severity == "high"

    def test_internal_cidr_is_not_flagged(
        self, ctx: DetectorContext, ec2: Any
    ) -> None:
        sg_id = self._make_sg_with_ingress(ec2, 22, cidr="10.0.0.0/8")
        findings = security.find_open_security_groups(ctx)
        assert all(sg_id not in f.resource_arn for f in findings)


# ---------------------------------------------------------------------------
# SEC-003 — stale access keys
# ---------------------------------------------------------------------------


class TestStaleAccessKeys:
    def test_key_older_than_90d_is_flagged(
        self, ctx: DetectorContext, iam: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        iam.create_user(UserName="alice")
        iam.create_access_key(UserName="alice")

        # Force "now" forward so the freshly-minted key is >90d old.
        future = datetime.now(timezone.utc) + timedelta(days=120)

        class _FakeDateTime(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
                return future

        monkeypatch.setattr(security, "datetime", _FakeDateTime)
        findings = security.find_stale_access_keys(ctx)
        assert len(findings) == 1
        assert findings[0].detector_id == "SEC-003-stale-key"
        assert "alice" in findings[0].title

    def test_recent_key_is_not_flagged(
        self, ctx: DetectorContext, iam: Any
    ) -> None:
        iam.create_user(UserName="bob")
        iam.create_access_key(UserName="bob")
        findings = security.find_stale_access_keys(ctx)
        assert findings == []


# ---------------------------------------------------------------------------
# SEC-004 — root MFA
# ---------------------------------------------------------------------------


class TestRootMfa:
    def test_missing_root_mfa_is_critical(self, ctx: DetectorContext) -> None:
        findings = security.check_root_mfa(ctx)
        assert len(findings) == 1
        assert findings[0].severity == "critical"
        assert findings[0].detector_id == "SEC-004-root-mfa"


# ---------------------------------------------------------------------------
# SEC-005 — wildcard IAM
# ---------------------------------------------------------------------------


class TestWildcardIam:
    def test_wildcard_policy_is_critical(
        self, ctx: DetectorContext, iam: Any
    ) -> None:
        doc = {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": "*", "Resource": "*"},
            ],
        }
        iam.create_policy(
            PolicyName="too-permissive",
            PolicyDocument=json.dumps(doc),
        )
        findings = security.find_overly_permissive_iam(ctx)
        assert len(findings) == 1
        assert findings[0].severity == "critical"
        assert findings[0].detector_id == "SEC-005-wildcard-iam"

    def test_scoped_policy_is_not_flagged(
        self, ctx: DetectorContext, iam: Any
    ) -> None:
        doc = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "s3:GetObject",
                    "Resource": "arn:aws:s3:::specific/*",
                }
            ],
        }
        iam.create_policy(PolicyName="scoped", PolicyDocument=json.dumps(doc))
        findings = security.find_overly_permissive_iam(ctx)
        assert findings == []


def test_run_all_aggregates(ctx: DetectorContext, s3: Any) -> None:
    s3.create_bucket(Bucket="aggregator-bucket")
    findings = security.run_all(ctx)
    detector_ids = {f.detector_id for f in findings}
    # At minimum the public S3 + root MFA detectors should fire.
    assert "SEC-001-public-s3" in detector_ids
    assert "SEC-004-root-mfa" in detector_ids
