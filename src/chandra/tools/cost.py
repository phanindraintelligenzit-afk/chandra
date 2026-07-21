"""Cost optimization detectors for AWS resources."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from src.chandra.briefing.schemas import Finding
from src.chandra.tools.base import DetectorContext


def find_unattached_ebs(ctx: DetectorContext) -> list[Finding]:
    """Find unattached EBS volumes (COST-002-unattached-ebs)."""
    findings = []

    regions = ctx.regions or [os.getenv("AWS_DEFAULT_REGION", "us-east-1")]
    for region in regions:
        try:
            ec2 = ctx.factory.client("ec2", region=region)
            resp = ec2.describe_volumes()

            for volume in resp.get("Volumes", []):
                if not volume.get("Attachments"):
                    findings.append(
                        Finding(
                            region=region,
                            detector_id="COST-002-unattached-ebs",
                            resource_arn=(
                                f"arn:aws:ec2:{region}:{ctx.account_id}:volume/{volume['VolumeId']}"
                            ),
                            resource_type="AWS::EC2::Volume",
                            title=f"Unattached EBS volume {volume['VolumeId']}",
                            severity="medium",
                            kra="cost",
                            recommendation=(
                                "Delete or snapshot this unattached EBS volume "
                                "to avoid ongoing storage charges."
                            ),
                            evidence={
                                "volume_id": volume["VolumeId"],
                                "size_gib": volume["Size"],
                                "volume_type": volume["VolumeType"],
                            },
                        )
                    )
        except Exception as e:
            ctx.errors.append(
                {"region": region, "detector": "COST-002-unattached-ebs", "error": str(e)}
            )

    return findings


def find_unused_eips(ctx: DetectorContext) -> list[Finding]:
    """Find unassociated Elastic IPs (COST-003-unused-eip)."""
    findings = []

    regions = ctx.regions or [os.getenv("AWS_DEFAULT_REGION", "us-east-1")]
    for region in regions:
        try:
            ec2 = ctx.factory.client("ec2", region=region)
            resp = ec2.describe_addresses()

            for addr in resp.get("Addresses", []):
                if not addr.get("InstanceId") and not addr.get("NetworkInterfaceId"):
                    findings.append(
                        Finding(
                            region=region,
                            detector_id="COST-003-unused-eip",
                            resource_arn=(
                                f"arn:aws:ec2:{region}:{ctx.account_id}"
                                f":address/{addr['AllocationId']}"
                            ),
                            resource_type="AWS::EC2::Address",
                            title=f"Unused Elastic IP {addr['PublicIp']}",
                            severity="low",
                            kra="cost",
                            recommendation=(
                                "Release this unassociated Elastic IP address "
                                "to stop incurring hourly charges."
                            ),
                            evidence={
                                "public_ip": addr["PublicIp"],
                                "allocation_id": addr["AllocationId"],
                            },
                        )
                    )
        except Exception as e:
            ctx.errors.append(
                {"region": region, "detector": "COST-003-unused-eip", "error": str(e)}
            )

    return findings


def find_untagged_billable(ctx: DetectorContext) -> list[Finding]:
    """Find untagged EC2 instances (COST-004-untagged-billable)."""
    findings = []
    required_tags = {"Environment", "Owner"}

    regions = ctx.regions or [os.getenv("AWS_DEFAULT_REGION", "us-east-1")]
    for region in regions:
        try:
            ec2 = ctx.factory.client("ec2", region=region)
            resp = ec2.describe_instances()

            for reservation in resp.get("Reservations", []):
                for instance in reservation.get("Instances", []):
                    tags = {tag["Key"] for tag in instance.get("Tags", [])}
                    missing = required_tags - tags

                    if missing:
                        findings.append(
                            Finding(
                                region=region,
                                detector_id="COST-004-untagged-billable",
                                resource_arn=(
                                    f"arn:aws:ec2:{region}:{ctx.account_id}"
                                    f":instance/{instance['InstanceId']}"
                                ),
                                resource_type="AWS::EC2::Instance",
                                title=f"Untagged EC2 instance {instance['InstanceId']}",
                                severity="medium",
                                kra="cost",
                                recommendation=(
                                    f"Add the missing tags ({', '.join(sorted(missing))}) "
                                    "to enable cost allocation and ownership tracking."
                                ),
                                evidence={
                                    "MissingTags": sorted(missing),
                                    "InstanceId": instance["InstanceId"],
                                },
                            )
                        )
        except Exception as e:
            ctx.errors.append(
                {"region": region, "detector": "COST-004-untagged-billable", "error": str(e)}
            )

    return findings


def find_idle_ec2(ctx: DetectorContext) -> list[Finding]:
    """Find idle EC2 instances with low CPU utilization (COST-001-idle-ec2)."""
    findings = []

    regions = ctx.regions or [os.getenv("AWS_DEFAULT_REGION", "us-east-1")]
    for region in regions:
        try:
            ec2 = ctx.factory.client("ec2", region=region)
            cw = ctx.factory.client("cloudwatch", region=region)

            resp = ec2.describe_instances()

            for reservation in resp.get("Reservations", []):
                for instance in reservation.get("Instances", []):
                    instance_id = instance["InstanceId"]

                    now = datetime.now(UTC)
                    start_time = max(instance["LaunchTime"], now - timedelta(days=14))
                    metrics = cw.get_metric_statistics(
                        Namespace="AWS/EC2",
                        MetricName="CPUUtilization",
                        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                        StartTime=start_time,
                        EndTime=now,
                        Period=3600,
                        Statistics=["Average"],
                    )

                    if metrics["Datapoints"]:
                        avg_cpu = sum(dp["Average"] for dp in metrics["Datapoints"]) / len(
                            metrics["Datapoints"]
                        )

                        if avg_cpu < 5.0:
                            findings.append(
                                Finding(
                                    region=region,
                                    detector_id="COST-001-idle-ec2",
                                    resource_arn=(
                                        f"arn:aws:ec2:{region}:{ctx.account_id}"
                                        f":instance/{instance_id}"
                                    ),
                                    resource_type="AWS::EC2::Instance",
                                    title=f"Idle EC2 instance {instance_id}",
                                    severity="medium",
                                    kra="cost",
                                    recommendation=(
                                        f"Average CPU is {round(avg_cpu, 1)}% over the observation "
                                        "window. Consider rightsizing, scheduling stop/start, or "
                                        "terminating this instance."
                                    ),
                                    evidence={
                                        "avg_cpu_utilization": round(avg_cpu, 2),
                                        "instance_id": instance_id,
                                    },
                                )
                            )
        except Exception as e:
            ctx.errors.append({"region": region, "detector": "COST-001-idle-ec2", "error": str(e)})

    return findings


def run_all(ctx: DetectorContext) -> list[Finding]:
    """Run all cost detectors and return combined findings."""
    findings: list[Finding] = []
    findings.extend(find_unattached_ebs(ctx))
    findings.extend(find_unused_eips(ctx))
    findings.extend(find_untagged_billable(ctx))
    findings.extend(find_idle_ec2(ctx))
    return findings
