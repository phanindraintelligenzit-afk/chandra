"""Cost KRA detectors.

Detector IDs:

* ``COST-001-idle-ec2``        — EC2 with CPU avg <5% for 14 days.
* ``COST-002-unattached-ebs``  — EBS volume in ``available`` state.
* ``COST-003-unused-eip``      — Elastic IP not associated to any resource.
* ``COST-004-untagged-billable`` — billable resources missing Environment/Owner tags.
"""

from __future__ import annotations

import asyncio
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from chandra.briefing.schemas import Finding
from chandra.logging import get_logger
from chandra.tools.base import DetectorContext, detector_guard, paginate

logger = get_logger(__name__)

IDLE_CPU_THRESHOLD = 5.0
IDLE_LOOKBACK_DAYS = 14
REQUIRED_TAGS: tuple[str, ...] = ("Environment", "Owner")
BILLABLE_RESOURCE_TYPES = ("AWS::EC2::Instance", "AWS::RDS::DBInstance", "AWS::S3::Bucket")


def find_idle_ec2(ctx: DetectorContext) -> list[Finding]:
    """Flag running EC2 instances whose mean CPU over 14d is below 5%."""
    detector_id = "COST-001-idle-ec2"
    findings: list[Finding] = []
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=IDLE_LOOKBACK_DAYS)

    for region in ctx.regions:
        ec2 = ctx.factory.client("ec2", region=region)
        cw = ctx.factory.client("cloudwatch", region=region)

        instances: list[dict[str, Any]] = []
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(
                ec2,
                "describe_instances",
                Filters=[{"Name": "instance-state-name", "Values": ["running"]}],
            ):
                for reservation in page.get("Reservations", []):
                    instances.extend(reservation.get("Instances", []))

        for inst in instances:
            instance_id = inst["InstanceId"]
            arn = (
                f"arn:aws:ec2:{region}:{inst.get('OwnerId', ctx.account_id)}:"
                f"instance/{instance_id}"
            )
            launch_time = inst.get("LaunchTime")
            if launch_time is None or launch_time > start:
                continue

            with detector_guard(
                ctx, detector_id=detector_id, region=region, resource_arn=arn
            ):
                metric = cw.get_metric_statistics(
                    Namespace="AWS/EC2",
                    MetricName="CPUUtilization",
                    Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                    StartTime=start,
                    EndTime=now,
                    Period=3600,
                    Statistics=["Average"],
                )
                datapoints = metric.get("Datapoints", [])
                if not datapoints:
                    continue
                avg = statistics.fmean(dp["Average"] for dp in datapoints)
                if avg >= IDLE_CPU_THRESHOLD:
                    continue
                findings.append(
                    Finding(
                        kra="cost",
                        severity="medium",
                        resource_arn=arn,
                        resource_type="AWS::EC2::Instance",
                        region=region,
                        title=(
                            f"EC2 {instance_id} ({inst.get('InstanceType', '?')}) "
                            f"idle: avg CPU {avg:.2f}% over {IDLE_LOOKBACK_DAYS}d"
                        ),
                        evidence={
                            "InstanceId": instance_id,
                            "InstanceType": inst.get("InstanceType"),
                            "AverageCPU": round(avg, 2),
                            "Samples": len(datapoints),
                            "LookbackDays": IDLE_LOOKBACK_DAYS,
                        },
                        recommendation=(
                            "Right-size to a smaller instance, schedule a stop/start "
                            "window, or terminate if the workload is no longer needed."
                        ),
                        detector_id=detector_id,
                    )
                )

    return findings


def find_unattached_ebs(ctx: DetectorContext) -> list[Finding]:
    """Flag EBS volumes in ``available`` state — paying for storage with no consumer."""
    detector_id = "COST-002-unattached-ebs"
    findings: list[Finding] = []

    for region in ctx.regions:
        ec2 = ctx.factory.client("ec2", region=region)
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(
                ec2,
                "describe_volumes",
                Filters=[{"Name": "status", "Values": ["available"]}],
            ):
                for vol in page.get("Volumes", []):
                    volume_id = vol["VolumeId"]
                    arn = f"arn:aws:ec2:{region}:{ctx.account_id}:volume/{volume_id}"
                    findings.append(
                        Finding(
                            kra="cost",
                            severity="low",
                            resource_arn=arn,
                            resource_type="AWS::EC2::Volume",
                            region=region,
                            title=(
                                f"EBS volume {volume_id} ({vol.get('Size')} GiB, "
                                f"{vol.get('VolumeType')}) is unattached"
                            ),
                            evidence={
                                "VolumeId": volume_id,
                                "Size": vol.get("Size"),
                                "VolumeType": vol.get("VolumeType"),
                                "CreateTime": (
                                    vol.get("CreateTime").isoformat()
                                    if vol.get("CreateTime")
                                    else None
                                ),
                            },
                            recommendation=(
                                "Snapshot the volume if data is needed, then delete the "
                                "volume. Use AWS Backup or DLM if periodic snapshots are "
                                "required."
                            ),
                            detector_id=detector_id,
                        )
                    )

    return findings


