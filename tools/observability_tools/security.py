"""Security KRA detectors."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from src.chandra.briefing.schemas import Finding
from src.chandra.config import settings
from structlog import get_logger
from tools.observability_tools import DetectorContext, detector_guard, paginate, run_per_region

logger = get_logger(__name__)

STALE_KEY_DAYS = 90
DANGEROUS_PORTS: dict[int, str] = {22: "SSH", 3389: "RDP", 3306: "MySQL", 5432: "PostgreSQL"}


def _stale_key_threshold_days() -> int:
    override = settings.stale_key_days_override
    return override if override is not None else STALE_KEY_DAYS


def find_public_s3_buckets(ctx: DetectorContext) -> list[Finding]:
    detector_id = "SEC-001-public-s3"
    findings: list[Finding] = []
    s3 = ctx.factory.client("s3")

    bucket_names: list[str] = []
    with detector_guard(ctx, detector_id=detector_id):
        resp = s3.list_buckets()
        bucket_names = [b["Name"] for b in resp.get("Buckets", [])]

    for name in bucket_names:
        arn = f"arn:aws:s3:::{name}"
        region = "us-east-1"
        with detector_guard(ctx, detector_id=detector_id, resource_arn=arn):
            loc = s3.get_bucket_location(Bucket=name).get("LocationConstraint")
            region = loc or "us-east-1"

        pab_disabled = False
        pab_evidence: dict[str, Any] = {}
        with detector_guard(ctx, detector_id=detector_id, resource_arn=arn):
            try:
                pab = s3.get_public_access_block(Bucket=name)["PublicAccessBlockConfiguration"]
                pab_evidence = pab
                if not all(
                    pab.get(k, False)
                    for k in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")
                ):
                    pab_disabled = True
            except s3.exceptions.ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code == "NoSuchPublicAccessBlockConfiguration":
                    pab_disabled = True
                    pab_evidence = {"PublicAccessBlockConfiguration": None}
                else:
                    raise

        policy_public = False
        policy_evidence: dict[str, Any] = {}
        with detector_guard(ctx, detector_id=detector_id, resource_arn=arn):
            try:
                policy_doc = json.loads(s3.get_bucket_policy(Bucket=name)["Policy"])
                policy_evidence = {"Policy": policy_doc}
                policy_public = _bucket_policy_is_public(policy_doc)
            except s3.exceptions.ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code == "NoSuchBucketPolicy":
                    policy_evidence = {"Policy": None}
                else:
                    raise

        if pab_disabled or policy_public:
            severity = "critical" if policy_public else "high"
            reason_bits = []
            if pab_disabled:
                reason_bits.append("public access block disabled")
            if policy_public:
                reason_bits.append("bucket policy allows wildcard principal")
            findings.append(
                Finding(
                    kra="security",
                    severity=severity,
                    resource_arn=arn,
                    resource_type="AWS::S3::Bucket",
                    region=region,
                    title=f"S3 bucket {name} is publicly exposed ({'; '.join(reason_bits)})",
                    evidence={**pab_evidence, **policy_evidence},
                    recommendation=(
                        "Enable all four Block Public Access settings on the bucket and "
                        "remove wildcard Principal statements from the bucket policy."
                    ),
                    detector_id=detector_id,
                )
            )

    return findings


def _bucket_policy_is_public(policy_doc: dict[str, Any]) -> bool:
    for stmt in policy_doc.get("Statement", []) or []:
        if stmt.get("Effect") != "Allow":
            continue
        principal = stmt.get("Principal")
        if principal == "*":
            return True
        if isinstance(principal, dict):
            aws = principal.get("AWS")
            if aws == "*" or (isinstance(aws, list) and "*" in aws):
                return True
    return False


def find_open_security_groups(ctx: DetectorContext) -> list[Finding]:
    detector_id = "SEC-002-open-sg-ssh"

    def _check_region(ctx: DetectorContext, region: str) -> list[Finding]:
        ec2 = ctx.factory.client("ec2", region=region)
        findings: list[Finding] = []
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(ec2, "describe_security_groups"):
                for sg in page.get("SecurityGroups", []):
                    findings.extend(_inspect_security_group(sg, region, detector_id))
        return findings

    return run_per_region(ctx, _check_region)


def _inspect_security_group(sg: dict[str, Any], region: str, detector_id: str) -> list[Finding]:
    findings: list[Finding] = []
    sg_id = sg["GroupId"]
    owner_id = sg.get("OwnerId", "unknown")
    arn = f"arn:aws:ec2:{region}:{owner_id}:security-group/{sg_id}"

    exposed: list[tuple[int, str]] = []
    for perm in sg.get("IpPermissions", []) or []:
        ip_protocol = perm.get("IpProtocol")
        if ip_protocol not in ("tcp", "-1"):
            continue
        from_port = perm.get("FromPort")
        to_port = perm.get("ToPort")
        open_to_world = any(r.get("CidrIp") == "0.0.0.0/0" for r in perm.get("IpRanges", []) or [])
        if not open_to_world:
            continue
        if ip_protocol == "-1":
            exposed.extend(DANGEROUS_PORTS.items())
            continue
        if from_port is None or to_port is None:
            continue
        for port, name in DANGEROUS_PORTS.items():
            if from_port <= port <= to_port:
                exposed.append((port, name))

    if not exposed:
        return findings

    severity = "critical" if any(p in (22, 3389) for p, _ in exposed) else "high"
    ports_listed = ", ".join(f"{p} ({n})" for p, n in exposed)
    findings.append(
        Finding(
            kra="security",
            severity=severity,
            resource_arn=arn,
            resource_type="AWS::EC2::SecurityGroup",
            region=region,
            title=f"Security group {sg_id} exposes {ports_listed} to 0.0.0.0/0",
            evidence={"GroupId": sg_id, "IpPermissions": sg.get("IpPermissions", [])},
            recommendation=(
                "Restrict the offending ingress rules to your corporate CIDR range, "
                "a VPN, or replace with SSM Session Manager / IAM-authenticated access."
            ),
            detector_id=detector_id,
        )
    )
    return findings


def find_stale_access_keys(ctx: DetectorContext) -> list[Finding]:
    detector_id = "SEC-003-stale-key"
    findings: list[Finding] = []
    iam = ctx.factory.client("iam")
    now = datetime.now(timezone.utc)
    threshold_days = _stale_key_threshold_days()
    threshold = now - timedelta(days=threshold_days)

    users: list[dict[str, Any]] = []
    with detector_guard(ctx, detector_id=detector_id):
        for page in paginate(iam, "list_users"):
            users.extend(page.get("Users", []))

    for user in users:
        username = user["UserName"]
        user_arn = user["Arn"]
        with detector_guard(ctx, detector_id=detector_id, resource_arn=user_arn):
            for page in paginate(iam, "list_access_keys", UserName=username):
                for key in page.get("AccessKeyMetadata", []):
                    if key.get("Status") != "Active":
                        continue
                    created = key.get("CreateDate")
                    if created is None:
                        continue
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if created > threshold:
                        continue
                    age_days = (now - created).days
                    findings.append(
                        Finding(
                            kra="security",
                            severity="high" if age_days > 180 else "medium",
                            resource_arn=user_arn,
                            resource_type="AWS::IAM::AccessKey",
                            region="global",
                            title=f"IAM access key for {username} is {age_days}d old (threshold {threshold_days}d)",
                            evidence={
                                "UserName": username,
                                "AccessKeyId": key["AccessKeyId"],
                                "CreateDate": created.isoformat(),
                                "AgeDays": age_days,
                            },
                            recommendation=(
                                "Rotate the key: create a replacement, deploy it, then "
                                "deactivate and delete the old key. Prefer SSO / IAM "
                                "Identity Center over long-lived keys when possible."
                            ),
                            detector_id=detector_id,
                        )
                    )

    return findings


def check_root_mfa(ctx: DetectorContext) -> list[Finding]:
    detector_id = "SEC-004-root-mfa"
    findings: list[Finding] = []
    iam = ctx.factory.client("iam")

    with detector_guard(ctx, detector_id=detector_id):
        summary = iam.get_account_summary().get("SummaryMap", {})
        if not bool(summary.get("AccountMFAEnabled", 0)):
            findings.append(
                Finding(
                    kra="security",
                    severity="critical",
                    resource_arn=f"arn:aws:iam::{ctx.account_id}:root",
                    resource_type="AWS::IAM::RootAccount",
                    region="global",
                    title="AWS account root user does not have MFA enabled",
                    evidence={"SummaryMap": summary},
                    recommendation=(
                        "Enable a hardware or virtual MFA device on the root user "
                        "immediately and store recovery codes in a sealed envelope."
                    ),
                    detector_id=detector_id,
                )
            )

    return findings


def find_overly_permissive_iam(ctx: DetectorContext) -> list[Finding]:
    detector_id = "SEC-005-wildcard-iam"
    findings: list[Finding] = []
    iam = ctx.factory.client("iam")

    policies: list[dict[str, Any]] = []
    with detector_guard(ctx, detector_id=detector_id):
        for page in paginate(iam, "list_policies", Scope="Local", OnlyAttached=False):
            policies.extend(page.get("Policies", []))

    for policy in policies:
        policy_arn = policy["Arn"]
        with detector_guard(ctx, detector_id=detector_id, resource_arn=policy_arn):
            version_id = policy["DefaultVersionId"]
            doc = iam.get_policy_version(PolicyArn=policy_arn, VersionId=version_id)[
                "PolicyVersion"
            ]["Document"]
            if isinstance(doc, str):
                doc = json.loads(doc)
            if _policy_doc_is_wildcard(doc):
                findings.append(
                    Finding(
                        kra="security",
                        severity="critical",
                        resource_arn=policy_arn,
                        resource_type="AWS::IAM::ManagedPolicy",
                        region="global",
                        title=(
                            f"Customer-managed policy {policy['PolicyName']} grants "
                            "Action:* on Resource:*"
                        ),
                        evidence={"PolicyDocument": doc, "VersionId": version_id},
                        recommendation=(
                            "Scope the policy to the minimum set of actions and "
                            "resources required. Use IAM Access Analyzer's policy "
                            "generation to derive a least-privilege replacement."
                        ),
                        detector_id=detector_id,
                    )
                )

    return findings


def _policy_doc_is_wildcard(doc: dict[str, Any]) -> bool:
    statements = doc.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    for stmt in statements:
        if stmt.get("Effect") != "Allow":
            continue
        if _has_wildcard(stmt.get("Action")) and _has_wildcard(stmt.get("Resource")):
            return True
    return False


def _has_wildcard(field: Any) -> bool:
    if field == "*":
        return True
    if isinstance(field, list) and "*" in field:
        return True
    return False


ALL_DETECTORS = (
    find_public_s3_buckets,
    find_open_security_groups,
    find_stale_access_keys,
    check_root_mfa,
    find_overly_permissive_iam,
)


def run_all(ctx: DetectorContext) -> list[Finding]:
    out: list[Finding] = []
    for fn in ALL_DETECTORS:
        out.extend(fn(ctx))
    return out
