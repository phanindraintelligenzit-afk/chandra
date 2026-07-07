"""Unit tests for ``chandra.tools.performance``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from src.chandra.tools import performance
from src.chandra.tools.base import DetectorContext


@pytest.fixture
def ami_id(ec2: Any) -> str:
    images = ec2.describe_images(Owners=["amazon"])["Images"]
    return str(images[0]["ImageId"])


class TestAutoScalingCoverage:
    def test_prod_instance_outside_asg_is_flagged(
        self, ctx: DetectorContext, ec2: Any, ami_id: str
    ) -> None:
        ec2.run_instances(
            ImageId=ami_id,
            MinCount=1,
            MaxCount=1,
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [{"Key": "Environment", "Value": "prod"}],
                }
            ],
        )
        findings = performance.check_autoscaling_coverage(ctx)
        assert len(findings) == 1
        assert findings[0].detector_id == "PERF-001-no-asg"

    def test_non_prod_instance_is_ignored(
        self, ctx: DetectorContext, ec2: Any, ami_id: str
    ) -> None:
        ec2.run_instances(
            ImageId=ami_id,
            MinCount=1,
            MaxCount=1,
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [{"Key": "Environment", "Value": "dev"}],
                }
            ],
        )
        findings = performance.check_autoscaling_coverage(ctx)
        assert findings == []


class TestRdsUnderutilized:
    def test_idle_rds_is_flagged(
        self, ctx: DetectorContext, rds: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rds.create_db_instance(
            DBInstanceIdentifier="quietdb",
            DBInstanceClass="db.t3.micro",
            Engine="postgres",
            MasterUsername="admin",
            MasterUserPassword="hunter22hunter22",
            AllocatedStorage=20,
        )
        cw = ctx.factory.client("cloudwatch")

        def _fake_stats(**kwargs: Any) -> dict[str, Any]:
            metric_name = kwargs.get("MetricName")
            value = 1.0 if metric_name == "CPUUtilization" else 0.5
            return {
                "Label": metric_name,
                "Datapoints": [
                    {"Average": value, "Timestamp": datetime.now(UTC)},
                ],
            }

        monkeypatch.setattr(cw, "get_metric_statistics", _fake_stats)
        findings = performance.find_underutilized_rds(ctx)
        assert len(findings) == 1
        assert findings[0].detector_id == "PERF-002-rds-idle"


class TestOversizedEc2:
    def test_low_peak_cpu_is_flagged(
        self,
        ctx: DetectorContext,
        ec2: Any,
        ami_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ec2.run_instances(ImageId=ami_id, MinCount=1, MaxCount=1)
        cw = ctx.factory.client("cloudwatch")

        def _low_peak(**kwargs: Any) -> dict[str, Any]:
            return {
                "Label": "CPUUtilization",
                "Datapoints": [
                    {"Maximum": 12.0, "Timestamp": datetime.now(UTC)},
                ],
            }

        monkeypatch.setattr(cw, "get_metric_statistics", _low_peak)
        findings = performance.find_oversized_ec2(ctx)
        assert len(findings) >= 1
        assert any(f.detector_id == "PERF-003-oversized-ec2" for f in findings)
