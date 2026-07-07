"""Unit tests for ``chandra.tools.cost``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from src.chandra.tools import cost
from src.chandra.tools.base import DetectorContext


@pytest.fixture
def ami_id(ec2: Any) -> str:
    images = ec2.describe_images(Owners=["amazon"])["Images"]
    return str(images[0]["ImageId"])


class TestUnattachedEbs:
    def test_available_volume_is_flagged(self, ctx: DetectorContext, ec2: Any) -> None:
        vol = ec2.create_volume(AvailabilityZone="us-east-1a", Size=10, VolumeType="gp3")
        findings = cost.find_unattached_ebs(ctx)
        assert len(findings) == 1
        assert findings[0].detector_id == "COST-002-unattached-ebs"
        assert vol["VolumeId"] in findings[0].resource_arn

    def test_attached_volume_is_not_flagged(
        self, ctx: DetectorContext, ec2: Any, ami_id: str
    ) -> None:
        run = ec2.run_instances(ImageId=ami_id, MinCount=1, MaxCount=1)
        instance_id = run["Instances"][0]["InstanceId"]
        vol = ec2.create_volume(AvailabilityZone="us-east-1a", Size=10, VolumeType="gp3")
        ec2.attach_volume(VolumeId=vol["VolumeId"], InstanceId=instance_id, Device="/dev/sdh")
        findings = cost.find_unattached_ebs(ctx)
        assert all(vol["VolumeId"] not in f.resource_arn for f in findings)


class TestUnusedEips:
    def test_unassociated_eip_is_flagged(self, ctx: DetectorContext, ec2: Any) -> None:
        ec2.allocate_address(Domain="vpc")
        findings = cost.find_unused_eips(ctx)
        assert len(findings) == 1
        assert findings[0].detector_id == "COST-003-unused-eip"

    def test_associated_eip_is_not_flagged(
        self, ctx: DetectorContext, ec2: Any, ami_id: str
    ) -> None:
        addr = ec2.allocate_address(Domain="vpc")
        run = ec2.run_instances(ImageId=ami_id, MinCount=1, MaxCount=1)
        instance_id = run["Instances"][0]["InstanceId"]
        ec2.associate_address(InstanceId=instance_id, AllocationId=addr["AllocationId"])
        findings = cost.find_unused_eips(ctx)
        assert findings == []


class TestUntaggedBillable:
    def test_untagged_instance_is_flagged(
        self, ctx: DetectorContext, ec2: Any, ami_id: str
    ) -> None:
        ec2.run_instances(ImageId=ami_id, MinCount=1, MaxCount=1)
        findings = cost.find_untagged_billable(ctx)
        assert len(findings) == 1
        assert findings[0].detector_id == "COST-004-untagged-billable"
        evidence = findings[0].evidence
        assert "Environment" in evidence["MissingTags"]
        assert "Owner" in evidence["MissingTags"]

    def test_fully_tagged_instance_is_not_flagged(
        self, ctx: DetectorContext, ec2: Any, ami_id: str
    ) -> None:
        ec2.run_instances(
            ImageId=ami_id,
            MinCount=1,
            MaxCount=1,
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Environment", "Value": "prod"},
                        {"Key": "Owner", "Value": "team-a"},
                    ],
                }
            ],
        )
        findings = cost.find_untagged_billable(ctx)
        assert findings == []


class TestIdleEc2:
    def test_idle_instance_with_low_cpu_is_flagged(
        self, ctx: DetectorContext, ec2: Any, ami_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Launch an instance and pretend it has been running for >14d.
        run = ec2.run_instances(ImageId=ami_id, MinCount=1, MaxCount=1)
        instance_id = run["Instances"][0]["InstanceId"]

        # moto's CloudWatch get_metric_statistics returns synthetic data — patch it
        # to return a low-average series so the detector triggers deterministically.
        cw = ctx.factory.client("cloudwatch")

        def _fake_stats(**kwargs: Any) -> dict[str, Any]:
            return {
                "Label": "CPUUtilization",
                "Datapoints": [
                    {"Average": 1.2, "Timestamp": datetime.now(UTC)},
                    {"Average": 0.8, "Timestamp": datetime.now(UTC)},
                ],
            }

        monkeypatch.setattr(cw, "get_metric_statistics", _fake_stats)

        # Move "now" forward 30 days so LaunchTime < start window.
        future = datetime.now(UTC) + timedelta(days=30)

        class _FakeDateTime(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
                return future

        monkeypatch.setattr(cost, "datetime", _FakeDateTime)

        findings = cost.find_idle_ec2(ctx)
        matching = [f for f in findings if instance_id in f.resource_arn]
        assert len(matching) == 1
        assert matching[0].detector_id == "COST-001-idle-ec2"

    def test_busy_instance_is_not_flagged(
        self, ctx: DetectorContext, ec2: Any, ami_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = ec2.run_instances(ImageId=ami_id, MinCount=1, MaxCount=1)
        instance_id = run["Instances"][0]["InstanceId"]

        cw = ctx.factory.client("cloudwatch")

        def _busy_stats(**kwargs: Any) -> dict[str, Any]:
            return {
                "Label": "CPUUtilization",
                "Datapoints": [
                    {"Average": 65.0, "Timestamp": datetime.now(UTC)},
                ],
            }

        monkeypatch.setattr(cw, "get_metric_statistics", _busy_stats)

        future = datetime.now(UTC) + timedelta(days=30)

        class _FakeDateTime(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
                return future

        monkeypatch.setattr(cost, "datetime", _FakeDateTime)

        findings = cost.find_idle_ec2(ctx)
        assert all(instance_id not in f.resource_arn for f in findings)
