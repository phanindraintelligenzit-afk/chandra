"""Performance KRA detectors.

Detector IDs:

* ``PERF-001-no-asg``          — EC2 tagged ``Environment=prod`` not in an Auto Scaling group.
* ``PERF-002-rds-idle``        — RDS with sustained CPU <10% AND connections <5 over 14d.
* ``PERF-003-oversized-ec2``   — EC2 with sustained CPU below 30% — headroom >70%.
* ``PERF-004-xray-error-rate`` — X-Ray service with sustained error or fault rate >5%.
* ``PERF-005-xray-latency``    — X-Ray service with p99 response latency >2 seconds.
* ``PERF-006-co-ec2``          — Compute Optimizer EC2 recommendation with OVER_PROVISIONED finding.
* ``PERF-007-co-lambda``       — Compute Optimizer Lambda recommendation with OVER_PROVISIONED finding.
"""

from __future__ import annotations

import asyncio
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from src.chandra.briefing.schemas import Finding
from src.chandra.logging import get_logger
from src.chandra.tools.base import DetectorContext, detector_guard, paginate

logger = get_logger(__name__)

RDS_IDLE_CPU = 10.0
RDS_IDLE_CONNECTIONS = 5.0
OVERSIZED_CPU = 30.0
LOOKBACK_DAYS = 14

# PERF-004 / PERF-005 thresholds
XRAY_ERROR_RATE_THRESHOLD = 5.0   # percent — combined error + fault rate
XRAY_LATENCY_P99_MS = 2000.0      # milliseconds — p99 threshold


# ---------------------------------------------------------------------------
# PERF-001  Production EC2 not in ASG
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# PERF-002  Underutilized RDS
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# PERF-003  Oversized EC2
# ---------------------------------------------------------------------------

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
            arn = f"arn:aws:ec2:{region}:{ctx.account_id}:instance/{instance_id}"
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


# ---------------------------------------------------------------------------
# PERF-004  X-Ray — high error / fault rate
# ---------------------------------------------------------------------------

def find_xray_high_error_rate(ctx: DetectorContext) -> list[Finding]:
    """Flag X-Ray services with a combined error+fault rate above 5%.

    Calls ``get_service_graph`` for a rolling 1-hour window and inspects the
    ``SummaryStatistics`` block on each service node.  Both ``ErrorStatistics``
    (4xx client errors) and ``FaultStatistics`` (5xx server faults) contribute
    to the combined rate.  Services with fewer than 10 total requests are
    skipped — low traffic makes rates statistically unreliable.
    """
    detector_id = "PERF-004-xray-error-rate"
    findings: list[Finding] = []
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=1)

    for region in ctx.regions:
        xray = ctx.factory.client("xray", region=region)

        service_nodes: list[dict[str, Any]] = []
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(
                xray,
                "get_service_graph",
                StartTime=window_start,
                EndTime=now,
            ):
                service_nodes.extend(page.get("Services", []))

        for node in service_nodes:
            if node.get("Type") in ("client", "remote"):
                continue

            stats = node.get("SummaryStatistics", {})
            total = stats.get("TotalCount", 0)
            if total < 10:
                continue

            error_count = stats.get("ErrorStatistics", {}).get("TotalCount", 0)
            fault_count = stats.get("FaultStatistics", {}).get("TotalCount", 0)
            bad_count = error_count + fault_count
            rate = (bad_count / total) * 100 if total else 0.0

            if rate < XRAY_ERROR_RATE_THRESHOLD:
                continue

            service_name = node.get("Name", "unknown")
            arn = (
                f"arn:aws:xray:{region}:{ctx.account_id}:"
                f"service/{service_name}"
            )
            severity = "high" if rate >= 20.0 else "medium"

            findings.append(
                Finding(
                    kra="performance",
                    severity=severity,
                    resource_arn=arn,
                    resource_type="AWS::XRay::Service",
                    region=region,
                    title=(
                        f"X-Ray service '{service_name}' error+fault rate "
                        f"{rate:.1f}% ({bad_count}/{total} requests) in the last hour"
                    ),
                    evidence={
                        "ServiceName": service_name,
                        "ServiceType": node.get("Type"),
                        "TotalRequests": total,
                        "ErrorCount": error_count,
                        "FaultCount": fault_count,
                        "CombinedRatePct": round(rate, 2),
                        "WindowStartUTC": window_start.isoformat(),
                        "WindowEndUTC": now.isoformat(),
                    },
                    recommendation=(
                        "Open the X-Ray Service Map in the AWS console, select the "
                        f"'{service_name}' node, and drill into traces filtered by "
                        "Error/Fault status to identify root-cause operations. "
                        "Check recent deployments, dependency health, and CloudWatch "
                        "alarms for correlated signals."
                    ),
                    detector_id=detector_id,
                )
            )

    return findings


