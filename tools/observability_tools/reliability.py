"""Reliability KRA detectors."""

from __future__ import annotations

from typing import Any

from briefing.schemas import Finding
from structlog import get_logger
from tools.observability_tools import DetectorContext, detector_guard, paginate, run_per_region

logger = get_logger(__name__)

PROD_VALUES = {"prod", "production"}
CRITICAL_TAG_KEY = "Criticality"
CRITICAL_TAG_VALUES = {"high", "critical"}


def check_rds_multi_az(ctx: DetectorContext) -> list[Finding]:
    detector_id = "REL-001-rds-single-az"

    def _check_region(ctx: DetectorContext, region: str) -> list[Finding]:
        rds = ctx.factory.client("rds", region=region)
        findings: list[Finding] = []
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(rds, "describe_db_instances"):
                for db in page.get("DBInstances", []):
                    tags = {t["Key"]: t["Value"] for t in db.get("TagList", []) or []}
                    if tags.get("Environment", "").lower() not in PROD_VALUES:
                        continue
                    if db.get("MultiAZ"):
                        continue
                    findings.append(
                        Finding(
                            kra="reliability",
                            severity="high",
                            resource_arn=db["DBInstanceArn"],
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

    return run_per_region(ctx, _check_region)


def check_s3_versioning(ctx: DetectorContext) -> list[Finding]:
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
                if exc.response.get("Error", {}).get("Code") != "NoSuchTagSet":
                    raise

        if tags.get(CRITICAL_TAG_KEY, "").lower() not in CRITICAL_TAG_VALUES:
            continue

        with detector_guard(ctx, detector_id=detector_id, resource_arn=arn):
            if s3.get_bucket_versioning(Bucket=name).get("Status") != "Enabled":
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
    detector_id = "REL-003-no-dlm"

    def _check_region(ctx: DetectorContext, region: str) -> list[Finding]:
        ec2 = ctx.factory.client("ec2", region=region)
        dlm = ctx.factory.client("dlm", region=region)

        has_attached = False
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(ec2, "describe_volumes", Filters=[{"Name": "status", "Values": ["in-use"]}]):
                if page.get("Volumes"):
                    has_attached = True
                    break

        if not has_attached:
            return []

        evidence: dict[str, Any] = {}
        with detector_guard(ctx, detector_id=detector_id, region=region):
            policies = dlm.get_lifecycle_policies().get("Policies", [])
            evidence = {"PolicyCount": len(policies)}
            if policies:
                return []

        return [
            Finding(
                kra="reliability",
                severity="medium",
                resource_arn=f"arn:aws:dlm:{region}:{ctx.account_id}:policy/*",
                resource_type="AWS::DLM::LifecyclePolicy",
                region=region,
                title=f"Region {region} has in-use EBS volumes but no DLM lifecycle policy",
                evidence=evidence,
                recommendation=(
                    "Create a DLM policy targeting volumes by tag (e.g. "
                    "Backup=daily) with a retention schedule that matches your RPO."
                ),
                detector_id=detector_id,
            )
        ]

    return run_per_region(ctx, _check_region)


def check_backup_plans(ctx: DetectorContext) -> list[Finding]:
    detector_id = "REL-004-no-backup-plan"

    def _check_region(ctx: DetectorContext, region: str) -> list[Finding]:
        backup = ctx.factory.client("backup", region=region)
        plan_count = 0
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(backup, "list_backup_plans"):
                plan_count += len(page.get("BackupPlansList", []))

        if plan_count > 0:
            return []
        return [
            Finding(
                kra="reliability",
                severity="medium",
                resource_arn=f"arn:aws:backup:{region}:{ctx.account_id}:backup-plan/*",
                resource_type="AWS::Backup::BackupPlan",
                region=region,
                title=f"No AWS Backup plans configured in {region}",
                evidence={"PlanCount": 0},
                recommendation=(
                    "Create at least one AWS Backup plan with a retention "
                    "rule that satisfies your business RPO/RTO, and assign "
                    "resources via tag-based selection."
                ),
                detector_id=detector_id,
            )
        ]

    return run_per_region(ctx, _check_region)


ALL_DETECTORS = (
    check_rds_multi_az,
    check_s3_versioning,
    check_ebs_snapshot_policy,
    check_backup_plans,
)


def run_all(ctx: DetectorContext) -> list[Finding]:
    out: list[Finding] = []
    for fn in ALL_DETECTORS:
        out.extend(fn(ctx))
    return out
