"""Compliance KRA detectors.

Detector IDs:

* ``COMP-001-rds-unencrypted``   — RDS DB instance with ``StorageEncrypted=False``.
* ``COMP-002-no-cloudtrail``     — account lacks a multi-region, logging, validated trail.
* ``COMP-003-no-config-recorder``— AWS Config recorder missing in an active region.
* ``COMP-004-ebs-unencrypted``   — EBS volume created without encryption.
* ``COMP-005-s3-default-enc``    — S3 bucket without default encryption configured.
"""

from __future__ import annotations

from typing import Any

from chandra.briefing.schemas import Finding
from chandra.logging import get_logger
from chandra.tools.base import DetectorContext, detector_guard, paginate

logger = get_logger(__name__)


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


def check_config_recorder(ctx: DetectorContext) -> list[Finding]:
    """Emit one finding per active region missing an AWS Config recorder."""
    detector_id = "COMP-003-no-config-recorder"
    findings: list[Finding] = []

    for region in ctx.regions:
        config = ctx.factory.client("config", region=region)
        recorder_ok = False
        recorder_evidence: dict[str, Any] = {}
        with detector_guard(ctx, detector_id=detector_id, region=region):
            recs = config.describe_configuration_recorders().get(
                "ConfigurationRecorders", []
            )
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


def check_encryption_at_rest_rds(ctx: DetectorContext) -> list[Finding]:
    """Flag RDS DB instances without storage encryption."""
    detector_id = "COMP-001-rds-unencrypted"
    findings: list[Finding] = []

    for region in ctx.regions:
        rds = ctx.factory.client("rds", region=region)
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(rds, "describe_db_instances"):
                for db in page.get("DBInstances", []):
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


def check_encryption_at_rest_ebs(ctx: DetectorContext) -> list[Finding]:
    """Flag any EBS volume with ``Encrypted=False``."""
    detector_id = "COMP-004-ebs-unencrypted"
    findings: list[Finding] = []

    for region in ctx.regions:
        ec2 = ctx.factory.client("ec2", region=region)
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(ec2, "describe_volumes"):
                for vol in page.get("Volumes", []):
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
                                f"EBS volume {volume_id} ({vol.get('Size')} GiB) "
                                "is unencrypted"
                            ),
                            evidence={
                                "VolumeId": volume_id,
                                "Size": vol.get("Size"),
                                "Encrypted": False,
                            },
                            recommendation=(
                                "Enable EBS encryption-by-default at the account "
                                "level, then snapshot/copy each unencrypted volume "
                                "into an encrypted replacement."
                            ),
                            detector_id=detector_id,
                        )
                    )

    return findings


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
                    region="us-east-1",
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


ALL_DETECTORS = (
    check_cloudtrail_multi_region,
    check_config_recorder,
    check_encryption_at_rest_rds,
    check_encryption_at_rest_ebs,
    check_s3_default_encryption,
)


def run_all(ctx: DetectorContext) -> list[Finding]:
    out: list[Finding] = []
    for fn in ALL_DETECTORS:
        out.extend(fn(ctx))
    return out