# ---------------------------------------------------------------------------
# PERF-005  X-Ray — high p99 latency
# ---------------------------------------------------------------------------

def find_xray_high_latency(ctx: DetectorContext) -> list[Finding]:
    """Flag X-Ray services with a p99 response latency above 2 seconds.

    Iterates the same service graph as PERF-004 but evaluates
    ``ResponseTimeHistogram`` to approximate p99.  When the histogram is absent,
    the detector falls back to ``SummaryStatistics.ResponseTime`` (average).
    Services with fewer than 10 total requests are skipped.
    """
    detector_id = "PERF-005-xray-latency"
    findings: list[Finding] = []
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=1)

    for region in ctx.regions:
        xray = ctx.factory.client("xray", region=region)

        service_nodes: list[dict[str, Any]] = []
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(
                xray,
                "get_service_graph",
                StartTime=window_start,
                EndTime=now,
            ):
                service_nodes.extend(page.get("Services", []))

        for node in service_nodes:
            if node.get("Type") in ("client", "remote"):
                continue

            stats = node.get("SummaryStatistics", {})
            total = stats.get("TotalCount", 0)
            if total < 10:
                continue

            histogram = node.get("ResponseTimeHistogram", [])
            p99_ms: float | None = None
            if histogram:
                sorted_buckets = sorted(histogram, key=lambda b: b["Value"])
                cumulative = 0
                p99_threshold = total * 0.99
                for bucket in sorted_buckets:
                    cumulative += bucket["Count"]
                    if cumulative >= p99_threshold:
                        p99_ms = bucket["Value"] * 1000  # seconds → ms
                        break

            if p99_ms is None:
                avg_s = stats.get("ResponseTime", 0.0)
                p99_ms = avg_s * 1000

            if p99_ms < XRAY_LATENCY_P99_MS:
                continue

            service_name = node.get("Name", "unknown")
            arn = (
                f"arn:aws:xray:{region}:{ctx.account_id}:"
                f"service/{service_name}"
            )
            severity = "high" if p99_ms >= 5000 else "medium"

            findings.append(
                Finding(
                    kra="performance",
                    severity=severity,
                    resource_arn=arn,
                    resource_type="AWS::XRay::Service",
                    region=region,
                    title=(
                        f"X-Ray service '{service_name}' p99 latency "
                        f"{p99_ms:.0f} ms exceeds {XRAY_LATENCY_P99_MS:.0f} ms "
                        "threshold (last hour)"
                    ),
                    evidence={
                        "ServiceName": service_name,
                        "ServiceType": node.get("Type"),
                        "TotalRequests": total,
                        "P99LatencyMs": round(p99_ms, 1),
                        "ThresholdMs": XRAY_LATENCY_P99_MS,
                        "HistogramBuckets": len(histogram),
                        "WindowStartUTC": window_start.isoformat(),
                        "WindowEndUTC": now.isoformat(),
                    },
                    recommendation=(
                        f"Use the X-Ray Traces console to filter on '{service_name}' "
                        "traces with a minimum response time at or above the threshold. "
                        "Look for slow database queries, synchronous downstream calls, "
                        "or cold-start latency on Lambda. Consider adding CloudWatch "
                        "Contributor Insights rules to surface the slowest operations."
                    ),
                    detector_id=detector_id,
                )
            )

    return findings


