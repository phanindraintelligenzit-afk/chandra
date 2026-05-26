"""Cost KRA detectors."""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from src.chandra.briefing.schemas import Finding
from structlog import get_logger
from tools.observability_tools import DetectorContext, detector_guard, paginate, run_per_region

logger = get_logger(__name__)

IDLE_CPU_THRESHOLD = 5.0
IDLE_LOOKBACK_DAYS = 14
REQUIRED_TAGS: tuple[str, ...] = ("Environment", "Owner")


def find_idle_ec2(ctx: DetectorContext) -> list[Finding]:
    detector_id = "COST-001-idle-ec2"
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=IDLE_LOOKBACK_DAYS)

    def _check_region(ctx: DetectorContext, region: str) -> list[Finding]:
        ec2 = ctx.factory.client("ec2", region=region)
        cw = ctx.factory.client("cloudwatch", region=region)
        findings: list[Finding] = []

        instances: list[dict[str, Any]] = []
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(
                ec2, "describe_instances",
                Filters=[{"Name": "instance-state-name", "Values": ["running"]}],
            ):
                for reservation in page.get("Reservations", []):
                    instances.extend(reservation.get("Instances", []))

        for inst in instances:
            instance_id = inst["InstanceId"]
            launch_time = inst.get("LaunchTime")
            if launch_time is None or launch_time > start:
                continue
            arn = (
                f"arn:aws:ec2:{region}:{inst.get('OwnerId', ctx.account_id)}:"
                f"instance/{instance_id}"
            )
            with detector_guard(ctx, detector_id=detector_id, region=region, resource_arn=arn):
                datapoints = cw.get_metric_statistics(
                    Namespace="AWS/EC2",
                    MetricName="CPUUtilization",
                    Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                    StartTime=start, EndTime=now, Period=3600, Statistics=["Average"],
                ).get("Datapoints", [])
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

    return run_per_region(ctx, _check_region)


def find_unattached_ebs(ctx: DetectorContext) -> list[Finding]:
    detector_id = "COST-002-unattached-ebs"

    def _check_region(ctx: DetectorContext, region: str) -> list[Finding]:
        ec2 = ctx.factory.client("ec2", region=region)
        findings: list[Finding] = []
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(
                ec2, "describe_volumes",
                Filters=[{"Name": "status", "Values": ["available"]}],
            ):
                for vol in page.get("Volumes", []):
                    volume_id = vol["VolumeId"]
                    findings.append(
                        Finding(
                            kra="cost",
                            severity="low",
                            resource_arn=f"arn:aws:ec2:{region}:{ctx.account_id}:volume/{volume_id}",
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
                                    vol["CreateTime"].isoformat() if vol.get("CreateTime") else None
                                ),
                            },
                            recommendation=(
                                "Snapshot the volume if data is needed, then delete the "
                                "volume. Use AWS Backup or DLM if periodic snapshots are required."
                            ),
                            detector_id=detector_id,
                        )
                    )
        return findings

    return run_per_region(ctx, _check_region)


def find_unused_eips(ctx: DetectorContext) -> list[Finding]:
    detector_id = "COST-003-unused-eip"

    def _check_region(ctx: DetectorContext, region: str) -> list[Finding]:
        ec2 = ctx.factory.client("ec2", region=region)
        findings: list[Finding] = []
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for addr in ec2.describe_addresses().get("Addresses", []):
                if addr.get("AssociationId"):
                    continue
                allocation_id = addr.get("AllocationId") or addr.get("PublicIp")
                findings.append(
                    Finding(
                        kra="cost",
                        severity="low",
                        resource_arn=f"arn:aws:ec2:{region}:{ctx.account_id}:elastic-ip/{allocation_id}",
                        resource_type="AWS::EC2::EIP",
                        region=region,
                        title=f"Elastic IP {addr.get('PublicIp')} is allocated but not associated",
                        evidence={
                            "AllocationId": addr.get("AllocationId"),
                            "PublicIp": addr.get("PublicIp"),
                            "Domain": addr.get("Domain"),
                        },
                        recommendation=(
                            "Release the EIP if it is no longer needed. "
                            "AWS bills unassociated EIPs by the hour."
                        ),
                        detector_id=detector_id,
                    )
                )
        return findings

    return run_per_region(ctx, _check_region)


def find_untagged_billable(ctx: DetectorContext) -> list[Finding]:
    detector_id = "COST-004-untagged-billable"

    def _check_region(ctx: DetectorContext, region: str) -> list[Finding]:
        ec2 = ctx.factory.client("ec2", region=region)
        findings: list[Finding] = []
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(
                ec2, "describe_instances",
                Filters=[{"Name": "instance-state-name", "Values": ["running", "stopped"]}],
            ):
                for reservation in page.get("Reservations", []):
                    for inst in reservation.get("Instances", []):
                        tags = {t["Key"]: t["Value"] for t in inst.get("Tags", []) or []}
                        missing = [t for t in REQUIRED_TAGS if t not in tags]
                        if not missing:
                            continue
                        instance_id = inst["InstanceId"]
                        findings.append(
                            Finding(
                                kra="cost",
                                severity="low",
                                resource_arn=f"arn:aws:ec2:{region}:{ctx.account_id}:instance/{instance_id}",
                                resource_type="AWS::EC2::Instance",
                                region=region,
                                title=f"EC2 {instance_id} missing required tag(s): {', '.join(missing)}",
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

    return run_per_region(ctx, _check_region)


ALL_DETECTORS = (
    find_idle_ec2,
    find_unattached_ebs,
    find_unused_eips,
    find_untagged_billable,
)


def run_all(ctx: DetectorContext) -> list[Finding]:
    out: list[Finding] = []
    for fn in ALL_DETECTORS:
        out.extend(fn(ctx))
    return out
