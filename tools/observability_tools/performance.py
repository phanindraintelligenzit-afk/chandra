"""Performance KRA detectors.

Detector IDs:

* ``PERF-001-no-asg``        — EC2 tagged ``Environment=prod`` not in an Auto Scaling group.
* ``PERF-002-rds-idle``      — RDS with sustained CPU <10% AND connections <5 over 14d.
* ``PERF-003-oversized-ec2`` — EC2 with sustained CPU below 30% — headroom >70%.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from briefing.schemas import Finding
from structlog import get_logger
from tools.observability_tools import DetectorContext, detector_guard, paginate

logger = get_logger(__name__)

RDS_IDLE_CPU = 10.0
RDS_IDLE_CONNECTIONS = 5.0
OVERSIZED_CPU = 30.0
LOOKBACK_DAYS = 14


def check_autoscaling_coverage(ctx: DetectorContext) -> list[Finding]:
    """Flag prod-tagged EC2 instances that are not members of any ASG."""
    detector_id = "PERF-001-no-asg"
    findings: list[Finding] = []

    for region in ctx.regions:
        ec2 = ctx.factory.client("ec2", region=region)
        asg = ctx.factory.client("autoscaling", region=region)

        asg_instance_ids: set[str] = set()
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(asg, "describe_auto_scaling_instances"):
                for inst in page.get("AutoScalingInstances", []):
                    asg_instance_ids.add(inst["InstanceId"])

        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(
                ec2,
                "describe_instances",
                Filters=[
                    {"Name": "instance-state-name", "Values": ["running"]},
                    {"Name": "tag:Environment", "Values": ["prod", "production"]},
                ],
            ):
                for reservation in page.get("Reservations", []):
                    for inst in reservation.get("Instances", []):
                        instance_id = inst["InstanceId"]
                        if instance_id in asg_instance_ids:
                            continue
                        arn = (
                            f"arn:aws:ec2:{region}:{ctx.account_id}:"
                            f"instance/{instance_id}"
                        )
                        findings.append(
                            Finding(
                                kra="performance",
                                severity="medium",
                                resource_arn=arn,
                                resource_type="AWS::EC2::Instance",
                                region=region,
                                title=(
                                    f"Production EC2 {instance_id} is not part of "
                                    "any Auto Scaling group"
                                ),
                                evidence={
                                    "InstanceId": instance_id,
                                    "InstanceType": inst.get("InstanceType"),
                                    "Tags": inst.get("Tags", []),
                                },
                                recommendation=(
                                    "Move the workload behind an Auto Scaling group "
                                    "with a Launch Template so capacity can flex "
                                    "with load and failed instances are replaced."
                                ),
                                detector_id=detector_id,
                            )
                        )

    return findings


def find_underutilized_rds(ctx: DetectorContext) -> list[Finding]:
    """Flag RDS instances with sustained low CPU AND low connection count."""
    detector_id = "PERF-002-rds-idle"
    findings: list[Finding] = []
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=LOOKBACK_DAYS)

    for region in ctx.regions:
        rds = ctx.factory.client("rds", region=region)
        cw = ctx.factory.client("cloudwatch", region=region)

        dbs: list[dict[str, Any]] = []
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(rds, "describe_db_instances"):
                dbs.extend(page.get("DBInstances", []))

        for db in dbs:
            identifier = db["DBInstanceIdentifier"]
            arn = db["DBInstanceArn"]
            with detector_guard(
                ctx, detector_id=detector_id, region=region, resource_arn=arn
            ):
                cpu = cw.get_metric_statistics(
                    Namespace="AWS/RDS",
                    MetricName="CPUUtilization",
                    Dimensions=[{"Name": "DBInstanceIdentifier", "Value": identifier}],
                    StartTime=start,
                    EndTime=now,
                    Period=3600,
                    Statistics=["Average"],
                )
                conn = cw.get_metric_statistics(
                    Namespace="AWS/RDS",
                    MetricName="DatabaseConnections",
                    Dimensions=[{"Name": "DBInstanceIdentifier", "Value": identifier}],
                    StartTime=start,
                    EndTime=now,
                    Period=3600,
                    Statistics=["Average"],
                )
                cpu_pts = cpu.get("Datapoints", [])
                conn_pts = conn.get("Datapoints", [])
                if not cpu_pts or not conn_pts:
                    continue
                avg_cpu = statistics.fmean(p["Average"] for p in cpu_pts)
                avg_conn = statistics.fmean(p["Average"] for p in conn_pts)
                if avg_cpu >= RDS_IDLE_CPU or avg_conn >= RDS_IDLE_CONNECTIONS:
                    continue
                findings.append(
                    Finding(
                        kra="performance",
                        severity="low",
                        resource_arn=arn,
                        resource_type="AWS::RDS::DBInstance",
                        region=region,
                        title=(
                            f"RDS {identifier} underutilized: avg CPU {avg_cpu:.1f}%, "
                            f"avg connections {avg_conn:.1f}"
                        ),
                        evidence={
                            "DBInstanceIdentifier": identifier,
                            "AverageCPU": round(avg_cpu, 2),
                            "AverageConnections": round(avg_conn, 2),
                            "LookbackDays": LOOKBACK_DAYS,
                        },
                        recommendation=(
                            "Right-size to a smaller instance class or migrate to "
                            "Aurora Serverless v2 for variable workloads."
                        ),
                        detector_id=detector_id,
                    )
                )

    return findings


def find_oversized_ec2(ctx: DetectorContext) -> list[Finding]:
    """Flag EC2 instances with sustained CPU below 30% — >70% headroom."""
    detector_id = "PERF-003-oversized-ec2"
    findings: list[Finding] = []
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=LOOKBACK_DAYS)

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
                f"arn:aws:ec2:{region}:{ctx.account_id}:instance/{instance_id}"
            )
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
                    Statistics=["Maximum"],
                )
                pts = metric.get("Datapoints", [])
                if not pts:
                    continue
                # If the *peak* hourly CPU never exceeded OVERSIZED_CPU,
                # the instance is over-provisioned.
                peak = max(p["Maximum"] for p in pts)
                if peak >= OVERSIZED_CPU:
                    continue
                findings.append(
                    Finding(
                        kra="performance",
                        severity="low",
                        resource_arn=arn,
                        resource_type="AWS::EC2::Instance",
                        region=region,
                        title=(
                            f"EC2 {instance_id} ({inst.get('InstanceType', '?')}) "
                            f"oversized: peak CPU {peak:.1f}% over {LOOKBACK_DAYS}d"
                        ),
                        evidence={
                            "InstanceId": instance_id,
                            "InstanceType": inst.get("InstanceType"),
                            "PeakCPU": round(peak, 2),
                            "LookbackDays": LOOKBACK_DAYS,
                        },
                        recommendation=(
                            "Use AWS Compute Optimizer to identify a smaller "
                            "instance type, or move the workload to a burstable "
                            "T-family instance if traffic is spiky."
                        ),
                        detector_id=detector_id,
                    )
                )

    return findings


ALL_DETECTORS = (
    check_autoscaling_coverage,
    find_underutilized_rds,
    find_oversized_ec2,
)


def run_all(ctx: DetectorContext) -> list[Finding]:
    out: list[Finding] = []
    for fn in ALL_DETECTORS:
        out.extend(fn(ctx))
    return out
