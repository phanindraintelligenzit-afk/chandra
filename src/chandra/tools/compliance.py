"""Compliance KRA detectors.

Detector IDs:

* ``COMP-001-rds-unencrypted``      — RDS DB instance with ``StorageEncrypted=False``
* ``COMP-002-no-cloudtrail``        — account lacks a multi-region, logging, validated trail
* ``COMP-003-no-config-recorder``   — AWS Config recorder missing or not recording in a region
* ``COMP-004-ebs-unencrypted``      — EBS volume created without encryption
* ``COMP-005-s3-default-enc``       — S3 bucket without default encryption configured
* ``COMP-006-no-backup-protection`` — no AWS Backup protected resources found in the account
* ``COMP-007-config-rule-noncompliant`` — AWS Config rule whose compliance is NON_COMPLIANT
* ``COMP-008-ebs-default-enc-off``  — EBS encryption-by-default disabled at the account/region level
* ``COMP-009-missing-tags``         — resources missing mandatory organizational tags
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from src.chandra.briefing.schemas import Finding
from src.chandra.logging import get_logger
from src.chandra.tools.base import DetectorContext, detector_guard, paginate

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# COMP-001  RDS unencrypted storage
# ---------------------------------------------------------------------------


def check_encryption_at_rest_rds(ctx: DetectorContext) -> list[Finding]:
    """Flag RDS DB instances without storage encryption."""
    detector_id = "COMP-001-rds-unencrypted"
    findings: list[Finding] = []

    for region in ctx.regions:
        rds = ctx.factory.client("rds", region=region)
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(rds, "describe_db_instances"):
                for db in page.get("DBInstances", []):
                    if db.get("DBInstanceStatus") == "deleting":
                        continue
                    if db.get("StorageEncrypted"):
                        continue
                    arn = db["DBInstanceArn"]
                    findings.append(
                        Finding(
                            kra="compliance",
                            severity="critical",
                            resource_arn=arn,
                            resource_type="AWS::RDS::DBInstance",
                            region=region,
                            title=(
                                f"RDS instance {db['DBInstanceIdentifier']} "
                                f"({db.get('Engine')}) has storage encryption disabled"
                            ),
                            evidence={
                                "DBInstanceIdentifier": db["DBInstanceIdentifier"],
                                "Engine": db.get("Engine"),
                                "StorageEncrypted": False,
                            },
                            recommendation=(
                                "Take a snapshot, copy it with encryption enabled "
                                "(KMS CMK), and restore into a new encrypted instance. "
                                "Decommission the unencrypted source after cutover."
                            ),
                            detector_id=detector_id,
                        )
                    )

    return findings


# ---------------------------------------------------------------------------
# COMP-002  CloudTrail multi-region
# ---------------------------------------------------------------------------


def check_cloudtrail_multi_region(ctx: DetectorContext) -> list[Finding]:
    """Emit a single account-scoped finding if no compliant trail exists.

    A compliant trail is multi-region, currently logging, and has log-file
    validation enabled.
    """
    detector_id = "COMP-002-no-cloudtrail"
    findings: list[Finding] = []
    compliant_trails: list[dict[str, Any]] = []

    for region in ctx.regions:
        ct = ctx.factory.client("cloudtrail", region=region)
        with detector_guard(ctx, detector_id=detector_id, region=region):
            resp = ct.describe_trails(includeShadowTrails=False)
            for trail in resp.get("trailList", []):
                if not trail.get("IsMultiRegionTrail"):
                    continue
                if not trail.get("LogFileValidationEnabled"):
                    continue
                status = ct.get_trail_status(Name=trail["TrailARN"])
                if not status.get("IsLogging"):
                    continue
                compliant_trails.append(trail)

    if not compliant_trails:
        findings.append(
            Finding(
                kra="compliance",
                severity="critical",
                resource_arn=f"arn:aws:cloudtrail::{ctx.account_id}:account",
                resource_type="AWS::CloudTrail::Account",
                region="global",
                title=(
                    "No multi-region CloudTrail with log-file validation is "
                    "actively logging in this account"
                ),
                evidence={"CompliantTrailsFound": 0, "RegionsChecked": ctx.regions},
                recommendation=(
                    "Create or re-enable a multi-region CloudTrail with "
                    "log-file validation. Ship logs to a dedicated, "
                    "MFA-Delete-protected S3 bucket and enable CloudWatch Logs."
                ),
                detector_id=detector_id,
            )
        )

    return findings


# ---------------------------------------------------------------------------
# COMP-003  Config recorder active per region
# ---------------------------------------------------------------------------


def check_config_recorder(ctx: DetectorContext) -> list[Finding]:
    """Emit one finding per active region missing an AWS Config recorder."""
    detector_id = "COMP-003-no-config-recorder"
    findings: list[Finding] = []

    for region in ctx.regions:
        config = ctx.factory.client("config", region=region)
        recorder_ok = False
        recorder_evidence: dict[str, Any] = {}
        with detector_guard(ctx, detector_id=detector_id, region=region):
            recs = config.describe_configuration_recorders().get("ConfigurationRecorders", [])
            statuses = config.describe_configuration_recorder_status().get(
                "ConfigurationRecordersStatus", []
            )
            recorder_evidence = {
                "ConfigurationRecorders": recs,
                "Statuses": statuses,
            }
            if recs and any(s.get("recording") for s in statuses):
                recorder_ok = True

        if not recorder_ok:
            findings.append(
                Finding(
                    kra="compliance",
                    severity="high",
                    resource_arn=f"arn:aws:config:{region}:{ctx.account_id}:recorder",
                    resource_type="AWS::Config::ConfigurationRecorder",
                    region=region,
                    title=(
                        f"AWS Config recorder is not active in {region} — "
                        "compliance posture is unobservable"
                    ),
                    evidence=recorder_evidence,
                    recommendation=(
                        "Enable AWS Config in this region with the default "
                        "Conformance Pack for CIS or a delegated administrator "
                        "via AWS Organizations."
                    ),
                    detector_id=detector_id,
                )
            )

    return findings


# ---------------------------------------------------------------------------
# COMP-004  EBS unencrypted volumes
# ---------------------------------------------------------------------------


def check_encryption_at_rest_ebs(ctx: DetectorContext) -> list[Finding]:
    """Flag any EBS volume with ``Encrypted=False``."""
    detector_id = "COMP-004-ebs-unencrypted"
    findings: list[Finding] = []

    for region in ctx.regions:
        ec2 = ctx.factory.client("ec2", region=region)
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(ec2, "describe_volumes"):
                for vol in page.get("Volumes", []):
                    if vol.get("State") == "deleting":
                        continue
                    if vol.get("Encrypted"):
                        continue
                    volume_id = vol["VolumeId"]
                    arn = f"arn:aws:ec2:{region}:{ctx.account_id}:volume/{volume_id}"
                    findings.append(
                        Finding(
                            kra="compliance",
                            severity="high",
                            resource_arn=arn,
                            resource_type="AWS::EC2::Volume",
                            region=region,
                            title=(
                                f"EBS volume {volume_id} ({vol.get('Size')} GiB) is unencrypted"
                            ),
                            evidence={
                                "VolumeId": volume_id,
                                "Size": vol.get("Size"),
                                "Encrypted": False,
                            },
                            recommendation=(
                                "Enable EBS encryption-by-default at the account "
                                "level (see COMP-008), then snapshot/copy each "
                                "unencrypted volume into an encrypted replacement."
                            ),
                            detector_id=detector_id,
                        )
                    )

    return findings


# ---------------------------------------------------------------------------
# COMP-005  S3 default encryption
# ---------------------------------------------------------------------------


def check_s3_default_encryption(ctx: DetectorContext) -> list[Finding]:
    """Flag S3 buckets without a default encryption configuration."""
    detector_id = "COMP-005-s3-default-enc"
    findings: list[Finding] = []
    s3 = ctx.factory.client("s3")

    buckets: list[str] = []
    with detector_guard(ctx, detector_id=detector_id):
        resp = s3.list_buckets()
        buckets = [b["Name"] for b in resp.get("Buckets", [])]

    for name in buckets:
        arn = f"arn:aws:s3:::{name}"
        encrypted = True
        evidence: dict[str, Any] = {}
        with detector_guard(ctx, detector_id=detector_id, resource_arn=arn):
            try:
                resp = s3.get_bucket_encryption(Bucket=name)
                evidence = resp.get("ServerSideEncryptionConfiguration", {})
            except s3.exceptions.ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code == "ServerSideEncryptionConfigurationNotFoundError":
                    encrypted = False
                    evidence = {"ServerSideEncryptionConfiguration": None}
                else:
                    raise
        if not encrypted:
            findings.append(
                Finding(
                    kra="compliance",
                    severity="high",
                    resource_arn=arn,
                    resource_type="AWS::S3::Bucket",
                    region=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
                    title=f"S3 bucket {name} has no default encryption configured",
                    evidence=evidence,
                    recommendation=(
                        "Enable default SSE-S3 or SSE-KMS on the bucket. Optionally "
                        "deny ``s3:PutObject`` requests without "
                        "``x-amz-server-side-encryption`` via a bucket policy."
                    ),
                    detector_id=detector_id,
                )
            )

    return findings


# ---------------------------------------------------------------------------
# COMP-006  AWS Backup — no protected resources
# ---------------------------------------------------------------------------


def check_backup_protection(ctx: DetectorContext) -> list[Finding]:
    """Emit a single account-scoped finding if AWS Backup has no protected resources.

    An empty protected-resource list means critical workloads (RDS, EBS, EFS,
    DynamoDB, etc.) are likely not covered by any backup plan, which violates
    most compliance frameworks (SOC 2, ISO 27001, PCI-DSS).
    """
    detector_id = "COMP-006-no-backup-protection"
    findings: list[Finding] = []
    backup = ctx.factory.client("backup")

    protected_resources: list[dict[str, Any]] = []
    with detector_guard(ctx, detector_id=detector_id):
        for page in paginate(backup, "list_protected_resources"):
            protected_resources.extend(page.get("Results", []))

    if not protected_resources:
        findings.append(
            Finding(
                kra="compliance",
                severity="high",
                resource_arn=f"arn:aws:backup::{ctx.account_id}:account",
                resource_type="AWS::Backup::Account",
                region="global",
                title=(
                    "AWS Backup has no protected resources — workloads are not "
                    "covered by any backup plan"
                ),
                evidence={
                    "ProtectedResourceCount": 0,
                    "AccountId": ctx.account_id,
                },
                recommendation=(
                    "Create one or more AWS Backup plans with appropriate retention "
                    "windows and assign resource selections covering RDS, EBS, EFS, "
                    "DynamoDB, and any other stateful services. Validate restore "
                    "capability on a regular schedule."
                ),
                detector_id=detector_id,
            )
        )

    return findings


# ---------------------------------------------------------------------------
# COMP-007  AWS Config rule-level compliance
# ---------------------------------------------------------------------------


def check_config_rule_compliance(ctx: DetectorContext) -> list[Finding]:
    """Emit one finding per Config rule whose aggregate compliance type is NON_COMPLIANT.

    This is a rule-level view: it answers *which rules are failing* rather than
    which individual resources are non-compliant (that is SEC-006's job).  The
    two detectors complement each other — this one is useful for compliance
    dashboards that track rule-adoption health.
    """
    detector_id = "COMP-007-config-rule-noncompliant"
    findings: list[Finding] = []

    for region in ctx.regions:
        config = ctx.factory.client("config", region=region)
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(config, "describe_compliance_by_config_rule"):
                for item in page.get("ComplianceByConfigRules", []):
                    compliance = item.get("Compliance", {})
                    if compliance.get("ComplianceType") != "NON_COMPLIANT":
                        continue

                    rule_name = item.get("ConfigRuleName", "unknown")
                    # Annotate count of non-compliant vs compliant resources when
                    # the API provides it (not always populated for custom rules).
                    contributor_counts = compliance.get("ComplianceContributorCount", {})

                    findings.append(
                        Finding(
                            kra="compliance",
                            severity="medium",
                            resource_arn=(
                                f"arn:aws:config:{region}:{ctx.account_id}:config-rule/{rule_name}"
                            ),
                            resource_type="AWS::Config::ConfigRule",
                            region=region,
                            title=(f"AWS Config rule '{rule_name}' is NON_COMPLIANT in {region}"),
                            evidence={
                                "ConfigRuleName": rule_name,
                                "ComplianceType": "NON_COMPLIANT",
                                "ComplianceContributorCount": contributor_counts,
                                "Region": region,
                            },
                            recommendation=(
                                "Open the Config console, select the rule, and "
                                "review the list of non-compliant resources. Use "
                                "the rule's remediation action or fix resources "
                                "manually, then re-evaluate to confirm compliance."
                            ),
                            detector_id=detector_id,
                        )
                    )

    return findings


# ---------------------------------------------------------------------------
# COMP-008  EBS encryption-by-default (account/region level)
# ---------------------------------------------------------------------------


def check_ebs_encryption_by_default(ctx: DetectorContext) -> list[Finding]:
    """Emit one finding per region where EBS encryption-by-default is disabled.

    This is an account-level preventive control: new EBS volumes and snapshots
    created without an explicit encryption setting will be unencrypted if this
    flag is off.  This complements COMP-004 (which detects already-existing
    unencrypted volumes) by closing the gap for future resources.
    """
    detector_id = "COMP-008-ebs-default-enc-off"
    findings: list[Finding] = []

    for region in ctx.regions:
        ec2 = ctx.factory.client("ec2", region=region)
        with detector_guard(ctx, detector_id=detector_id, region=region):
            resp = ec2.get_ebs_encryption_by_default()
            if resp.get("EbsEncryptionByDefault"):
                continue

            default_kms = resp.get("KmsKeyId", "aws/ebs (default)")
            findings.append(
                Finding(
                    kra="compliance",
                    severity="high",
                    resource_arn=(
                        f"arn:aws:ec2:{region}:{ctx.account_id}:ebs-encryption-by-default"
                    ),
                    resource_type="AWS::EC2::EBSEncryptionByDefault",
                    region=region,
                    title=(
                        f"EBS encryption-by-default is disabled in {region} — "
                        "new volumes will be unencrypted unless explicitly set"
                    ),
                    evidence={
                        "EbsEncryptionByDefault": False,
                        "KmsKeyId": default_kms,
                        "Region": region,
                    },
                    recommendation=(
                        "Enable EBS encryption-by-default via "
                        "``ec2:EnableEbsEncryptionByDefault`` in every active region. "
                        "Specify a customer-managed KMS key rather than the AWS "
                        "default key for finer-grained access control."
                    ),
                    detector_id=detector_id,
                )
            )

    return findings


def check_missing_mandatory_tags(ctx: DetectorContext) -> list[Finding]:
    """Flag resources that are missing mandatory organizational tags.

    Checks across all active regions for resources lacking required
    tag keys (e.g., 'Environment', 'Owner', 'Project') using the
    AWS Resource Groups Tagging API.
    """
    detector_id = "COMP-009-missing-tags"
    findings: list[Finding] = []

    # Define the baseline tags required by your policy
    mandatory_tags = {"Environment", "Owner", "Project"}

    for region in ctx.regions:
        tagging = ctx.factory.client("resourcegroupstaggingapi", region=region)

        with detector_guard(ctx, detector_id=detector_id, region=region):
            # Use the framework's pagination tool to handle large environments
            for page in paginate(tagging, "get_resources"):
                for resource in page.get("ResourceTagMappingList", []):
                    arn = resource.get("ResourceARN")
                    if not arn:
                        continue

                    # Extract just the tag keys from the resource
                    current_tags = {t.get("Key") for t in resource.get("Tags", [])}
                    missing_tags = mandatory_tags - current_tags

                    if missing_tags:
                        # Attempt to cleanly extract the resource type from the ARN
                        # (e.g., splitting "arn:aws:ec2:..." to get "ec2")
                        try:
                            service_name = arn.split(":")[2]
                            res_type = f"AWS::{service_name.upper()}::Resource"
                        except IndexError:
                            res_type = "AWS::Generic::Resource"

                        findings.append(
                            Finding(
                                kra="compliance",
                                severity="medium",  # Medium severity for tagging violations
                                resource_arn=arn,
                                resource_type=res_type,
                                region=region,
                                title=(
                                    f"Resource is missing mandatory tags: "
                                    f"{', '.join(sorted(missing_tags))}"
                                ),
                                evidence={
                                    "MissingTags": sorted(list(missing_tags)),
                                    "CurrentTags": sorted(list(current_tags)),
                                    "RequiredTags": sorted(list(mandatory_tags)),
                                },
                                recommendation=(
                                    "Apply the missing mandatory tags to comply with "
                                    "the organization's resource allocation and billing "
                                    "tag policy. You can use AWS Tag Editor to update "
                                    "these in bulk."
                                ),
                                detector_id=detector_id,
                            )
                        )

    return findings


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# TEMP (testing): high-volume detectors commented out to reduce finding count.
# Re-enable by uncommenting the entries below once we're done with the smoke run.
ALL_DETECTORS = (
    check_encryption_at_rest_rds,  # COMP-001
    check_cloudtrail_multi_region,  # COMP-002
    check_config_recorder,  # COMP-003
    check_encryption_at_rest_ebs,  # COMP-004 — disabled for testing
    check_s3_default_encryption,  # COMP-005 — disabled for testing
    check_backup_protection,  # COMP-006
    check_config_rule_compliance,  # COMP-007 — disabled for testing
    check_ebs_encryption_by_default,  # COMP-008
    check_missing_mandatory_tags,  # COMP-009 — disabled for testing
)


async def _run_all_async(ctx: DetectorContext) -> list[Finding]:
    tasks = [asyncio.to_thread(fn, ctx) for fn in ALL_DETECTORS]
    results = await asyncio.gather(*tasks)
    out: list[Finding] = []
    for result in results:
        out.extend(result)
    return out


def run_all(ctx: DetectorContext) -> list[Finding]:
    """Run every compliance detector and concatenate findings."""
    return asyncio.run(_run_all_async(ctx))
