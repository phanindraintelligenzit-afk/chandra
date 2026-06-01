"""Unit tests for ``chandra.tools.reliability``."""

from __future__ import annotations

from typing import Any

from src.chandra.tools import reliability
from src.chandra.tools.base import DetectorContext


class TestRdsMultiAz:
    def test_prod_single_az_rds_is_flagged(
        self, ctx: DetectorContext, rds: Any
    ) -> None:
        rds.create_db_instance(
            DBInstanceIdentifier="prod-db",
            DBInstanceClass="db.t3.micro",
            Engine="postgres",
            MasterUsername="admin",
            MasterUserPassword="hunter22hunter22",
            AllocatedStorage=20,
            MultiAZ=False,
            Tags=[{"Key": "Environment", "Value": "prod"}],
        )
        findings = reliability.check_rds_multi_az(ctx)
        assert len(findings) == 1
        assert findings[0].detector_id == "REL-001-rds-single-az"
        assert findings[0].severity == "high"

    def test_prod_multi_az_rds_is_not_flagged(
        self, ctx: DetectorContext, rds: Any
    ) -> None:
        rds.create_db_instance(
            DBInstanceIdentifier="prod-multi",
            DBInstanceClass="db.t3.micro",
            Engine="postgres",
            MasterUsername="admin",
            MasterUserPassword="hunter22hunter22",
            AllocatedStorage=20,
            MultiAZ=True,
            Tags=[{"Key": "Environment", "Value": "prod"}],
        )
        findings = reliability.check_rds_multi_az(ctx)
        assert findings == []

    def test_dev_single_az_rds_is_ignored(
        self, ctx: DetectorContext, rds: Any
    ) -> None:
        rds.create_db_instance(
            DBInstanceIdentifier="dev-db",
            DBInstanceClass="db.t3.micro",
            Engine="postgres",
            MasterUsername="admin",
            MasterUserPassword="hunter22hunter22",
            AllocatedStorage=20,
            MultiAZ=False,
            Tags=[{"Key": "Environment", "Value": "dev"}],
        )
        findings = reliability.check_rds_multi_az(ctx)
        assert findings == []


class TestS3Versioning:
    def test_critical_bucket_without_versioning_is_flagged(
        self, ctx: DetectorContext, s3: Any
    ) -> None:
        s3.create_bucket(Bucket="critical-data")
        s3.put_bucket_tagging(
            Bucket="critical-data",
            Tagging={"TagSet": [{"Key": "Criticality", "Value": "high"}]},
        )
        findings = reliability.check_s3_versioning(ctx)
        flagged = [f for f in findings if "critical-data" in f.resource_arn]
        assert len(flagged) == 1
        assert flagged[0].detector_id == "REL-002-s3-versioning"

    def test_critical_bucket_with_versioning_is_not_flagged(
        self, ctx: DetectorContext, s3: Any
    ) -> None:
        s3.create_bucket(Bucket="critical-versioned")
        s3.put_bucket_tagging(
            Bucket="critical-versioned",
            Tagging={"TagSet": [{"Key": "Criticality", "Value": "high"}]},
        )
        s3.put_bucket_versioning(
            Bucket="critical-versioned",
            VersioningConfiguration={"Status": "Enabled"},
        )
        findings = reliability.check_s3_versioning(ctx)
        assert all("critical-versioned" not in f.resource_arn for f in findings)

    def test_non_critical_bucket_is_ignored(
        self, ctx: DetectorContext, s3: Any
    ) -> None:
        s3.create_bucket(Bucket="ordinary")
        findings = reliability.check_s3_versioning(ctx)
        assert all("ordinary" not in f.resource_arn for f in findings)


class TestBackupPlans:
    def test_region_without_backup_plan_is_flagged(
        self, ctx: DetectorContext
    ) -> None:
        findings = reliability.check_backup_plans(ctx)
        assert any(f.detector_id == "REL-004-no-backup-plan" for f in findings)


class TestDlm:
    def test_region_with_attached_volumes_but_no_dlm_is_flagged(
        self, ctx: DetectorContext, ec2: Any
    ) -> None:
        # Create an attached volume.
        images = ec2.describe_images(Owners=["amazon"])["Images"]
        ami_id = images[0]["ImageId"]
        run = ec2.run_instances(ImageId=ami_id, MinCount=1, MaxCount=1)
        instance_id = run["Instances"][0]["InstanceId"]
        vol = ec2.create_volume(
            AvailabilityZone="us-east-1a", Size=10, VolumeType="gp3"
        )
        ec2.attach_volume(
            VolumeId=vol["VolumeId"], InstanceId=instance_id, Device="/dev/sdh"
        )

        findings = reliability.check_ebs_snapshot_policy(ctx)
        assert any(f.detector_id == "REL-003-no-dlm" for f in findings)