# ---------------------------------------------------------------------------
# PERF-006  Compute Optimizer — over-provisioned EC2
# ---------------------------------------------------------------------------

def find_compute_optimizer_ec2(ctx: DetectorContext) -> list[Finding]:
    """Surface Compute Optimizer EC2 recommendations where finding is OVER_PROVISIONED.

    ``get_ec2_instance_recommendations`` is NOT in the botocore paginator
    registry, so pagination is driven manually via ``NextToken``.

    Requires Compute Optimizer to be opted in for the account. If it has not
    been enabled, the API raises ``OptInRequiredException``; ``detector_guard``
    logs it as a warning and skips the region cleanly.
    """
    detector_id = "PERF-006-co-ec2"
    findings: list[Finding] = []

    for region in ctx.regions:
        # Correct boto3 service name is "compute-optimizer" (hyphenated).
        co = ctx.factory.client("compute-optimizer", region=region)

        recommendations: list[dict[str, Any]] = []
        with detector_guard(ctx, detector_id=detector_id, region=region):
            # Manual NextToken loop — this API is not registered as pageable.
            kwargs: dict[str, Any] = {}
            while True:
                resp = co.get_ec2_instance_recommendations(**kwargs)
                recommendations.extend(resp.get("instanceRecommendations", []))
                next_token = resp.get("NextToken")
                if not next_token:
                    break
                kwargs["NextToken"] = next_token

        for rec in recommendations:
            if rec.get("finding") != "OVER_PROVISIONED":
                continue

            instance_arn = rec.get("instanceArn", "")
            instance_id = instance_arn.split("/")[-1] if instance_arn else "unknown"
            current_type = rec.get("currentInstanceType", "?")

            options = rec.get("recommendationOptions", [])
            best: dict[str, Any] = options[0] if options else {}
            recommended_type = best.get("instanceType", "unknown")
            savings_opportunity = best.get("savingsOpportunity", {})

            monthly_savings: float | None = None
            savings_value = savings_opportunity.get("estimatedMonthlySavings", {})
            if savings_value:
                try:
                    monthly_savings = float(savings_value.get("value", 0))
                except (TypeError, ValueError):
                    pass

            severity = "high" if (monthly_savings or 0) >= 100 else "medium"

            title_parts = [
                f"Compute Optimizer: EC2 {instance_id} ({current_type}) is "
                f"over-provisioned — recommended type: {recommended_type}"
            ]
            if monthly_savings is not None:
                title_parts.append(f" (est. ${monthly_savings:.2f}/mo savings)")

            findings.append(
                Finding(
                    kra="performance",
                    severity=severity,
                    resource_arn=instance_arn,
                    resource_type="AWS::EC2::Instance",
                    region=region,
                    title="".join(title_parts),
                    evidence={
                        "InstanceId": instance_id,
                        "CurrentInstanceType": current_type,
                        "RecommendedInstanceType": recommended_type,
                        "Finding": rec.get("finding"),
                        "FindingReasonCodes": rec.get("findingReasonCodes", []),
                        "EstimatedMonthlySavingsUSD": monthly_savings,
                        "SavingsOpportunityPct": savings_opportunity.get(
                            "savingsOpportunityPercentage"
                        ),
                        "TopRecommendationOption": best,
                        "LookBackPeriodDays": rec.get("lookBackPeriodInDays"),
                    },
                    recommendation=(
                        f"Change the instance type from {current_type} to "
                        f"{recommended_type} during the next maintenance window. "
                        "Validate with a load test before promoting to production. "
                        "Review all recommendation options in Compute Optimizer for "
                        "ARM (Graviton) alternatives that offer additional savings."
                    ),
                    detector_id=detector_id,
                )
            )

    return findings


# ---------------------------------------------------------------------------
# PERF-007  Compute Optimizer — over-provisioned Lambda
# ---------------------------------------------------------------------------

