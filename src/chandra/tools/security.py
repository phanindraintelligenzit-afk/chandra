"""Security KRA detectors.

Detector IDs:

* ``SEC-001-public-s3``          — bucket block-public-access disabled OR policy allows ``*``
* ``SEC-002-open-sg-ssh``        — SG with 0.0.0.0/0 on 22/3389/3306/5432
* ``SEC-003-stale-key``          — IAM access key older than 90 days
* ``SEC-004-root-mfa``           — root account MFA not enabled
* ``SEC-005-wildcard-iam``       — customer-managed policy with ``Action: *`` + ``Resource: *``
* ``SEC-006-config-noncompliant``— AWS Config rules with NON_COMPLIANT resources
* ``SEC-007-guardduty``          — active GuardDuty threat findings
* ``SEC-008-access-analyzer``    — IAM Access Analyzer active findings
* ``SEC-009-kms-rotation``       — customer-managed KMS keys with rotation disabled
* ``SEC-010-security-hub``       — Security Hub findings at MEDIUM severity or above
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from src.chandra.briefing.schemas import Finding
from src.chandra.config import settings
from src.chandra.logging import get_logger
from src.chandra.tools.base import DetectorContext, detector_guard, paginate

logger = get_logger(__name__)

STALE_KEY_DAYS = 90

# Security Hub severities we care about, in ascending order.
_SH_SEVERITY_RANK = {"INFORMATIONAL": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
_SH_MIN_SEVERITY = "MEDIUM"


def _stale_key_threshold_days() -> int:
    override = settings.stale_key_days_override
    return override if override is not None else STALE_KEY_DAYS


DANGEROUS_PORTS: dict[int, str] = {
    22: "SSH",
    3389: "RDP",
    3306: "MySQL",
    5432: "PostgreSQL",
}


# ---------------------------------------------------------------------------
# SEC-001  Public S3 buckets
# ---------------------------------------------------------------------------


def find_public_s3_buckets(ctx: DetectorContext) -> list[Finding]:  # noqa: PLR0915  # enumerates checks
    """Detect S3 buckets that are effectively public.

    Triggers when EITHER:

    1. Public Access Block is missing or any of its four flags is False, OR
    2. The bucket policy contains a statement with ``Effect=Allow`` and
       ``Principal`` set to ``*`` or ``{"AWS": "*"}``.
    """
    detector_id = "SEC-001-public-s3"
    findings: list[Finding] = []
    factory = ctx.factory
    s3 = factory.client("s3")

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
                pab_resp = s3.get_public_access_block(Bucket=name)
                pab = pab_resp["PublicAccessBlockConfiguration"]
                pab_evidence = pab
                if not all(
                    pab.get(k, False)
                    for k in (
                        "BlockPublicAcls",
                        "IgnorePublicAcls",
                        "BlockPublicPolicy",
                        "RestrictPublicBuckets",
                    )
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
                policy_resp = s3.get_bucket_policy(Bucket=name)
                policy_doc = json.loads(policy_resp["Policy"])
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


# ---------------------------------------------------------------------------
# SEC-002  Open security groups
# ---------------------------------------------------------------------------


def find_open_security_groups(ctx: DetectorContext) -> list[Finding]:
    """Detect SGs exposing dangerous ports to 0.0.0.0/0 in any active region."""
    detector_id = "SEC-002-open-sg-ssh"
    findings: list[Finding] = []

    for region in ctx.regions:
        ec2 = ctx.factory.client("ec2", region=region)
        with detector_guard(ctx, detector_id=detector_id, region=region):
            for page in paginate(ec2, "describe_security_groups"):
                for sg in page.get("SecurityGroups", []):
                    findings.extend(_inspect_security_group(sg, region, detector_id))

    return findings


def _inspect_security_group(sg: dict[str, Any], region: str, detector_id: str) -> list[Finding]:
    findings: list[Finding] = []
    sg_id = sg["GroupId"]
    owner_id = sg.get("OwnerId", "unknown")
    arn = f"arn:aws:ec2:{region}:{owner_id}:security-group/{sg_id}"

    exposed_set: set[tuple[int, str]] = set()
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
            for port, name in DANGEROUS_PORTS.items():
                exposed_set.add((port, name))
            continue
        if from_port is None or to_port is None:
            continue
        for port, name in DANGEROUS_PORTS.items():
            if from_port <= port <= to_port:
                exposed_set.add((port, name))

    if not exposed_set:
        return findings

    # Preserve order of DANGEROUS_PORTS to ensure deterministic output
    exposed = [(p, n) for p, n in DANGEROUS_PORTS.items() if (p, n) in exposed_set]

    severity = "critical" if any(p in (22, 3389) for p, _ in exposed) else "high"
    ports_listed = ", ".join(f"{p} ({n})" for p, n in exposed)
    findings.append(
        Finding(
            kra="security",
            severity=severity,
            resource_arn=arn,
            resource_type="AWS::EC2::SecurityGroup",
            region=region,
            title=(f"Security group {sg_id} exposes {ports_listed} to 0.0.0.0/0"),
            evidence={"GroupId": sg_id, "IpPermissions": sg.get("IpPermissions", [])},
            recommendation=(
                "Restrict the offending ingress rules to your corporate CIDR range, "
                "a VPN, or replace with SSM Session Manager / IAM-authenticated access."
            ),
            detector_id=detector_id,
        )
    )
    return findings


# ---------------------------------------------------------------------------
# SEC-003  Stale IAM access keys
# ---------------------------------------------------------------------------


def find_stale_access_keys(ctx: DetectorContext) -> list[Finding]:
    """Detect IAM access keys older than ``STALE_KEY_DAYS`` (default 90)."""
    detector_id = "SEC-003-stale-key"
    findings: list[Finding] = []
    iam = ctx.factory.client("iam")
    now = datetime.now(UTC)
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
                        created = created.replace(tzinfo=UTC)
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
                            title=(
                                f"IAM access key for {username} is {age_days}d old "
                                f"(threshold {threshold_days}d)"
                            ),
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


# ---------------------------------------------------------------------------
# SEC-004  Root MFA
# ---------------------------------------------------------------------------


def check_root_mfa(ctx: DetectorContext) -> list[Finding]:
    """Emit a finding if the AWS account root user does not have MFA enabled."""
    detector_id = "SEC-004-root-mfa"
    findings: list[Finding] = []
    iam = ctx.factory.client("iam")

    with detector_guard(ctx, detector_id=detector_id):
        summary = iam.get_account_summary().get("SummaryMap", {})
        mfa_enabled = bool(summary.get("AccountMFAEnabled", 0))
        if not mfa_enabled:
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


# ---------------------------------------------------------------------------
# SEC-005  Wildcard IAM policies
# ---------------------------------------------------------------------------


def find_overly_permissive_iam(ctx: DetectorContext) -> list[Finding]:
    """Detect customer-managed policies that grant ``Action: *`` on ``Resource: *``."""
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
            version = iam.get_policy_version(PolicyArn=policy_arn, VersionId=version_id)
            doc = version["PolicyVersion"]["Document"]
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
        actions = stmt.get("Action")
        resources = stmt.get("Resource")
        if _has_wildcard(actions) and _has_wildcard(resources):
            return True
    return False


def _has_wildcard(field: Any) -> bool:
    if field == "*":
        return True
    return bool(isinstance(field, list) and "*" in field)


# ---------------------------------------------------------------------------
# SEC-006  AWS Config non-compliant rules
# ---------------------------------------------------------------------------


def find_config_noncompliant_rules(ctx: DetectorContext) -> list[Finding]:
    """Detect resources marked NON_COMPLIANT by any active AWS Config rule.

    One finding is emitted per (rule, resource) pair so that each non-compliant
    resource can be tracked and remediated independently.
    """
    detector_id = "SEC-006-config-noncompliant"
    findings: list[Finding] = []
    config = ctx.factory.client("config")

    rules: list[dict[str, Any]] = []
    with detector_guard(ctx, detector_id=detector_id):
        for page in paginate(config, "describe_config_rules"):
            rules.extend(page.get("ConfigRules", []))

    for rule in rules:
        rule_name = rule["ConfigRuleName"]
        rule_arn = rule.get("ConfigRuleArn", rule_name)
        with detector_guard(ctx, detector_id=detector_id, resource_arn=rule_arn):
            for page in paginate(
                config,
                "get_compliance_details_by_config_rule",
                ConfigRuleName=rule_name,
                ComplianceTypes=["NON_COMPLIANT"],
            ):
                for result in page.get("EvaluationResults", []):
                    qualifier = result.get("EvaluationResultIdentifier", {}).get(
                        "EvaluationResultQualifier", {}
                    )
                    resource_id = qualifier.get("ResourceId", "unknown")
                    resource_type = qualifier.get("ResourceType", "Unknown")
                    # Build a best-effort ARN; Config doesn't always surface one.
                    resource_arn = (
                        result.get("EvaluationResultIdentifier", {})
                        .get("EvaluationResultQualifier", {})
                        .get("ResourceArn")
                        or resource_id
                    )
                    findings.append(
                        Finding(
                            kra="security",
                            severity="medium",
                            resource_arn=resource_arn,
                            resource_type=resource_type,
                            region=qualifier.get("ConfigRuleInvokedTime", "global"),
                            title=(
                                f"AWS Config rule '{rule_name}' reports "
                                f"{resource_type} {resource_id} as NON_COMPLIANT"
                            ),
                            evidence={
                                "ConfigRuleName": rule_name,
                                "ConfigRuleArn": rule_arn,
                                "ResourceId": resource_id,
                                "ResourceType": resource_type,
                                "EvaluationResult": result,
                            },
                            recommendation=(
                                "Review the Config rule definition and the resource "
                                "configuration to understand the gap, then remediate "
                                "manually or via an SSM Automation remediation action."
                            ),
                            detector_id=detector_id,
                        )
                    )

    return findings


# ---------------------------------------------------------------------------
# SEC-007  GuardDuty threat findings
# ---------------------------------------------------------------------------


def find_guardduty_threats(ctx: DetectorContext) -> list[Finding]:
    """Surface active GuardDuty findings across all detectors in the account.

    Severity mapping mirrors GuardDuty's own numeric scale:
    * 7.0-10.0 → critical
    * 4.0-6.9  → high
    * 1.0-3.9  → medium
    """
    detector_id = "SEC-007-guardduty"
    findings: list[Finding] = []
    gd = ctx.factory.client("guardduty")

    detector_ids: list[str] = []
    with detector_guard(ctx, detector_id=detector_id):
        for page in paginate(gd, "list_detectors"):
            detector_ids.extend(page.get("DetectorIds", []))

    for gd_detector_id in detector_ids:
        finding_ids: list[str] = []
        with detector_guard(ctx, detector_id=detector_id):
            for page in paginate(
                gd,
                "list_findings",
                DetectorId=gd_detector_id,
                FindingCriteria={
                    "Criterion": {
                        "service.archived": {"Eq": ["false"]},
                    }
                },
            ):
                finding_ids.extend(page.get("FindingIds", []))

        # get_findings accepts up to 50 IDs per call.
        for i in range(0, len(finding_ids), 50):
            batch = finding_ids[i : i + 50]
            with detector_guard(ctx, detector_id=detector_id):
                resp = gd.get_findings(DetectorId=gd_detector_id, FindingIds=batch)
                for gd_finding in resp.get("Findings", []):
                    numeric_severity = float(gd_finding.get("Severity", 0))
                    if numeric_severity >= 7.0:
                        severity = "critical"
                    elif numeric_severity >= 4.0:
                        severity = "high"
                    else:
                        severity = "medium"

                    resource = gd_finding.get("Resource", {})
                    resource_type = resource.get("ResourceType", "Unknown")
                    # Best-effort ARN from the embedded resource details.
                    instance_details = resource.get("InstanceDetails", {})
                    resource_arn = (
                        instance_details.get("InstanceArn")
                        or gd_finding.get("Arn")
                        or gd_finding["Id"]
                    )

                    findings.append(
                        Finding(
                            kra="security",
                            severity=severity,
                            resource_arn=resource_arn,
                            resource_type=f"AWS::GuardDuty::{resource_type}",
                            region=gd_finding.get("Region", "unknown"),
                            title=gd_finding.get(
                                "Title",
                                f"GuardDuty finding {gd_finding['Id']}",
                            ),
                            evidence={
                                "FindingId": gd_finding["Id"],
                                "DetectorId": gd_detector_id,
                                "Type": gd_finding.get("Type"),
                                "Severity": numeric_severity,
                                "Description": gd_finding.get("Description"),
                                "Service": gd_finding.get("Service", {}),
                                "Resource": resource,
                            },
                            recommendation=(
                                "Investigate the GuardDuty finding in the console "
                                "(Security → GuardDuty → Findings). Archive the "
                                "finding after confirming it is a false positive or "
                                "after completing incident response."
                            ),
                            detector_id=detector_id,
                        )
                    )

    return findings


# ---------------------------------------------------------------------------
# SEC-008  IAM Access Analyzer active findings
# ---------------------------------------------------------------------------


def _paginate_list_findings_v2(aa: Any, analyzer_arn: str) -> Any:
    if aa.can_paginate("list_findings_v2"):
        yield from paginate(
            aa,
            "list_findings_v2",
            analyzerArn=analyzer_arn,
            filter={"status": {"eq": ["ACTIVE"]}},
        )
        return

    next_token: str | None = None
    while True:
        kwargs = {
            "analyzerArn": analyzer_arn,
            "filter": {"status": {"eq": ["ACTIVE"]}},
        }
        if next_token:
            kwargs["nextToken"] = next_token
        resp = aa.list_findings_v2(**kwargs)
        yield resp
        next_token = resp.get("nextToken")
        if not next_token:
            break


def _access_analyzer_findings_pages(aa: Any, analyzer_arn: str) -> Any:
    try:
        yield from paginate(
            aa,
            "list_findings",
            analyzerArn=analyzer_arn,
            filter={"status": {"eq": ["ACTIVE"]}},
        )
    except Exception as exc:
        error_code = None
        if hasattr(exc, "response"):
            error_code = exc.response.get("Error", {}).get("Code")
        if error_code == "ValidationException" and "ListFindingsV2" in str(exc):
            yield from _paginate_list_findings_v2(aa, analyzer_arn)
        else:
            raise


def find_access_analyzer_findings(ctx: DetectorContext) -> list[Finding]:
    """Surface ACTIVE findings from all IAM Access Analyzers in the account.

    Each finding represents an external-access or unused-access path that
    Access Analyzer has identified and that has not yet been archived.
    """
    detector_id = "SEC-008-access-analyzer"
    findings: list[Finding] = []
    aa = ctx.factory.client("accessanalyzer")

    analyzers: list[dict[str, Any]] = []
    with detector_guard(ctx, detector_id=detector_id):
        for page in paginate(aa, "list_analyzers"):
            analyzers.extend(page.get("analyzers", []))

    for analyzer in analyzers:
        analyzer_arn = analyzer["arn"]
        analyzer_name = analyzer["name"]
        with detector_guard(ctx, detector_id=detector_id, resource_arn=analyzer_arn):
            for page in _access_analyzer_findings_pages(aa, analyzer_arn):
                for aa_finding in page.get("findings", []):
                    finding_id = aa_finding.get("id", "unknown")
                    resource_arn = aa_finding.get("resource", finding_id)
                    resource_type = aa_finding.get("resourceType", "Unknown")
                    finding_type = aa_finding.get("findingType") or aa_finding.get(
                        "type", "ExternalAccess"
                    )
                    # Unused-access findings (e.g. unused permissions) are lower
                    # severity than confirmed external-access findings.
                    severity = (
                        "high"
                        if "External" in finding_type or "Public" in finding_type
                        else "medium"
                    )
                    findings.append(
                        Finding(
                            kra="security",
                            severity=severity,
                            resource_arn=resource_arn,
                            resource_type=resource_type,
                            region=aa_finding.get("region", "global"),
                            title=(
                                f"Access Analyzer [{analyzer_name}] reports "
                                f"{finding_type} on {resource_type} {resource_arn}"
                            ),
                            evidence={
                                "FindingId": finding_id,
                                "AnalyzerArn": analyzer_arn,
                                "FindingType": finding_type,
                                "Principal": aa_finding.get("principal"),
                                "Action": aa_finding.get("action"),
                                "Condition": aa_finding.get("condition"),
                                "CreatedAt": (
                                    aa_finding["createdAt"].isoformat()
                                    if hasattr(aa_finding.get("createdAt"), "isoformat")
                                    else aa_finding.get("createdAt")
                                ),
                            },
                            recommendation=(
                                "Review the Access Analyzer finding and either update "
                                "the resource policy to remove unintended access or "
                                "archive the finding if the access is expected."
                            ),
                            detector_id=detector_id,
                        )
                    )

    return findings


# ---------------------------------------------------------------------------
# SEC-009  KMS keys with rotation disabled
# ---------------------------------------------------------------------------


def find_kms_rotation_disabled(ctx: DetectorContext) -> list[Finding]:
    """Detect customer-managed KMS keys that do not have automatic rotation enabled.

    AWS-managed keys (alias starting with ``aws/``) and keys in states other
    than ``Enabled`` are skipped — rotation cannot be configured for them.
    """
    detector_id = "SEC-009-kms-rotation"
    findings: list[Finding] = []
    kms = ctx.factory.client("kms")

    key_ids: list[str] = []
    with detector_guard(ctx, detector_id=detector_id):
        for page in paginate(kms, "list_keys"):
            key_ids.extend(k["KeyId"] for k in page.get("Keys", []))

    for key_id in key_ids:
        with detector_guard(ctx, detector_id=detector_id):
            meta_resp = kms.describe_key(KeyId=key_id)
            meta = meta_resp["KeyMetadata"]
            key_arn = meta["Arn"]

            # Skip AWS-managed, asymmetric, HMAC, and non-enabled keys.
            if meta.get("KeyManager") != "CUSTOMER":
                continue
            if meta.get("KeyState") != "Enabled":
                continue
            if meta.get("KeySpec", "SYMMETRIC_DEFAULT") != "SYMMETRIC_DEFAULT":
                continue

            rotation_resp = kms.get_key_rotation_status(KeyId=key_id)
            if rotation_resp.get("KeyRotationEnabled"):
                continue

            # Surface the key alias if available for a friendlier title.
            alias_name = key_id
            try:
                alias_pages = paginate(kms, "list_aliases", KeyId=key_id)
                for alias_page in alias_pages:
                    aliases = alias_page.get("Aliases", [])
                    if aliases:
                        alias_name = aliases[0].get("AliasName", key_id)
                        break
            except Exception:
                pass

            findings.append(
                Finding(
                    kra="security",
                    severity="medium",
                    resource_arn=key_arn,
                    resource_type="AWS::KMS::Key",
                    region=meta.get("KeyUsage", "global"),
                    title=(
                        f"KMS key {alias_name} ({key_id}) does not have automatic rotation enabled"
                    ),
                    evidence={
                        "KeyId": key_id,
                        "KeyArn": key_arn,
                        "AliasName": alias_name,
                        "KeyState": meta.get("KeyState"),
                        "KeyRotationEnabled": False,
                    },
                    recommendation=(
                        "Enable automatic annual key rotation via "
                        "``kms:EnableKeyRotation``. For keys that cannot use "
                        "automatic rotation, establish a manual rotation schedule "
                        "and document it in your key policy."
                    ),
                    detector_id=detector_id,
                )
            )

    return findings


# ---------------------------------------------------------------------------
# SEC-010  Security Hub findings
# ---------------------------------------------------------------------------


def find_security_hub_findings(ctx: DetectorContext) -> list[Finding]:
    """Surface active Security Hub findings at MEDIUM severity or above.

    Only findings with ``RecordState=ACTIVE`` and ``WorkflowStatus`` not
    equal to ``SUPPRESSED`` are included, mirroring the default console view.
    """
    detector_id = "SEC-010-security-hub"
    findings: list[Finding] = []
    sh = ctx.factory.client("securityhub")

    _severity_map = {
        "INFORMATIONAL": "low",  # below our threshold — filtered out below
        "LOW": "low",
        "MEDIUM": "medium",
        "HIGH": "high",
        "CRITICAL": "critical",
    }

    with detector_guard(ctx, detector_id=detector_id):
        for page in paginate(
            sh,
            "get_findings",
            Filters={
                "RecordState": [{"Value": "ACTIVE", "Comparison": "EQUALS"}],
                "WorkflowStatus": [{"Value": "SUPPRESSED", "Comparison": "NOT_EQUALS"}],
                "SeverityLabel": [
                    {"Value": sev, "Comparison": "EQUALS"} for sev in ("MEDIUM", "HIGH", "CRITICAL")
                ],
            },
        ):
            for sh_finding in page.get("Findings", []):
                sh_id = sh_finding.get("Id", "unknown")
                label = sh_finding.get("Severity", {}).get("Label", "MEDIUM").upper()
                severity = _severity_map.get(label, "medium")

                # Use the first listed resource as the canonical resource.
                resources = sh_finding.get("Resources", [{}])
                primary = resources[0] if resources else {}
                resource_arn = primary.get("Id", sh_id)
                resource_type = primary.get("Type", "Unknown")

                findings.append(
                    Finding(
                        kra="security",
                        severity=severity,
                        resource_arn=resource_arn,
                        resource_type=resource_type,
                        region=sh_finding.get("Region", "unknown"),
                        title=sh_finding.get(
                            "Title",
                            f"Security Hub finding {sh_id}",
                        ),
                        evidence={
                            "FindingId": sh_id,
                            "ProductArn": sh_finding.get("ProductArn"),
                            "GeneratorId": sh_finding.get("GeneratorId"),
                            "SeverityLabel": label,
                            "Description": sh_finding.get("Description"),
                            "Remediation": sh_finding.get("Remediation", {}),
                            "Resources": resources,
                        },
                        recommendation=(
                            sh_finding.get("Remediation", {}).get("Recommendation", {}).get("Text")
                            or (
                                "Review the finding in the Security Hub console and "
                                "follow the provider's remediation guidance. Update "
                                "the workflow status to RESOLVED or SUPPRESSED once "
                                "addressed."
                            )
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
    find_public_s3_buckets,
    find_open_security_groups,
    find_stale_access_keys,
    check_root_mfa,
    find_overly_permissive_iam,
    # find_config_noncompliant_rules,   
    # find_guardduty_threats,            
    find_access_analyzer_findings,    
    find_kms_rotation_disabled,
    find_security_hub_findings,        
)


async def _run_all_async(ctx: DetectorContext) -> list[Finding]:
    tasks = [asyncio.to_thread(fn, ctx) for fn in ALL_DETECTORS]
    results = await asyncio.gather(*tasks)
    out: list[Finding] = []
    for result in results:
        out.extend(result)
    return out


def run_all(ctx: DetectorContext) -> list[Finding]:
    """Run every security detector and concatenate findings."""
    return asyncio.run(_run_all_async(ctx))