def find_unused_eips(ctx: DetectorContext) -> list[Finding]:
    """Flag Elastic IPs that are allocated but not associated."""
    detector_id = "COST-003-unused-eip"
    findings: list[Finding] = []

    for region in ctx.regions:
        ec2 = ctx.factory.client("ec2", region=region)
        with detector_guard(ctx, detector_id=detector_id, region=region):
            resp = ec2.describe_addresses()
            for addr in resp.get("Addresses", []):
                if addr.get("AssociationId"):
                    continue
                allocation_id = addr.get("AllocationId") or addr.get("PublicIp")
                arn = (
                    f"arn:aws:ec2:{region}:{ctx.account_id}:elastic-ip/{allocation_id}"
                )
                findings.append(
                    Finding(
                        kra="cost",
                        severity="low",
                        resource_arn=arn,
                        resource_type="AWS::EC2::EIP",
                        region=region,
                        title=(
                            f"Elastic IP {addr.get('PublicIp')} is allocated but "
                            "not associated"
                        ),
                        evidence={
                            "AllocationId": addr.get("AllocationId"),
                            "PublicIp": addr.get("PublicIp"),
                            "Domain": addr.get("Domain"),
                        },
                        recommendation=(
                            "Release the EIP if it is no longer needed. AWS bills "
                            "unassociated EIPs by the hour."
                        ),
                        detector_id=detector_id,
                    )
                )

    return findings


def find_untagged_billable(ctx: DetectorContext) -> list[Finding]:
    """Flag billable resources missing Environment/Owner tags."""
    detector_id = "COST-004-untagged-billable"
    findings: list[Finding] = []

    for region in ctx.regions:
        ec2 = ctx.factory.client("ec2", region=region)
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(
                ec2,
                "describe_instances",
                Filters=[
                    {"Name": "instance-state-name", "Values": ["running", "stopped"]}
                ],
            ):
                for reservation in page.get("Reservations", []):
                    for inst in reservation.get("Instances", []):
                        tags = {t["Key"]: t["Value"] for t in inst.get("Tags", []) or []}
                        missing = [t for t in REQUIRED_TAGS if t not in tags]
                        if not missing:
                            continue
                        instance_id = inst["InstanceId"]
                        arn = (
                            f"arn:aws:ec2:{region}:{ctx.account_id}:"
                            f"instance/{instance_id}"
                        )
                        findings.append(
                            Finding(
                                kra="cost",
                                severity="low",
                                resource_arn=arn,
                                resource_type="AWS::EC2::Instance",
                                region=region,
                                title=(
                                    f"EC2 {instance_id} missing required tag(s): "
                                    f"{', '.join(missing)}"
                                ),
                                evidence={
                                    "InstanceId": instance_id,
                                    "PresentTags": tags,
                                    "MissingTags": missing,
                                },
                                recommendation=(
                                    "Attach Environment and Owner tags. Enable an "
                                    "SCP / Tag Policy to enforce on new resources."
                                ),
                                detector_id=detector_id,
                            )
                        )

    return findings


ALL_DETECTORS = (
    find_idle_ec2,
    find_unattached_ebs,
    find_unused_eips,
    find_untagged_billable,
)


async def _run_all_async(ctx: DetectorContext) -> list[Finding]:
    tasks = [asyncio.to_thread(fn, ctx) for fn in ALL_DETECTORS]
    results = await asyncio.gather(*tasks)
    out: list[Finding] = []
    for result in results:
        out.extend(result)
    return out


def run_all(ctx: DetectorContext) -> list[Finding]:
    return asyncio.run(_run_all_async(ctx))