def find_compute_optimizer_lambda(ctx: DetectorContext) -> list[Finding]:
    """Surface Compute Optimizer Lambda memory recommendations where finding is OVER_PROVISIONED.

    ``get_lambda_function_recommendations`` is also NOT in the botocore paginator
    registry; pagination driven manually via ``NextToken``.

    Severity escalates to ``high`` when estimated monthly savings exceed $100.
    """
    detector_id = "PERF-007-co-lambda"
    findings: list[Finding] = []

    for region in ctx.regions:
        co = ctx.factory.client("compute-optimizer", region=region)

        recommendations: list[dict[str, Any]] = []
        with detector_guard(ctx, detector_id=detector_id, region=region):
            kwargs: dict[str, Any] = {}
            while True:
                resp = co.get_lambda_function_recommendations(**kwargs)
                recommendations.extend(
                    resp.get("lambdaFunctionRecommendations", [])
                )
                next_token = resp.get("NextToken")
                if not next_token:
                    break
                kwargs["NextToken"] = next_token

        for rec in recommendations:
            if rec.get("finding") != "OVER_PROVISIONED":
                continue

            function_arn = rec.get("functionArn", "")
            function_name = function_arn.split(":")[-1] if function_arn else "unknown"
            current_memory = rec.get("currentMemorySize", 0)

            options = rec.get("memorySizeRecommendationOptions", [])
            best: dict[str, Any] = options[0] if options else {}
            recommended_memory = best.get("memorySize", "unknown")
            savings_opportunity = best.get("savingsOpportunity", {})

            monthly_savings: float | None = None
            savings_value = savings_opportunity.get("estimatedMonthlySavings", {})
            if savings_value:
                try:
                    monthly_savings = float(savings_value.get("value", 0))
                except (TypeError, ValueError):
                    pass

            severity = "high" if (monthly_savings or 0) >= 100 else "medium"

            title_parts = [
                f"Compute Optimizer: Lambda '{function_name}' memory over-provisioned "
                f"— {current_memory} MB configured, {recommended_memory} MB recommended"
            ]
            if monthly_savings is not None:
                title_parts.append(f" (est. ${monthly_savings:.2f}/mo savings)")

            findings.append(
                Finding(
                    kra="performance",
                    severity=severity,
                    resource_arn=function_arn,
                    resource_type="AWS::Lambda::Function",
                    region=region,
                    title="".join(title_parts),
                    evidence={
                        "FunctionArn": function_arn,
                        "FunctionName": function_name,
                        "CurrentMemoryMB": current_memory,
                        "RecommendedMemoryMB": recommended_memory,
                        "Finding": rec.get("finding"),
                        "FindingReasonCodes": rec.get("findingReasonCodes", []),
                        "EstimatedMonthlySavingsUSD": monthly_savings,
                        "SavingsOpportunityPct": savings_opportunity.get(
                            "savingsOpportunityPercentage"
                        ),
                        "TopRecommendationOption": best,
                        "LookBackPeriodDays": rec.get("lookBackPeriodInDays"),
                    },
                    recommendation=(
                        f"Update the Lambda function memory configuration from "
                        f"{current_memory} MB to {recommended_memory} MB. Run "
                        "integration tests to confirm p99 duration stays within "
                        "acceptable bounds — lower memory reduces CPU share and may "
                        "slightly increase execution time. Use AWS Lambda Power Tuning "
                        "(open-source Step Functions tool) to validate the optimal "
                        "memory/cost trade-off for your workload."
                    ),
                    detector_id=detector_id,
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

ALL_DETECTORS = (
    check_autoscaling_coverage,      # PERF-001
    find_underutilized_rds,          # PERF-002
    find_oversized_ec2,              # PERF-003
    find_xray_high_error_rate,       # PERF-004
    find_xray_high_latency,          # PERF-005
    find_compute_optimizer_ec2,      # PERF-006
    find_compute_optimizer_lambda,   # PERF-007
)


async def _run_all_async(ctx: DetectorContext) -> list[Finding]:
    tasks = [asyncio.to_thread(fn, ctx) for fn in ALL_DETECTORS]
    results = await asyncio.gather(*tasks)
    out: list[Finding] = []
    for result in results:
        out.extend(result)
    return out


def run_all(ctx: DetectorContext) -> list[Finding]:
    """Run every performance detector and concatenate findings."""
    return asyncio.run(_run_all_async(ctx))