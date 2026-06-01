"""Reliability KRA detectors.

Detector IDs:

* ``REL-001-rds-single-az`` — prod-tagged RDS without Multi-AZ.
* ``REL-002-s3-versioning`` — versioning disabled on ``Criticality=high`` buckets.
* ``REL-003-no-dlm``        — no DLM lifecycle policy in a region with attached volumes.
* ``REL-004-no-backup-plan``— no AWS Backup plans configured in an active region.
"""

from __future__ import annotations

import asyncio
from typing import Any

from src.chandra.briefing.schemas import Finding
from src.chandra.logging import get_logger
from src.chandra.tools.base import DetectorContext, detector_guard, paginate

logger = get_logger(__name__)

PROD_VALUES = {"prod", "production"}
CRITICAL_TAG_KEY = "Criticality"
CRITICAL_TAG_VALUES = {"high", "critical"}


def check_rds_multi_az(ctx: DetectorContext) -> list[Finding]:
    """Flag prod-tagged RDS instances that are NOT Multi-AZ deployments."""
    detector_id = "REL-001-rds-single-az"
    findings: list[Finding] = []

    for region in ctx.regions:
        rds = ctx.factory.client("rds", region=region)
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(rds, "describe_db_instances"):
                for db in page.get("DBInstances", []):
                    tags = {t["Key"]: t["Value"] for t in db.get("TagList", []) or []}
                    env = tags.get("Environment", "").lower()
                    if env not in PROD_VALUES:
                        continue
                    if db.get("MultiAZ"):
                        continue
                    arn = db["DBInstanceArn"]
                    findings.append(
                        Finding(
                            kra="reliability",
                            severity="high",
                            resource_arn=arn,
                            resource_type="AWS::RDS::DBInstance",
                            region=region,
                            title=(
                                f"Production RDS {db['DBInstanceIdentifier']} "
                                "is deployed in a single AZ"
                            ),
                            evidence={
                                "DBInstanceIdentifier": db["DBInstanceIdentifier"],
                                "MultiAZ": False,
                                "Environment": tags.get("Environment"),
                            },
                            recommendation=(
                                "Modify the instance to enable Multi-AZ. For Aurora, "
                                "ensure at least one reader replica in a separate AZ."
                            ),
                            detector_id=detector_id,
                        )
                    )

    return findings


def check_s3_versioning(ctx: DetectorContext) -> list[Finding]:
    """Flag buckets tagged ``Criticality=high|critical`` without versioning enabled."""
    detector_id = "REL-002-s3-versioning"
    findings: list[Finding] = []
    s3 = ctx.factory.client("s3")

    buckets: list[str] = []
    with detector_guard(ctx, detector_id=detector_id):
        resp = s3.list_buckets()
        buckets = [b["Name"] for b in resp.get("Buckets", [])]

    for name in buckets:
        arn = f"arn:aws:s3:::{name}"
        tags: dict[str, str] = {}
        with detector_guard(ctx, detector_id=detector_id, resource_arn=arn):
            try:
                tag_resp = s3.get_bucket_tagging(Bucket=name)
                tags = {t["Key"]: t["Value"] for t in tag_resp.get("TagSet", [])}
            except s3.exceptions.ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code != "NoSuchTagSet":
                    raise

        if tags.get(CRITICAL_TAG_KEY, "").lower() not in CRITICAL_TAG_VALUES:
            continue

        versioning_enabled = False
        with detector_guard(ctx, detector_id=detector_id, resource_arn=arn):
            ver = s3.get_bucket_versioning(Bucket=name)
            versioning_enabled = ver.get("Status") == "Enabled"

        if not versioning_enabled:
            findings.append(
                Finding(
                    kra="reliability",
                    severity="high",
                    resource_arn=arn,
                    resource_type="AWS::S3::Bucket",
                    region="us-east-1",
                    title=(
                        f"S3 bucket {name} is tagged "
                        f"{CRITICAL_TAG_KEY}={tags[CRITICAL_TAG_KEY]} "
                        "but versioning is disabled"
                    ),
                    evidence={"Tags": tags, "VersioningStatus": "Disabled"},
                    recommendation=(
                        "Enable versioning and, for highly-sensitive data, "
                        "MFA Delete. Pair with a lifecycle rule to expire "
                        "non-current versions on a defined schedule."
                    ),
                    detector_id=detector_id,
                )
            )

    return findings


def check_ebs_snapshot_policy(ctx: DetectorContext) -> list[Finding]:
    """Flag regions that have attached EBS volumes but no DLM lifecycle policy."""
    detector_id = "REL-003-no-dlm"
    findings: list[Finding] = []

    for region in ctx.regions:
        ec2 = ctx.factory.client("ec2", region=region)
        dlm = ctx.factory.client("dlm", region=region)

        has_attached_volumes = False
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(
                ec2,
                "describe_volumes",
                Filters=[{"Name": "status", "Values": ["in-use"]}],
            ):
                if page.get("Volumes"):
                    has_attached_volumes = True
                    break

        if not has_attached_volumes:
            continue

        has_policy = False
        evidence: dict[str, Any] = {}
        with detector_guard(ctx, detector_id=detector_id, region=region):
            resp = dlm.get_lifecycle_policies()
            policies = resp.get("Policies", [])
            evidence = {"PolicyCount": len(policies)}
            has_policy = bool(policies)

        if not has_policy:
            findings.append(
                Finding(
                    kra="reliability",
                    severity="medium",
                    resource_arn=f"arn:aws:dlm:{region}:{ctx.account_id}:policy/*",
                    resource_type="AWS::DLM::LifecyclePolicy",
                    region=region,
                    title=(
                        f"Region {region} has in-use EBS volumes but no DLM "
                        "lifecycle policy"
                    ),
                    evidence=evidence,
                    recommendation=(
                        "Create a DLM policy targeting volumes by tag (e.g. "
                        "Backup=daily) with a retention schedule that matches "
                        "your RPO."
                    ),
                    detector_id=detector_id,
                )
            )

    return findings


def check_backup_plans(ctx: DetectorContext) -> list[Finding]:
    """Flag regions with zero AWS Backup plans."""
    detector_id = "REL-004-no-backup-plan"
    findings: list[Finding] = []

    for region in ctx.regions:
        backup = ctx.factory.client("backup", region=region)
        plan_count = 0
        evidence: dict[str, Any] = {}
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(backup, "list_backup_plans"):
                plans = page.get("BackupPlansList", [])
                plan_count += len(plans)
            evidence = {"PlanCount": plan_count}

        if plan_count == 0:
            findings.append(
                Finding(
                    kra="reliability",
                    severity="medium",
                    resource_arn=f"arn:aws:backup:{region}:{ctx.account_id}:backup-plan/*",
                    resource_type="AWS::Backup::BackupPlan",
                    region=region,
                    title=f"No AWS Backup plans configured in {region}",
                    evidence=evidence,
                    recommendation=(
                        "Create at least one AWS Backup plan with a retention "
                        "rule that satisfies your business RPO/RTO, and assign "
                        "resources via tag-based selection."
                    ),
                    detector_id=detector_id,
                )
            )

    return findings


ALL_DETECTORS = (
    check_rds_multi_az,
    check_s3_versioning,
    check_ebs_snapshot_policy,
    check_backup_plans,
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
