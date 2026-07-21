"""
Action Executor Node

Executes remediation actions on AWS based on the auto-fixed ProposedWrites
emitted by decision_router. The node loops over state["auto_fixed"], dispatches
each write to the right handler via a detector_id -> handler registry, and emits
a list[ActionResult] to state["action_results"].

The ActionExecutor class below is the low-level single-action executor (unchanged
contract). The new action_executor_node wraps it in a loop, with deterministic
dispatch and a clean ActionResult envelope.

BREAKING: the old action_executor_node returned {"action_result": <dict>,
"action_executed": bool}. It now returns {"action_results": list[ActionResult]}.
Nothing in the codebase reads the old keys.
"""

from __future__ import annotations

import os

import json as _json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from botocore.exceptions import ClientError, WaiterError

from src.chandra.aws.client_factory import get_default_factory
from src.chandra.briefing.schemas import ActionResult, ProposedWrite
from src.chandra.graphs.state import ChandraState
from src.chandra.logging import get_logger
from src.chandra.observability import traced_node

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Sentinel exception — raised by handlers that encounter a resource that is
# technically valid but cannot be remediated for a non-error reason (e.g.
# an AWS-managed resource that forbids tagging). The executor node converts
# this to status="skipped" rather than status="failure".
# ---------------------------------------------------------------------------


class _SkippedRemediation(Exception):
    """Raised when remediation is intentionally skipped for a valid non-error reason.

    Examples:
    - The target is an AWS-managed resource (e.g. EventBridge managed rule)
      that does not permit tagging or modification.
    - The resource no longer exists in the expected state and the finding
      is therefore already resolved.

    ``action_executor_node`` catches this and emits ``status="skipped"``
    with the exception message as the result message, rather than the
    ``status="failure"`` that a generic exception would produce.
    """


# Error codes in the Resource Groups Tagging API FailedResourcesMap that
# mean the resource is managed by AWS and cannot be tagged — these should
# produce a skipped result, not a failure.
_SKIP_TAGGING_ERROR_CODES: frozenset[str] = frozenset({
    "ManagedRuleException",    # AWS-owned EventBridge managed rules
    "InvalidParameterException",  # certain service-owned resources reject tags
})


def _error_code(exc: Exception) -> str:
    """Best-effort extraction of the AWS error code from a ``ClientError``.

    Returns ``""`` for non-``ClientError`` exceptions so call sites can
    compare with ``==``/``in`` without an ``isinstance`` check every time.
    """
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code", "") or ""
    return ""


# Error codes that mean "the target resource (or the specific sub-state we
# were about to change) no longer exists, or is already in the desired
# end-state" — the finding was valid when detected but the world moved on
# before remediation ran (deleted, terminated, manually fixed, etc). These
# races are reported as ``status="skipped"`` (already resolved), never as a
# hard failure.
_ALREADY_RESOLVED_ERROR_CODES: frozenset[str] = frozenset({
    "NoSuchBucket",
    "NoSuchEntity",
    "NoSuchEntityException",
    "InvalidVolume.NotFound",
    "InvalidGroup.NotFound",
    "InvalidPermission.NotFound",
    "InvalidInstanceID.NotFound",
    "InvalidAllocationID.NotFound",
    "InvalidAddress.NotFound",
    "DBInstanceNotFoundFault",
    "DBSnapshotNotFoundFault",
    "DBClusterNotFoundFault",
    "NotFoundException",
    "ResourceNotFoundException",
    "TrailNotFoundException",
    "NoSuchConfigRuleException",
    "NoSuchRemediationConfigurationException",
})

# Error codes that mean the target is AWS-managed, at an account/resource
# limit, in a conflicting state, or otherwise structurally forbids the
# requested modification right now. These are non-retryable-by-us in the
# current run and should be reported as skipped rather than failed.
_SKIP_STATE_CONFLICT_ERROR_CODES: frozenset[str] = frozenset({
    "KMSInvalidStateException",
    "UnsupportedOperationException",
    "InvalidDBInstanceStateFault",
    "InvalidDBClusterStateFault",
    "IncorrectInstanceState",
    "IncorrectState",
    "VolumeInUse",
    "LimitExceededException",
    "MaxNumberOfConfigurationRecordersExceededException",
    "MaxNumberOfDeliveryChannelsExceededException",
    "NoAvailableConfigurationRecorderException",
    "MaximumNumberOfTrailsExceededException",
    "InsufficientS3BucketPolicyException",
    "AlreadyExistsException",
    "InvalidRequestException",
    "ResourceConflictException",
    "InvalidAccessException",
})


# ---------------------------------------------------------------------------
# ARN -> resource-id parsers (one per handler)
# ---------------------------------------------------------------------------


def _s3_bucket_from_arn(arn: str) -> str:
    """arn:aws:s3:::bucket-name  ->  bucket-name"""
    parts = arn.split(":")
    if len(parts) < 6 or not parts[5]:
        raise ValueError(f"Invalid S3 ARN: {arn!r}")
    return parts[5]


def _sg_id_from_arn(arn: str) -> str:
    """arn:aws:ec2:region:acct:security-group/sg-xxxx  ->  sg-xxxx"""
    if "/" not in arn:
        raise ValueError(f"Invalid security-group ARN: {arn!r}")
    return arn.rsplit("/", 1)[-1]


def _iam_key_id_from_arn(arn: str) -> str:
    """arn:aws:iam::acct:key/KEYID  ->  KEYID"""
    if "/" not in arn:
        raise ValueError(f"Invalid IAM key ARN: {arn!r}")
    return arn.rsplit("/", 1)[-1]


def _volume_id_from_arn(arn: str) -> str:
    """arn:aws:ec2:region:acct:volume/vol-xxx  ->  vol-xxx"""
    if "/" not in arn or not arn.endswith(arn.rsplit("/", 1)[-1]):
        raise ValueError(f"Invalid EBS volume ARN: {arn!r}")
    return arn.rsplit("/", 1)[-1]


def _instance_id_from_arn(arn: str) -> str:
    """arn:aws:ec2:region:acct:instance/i-xxx  ->  i-xxx"""
    if "/" not in arn:
        raise ValueError(f"Invalid EC2 instance ARN: {arn!r}")
    return arn.rsplit("/", 1)[-1]


def _kms_key_id_from_arn(arn: str) -> str:
    """arn:aws:kms:region:acct:key/KEY-ID  ->  KEY-ID"""
    if ":key/" not in arn:
        raise ValueError(f"Invalid KMS key ARN: {arn!r}")
    return arn.rsplit("/", 1)[-1]


def _eip_alloc_from_arn(arn: str) -> str:
    """arn:aws:ec2:region:acct:address/eipalloc-xxx  ->  eipalloc-xxx"""
    if ":address/" not in arn:
        raise ValueError(f"Invalid Elastic IP ARN: {arn!r}")
    return arn.rsplit("/", 1)[-1]


def _region_scope_from_arn(arn: str) -> str:
    """Account/region-scoped control (no per-resource id).

    arn:aws:ec2:region:acct:ebs-encryption-by-default -> region

    The remediation is an account-wide toggle keyed only by region, so we
    surface the ARN's region field as the "resource id". The executor
    method uses ``self.region`` for the actual call; this value exists so
    the audit log and dispatch have a stable, human-meaningful handle.
    """
    parts = arn.split(":")
    if len(parts) < 4 or not parts[3]:
        raise ValueError(f"Invalid region-scoped ARN: {arn!r}")
    return parts[3]


def _identity_arn(arn: str) -> str:
    """Pass the full ARN through unchanged.

    Used by handlers whose remediation operates on the ARN directly (e.g.
    the Resource Groups Tagging API, which tags any resource type by ARN),
    so there is no single sub-id to extract.
    """
    if not arn or not arn.startswith("arn:"):
        raise ValueError(f"Invalid ARN: {arn!r}")
    return arn


def _iam_policy_arn_passthrough(arn: str) -> str:
    """Pass IAM managed policy ARN through unchanged (SEC-005).

    arn:aws:iam::acct:policy/name -> arn:aws:iam::acct:policy/name
    """
    if ":policy/" not in arn:
        raise ValueError(f"Invalid IAM managed policy ARN: {arn!r}")
    return arn


def _rds_instance_arn_passthrough(arn: str) -> str:
    """Pass RDS DB instance ARN through unchanged (REL-001, PERF-002).

    arn:aws:rds:region:acct:db:identifier -> same
    """
    if ":db:" not in arn:
        raise ValueError(f"Invalid RDS DB instance ARN: {arn!r}")
    return arn


def _lambda_function_arn_passthrough(arn: str) -> str:
    """Pass Lambda function ARN through unchanged (PERF-007).

    arn:aws:lambda:region:acct:function:name -> same
    """
    if ":function:" not in arn:
        raise ValueError(f"Invalid Lambda function ARN: {arn!r}")
    return arn


def _config_rule_arn_passthrough(arn: str) -> str:
    """Pass Config rule ARN through unchanged (COMP-007).

    arn:aws:config:region:acct:config-rule/name -> same
    Also accepts plain rule names (non-ARN strings) for compatibility.
    """
    if not arn:
        raise ValueError(f"Empty Config rule identifier: {arn!r}")
    return arn


def _account_scope_from_arn(arn: str) -> str:
    """Extract AWS account ID from an ARN (COMP-002, COMP-006, REL-003, REL-004).

    arn:aws:...:account-id:... -> account-id
    """
    parts = arn.split(":")
    if len(parts) < 5 or not parts[4]:
        raise ValueError(f"Cannot extract account ID from ARN: {arn!r}")
    return parts[4]


def _lenient_resource_id(arn: str) -> str:
    """Accept any non-empty string as a resource identifier (SEC-006).

    SEC-006 findings may surface a plain resource ID (not a full ARN) as their
    target_arn when Config does not surface a full ARN for the resource type.
    We accept both ARNs and bare IDs so the handler always receives something
    actionable to match against Config compliance records.
    """
    if not arn:
        raise ValueError(f"Empty resource identifier: {arn!r}")
    return arn


# ---------------------------------------------------------------------------
# Detector-id -> handler registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Handler:
    """Wires a detector id to the right ActionExecutor method and ARN parser."""

    problem_type: str
    extract_resource_id: Callable[[str], str]
    run: Callable[[ActionExecutor, str, str], None]


@dataclass(frozen=True)
class _ObservationOnlyHandler:
    """Marks a detector as observation-only — no automated AWS remediation is possible.

    The action_executor_node emits status="skipped" with skip_reason as the message.
    No AWS API call is made for these detectors.
    """

    problem_type: str
    skip_reason: str


# ---------------------------------------------------------------------------
# Via shims — existing (11)
# ---------------------------------------------------------------------------


def _fix_public_s3_via(executor: ActionExecutor, resource_id: str, region: str) -> None:
    executor._fix_public_s3(resource_id, region)


def _fix_open_sg_via(executor: ActionExecutor, resource_id: str, region: str) -> None:
    executor._fix_open_sg(resource_id, region)


def _disable_iam_key_via(executor: ActionExecutor, resource_id: str, region: str) -> None:
    executor._disable_iam_key(resource_id, region)


def _fix_unattached_ebs_via(executor: ActionExecutor, resource_id: str, region: str) -> None:
    executor._fix_unattached_ebs(resource_id, region)


def _fix_untagged_via(executor: ActionExecutor, resource_id: str, region: str) -> None:
    executor._fix_untagged_instance(resource_id, region)


def _release_unused_eip_via(executor: ActionExecutor, resource_id: str, region: str) -> None:
    executor._release_unused_eip(resource_id, region)


def _enable_s3_default_encryption_via(
    executor: ActionExecutor, resource_id: str, region: str
) -> None:
    executor._enable_s3_default_encryption(resource_id, region)


def _enable_s3_versioning_via(executor: ActionExecutor, resource_id: str, region: str) -> None:
    executor._enable_s3_versioning(resource_id, region)


def _enable_ebs_encryption_default_via(
    executor: ActionExecutor, resource_id: str, region: str
) -> None:
    executor._enable_ebs_encryption_by_default(resource_id, region)


def _enable_kms_rotation_via(executor: ActionExecutor, resource_id: str, region: str) -> None:
    executor._enable_kms_key_rotation(resource_id, region)


def _apply_mandatory_tags_via(executor: ActionExecutor, resource_id: str, region: str) -> None:
    executor._apply_mandatory_tags(resource_id, region)


# ---------------------------------------------------------------------------
# Via shims — new (16)
# ---------------------------------------------------------------------------


def _remove_wildcard_iam_statements_via(
    executor: ActionExecutor, resource_id: str, region: str
) -> None:
    executor._remove_wildcard_iam_statements(resource_id, region)


def _archive_guardduty_finding_via(
    executor: ActionExecutor, resource_id: str, region: str
) -> None:
    executor._archive_guardduty_finding(resource_id, region)


def _archive_access_analyzer_finding_via(
    executor: ActionExecutor, resource_id: str, region: str
) -> None:
    executor._archive_access_analyzer_finding(resource_id, region)


def _notify_security_hub_finding_via(
    executor: ActionExecutor, resource_id: str, region: str
) -> None:
    executor._notify_security_hub_finding(resource_id, region)


def _enable_cloudtrail_multi_region_via(
    executor: ActionExecutor, resource_id: str, region: str
) -> None:
    executor._enable_cloudtrail_multi_region(resource_id, region)


def _enable_config_recorder_via(
    executor: ActionExecutor, resource_id: str, region: str
) -> None:
    executor._enable_config_recorder(resource_id, region)


def _create_default_backup_plan_via(
    executor: ActionExecutor, resource_id: str, region: str
) -> None:
    executor._create_default_backup_plan(resource_id, region)


def _trigger_config_rule_remediation_via(
    executor: ActionExecutor, resource_id: str, region: str
) -> None:
    executor._trigger_config_rule_remediation(resource_id, region)


def _enable_rds_multi_az_via(
    executor: ActionExecutor, resource_id: str, region: str
) -> None:
    executor._enable_rds_multi_az(resource_id, region)


def _create_default_dlm_policy_via(
    executor: ActionExecutor, resource_id: str, region: str
) -> None:
    executor._create_default_dlm_policy(resource_id, region)


def _stop_idle_ec2_via(executor: ActionExecutor, resource_id: str, region: str) -> None:
    executor._stop_idle_ec2(resource_id, region)


def _stop_idle_rds_via(executor: ActionExecutor, resource_id: str, region: str) -> None:
    executor._stop_idle_rds(resource_id, region)


def _fix_ec2_instance_type_via(
    executor: ActionExecutor, resource_id: str, region: str
) -> None:
    executor._fix_ec2_instance_type(resource_id, region)


def _update_lambda_memory_via(
    executor: ActionExecutor, resource_id: str, region: str
) -> None:
    executor._update_lambda_memory(resource_id, region)


def _trigger_config_resource_remediation_via(
    executor: ActionExecutor, resource_id: str, region: str
) -> None:
    executor._trigger_config_resource_remediation(resource_id, region)


def _create_encrypted_rds_replacement_via(
    executor: ActionExecutor, resource_id: str, region: str
) -> None:
    executor._create_encrypted_rds_replacement(resource_id, region)


def _create_encrypted_ebs_replacement_via(
    executor: ActionExecutor, resource_id: str, region: str
) -> None:
    executor._create_encrypted_ebs_replacement(resource_id, region)


def _migrate_ec2_to_asg_via(
    executor: ActionExecutor, resource_id: str, region: str
) -> None:
    executor._migrate_ec2_to_asg(resource_id, region)


# ---------------------------------------------------------------------------
# _HANDLERS registry — all 34 detectors
# ---------------------------------------------------------------------------

_HANDLERS: dict[str, _Handler | _ObservationOnlyHandler] = {
    # ── Existing auto-remediable (11) ─────────────────────────────────────
    "SEC-001-public-s3": _Handler(
        problem_type="public_s3",
        extract_resource_id=_s3_bucket_from_arn,
        run=_fix_public_s3_via,
    ),
    "SEC-002-open-sg-ssh": _Handler(
        problem_type="open_security_group",
        extract_resource_id=_sg_id_from_arn,
        run=_fix_open_sg_via,
    ),
    "SEC-003-stale-key": _Handler(
        problem_type="stale_iam_key",
        extract_resource_id=_iam_key_id_from_arn,
        run=_disable_iam_key_via,
    ),
    "SEC-009-kms-rotation": _Handler(
        problem_type="kms_key_rotation",
        extract_resource_id=_kms_key_id_from_arn,
        run=_enable_kms_rotation_via,
    ),
    "COMP-005-s3-default-enc": _Handler(
        problem_type="s3_default_encryption",
        extract_resource_id=_s3_bucket_from_arn,
        run=_enable_s3_default_encryption_via,
    ),
    # COMP-008: account/region EBS encryption-by-default is off. Toggles
    # it on for the finding's region. Idempotent and preventive — only
    # affects *future* volumes; existing volumes are unchanged.
    "COMP-008-ebs-default-enc-off": _Handler(
        problem_type="ebs_encryption_by_default",
        extract_resource_id=_region_scope_from_arn,
        run=_enable_ebs_encryption_default_via,
    ),
    # COMP-009: resource missing mandatory tags. Applies placeholder
    # values via the Resource Groups Tagging API so the resource is
    # attributable. Metadata-only; a human sets real values.
    "COMP-009-missing-tags": _Handler(
        problem_type="mandatory_tags",
        extract_resource_id=_identity_arn,
        run=_apply_mandatory_tags_via,
    ),
    # REL-002: business-critical S3 bucket with versioning disabled.
    # Enables versioning. Idempotent and non-destructive.
    "REL-002-s3-versioning": _Handler(
        problem_type="s3_versioning",
        extract_resource_id=_s3_bucket_from_arn,
        run=_enable_s3_versioning_via,
    ),
    # COST-002: unattached EBS volumes. Delete after verifying the
    # volume is in the ``available`` state; refuses otherwise.
    "COST-002-unattached-ebs": _Handler(
        problem_type="unattached_ebs",
        extract_resource_id=_volume_id_from_arn,
        run=_fix_unattached_ebs_via,
    ),
    # COST-003: unassociated Elastic IP. Releases the allocation.
    # Re-checks association state first and refuses if attached.
    "COST-003-unused-eip": _Handler(
        problem_type="unused_eip",
        extract_resource_id=_eip_alloc_from_arn,
        run=_release_unused_eip_via,
    ),
    # COST-004: untagged billable resources. Adds placeholder tags so
    # the instance shows up under cost allocation.
    "COST-004-untagged-billable": _Handler(
        problem_type="untagged_instance",
        extract_resource_id=_instance_id_from_arn,
        run=_fix_untagged_via,
    ),

    # ── New auto-remediable (16) ───────────────────────────────────────────

    # SEC-005: Remove Action:* + Resource:* Allow statements from the IAM
    # managed policy. Creates a new restricted default version and deletes
    # the old wildcard version. WARNING: may remove permissions applications
    # depend on — review the diff after execution.
    "SEC-005-wildcard-iam": _Handler(
        problem_type="wildcard_iam",
        extract_resource_id=_iam_policy_arn_passthrough,
        run=_remove_wildcard_iam_statements_via,
    ),
    # SEC-007: Archive active GuardDuty findings for the resource in this
    # region. Archived findings are suppressed from the active view but
    # retained for audit purposes.
    "SEC-007-guardduty": _Handler(
        problem_type="guardduty_finding",
        extract_resource_id=_identity_arn,
        run=_archive_guardduty_finding_via,
    ),
    # SEC-008: Archive the IAM Access Analyzer finding for the reported
    # resource ARN, suppressing it from the active findings view.
    "SEC-008-access-analyzer": _Handler(
        problem_type="access_analyzer_finding",
        extract_resource_id=_identity_arn,
        run=_archive_access_analyzer_finding_via,
    ),
    # SEC-010: Update Security Hub findings for the resource to workflow
    # status NOTIFIED — acknowledges review without suppressing the finding.
    "SEC-010-security-hub": _Handler(
        problem_type="security_hub_finding",
        extract_resource_id=_identity_arn,
        run=_notify_security_hub_finding_via,
    ),
    # COMP-002: Create and start a multi-region, log-file-validated
    # CloudTrail trail. Creates the S3 delivery bucket if needed.
    "COMP-002-no-cloudtrail": _Handler(
        problem_type="cloudtrail_multi_region",
        extract_resource_id=_account_scope_from_arn,
        run=_enable_cloudtrail_multi_region_via,
    ),
    # COMP-003: Create a default Config recorder + delivery channel and
    # start recording in the region. Creates S3 bucket for delivery.
    "COMP-003-no-config-recorder": _Handler(
        problem_type="config_recorder",
        extract_resource_id=_region_scope_from_arn,
        run=_enable_config_recorder_via,
    ),
    # COMP-006: Create a default daily AWS Backup plan (7-day retention)
    # at the account level targeting resources tagged Backup=daily.
    "COMP-006-no-backup-protection": _Handler(
        problem_type="no_backup_protection",
        extract_resource_id=_account_scope_from_arn,
        run=_create_default_backup_plan_via,
    ),
    # COMP-007: Trigger Config auto-remediation execution for the
    # NON_COMPLIANT rule. Requires a remediation action to be pre-configured
    # on the Config rule; no-ops gracefully if none is configured.
    "COMP-007-config-rule-noncompliant": _Handler(
        problem_type="config_rule_noncompliant",
        extract_resource_id=_config_rule_arn_passthrough,
        run=_trigger_config_rule_remediation_via,
    ),
    # REL-001: Enable Multi-AZ on a single-AZ prod RDS instance.
    # WARNING: causes a brief automatic failover (~60s). Only runs on
    # instances in the ``available`` state.
    "REL-001-rds-single-az": _Handler(
        problem_type="rds_single_az",
        extract_resource_id=_rds_instance_arn_passthrough,
        run=_enable_rds_multi_az_via,
    ),
    # REL-003: Create a default DLM EBS snapshot lifecycle policy (daily
    # at 05:00 UTC, 7-day retention) targeting volumes tagged Backup=daily.
    "REL-003-no-dlm": _Handler(
        problem_type="no_dlm_policy",
        extract_resource_id=_account_scope_from_arn,
        run=_create_default_dlm_policy_via,
    ),
    # REL-004: Create a default regional AWS Backup plan (same policy as
    # COMP-006 but the finding is region-scoped rather than account-wide).
    "REL-004-no-backup-plan": _Handler(
        problem_type="no_backup_plan",
        extract_resource_id=_account_scope_from_arn,
        run=_create_default_backup_plan_via,
    ),
    # COST-001: Stop an idle EC2 instance (CPU < 5% over 14 days). Tags
    # the instance with StoppedBy=chandra-auto-fix before stopping.
    # Only stops instances in the ``running`` state.
    "COST-001-idle-ec2": _Handler(
        problem_type="idle_ec2",
        extract_resource_id=_instance_id_from_arn,
        run=_stop_idle_ec2_via,
    ),
    # PERF-002: Stop an idle RDS instance (CPU < 10% AND connections < 5
    # over 14 days). AWS auto-restarts stopped RDS after 7 days.
    "PERF-002-rds-idle": _Handler(
        problem_type="rds_idle",
        extract_resource_id=_rds_instance_arn_passthrough,
        run=_stop_idle_rds_via,
    ),
    # PERF-003: Stop → change to Compute Optimizer recommended type (or
    # next-smaller same-family type if CO unavailable) → start.
    # WARNING: causes downtime for the stop/start cycle (~2-5 min).
    "PERF-003-oversized-ec2": _Handler(
        problem_type="oversized_ec2",
        extract_resource_id=_instance_id_from_arn,
        run=_fix_ec2_instance_type_via,
    ),
    # PERF-006: Same stop/resize/start as PERF-003 but driven by Compute
    # Optimizer's OVER_PROVISIONED recommendation directly.
    "PERF-006-co-ec2": _Handler(
        problem_type="co_ec2_over_provisioned",
        extract_resource_id=_instance_id_from_arn,
        run=_fix_ec2_instance_type_via,
    ),
    # PERF-007: Update Lambda function memory to Compute Optimizer's top
    # recommendation. No downtime — applies to new invocations immediately.
    "PERF-007-co-lambda": _Handler(
        problem_type="lambda_over_provisioned",
        extract_resource_id=_lambda_function_arn_passthrough,
        run=_update_lambda_memory_via,
    ),

    # ── Observation-only & assisted-remediation (7) ─────────────────────────
    # NOTE: only SEC-004, PERF-004, and PERF-005 below are true
    # _ObservationOnlyHandler entries — no AWS API is ever called for them.
    # SEC-006, COMP-001, COMP-004, and PERF-001 ARE auto-remediated via a
    # real _Handler; they use an assisted workaround (e.g. snapshot + create
    # a replacement + tag the original) rather than a direct in-place fix,
    # because the direct in-place action would be destructive, irreversible,
    # or require a live-traffic cutover a human should approve.

    # SEC-004: AWS has no API to enroll MFA on the root account.
    "SEC-004-root-mfa": _ObservationOnlyHandler(
        problem_type="root_mfa",
        skip_reason=(
            "AWS has no API to enroll MFA on the root account — "
            "this must be done manually in the AWS console."
        ),
    ),
    # SEC-006: Find all Config rules where this resource is NON_COMPLIANT and
    # trigger StartRemediationExecution for each. Requires remediation actions
    # to be pre-configured on each rule; rules without one are skipped gracefully.
    "SEC-006-config-noncompliant": _Handler(
        problem_type="config_noncompliant_resource",
        extract_resource_id=_lenient_resource_id,
        run=_trigger_config_resource_remediation_via,
    ),
    # COMP-001: Create an encrypted RDS snapshot, copy it with the default
    # aws/rds KMS key, and restore a NEW instance named <original>-encrypted.
    # The original instance is NOT deleted — it is tagged for manual cutover.
    # WARNING: takes several minutes; new instance has a different endpoint.
    "COMP-001-rds-unencrypted": _Handler(
        problem_type="rds_unencrypted",
        extract_resource_id=_rds_instance_arn_passthrough,
        run=_create_encrypted_rds_replacement_via,
    ),
    # COMP-004: Snapshot an unencrypted EBS volume, copy the snapshot with
    # encryption enabled, and create a new encrypted volume from it.
    # If the volume is already detached (available), also deletes the original.
    # If in-use, the original is tagged for manual swap.
    "COMP-004-ebs-unencrypted": _Handler(
        problem_type="ebs_unencrypted_volume",
        extract_resource_id=_volume_id_from_arn,
        run=_create_encrypted_ebs_replacement_via,
    ),
    # PERF-001: Create a Launch Template from the instance's current config,
    # create an Auto Scaling Group, and attach the existing instance to it.
    # Sets MinSize=1, MaxSize=3, DesiredCapacity=1 as baseline defaults.
    "PERF-001-no-asg": _Handler(
        problem_type="no_autoscaling_group",
        extract_resource_id=_instance_id_from_arn,
        run=_migrate_ec2_to_asg_via,
    ),
    # PERF-004: X-Ray error rates are application-level — no AWS control
    # plane API can fix application code errors.
    "PERF-004-xray-error-rate": _ObservationOnlyHandler(
        problem_type="xray_error_rate",
        skip_reason=(
            "Application-level X-Ray error rates require developer investigation. "
            "No AWS control plane API can fix application code errors."
        ),
    ),
    # PERF-005: X-Ray latency is application-level — no AWS control plane
    # API can fix application performance issues.
    "PERF-005-xray-latency": _ObservationOnlyHandler(
        problem_type="xray_latency",
        skip_reason=(
            "Application-level X-Ray latency requires developer investigation. "
            "No AWS control plane API can fix application performance issues."
        ),
    ),
}


def registered_problem_type(detector_id: str) -> str | None:
    """Public lookup: the handler problem_type for a detector id, or ``None``.

    Lets other packages (e.g. the Digital Worker execution node) check
    whether automated remediation exists without reaching into the
    private ``_HANDLERS`` registry.
    """
    handler = _HANDLERS.get(detector_id)
    return handler.problem_type if handler else None


# ---------------------------------------------------------------------------
# Low-level single-action executor (unchanged contract)
# ---------------------------------------------------------------------------


class ActionExecutor:
    """Executes a single AWS remediation action.

    Kept for backwards-compat with the legacy ``tests/test_action_executor.py``
    and any direct callers. ``action_executor_node`` is the new entry point for
    the LangGraph path.
    """

    def __init__(self, dry_run: bool = True, region: str = os.getenv("AWS_DEFAULT_REGION", "us-east-1")):
        """
        Args:
            dry_run: If True, show what would happen without doing it.
            region: AWS region for the executor.
        """
        self.dry_run = dry_run
        self.region = region
        factory = get_default_factory()
        # Core clients (existing)
        self.s3_client = factory.client("s3", region=region)
        self.iam_client = factory.client("iam", region=region)
        self.ec2_client = factory.client("ec2", region=region)
        self.kms_client = factory.client("kms", region=region)
        self.tagging_client = factory.client("resourcegroupstaggingapi", region=region)
        # New clients for additional handlers
        self.sts_client = factory.client("sts", region=region)
        self.guardduty_client = factory.client("guardduty", region=region)
        self.accessanalyzer_client = factory.client("accessanalyzer", region=region)
        self.securityhub_client = factory.client("securityhub", region=region)
        self.cloudtrail_client = factory.client("cloudtrail", region=region)
        self.config_client = factory.client("config", region=region)
        self.backup_client = factory.client("backup", region=region)
        self.dlm_client = factory.client("dlm", region=region)
        self.rds_client = factory.client("rds", region=region)
        self.lambda_client = factory.client("lambda", region=region)
        self.co_client = factory.client("compute-optimizer", region=region)
        self.asg_client = factory.client("autoscaling", region=region)

    def _get_account_id(self) -> str:
        """Return the current AWS account ID via STS GetCallerIdentity."""
        try:
            return self.sts_client.get_caller_identity()["Account"]
        except Exception as exc:
            logger.warning("action._get_account_id.failed", error=str(exc))
            return "unknown"

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        """
        Execute a single action from state.

        Args:
            state: action spec
                {
                  "action_type": "remediate_SEC-001-public-s3",
                  "resource_id": "bucket-name",
                  "region": os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
                  "problem_type": "public_s3"
                }

        Returns:
            {
              "action_executed": True/False,
              "status": "success|failure|dry_run",
              "message": "what happened",
              "audit_log": "log entry",
              "error": "..."  # only on failure
            }
        """
        action_type = state.get("action_type")
        resource_id = state.get("resource_id")
        region = state.get("region", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
        problem_type = state.get("problem_type")

        timestamp = datetime.now().isoformat()

        logger.info(f"Action executor started: {action_type} on {resource_id}")

        try:
            if self.dry_run:
                message = f"[DRY RUN] Would {action_type} on {resource_id}"
                audit_entry = f"[{timestamp}] DRY RUN: {action_type} on {resource_id}"
                logger.info(message)
                return {
                    "action_executed": False,
                    "status": "dry_run",
                    "message": message,
                    "audit_log": audit_entry,
                }

            if not isinstance(resource_id, str) or not resource_id:
                raise ValueError(f"Missing resource_id for action {action_type}")

            # problem_type -> bound remediation method. Kept as a table (not
            # an if/elif chain) so adding a handler is one line and the
            # dispatch stays flat.
            dispatch: dict[str, Callable[[str, str], None]] = {
                # Existing
                "public_s3": self._fix_public_s3,
                "open_security_group": self._fix_open_sg,
                "stale_iam_key": self._disable_iam_key,
                "unattached_ebs": self._fix_unattached_ebs,
                "untagged_instance": self._fix_untagged_instance,
                "unused_eip": self._release_unused_eip,
                "s3_default_encryption": self._enable_s3_default_encryption,
                "s3_versioning": self._enable_s3_versioning,
                "ebs_encryption_by_default": self._enable_ebs_encryption_by_default,
                "kms_key_rotation": self._enable_kms_key_rotation,
                "mandatory_tags": self._apply_mandatory_tags,
                # New
                "wildcard_iam": self._remove_wildcard_iam_statements,
                "guardduty_finding": self._archive_guardduty_finding,
                "access_analyzer_finding": self._archive_access_analyzer_finding,
                "security_hub_finding": self._notify_security_hub_finding,
                "cloudtrail_multi_region": self._enable_cloudtrail_multi_region,
                "config_recorder": self._enable_config_recorder,
                "no_backup_protection": self._create_default_backup_plan,
                "no_backup_plan": self._create_default_backup_plan,
                "config_rule_noncompliant": self._trigger_config_rule_remediation,
                "config_noncompliant_resource": self._trigger_config_resource_remediation,
                "rds_single_az": self._enable_rds_multi_az,
                "rds_unencrypted": self._create_encrypted_rds_replacement,
                "ebs_unencrypted_volume": self._create_encrypted_ebs_replacement,
                "no_autoscaling_group": self._migrate_ec2_to_asg,
                "no_dlm_policy": self._create_default_dlm_policy,
                "idle_ec2": self._stop_idle_ec2,
                "rds_idle": self._stop_idle_rds,
                "oversized_ec2": self._fix_ec2_instance_type,
                "co_ec2_over_provisioned": self._fix_ec2_instance_type,
                "lambda_over_provisioned": self._update_lambda_memory,
            }
            handler = dispatch.get(problem_type) if isinstance(problem_type, str) else None
            if handler is None:
                raise ValueError(f"Unknown problem type: {problem_type}")
            handler(resource_id, region)

            message = f"Successfully executed {action_type} on {resource_id}"
            audit_entry = f"[{timestamp}] SUCCESS: {action_type} on {resource_id}"
            logger.info(message)

            return {
                "action_executed": True,
                "status": "success",
                "message": message,
                "audit_log": audit_entry,
            }

        except _SkippedRemediation as e:
            # Handler determined the resource cannot be remediated for a
            # non-error reason (e.g. AWS-managed resource, already resolved).
            message = str(e)
            audit_entry = (
                f"[{timestamp}] SKIPPED: {action_type} on {resource_id}. "
                f"Reason: {message}"
            )
            logger.info("action.skipped", action=action_type, resource=resource_id, reason=message)
            return {
                "action_executed": False,
                "status": "skipped",
                "message": message,
                "audit_log": audit_entry,
            }

        except Exception as e:
            message = f"Failed to execute {action_type}: {e!s}"
            audit_entry = f"[{timestamp}] FAILED: {action_type} on {resource_id}. Error: {e!s}"
            logger.error(message)

            return {
                "action_executed": False,
                "status": "failure",
                "message": message,
                "audit_log": audit_entry,
                "error": str(e),
            }

    # ── Existing remediation methods ──────────────────────────────────────

    def _fix_public_s3(self, bucket_name: str, region: str) -> None:
        """Make S3 bucket private by enabling Block Public Access (modern AWS standard).

        Race handling: if the bucket was deleted between detection and
        remediation, ``NoSuchBucket`` means the finding is already resolved.
        """
        logger.info(f"Fixing public S3: {bucket_name}")
        try:
            self.s3_client.put_public_access_block(
                Bucket=bucket_name,
                PublicAccessBlockConfiguration={
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                },
            )
        except ClientError as exc:
            if _error_code(exc) == "NoSuchBucket":
                raise _SkippedRemediation(
                    f"S3 bucket {bucket_name!r} no longer exists; already resolved."
                ) from exc
            raise

    def _fix_open_sg(self, sg_id: str, region: str) -> None:
        """Close open security group.

        Race handling: a deleted security group (``InvalidGroup.NotFound``)
        or an ingress rule that was already revoked by a previous run or a
        human (``InvalidPermission.NotFound``) both mean the finding is
        already resolved — neither should be reported as a failure.
        """
        logger.info(f"Fixing security group: {sg_id}")
        try:
            resp = self.ec2_client.describe_security_groups(GroupIds=[sg_id])
        except ClientError as exc:
            if _error_code(exc) == "InvalidGroup.NotFound":
                raise _SkippedRemediation(
                    f"Security group {sg_id!r} no longer exists; already resolved."
                ) from exc
            raise
            
        sg = resp["SecurityGroups"][0]
        to_revoke = []
        for perm in sg.get("IpPermissions", []):
            if any(r.get("CidrIp") == "0.0.0.0/0" for r in perm.get("IpRanges", [])):
                ip_protocol = perm.get("IpProtocol")
                if ip_protocol in ("tcp", "-1"):
                    from_port = perm.get("FromPort")
                    to_port = perm.get("ToPort")
                    if ip_protocol == "-1" or (from_port and to_port and any(p in (22, 3389, 3306, 5432) for p in range(from_port, to_port + 1))):
                        new_perm = {
                            "IpProtocol": ip_protocol,
                            "IpRanges": [{"CidrIp": "0.0.0.0/0"}]
                        }
                        if from_port is not None:
                            new_perm["FromPort"] = from_port
                            new_perm["ToPort"] = to_port
                        to_revoke.append(new_perm)

        if not to_revoke:
            raise _SkippedRemediation(
                f"Security group {sg_id!r} ingress rule was already revoked; already resolved."
            )

        try:
            self.ec2_client.revoke_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=to_revoke,
            )
        except ClientError as exc:
            code = _error_code(exc)
            if code == "InvalidGroup.NotFound":
                raise _SkippedRemediation(
                    f"Security group {sg_id!r} no longer exists; already resolved."
                ) from exc
            if code == "InvalidPermission.NotFound":
                raise _SkippedRemediation(
                    f"Security group {sg_id!r} ingress rule was already revoked; "
                    "already resolved."
                ) from exc
            raise

    def _disable_iam_key(self, key_id: str, region: str) -> None:
        """Disable stale IAM key.

        Race handling: ``NoSuchEntity`` means the key (or its owning user)
        was already deleted — the finding is already resolved.
        """
        logger.info(f"Disabling IAM key: {key_id}")
        try:
            self.iam_client.update_access_key_status(
                AccessKeyId=key_id,
                Status="Inactive",
            )
        except ClientError as exc:
            if _error_code(exc) == "NoSuchEntity":
                raise _SkippedRemediation(
                    f"IAM access key {key_id!r} no longer exists; already resolved."
                ) from exc
            raise

    def _fix_unattached_ebs(self, volume_id: str, region: str) -> None:
        """Delete an unattached EBS volume after a state check.

        Refuses to delete anything that isn't in the ``available``
        state, which is the only state that means "no attachments".
        Calling ``delete_volume`` on an in-use volume would either
        fail with ``VolumeInUse`` (good) or, if the volume was just
        detached and the state hasn't caught up, succeed and lose
        data. Verify explicitly so the failure mode is a clear
        refusal rather than a 5xx.

        Race handling: the volume may have been deleted, or (re-)attached
        to an instance, between detection and remediation — both are
        reported as skipped rather than a hard failure.
        """
        logger.info(f"Inspecting EBS volume: {volume_id}")
        try:
            resp = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
        except ClientError as exc:
            if _error_code(exc) == "InvalidVolume.NotFound":
                raise _SkippedRemediation(
                    f"EBS volume {volume_id!r} no longer exists; already resolved."
                ) from exc
            raise
        if not resp["Volumes"]:
            raise _SkippedRemediation(
                f"EBS volume {volume_id!r} no longer exists; already resolved."
            )
        state = resp["Volumes"][0].get("State")
        if state != "available":
            raise _SkippedRemediation(
                f"Volume {volume_id} is in state {state!r}, not 'available'; "
                "it is now in use and cannot be safely deleted."
            )
        logger.info(f"Deleting EBS volume: {volume_id}")
        try:
            self.ec2_client.delete_volume(VolumeId=volume_id)
        except ClientError as exc:
            code = _error_code(exc)
            if code == "InvalidVolume.NotFound":
                raise _SkippedRemediation(
                    f"EBS volume {volume_id!r} was deleted concurrently; already resolved."
                ) from exc
            if code == "VolumeInUse":
                raise _SkippedRemediation(
                    f"EBS volume {volume_id!r} became in-use between the state check "
                    "and delete; refusing to delete."
                ) from exc
            raise

    def _fix_untagged_instance(self, instance_id: str, region: str) -> None:
        """Apply placeholder Environment/Owner tags flagged as missing.

        The placeholder values (``Environment=untagged`` and
        ``Owner=chandra-auto-fix``) are deliberately distinct from any
        value a human would type, so a reviewer can see at a glance
        which instances were auto-fixed and need real tags applied.
        This is a metadata-only change — no runtime impact.

        Race handling: ``InvalidInstanceID.NotFound`` means the instance
        was terminated between detection and remediation; already resolved.
        """
        logger.info(f"Applying placeholder tags to instance: {instance_id}")
        try:
            self.ec2_client.create_tags(
                Resources=[instance_id],
                Tags=[
                    {"Key": "Environment", "Value": "untagged"},
                    {"Key": "Owner", "Value": "chandra-auto-fix"},
                ],
            )
        except ClientError as exc:
            if _error_code(exc) == "InvalidInstanceID.NotFound":
                raise _SkippedRemediation(
                    f"EC2 instance {instance_id!r} no longer exists; already resolved."
                ) from exc
            raise

    def _release_unused_eip(self, allocation_id: str, region: str) -> None:
        """Release an unassociated Elastic IP after re-checking association.

        Releasing an EIP is irreversible (the address is returned to the
        AWS pool). The detector flagged it as unassociated, but state can
        change between detection and remediation, so we re-describe and
        refuse to release anything that has since been attached to an
        instance or network interface — releasing an in-use EIP would drop
        live traffic. A missing allocation (whether reported via an empty
        ``Addresses`` list or an ``InvalidAllocationID.NotFound`` error) is
        treated as already-resolved.
        """
        logger.info("action.release_eip.inspect", allocation_id=allocation_id, region=region)
        try:
            resp = self.ec2_client.describe_addresses(AllocationIds=[allocation_id])
        except ClientError as exc:
            if _error_code(exc) == "InvalidAllocationID.NotFound":
                raise _SkippedRemediation(
                    f"Elastic IP {allocation_id!r} no longer exists; already resolved."
                ) from exc
            raise
        addresses = resp.get("Addresses", [])
        if not addresses:
            raise _SkippedRemediation(
                f"Elastic IP {allocation_id!r} not found (already released?); already resolved."
            )
        addr = addresses[0]
        if addr.get("InstanceId") or addr.get("NetworkInterfaceId"):
            raise _SkippedRemediation(
                f"Elastic IP {allocation_id} is now associated "
                f"(instance={addr.get('InstanceId')}, eni={addr.get('NetworkInterfaceId')}); "
                "refusing to release an address that is in use."
            )
        logger.info("action.release_eip.release", allocation_id=allocation_id, region=region)
        try:
            self.ec2_client.release_address(AllocationId=allocation_id)
        except ClientError as exc:
            if _error_code(exc) == "InvalidAllocationID.NotFound":
                raise _SkippedRemediation(
                    f"Elastic IP {allocation_id!r} was released concurrently; already resolved."
                ) from exc
            raise

    def _enable_s3_default_encryption(self, bucket_name: str, region: str) -> None:
        """Enable SSE-S3 (AES256) default encryption on the bucket.

        Idempotent and non-destructive: it sets the default applied to
        *new* objects; existing objects are not rewritten. Re-running on an
        already-encrypted bucket simply re-asserts the same configuration.

        Race handling: ``NoSuchBucket`` means the bucket was deleted between
        detection and remediation; already resolved.
        """
        logger.info("action.s3_default_encryption", bucket=bucket_name, region=region)
        try:
            self.s3_client.put_bucket_encryption(
                Bucket=bucket_name,
                ServerSideEncryptionConfiguration={
                    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
                },
            )
        except ClientError as exc:
            if _error_code(exc) == "NoSuchBucket":
                raise _SkippedRemediation(
                    f"S3 bucket {bucket_name!r} no longer exists; already resolved."
                ) from exc
            raise

    def _enable_s3_versioning(self, bucket_name: str, region: str) -> None:
        """Enable versioning on a business-critical S3 bucket.

        Idempotent and non-destructive: only affects retention of *future*
        object versions. Nothing existing is deleted or overwritten.

        Race handling: ``NoSuchBucket`` means the bucket was deleted between
        detection and remediation; already resolved.
        """
        logger.info("action.s3_versioning", bucket=bucket_name, region=region)
        try:
            self.s3_client.put_bucket_versioning(
                Bucket=bucket_name,
                VersioningConfiguration={"Status": "Enabled"},
            )
        except ClientError as exc:
            if _error_code(exc) == "NoSuchBucket":
                raise _SkippedRemediation(
                    f"S3 bucket {bucket_name!r} no longer exists; already resolved."
                ) from exc
            raise

    def _enable_ebs_encryption_by_default(self, scope: str, region: str) -> None:
        """Enable account-level EBS encryption-by-default for this region.

        Preventive and idempotent: it applies to *new* volumes only;
        existing volumes are unchanged (COMP-004 covers those separately).
        ``scope`` is the region surfaced from the ARN for audit clarity;
        the API call itself is region-scoped via the client.
        """
        logger.info("action.ebs_encryption_by_default", scope=scope, region=region)
        self.ec2_client.enable_ebs_encryption_by_default()

    def _enable_kms_key_rotation(self, key_id: str, region: str) -> None:
        """Enable automatic annual rotation on a customer-managed KMS key.

        Idempotent and transparent: AWS retains prior key material, so data
        encrypted before rotation still decrypts, and consumers referencing
        the key id/alias are unaffected.

        Edge cases handled:
        - ``NotFoundException``: the key was deleted between detection and
          remediation -> skipped, already resolved.
        - ``KMSInvalidStateException``: the key is pending deletion,
          disabled, or otherwise not in a state that permits rotation
          changes -> skipped.
        - ``UnsupportedOperationException``: asymmetric/HMAC or
          AWS-managed keys don't support customer-configured rotation ->
          skipped (this should be rare since the detector already filters
          to symmetric CUSTOMER-managed keys, but AWS state can change).
        """
        logger.info("action.kms_key_rotation", key_id=key_id, region=region)
        try:
            self.kms_client.enable_key_rotation(KeyId=key_id)
        except ClientError as exc:
            code = _error_code(exc)
            if code == "NotFoundException":
                raise _SkippedRemediation(
                    f"KMS key {key_id!r} no longer exists; already resolved."
                ) from exc
            if code == "KMSInvalidStateException":
                raise _SkippedRemediation(
                    f"KMS key {key_id!r} is in a state that does not permit "
                    "enabling rotation right now (e.g. pending deletion or disabled)."
                ) from exc
            if code == "UnsupportedOperationException":
                raise _SkippedRemediation(
                    f"KMS key {key_id!r} does not support automatic rotation "
                    "(asymmetric, HMAC, or AWS-managed key)."
                ) from exc
            raise

    def _apply_mandatory_tags(self, resource_arn: str, region: str) -> None:
        """Apply placeholder values for mandatory tags via the Tagging API.

        Works on any resource type by ARN. The placeholders
        (``Environment=untagged``, ``Owner=chandra-auto-fix``,
        ``Project=unassigned``) are deliberately distinct from real values
        so a reviewer can spot auto-tagged resources and replace them.
        Metadata-only — no runtime impact.

        Error handling:
        - ``ManagedRuleException`` / ``InvalidParameterException``: the
          resource is AWS-managed and forbids tagging (e.g. Amazon Inspector
          EventBridge rules). Raises ``_SkippedRemediation`` so the node
          emits ``status="skipped"`` rather than ``status="failure"``.
        - Any other error code in ``FailedResourcesMap``: raises ``ValueError``
          so the node emits ``status="failure"``.
        """
        logger.info("action.mandatory_tags", resource_arn=resource_arn, region=region)
        resp = self.tagging_client.tag_resources(
            ResourceARNList=[resource_arn],
            Tags={
                "Environment": "untagged",
                "Owner": "chandra-auto-fix",
                "Project": "unassigned",
            },
        )
        failed = resp.get("FailedResourcesMap", {})
        if not failed:
            return

        # Separate skippable errors from real failures.
        skippable: list[str] = []
        real_failures: dict[str, Any] = {}
        for arn, info in failed.items():
            error_code = info.get("ErrorCode", "")
            if error_code in _SKIP_TAGGING_ERROR_CODES:
                skippable.append(f"{arn} ({error_code}: {info.get('ErrorMessage', '')})")
                logger.info(
                    "action.mandatory_tags.managed_resource_skipped",
                    resource_arn=arn,
                    error_code=error_code,
                )
            else:
                real_failures[arn] = info

        if real_failures:
            raise ValueError(f"Failed to tag {resource_arn}: {real_failures}")

        # All failures were managed-resource errors — emit a skipped result.
        raise _SkippedRemediation(
            f"Resource is AWS-managed and does not permit tagging: "
            + "; ".join(skippable)
        )

    # ── New remediation methods ───────────────────────────────────────────

    def _remove_wildcard_iam_statements(self, policy_arn: str, region: str) -> None:
        """Remove Action:* + Resource:* Allow statements from an IAM managed policy.

        Reads the current default policy version, strips every Allow statement
        that has both Action:* and Resource:*, then creates a new policy version
        with the sanitised document and sets it as the default. The old wildcard
        version is deleted to stay within the 5-version limit.

        WARNING: This removes broad permissions. Applications relying on these
        permissions will lose access. Review the change after execution.
        """
        logger.info("action.remove_wildcard_iam", policy_arn=policy_arn)
        try:
            resp = self.iam_client.get_policy(PolicyArn=policy_arn)
        except ClientError as exc:
            if _error_code(exc) == "NoSuchEntity":
                raise _SkippedRemediation(
                    f"IAM policy {policy_arn!r} no longer exists; already resolved."
                ) from exc
            raise
        version_id = resp["Policy"]["DefaultVersionId"]
        try:
            ver = self.iam_client.get_policy_version(PolicyArn=policy_arn, VersionId=version_id)
        except ClientError as exc:
            if _error_code(exc) == "NoSuchEntity":
                raise _SkippedRemediation(
                    f"IAM policy {policy_arn!r} version {version_id!r} no longer "
                    "exists; already resolved."
                ) from exc
            raise
        doc = ver["PolicyVersion"]["Document"]

        if isinstance(doc, str):
            doc = _json.loads(doc)

        def _is_wildcard_stmt(stmt: dict[str, Any]) -> bool:
            if stmt.get("Effect") != "Allow":
                return False
            actions = stmt.get("Action", [])
            resources = stmt.get("Resource", [])
            has_wildcard_action = actions == "*" or (
                isinstance(actions, list) and "*" in actions
            )
            has_wildcard_resource = resources == "*" or (
                isinstance(resources, list) and "*" in resources
            )
            return has_wildcard_action and has_wildcard_resource

        original_stmts = doc.get("Statement", [])
        if isinstance(original_stmts, dict):
            original_stmts = [original_stmts]
        filtered_stmts = [s for s in original_stmts if not _is_wildcard_stmt(s)]

        if len(filtered_stmts) == len(original_stmts):
            logger.info("action.remove_wildcard_iam.no_change", policy_arn=policy_arn)
            return

        new_doc = {**doc, "Statement": filtered_stmts}
        try:
            self.iam_client.create_policy_version(
                PolicyArn=policy_arn,
                PolicyDocument=_json.dumps(new_doc),
                SetAsDefault=True,
            )
        except ClientError as exc:
            code = _error_code(exc)
            if code == "LimitExceededException":
                raise _SkippedRemediation(
                    f"IAM policy {policy_arn!r} is at its version limit, or the "
                    "sanitised document exceeds the managed-policy size limit; "
                    "manual cleanup is required before this can be auto-remediated."
                ) from exc
            if code == "MalformedPolicyDocumentException":
                raise _SkippedRemediation(
                    f"Sanitised policy document for {policy_arn!r} failed IAM "
                    "validation; manual review required."
                ) from exc
            if code == "NoSuchEntity":
                raise _SkippedRemediation(
                    f"IAM policy {policy_arn!r} no longer exists; already resolved."
                ) from exc
            raise
        # Clean up old version to stay within the 5-version limit. Best-effort:
        # the restricted version is already live, so a cleanup failure here
        # should not surface as a remediation failure.
        try:
            self.iam_client.delete_policy_version(PolicyArn=policy_arn, VersionId=version_id)
        except ClientError as exc:
            logger.warning(
                "action.remove_wildcard_iam.old_version_cleanup_failed",
                policy_arn=policy_arn,
                version_id=version_id,
                error=str(exc),
            )
        removed = len(original_stmts) - len(filtered_stmts)
        logger.info(
            "action.remove_wildcard_iam.done",
            policy_arn=policy_arn,
            removed=removed,
        )

    def _archive_guardduty_finding(self, resource_arn: str, region: str) -> None:
        """Archive active GuardDuty findings associated with the resource in this region.

        Lists all GuardDuty detectors in the region, retrieves all active
        (non-archived) findings, and archives them. Archived findings are
        suppressed from the active view but retained for audit purposes.
        """
        logger.info("action.archive_guardduty", resource_arn=resource_arn, region=region)
        detector_ids: list[str] = []
        try:
            paginator = self.guardduty_client.get_paginator("list_detectors")
            for page in paginator.paginate():
                detector_ids.extend(page.get("DetectorIds", []))
        except Exception as exc:
            raise ValueError(f"Failed to list GuardDuty detectors: {exc}") from exc

        if not detector_ids:
            logger.info("action.archive_guardduty.no_detectors", region=region)
            return

        archived_total = 0
        for det_id in detector_ids:
            finding_ids: list[str] = []
            try:
                paginator = self.guardduty_client.get_paginator("list_findings")
                for page in paginator.paginate(
                    DetectorId=det_id,
                    FindingCriteria={
                        "Criterion": {"service.archived": {"Eq": ["false"]}}
                    },
                ):
                    finding_ids.extend(page.get("FindingIds", []))
            except ClientError as exc:
                # Detector may have been deleted (GuardDuty disabled) between
                # the list_detectors call and here — skip this detector
                # rather than failing the whole remediation.
                logger.warning(
                    "action.archive_guardduty.detector_unavailable",
                    detector_id=det_id,
                    error=str(exc),
                )
                continue

            # ArchiveFindings accepts up to 50 IDs per call.
            for i in range(0, len(finding_ids), 50):
                batch = finding_ids[i : i + 50]
                try:
                    self.guardduty_client.archive_findings(
                        DetectorId=det_id, FindingIds=batch
                    )
                    archived_total += len(batch)
                except ClientError as exc:
                    logger.warning(
                        "action.archive_guardduty.archive_batch_failed",
                        detector_id=det_id,
                        error=str(exc),
                    )

        logger.info(
            "action.archive_guardduty.done",
            region=region,
            archived=archived_total,
        )

    def _archive_access_analyzer_finding(self, resource_arn: str, region: str) -> None:
        """Archive IAM Access Analyzer ACTIVE findings for the target resource.

        Iterates all analyzers in the region, filters for ACTIVE findings
        whose resource matches the target ARN, and archives each one.
        Archived findings are removed from the active findings view.
        """
        logger.info(
            "action.archive_access_analyzer",
            resource_arn=resource_arn,
            region=region,
        )
        analyzers: list[dict[str, Any]] = []
        for page in self.accessanalyzer_client.get_paginator("list_analyzers").paginate():
            analyzers.extend(page.get("analyzers", []))

        archived = 0
        for analyzer in analyzers:
            analyzer_arn = analyzer["arn"]
            try:
                for page in self.accessanalyzer_client.get_paginator(
                    "list_findings"
                ).paginate(
                    analyzerArn=analyzer_arn,
                    filter={
                        "status": {"eq": ["ACTIVE"]},
                        "resource": {"eq": [resource_arn]},
                    },
                ):
                    for finding in page.get("findings", []):
                        self.accessanalyzer_client.update_findings(
                            analyzerArn=analyzer_arn,
                            status="ARCHIVED",
                            ids=[finding["id"]],
                        )
                        archived += 1
            except Exception as exc:
                logger.warning(
                    "action.archive_access_analyzer.skip_analyzer",
                    analyzer=analyzer_arn,
                    error=str(exc),
                )

        logger.info(
            "action.archive_access_analyzer.done",
            resource_arn=resource_arn,
            archived=archived,
        )

    def _notify_security_hub_finding(self, resource_arn: str, region: str) -> None:
        """Update Security Hub ACTIVE findings for the resource to WorkflowStatus=NOTIFIED.

        Fetches all ACTIVE, non-SUPPRESSED findings whose ResourceId matches
        the target ARN and updates their workflow status to NOTIFIED —
        acknowledging review without suppressing the finding from dashboards.
        """
        logger.info(
            "action.notify_security_hub",
            resource_arn=resource_arn,
            region=region,
        )
        filters = {
            "ResourceId": [{"Value": resource_arn, "Comparison": "EQUALS"}],
            "RecordState": [{"Value": "ACTIVE", "Comparison": "EQUALS"}],
            "WorkflowStatus": [{"Value": "SUPPRESSED", "Comparison": "NOT_EQUALS"}],
        }
        finding_identifiers: list[dict[str, str]] = []
        try:
            for page in self.securityhub_client.get_paginator("get_findings").paginate(
                Filters=filters
            ):
                for f in page.get("Findings", []):
                    finding_identifiers.append(
                        {"Id": f["Id"], "ProductArn": f["ProductArn"]}
                    )
        except ClientError as exc:
            if _error_code(exc) == "InvalidAccessException":
                raise _SkippedRemediation(
                    f"Security Hub is not enabled in region {region}; "
                    "nothing to notify."
                ) from exc
            raise

        if not finding_identifiers:
            logger.info(
                "action.notify_security_hub.no_findings",
                resource_arn=resource_arn,
            )
            return

        # BatchUpdateFindings accepts up to 100 identifiers per call.
        for i in range(0, len(finding_identifiers), 100):
            batch = finding_identifiers[i : i + 100]
            self.securityhub_client.batch_update_findings(
                FindingIdentifiers=batch,
                Workflow={"Status": "NOTIFIED"},
            )

        logger.info(
            "action.notify_security_hub.done",
            resource_arn=resource_arn,
            notified=len(finding_identifiers),
        )

    def _enable_cloudtrail_multi_region(self, account_id: str, region: str) -> None:
        """Create and start a multi-region, log-file-validated CloudTrail trail (COMP-002).

        Creates an S3 bucket named ``chandra-cloudtrail-<account_id>`` if it does
        not already exist, attaches the required CloudTrail bucket policy, then
        creates a trail named ``chandra-multi-region-trail`` and starts logging.
        Idempotent: skips creation steps if the trail already exists.
        """
        logger.info(
            "action.enable_cloudtrail",
            account_id=account_id,
            region=region,
        )
        trail_name = "chandra-multi-region-trail"
        bucket_name = f"chandra-cloudtrail-{account_id}"

        # Ensure S3 delivery bucket exists with the required CloudTrail policy.
        try:
            self.s3_client.head_bucket(Bucket=bucket_name)
        except Exception:
            try:
                if region == "us-east-1":
                    self.s3_client.create_bucket(Bucket=bucket_name)
                else:
                    self.s3_client.create_bucket(
                        Bucket=bucket_name,
                        CreateBucketConfiguration={"LocationConstraint": region},
                    )
            except Exception as exc:
                # BucketAlreadyOwnedByYou is fine; other errors bubble up.
                if "BucketAlreadyOwnedByYou" not in str(exc):
                    raise

            self.s3_client.put_bucket_policy(
                Bucket=bucket_name,
                Policy=_json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "AWSCloudTrailAclCheck",
                            "Effect": "Allow",
                            "Principal": {"Service": "cloudtrail.amazonaws.com"},
                            "Action": "s3:GetBucketAcl",
                            "Resource": f"arn:aws:s3:::{bucket_name}",
                        },
                        {
                            "Sid": "AWSCloudTrailWrite",
                            "Effect": "Allow",
                            "Principal": {"Service": "cloudtrail.amazonaws.com"},
                            "Action": "s3:PutObject",
                            "Resource": (
                                f"arn:aws:s3:::{bucket_name}"
                                f"/AWSLogs/{account_id}/*"
                            ),
                            "Condition": {
                                "StringEquals": {
                                    "s3:x-amz-acl": "bucket-owner-full-control"
                                }
                            },
                        },
                    ],
                }),
            )

        # Create the trail (idempotent — TrailAlreadyExistsException is fine).
        try:
            self.cloudtrail_client.create_trail(
                Name=trail_name,
                S3BucketName=bucket_name,
                IsMultiRegionTrail=True,
                EnableLogFileValidation=True,
                IncludeGlobalServiceEvents=True,
            )
        except self.cloudtrail_client.exceptions.TrailAlreadyExistsException:
            logger.info(
                "action.enable_cloudtrail.already_exists",
                trail=trail_name,
            )
        except ClientError as exc:
            code = _error_code(exc)
            if code in (
                "MaximumNumberOfTrailsExceededException",
                "InsufficientS3BucketPolicyException",
            ):
                raise _SkippedRemediation(
                    f"Cannot create CloudTrail trail {trail_name!r}: {code}. "
                    "Manual remediation required (trail limit reached or "
                    "bucket policy misconfigured)."
                ) from exc
            raise

        try:
            self.cloudtrail_client.start_logging(Name=trail_name)
        except ClientError as exc:
            if _error_code(exc) == "TrailNotFoundException":
                raise _SkippedRemediation(
                    f"CloudTrail trail {trail_name!r} disappeared before "
                    "logging could be started; will retry on next run."
                ) from exc
            raise
        logger.info(
            "action.enable_cloudtrail.done",
            trail=trail_name,
            bucket=bucket_name,
        )

    def _enable_config_recorder(self, scope: str, region: str) -> None:
        """Create a Config recorder + delivery channel and start recording (COMP-003).

        Uses ``default`` as both the recorder and delivery channel name.
        Creates an S3 bucket named ``chandra-config-<account_id>-<region>``
        for delivery if it does not already exist. Relies on the AWS Config
        service-linked role (created automatically on first Config use).
        Idempotent: re-running on an active recorder is a no-op.
        """
        logger.info("action.enable_config_recorder", scope=scope, region=region)
        account_id = self._get_account_id()
        bucket_name = f"chandra-config-{account_id}-{region}"

        try:
            if region == "us-east-1":
                self.s3_client.create_bucket(Bucket=bucket_name)
            else:
                self.s3_client.create_bucket(
                    Bucket=bucket_name,
                    CreateBucketConfiguration={"LocationConstraint": region},
                )
        except Exception as exc:
            if "BucketAlreadyOwnedByYou" not in str(exc) and "already exists" not in str(exc):
                logger.warning(
                    "action.enable_config_recorder.bucket_warning",
                    error=str(exc),
                )

        role_arn = (
            f"arn:aws:iam::{account_id}:role/aws-service-role/"
            "config.amazonaws.com/AWSServiceRoleForConfig"
        )
        try:
            self.config_client.put_configuration_recorder(
                ConfigurationRecorder={
                    "name": "default",
                    "roleARN": role_arn,
                    "recordingGroup": {
                        "allSupported": True,
                        "includeGlobalResourceTypes": True,
                    },
                }
            )
        except ClientError as exc:
            if _error_code(exc) == "MaxNumberOfConfigurationRecordersExceededException":
                raise _SkippedRemediation(
                    f"Region {region} already has the maximum number of Config "
                    "recorders; a recorder likely already exists under a "
                    "different name."
                ) from exc
            raise
        try:
            self.config_client.put_delivery_channel(
                DeliveryChannel={
                    "name": "default",
                    "s3BucketName": bucket_name,
                }
            )
        except ClientError as exc:
            if _error_code(exc) == "MaxNumberOfDeliveryChannelsExceededException":
                raise _SkippedRemediation(
                    f"Region {region} already has the maximum number of Config "
                    "delivery channels; one likely already exists."
                ) from exc
            raise
        try:
            self.config_client.start_configuration_recorder(
                ConfigurationRecorderName="default"
            )
        except ClientError as exc:
            if _error_code(exc) == "NoAvailableConfigurationRecorderException":
                raise _SkippedRemediation(
                    f"No Config recorder available to start in region {region}."
                ) from exc
            raise
        logger.info("action.enable_config_recorder.done", region=region)

    def _create_default_backup_plan(self, account_id: str, region: str) -> None:
        """Create a default daily AWS Backup plan with 7-day retention (COMP-006, REL-004).

        Plan name: ``chandra-default-backup-plan``. Backs up all resources
        tagged ``Backup=daily`` daily at 05:00 UTC, retaining snapshots for
        7 days. Idempotent: skips creation if the plan already exists.
        """
        logger.info(
            "action.create_backup_plan",
            account_id=account_id,
            region=region,
        )
        plan_name = "chandra-default-backup-plan"

        existing_plans = self.backup_client.list_backup_plans().get(
            "BackupPlansList", []
        )
        if any(p["BackupPlanName"] == plan_name for p in existing_plans):
            logger.info("action.create_backup_plan.exists", name=plan_name)
            return

        try:
            resp = self.backup_client.create_backup_plan(
                BackupPlan={
                    "BackupPlanName": plan_name,
                    "Rules": [
                        {
                            "RuleName": "DailyBackup",
                            "TargetBackupVaultName": "Default",
                            "ScheduleExpression": "cron(0 5 * * ? *)",
                            "Lifecycle": {"DeleteAfterDays": 7},
                        }
                    ],
                }
            )
        except ClientError as exc:
            if _error_code(exc) == "AlreadyExistsException":
                raise _SkippedRemediation(
                    f"Backup plan {plan_name!r} was created concurrently by "
                    "another run; already resolved."
                ) from exc
            raise
        plan_id = resp["BackupPlanId"]

        try:
            self.backup_client.create_backup_selection(
                BackupPlanId=plan_id,
                BackupSelection={
                    "SelectionName": "AllTaggedResources",
                    "IamRoleArn": (
                        f"arn:aws:iam::{account_id}:role/service-role/"
                        "AWSBackupDefaultServiceRole"
                    ),
                    "ListOfTags": [
                        {
                            "ConditionType": "STRINGEQUALS",
                            "ConditionKey": "Backup",
                            "ConditionValue": "daily",
                        }
                    ],
                },
            )
        except ClientError as exc:
            code = _error_code(exc)
            if code == "AlreadyExistsException":
                logger.info(
                    "action.create_backup_plan.selection_exists",
                    plan_id=plan_id,
                )
            elif code == "InvalidParameterValueException":
                raise _SkippedRemediation(
                    f"Backup plan {plan_name!r} was created, but the default "
                    "AWSBackupDefaultServiceRole IAM role does not exist in "
                    "this account; create it (or a custom Backup service "
                    "role) before resource selection can be attached."
                ) from exc
            else:
                raise
        logger.info("action.create_backup_plan.done", plan_id=plan_id)

    def _trigger_config_rule_remediation(self, rule_identifier: str, region: str) -> None:
        """Trigger Config auto-remediation execution for a NON_COMPLIANT rule (COMP-007).

        Calls ``StartRemediationExecution`` for the rule's non-compliant
        resources. Requires a remediation action to be pre-configured on the
        Config rule; no-ops gracefully if none is configured or no non-compliant
        resources are found.
        """
        logger.info(
            "action.trigger_config_remediation",
            rule=rule_identifier,
            region=region,
        )
        # Extract rule name from ARN if necessary.
        rule_name = (
            rule_identifier.rsplit("/", 1)[-1]
            if "/" in rule_identifier
            else rule_identifier
        )

        resource_keys: list[dict[str, str]] = []
        try:
            for page in self.config_client.get_paginator(
                "get_compliance_details_by_config_rule"
            ).paginate(
                ConfigRuleName=rule_name,
                ComplianceTypes=["NON_COMPLIANT"],
            ):
                for result in page.get("EvaluationResults", []):
                    qualifier = (
                        result.get("EvaluationResultIdentifier", {})
                        .get("EvaluationResultQualifier", {})
                    )
                    resource_keys.append(
                        {
                            "resourceType": qualifier.get("ResourceType", ""),
                            "resourceId": qualifier.get("ResourceId", ""),
                        }
                    )
        except ClientError as exc:
            if _error_code(exc) == "NoSuchConfigRuleException":
                raise _SkippedRemediation(
                    f"Config rule {rule_name!r} no longer exists; already resolved."
                ) from exc
            raise ValueError(
                f"Failed to get compliance details for rule {rule_name}: {exc}"
            ) from exc
        except Exception as exc:
            raise ValueError(
                f"Failed to get compliance details for rule {rule_name}: {exc}"
            ) from exc

        if not resource_keys:
            logger.info(
                "action.trigger_config_remediation.no_resources",
                rule=rule_name,
            )
            return

        # StartRemediationExecution accepts up to 25 resource keys per call.
        # No remediation action configured on the rule is a documented,
        # non-error outcome — surface it as skipped rather than a failure.
        try:
            self.config_client.start_remediation_execution(
                ConfigRuleName=rule_name,
                ResourceKeys=resource_keys[:25],
            )
        except ClientError as exc:
            if _error_code(exc) == "NoSuchRemediationConfigurationException":
                raise _SkippedRemediation(
                    f"Config rule {rule_name!r} has no remediation action "
                    "configured; nothing to trigger."
                ) from exc
            raise
        logger.info(
            "action.trigger_config_remediation.done",
            rule=rule_name,
            resources=len(resource_keys[:25]),
        )

    def _enable_rds_multi_az(self, db_arn: str, region: str) -> None:
        """Enable Multi-AZ on a single-AZ RDS instance (REL-001).

        Extracts the DB instance identifier from the ARN and calls
        ModifyDBInstance with ApplyImmediately=True. Checks that the instance
        is in ``available`` state before modifying; refuses otherwise.

        WARNING: Causes a brief automatic failover (~60 seconds).
        """
        logger.info("action.enable_rds_multi_az", db_arn=db_arn, region=region)
        db_id = db_arn.rsplit(":", 1)[-1]
        try:
            resp = self.rds_client.describe_db_instances(DBInstanceIdentifier=db_id)
        except ClientError as exc:
            if _error_code(exc) == "DBInstanceNotFoundFault":
                raise _SkippedRemediation(
                    f"RDS instance {db_id!r} no longer exists; already resolved."
                ) from exc
            raise
        instances = resp.get("DBInstances", [])
        if not instances:
            raise _SkippedRemediation(
                f"RDS instance {db_id!r} no longer exists; already resolved."
            )
        db = instances[0]
        if db.get("MultiAZ"):
            logger.info("action.enable_rds_multi_az.already_enabled", db=db_id)
            return
        db_status = db.get("DBInstanceStatus", "unknown")
        if db_status != "available":
            raise _SkippedRemediation(
                f"RDS {db_id} is in state {db_status!r}; Multi-AZ can only "
                "be enabled on an 'available' instance. Retry once the "
                "current operation (e.g. backup, maintenance) completes."
            )
        try:
            self.rds_client.modify_db_instance(
                DBInstanceIdentifier=db_id,
                MultiAZ=True,
                ApplyImmediately=True,
            )
        except ClientError as exc:
            code = _error_code(exc)
            if code == "InvalidDBInstanceStateFault":
                raise _SkippedRemediation(
                    f"RDS {db_id} became busy (e.g. backing up) between the "
                    "state check and modify call; retry on the next run."
                ) from exc
            if code == "DBInstanceNotFoundFault":
                raise _SkippedRemediation(
                    f"RDS instance {db_id!r} no longer exists; already resolved."
                ) from exc
            raise
        logger.info("action.enable_rds_multi_az.done", db=db_id)

    def _create_default_dlm_policy(self, account_id: str, region: str) -> None:
        """Create a default DLM EBS snapshot lifecycle policy (REL-003).

        Policy: daily snapshots at 05:00 UTC, retain 7 snapshots, targeting
        EBS volumes tagged ``Backup=daily``. Uses the default DLM execution
        role (``AWSDataLifecycleManagerDefaultRole``). Idempotent: skips if
        a chandra-managed DLM policy already exists in the region.
        """
        logger.info(
            "action.create_dlm_policy",
            account_id=account_id,
            region=region,
        )
        existing = self.dlm_client.get_lifecycle_policies().get("Policies", [])
        if any("chandra" in p.get("Description", "") for p in existing):
            logger.info("action.create_dlm_policy.exists", region=region)
            return

        role_arn = (
            f"arn:aws:iam::{account_id}:role/AWSDataLifecycleManagerDefaultRole"
        )
        try:
            self.dlm_client.create_lifecycle_policy(
                ExecutionRoleArn=role_arn,
                Description="chandra-auto: daily EBS snapshots, 7-snapshot retention",
                State="ENABLED",
                PolicyDetails={
                    "PolicyType": "EBS_SNAPSHOT_MANAGEMENT",
                    "ResourceTypes": ["VOLUME"],
                    "TargetTags": [{"Key": "Backup", "Value": "daily"}],
                    "Schedules": [
                        {
                            "Name": "DailySnapshots",
                            "CreateRule": {
                                "Interval": 24,
                                "IntervalUnit": "HOURS",
                                "Times": ["05:00"],
                            },
                            "RetainRule": {"Count": 7},
                            "CopyTags": True,
                        }
                    ],
                },
            )
        except ClientError as exc:
            code = _error_code(exc)
            if code == "LimitExceededException":
                raise _SkippedRemediation(
                    f"Region {region} is already at the maximum number of "
                    "DLM lifecycle policies; manual cleanup required."
                ) from exc
            if code == "InvalidRequestException":
                raise _SkippedRemediation(
                    f"Cannot create DLM policy in {region}: the default "
                    "AWSDataLifecycleManagerDefaultRole IAM role does not "
                    "exist in this account; create it first."
                ) from exc
            raise
        logger.info("action.create_dlm_policy.done", region=region)

    def _stop_idle_ec2(self, instance_id: str, region: str) -> None:
        """Stop an idle EC2 instance (COST-001).

        Tags the instance with ``StoppedBy=chandra-auto-fix`` and
        ``StopReason=idle-cpu-under-5pct`` before stopping, providing a
        clear audit trail and making it easy to identify auto-stopped
        instances for manual review or restart.

        Only stops instances in the ``running`` state; raises for all others.
        """
        logger.info("action.stop_idle_ec2", instance_id=instance_id, region=region)
        try:
            resp = self.ec2_client.describe_instances(InstanceIds=[instance_id])
        except ClientError as exc:
            if _error_code(exc) == "InvalidInstanceID.NotFound":
                raise _SkippedRemediation(
                    f"EC2 instance {instance_id!r} no longer exists; already resolved."
                ) from exc
            raise
        reservations = resp.get("Reservations", [])
        if not reservations or not reservations[0].get("Instances"):
            raise _SkippedRemediation(
                f"EC2 instance {instance_id!r} no longer exists; already resolved."
            )
        state = reservations[0]["Instances"][0].get("State", {}).get("Name")
        if state in ("stopped", "stopping", "terminated", "shutting-down"):
            # Desired end-state (not running) is already achieved, or the
            # instance is gone — either way there is nothing left to do.
            raise _SkippedRemediation(
                f"Instance {instance_id} is already in state {state!r}; "
                "already resolved."
            )
        if state != "running":
            raise _SkippedRemediation(
                f"Instance {instance_id} is in state {state!r}; "
                "only 'running' instances are stopped."
            )
        self.ec2_client.create_tags(
            Resources=[instance_id],
            Tags=[
                {"Key": "StoppedBy", "Value": "chandra-auto-fix"},
                {"Key": "StopReason", "Value": "idle-cpu-under-5pct"},
            ],
        )
        try:
            self.ec2_client.stop_instances(InstanceIds=[instance_id])
        except ClientError as exc:
            code = _error_code(exc)
            if code == "IncorrectInstanceState":
                raise _SkippedRemediation(
                    f"Instance {instance_id} changed state before it could "
                    "be stopped; already resolved or in transition."
                ) from exc
            if code == "InvalidInstanceID.NotFound":
                raise _SkippedRemediation(
                    f"EC2 instance {instance_id!r} no longer exists; already resolved."
                ) from exc
            raise
        logger.info("action.stop_idle_ec2.done", instance_id=instance_id)

    def _stop_idle_rds(self, db_arn: str, region: str) -> None:
        """Stop an idle RDS instance (PERF-002).

        Adds ``StoppedBy=chandra-auto-fix`` and ``StopReason=idle-cpu-and-connections``
        tags to the instance before stopping. AWS automatically restarts stopped
        RDS instances after 7 days.

        Only stops instances in the ``available`` state; raises for all others.
        """
        logger.info("action.stop_idle_rds", db_arn=db_arn, region=region)
        db_id = db_arn.rsplit(":", 1)[-1]
        try:
            resp = self.rds_client.describe_db_instances(DBInstanceIdentifier=db_id)
        except ClientError as exc:
            if _error_code(exc) == "DBInstanceNotFoundFault":
                raise _SkippedRemediation(
                    f"RDS instance {db_id!r} no longer exists; already resolved."
                ) from exc
            raise
        instances = resp.get("DBInstances", [])
        if not instances:
            raise _SkippedRemediation(
                f"RDS instance {db_id!r} no longer exists; already resolved."
            )
        db_status = instances[0].get("DBInstanceStatus", "unknown")
        if db_status in ("stopped", "stopping"):
            raise _SkippedRemediation(
                f"RDS {db_id} is already in state {db_status!r}; already resolved."
            )
        if db_status != "available":
            raise _SkippedRemediation(
                f"RDS {db_id} is in state {db_status!r}; only 'available' "
                "instances can be stopped. Retry once the current operation "
                "completes."
            )
        self.rds_client.add_tags_to_resource(
            ResourceName=db_arn,
            Tags=[
                {"Key": "StoppedBy", "Value": "chandra-auto-fix"},
                {"Key": "StopReason", "Value": "idle-cpu-and-connections"},
            ],
        )
        try:
            self.rds_client.stop_db_instance(DBInstanceIdentifier=db_id)
        except ClientError as exc:
            code = _error_code(exc)
            if code == "InvalidDBInstanceStateFault":
                raise _SkippedRemediation(
                    f"RDS {db_id} cannot be stopped right now (e.g. it is a "
                    "read replica, part of a Multi-AZ cluster, or already "
                    "transitioning); retry on the next run."
                ) from exc
            if code == "DBInstanceNotFoundFault":
                raise _SkippedRemediation(
                    f"RDS instance {db_id!r} no longer exists; already resolved."
                ) from exc
            raise
        logger.info("action.stop_idle_rds.done", db=db_id)

    def _fix_ec2_instance_type(self, instance_id: str, region: str) -> None:
        """Stop → change instance type → start an over-provisioned EC2 (PERF-003, PERF-006).

        Queries Compute Optimizer for the recommended instance type. Falls back
        to a conservative one-step downsize within the same family if Compute
        Optimizer is not opted in or has no recommendation.

        Tags the instance with ``ChandraResizedFrom`` and ``ChandraResizedBy``
        for audit. Only resizes running or stopped instances; re-starts the
        instance if it was running before the change.

        WARNING: Causes downtime for the stop/start cycle (~2-5 minutes).
        """
        logger.info(
            "action.fix_ec2_instance_type",
            instance_id=instance_id,
            region=region,
        )

        # 1. Try to get the Compute Optimizer recommendation.
        recommended_type: str | None = None
        try:
            account_id = self._get_account_id()
            instance_arn = (
                f"arn:aws:ec2:{region}:{account_id}:instance/{instance_id}"
            )
            co_resp = self.co_client.get_ec2_instance_recommendations(
                instanceArns=[instance_arn]
            )
            recs = co_resp.get("instanceRecommendations", [])
            if recs and recs[0].get("recommendationOptions"):
                recommended_type = recs[0]["recommendationOptions"][0].get(
                    "instanceType"
                )
        except Exception as exc:
            logger.warning(
                "action.fix_ec2_instance_type.co_unavailable",
                instance_id=instance_id,
                error=str(exc),
            )

        # 2. Describe the instance to get current type and state.
        try:
            resp = self.ec2_client.describe_instances(InstanceIds=[instance_id])
        except ClientError as exc:
            if _error_code(exc) == "InvalidInstanceID.NotFound":
                raise _SkippedRemediation(
                    f"EC2 instance {instance_id!r} no longer exists; already resolved."
                ) from exc
            raise
        reservations = resp.get("Reservations", [])
        if not reservations or not reservations[0].get("Instances"):
            raise _SkippedRemediation(
                f"EC2 instance {instance_id!r} no longer exists; already resolved."
            )
        inst = reservations[0]["Instances"][0]
        current_type = inst.get("InstanceType", "")
        state = inst.get("State", {}).get("Name", "")

        if state in ("terminated", "shutting-down"):
            raise _SkippedRemediation(
                f"Instance {instance_id} is {state!r}; nothing left to resize."
            )
        if state not in ("running", "stopped"):
            raise _SkippedRemediation(
                f"Instance {instance_id} is in state {state!r}; can only "
                "resize 'running' or 'stopped' instances. Retry once it "
                "reaches a stable state."
            )

        # 3. Fall back to a one-step downsize within the same family.
        if recommended_type is None:
            _DOWNSIZE_MAP = {
                "nano": "nano",
                "micro": "nano",
                "small": "micro",
                "medium": "small",
                "large": "medium",
                "xlarge": "large",
                "2xlarge": "xlarge",
                "4xlarge": "2xlarge",
                "8xlarge": "4xlarge",
                "12xlarge": "8xlarge",
                "16xlarge": "12xlarge",
                "24xlarge": "16xlarge",
                "32xlarge": "24xlarge",
            }
            family, _, size = current_type.partition(".")
            recommended_type = f"{family}.{_DOWNSIZE_MAP.get(size, size)}"

        if recommended_type == current_type:
            logger.info(
                "action.fix_ec2_instance_type.no_change",
                instance_id=instance_id,
                type=current_type,
            )
            return

        # 4. Stop if running.
        was_running = state == "running"
        if was_running:
            self.ec2_client.stop_instances(InstanceIds=[instance_id])
            try:
                waiter = self.ec2_client.get_waiter("instance_stopped")
                waiter.wait(InstanceIds=[instance_id])
            except WaiterError as exc:
                raise ValueError(
                    f"Instance {instance_id} did not reach 'stopped' state "
                    f"in time: {exc}"
                ) from exc

        # 5. Change instance type.
        try:
            self.ec2_client.modify_instance_attribute(
                InstanceId=instance_id,
                InstanceType={"Value": recommended_type},
            )
        except ClientError as exc:
            code = _error_code(exc)
            if code == "IncorrectInstanceState":
                raise _SkippedRemediation(
                    f"Instance {instance_id} is not in a state that permits "
                    "changing its instance type right now; retry on the "
                    "next run."
                ) from exc
            if code in ("InvalidParameterValue", "Unsupported"):
                raise _SkippedRemediation(
                    f"Instance type {recommended_type!r} is not available "
                    f"for instance {instance_id} (e.g. unsupported in its "
                    "Availability Zone); manual review required."
                ) from exc
            raise
        self.ec2_client.create_tags(
            Resources=[instance_id],
            Tags=[
                {"Key": "ChandraResizedFrom", "Value": current_type},
                {"Key": "ChandraResizedBy", "Value": "chandra-auto-fix"},
            ],
        )

        # 6. Restart if it was running before.
        if was_running:
            self.ec2_client.start_instances(InstanceIds=[instance_id])

        logger.info(
            "action.fix_ec2_instance_type.done",
            instance_id=instance_id,
            from_type=current_type,
            to_type=recommended_type,
        )

    def _update_lambda_memory(self, function_arn: str, region: str) -> None:
        """Update Lambda memory to Compute Optimizer's recommendation (PERF-007).

        Queries Compute Optimizer for the top memory recommendation for the
        function and applies it via UpdateFunctionConfiguration. No downtime —
        Lambda applies the new memory configuration to subsequent invocations
        immediately.

        Raises if Compute Optimizer has no recommendation for the function.
        """
        logger.info(
            "action.update_lambda_memory",
            function_arn=function_arn,
            region=region,
        )
        co_resp = self.co_client.get_lambda_function_recommendations(
            functionArns=[function_arn]
        )
        recs = co_resp.get("lambdaFunctionRecommendations", [])
        if not recs or not recs[0].get("memorySizeRecommendationOptions"):
            raise ValueError(
                f"No Compute Optimizer memory recommendation found for {function_arn}"
            )

        recommended_memory: int = recs[0]["memorySizeRecommendationOptions"][0][
            "memorySize"
        ]
        current_memory: int = recs[0].get("currentMemorySize", 0)

        if recommended_memory == current_memory:
            logger.info(
                "action.update_lambda_memory.no_change",
                function_arn=function_arn,
                memory_mb=current_memory,
            )
            return

        try:
            self.lambda_client.update_function_configuration(
                FunctionName=function_arn,
                MemorySize=recommended_memory,
            )
        except ClientError as exc:
            code = _error_code(exc)
            if code == "ResourceNotFoundException":
                raise _SkippedRemediation(
                    f"Lambda function {function_arn!r} no longer exists; "
                    "already resolved."
                ) from exc
            if code == "ResourceConflictException":
                raise _SkippedRemediation(
                    f"Lambda function {function_arn!r} has another update "
                    "in progress; retry on the next run."
                ) from exc
            raise
        logger.info(
            "action.update_lambda_memory.done",
            function_arn=function_arn,
            from_mb=current_memory,
            to_mb=recommended_memory,
        )

    def _trigger_config_resource_remediation(self, resource_id: str, region: str) -> None:
        """Trigger Config auto-remediation for every rule where this resource is NON_COMPLIANT.

        Scans all Config rules in the region, finds rules where the given resource
        ID or ARN is non-compliant, and calls StartRemediationExecution for each.
        Rules that have no remediation action configured are silently skipped.
        Raises only if no Config rules at all could be listed.
        """
        logger.info(
            "action.trigger_config_resource_remediation",
            resource_id=resource_id,
            region=region,
        )
        rules: list[dict[str, Any]] = []
        for page in self.config_client.get_paginator("describe_config_rules").paginate():
            rules.extend(page.get("ConfigRules", []))

        if not rules:
            raise _SkippedRemediation(
                f"No Config rules exist in region {region}; nothing to check "
                f"for resource {resource_id!r}."
            )

        triggered = 0
        for rule in rules:
            rule_name = rule["ConfigRuleName"]
            try:
                for page in self.config_client.get_paginator(
                    "get_compliance_details_by_config_rule"
                ).paginate(
                    ConfigRuleName=rule_name,
                    ComplianceTypes=["NON_COMPLIANT"],
                ):
                    for result in page.get("EvaluationResults", []):
                        qualifier = (
                            result.get("EvaluationResultIdentifier", {})
                            .get("EvaluationResultQualifier", {})
                        )
                        r_id = qualifier.get("ResourceId", "")
                        r_arn = qualifier.get("ResourceArn", "")
                        # Match if the resource_id appears in either field.
                        if resource_id in (r_id, r_arn) or r_id in resource_id:
                            try:
                                self.config_client.start_remediation_execution(
                                    ConfigRuleName=rule_name,
                                    ResourceKeys=[
                                        {
                                            "resourceType": qualifier.get("ResourceType", ""),
                                            "resourceId": r_id,
                                        }
                                    ],
                                )
                                triggered += 1
                                logger.info(
                                    "action.trigger_config_resource_remediation.triggered",
                                    rule=rule_name,
                                    resource=r_id,
                                )
                            except Exception as exc:
                                logger.warning(
                                    "action.trigger_config_resource_remediation.skip",
                                    rule=rule_name,
                                    reason=str(exc),
                                )
            except Exception as exc:
                logger.warning(
                    "action.trigger_config_resource_remediation.rule_error",
                    rule=rule_name,
                    error=str(exc),
                )

        if triggered == 0:
            # Either the resource is no longer non-compliant anywhere (the
            # finding resolved itself), or none of the matching rules have a
            # remediation action configured. Both are non-error, no-action
            # outcomes — surface as skipped rather than a hard failure.
            raise _SkippedRemediation(
                f"No Config auto-remediation actions were configured, or the "
                f"resource {resource_id!r} is no longer reported as "
                "non-compliant by any rule; already resolved or nothing to do."
            )
        logger.info(
            "action.trigger_config_resource_remediation.done",
            resource_id=resource_id,
            triggered=triggered,
        )

    def _create_encrypted_rds_replacement(self, db_arn: str, region: str) -> None:
        """Create an encrypted RDS instance as a replacement for an unencrypted one (COMP-001).

        Workflow:
          1. Create a snapshot of the source instance.
          2. Copy the snapshot with the aws/rds KMS key (creates an encrypted copy).
          3. Restore a NEW DB instance named ``<original-id>-encrypted`` from the copy.
          4. Tag the original instance as pending cutover.

        The original instance is intentionally NOT deleted — it remains running until
        a human updates the application endpoint and confirms the migration is complete.

        WARNING: Takes several minutes (snapshot + copy + restore). The new instance
        will have a different endpoint than the original.
        """
        logger.info(
            "action.create_encrypted_rds_replacement",
            db_arn=db_arn,
            region=region,
        )
        db_id = db_arn.rsplit(":", 1)[-1]
        timestamp_suffix = int(datetime.now().timestamp())
        snapshot_id = f"chandra-snap-{db_id[:30]}-{timestamp_suffix}"
        encrypted_snapshot_id = f"chandra-enc-{db_id[:30]}-{timestamp_suffix}"
        new_db_id = f"{db_id[:40]}-encrypted"

        # 1. Create snapshot of the source instance.
        logger.info(
            "action.create_encrypted_rds_replacement.snapshot",
            snapshot_id=snapshot_id,
        )
        try:
            self.rds_client.create_db_snapshot(
                DBSnapshotIdentifier=snapshot_id,
                DBInstanceIdentifier=db_id,
            )
        except ClientError as exc:
            code = _error_code(exc)
            if code == "DBInstanceNotFoundFault":
                raise _SkippedRemediation(
                    f"RDS instance {db_id!r} no longer exists; already resolved."
                ) from exc
            if code == "InvalidDBInstanceStateFault":
                raise _SkippedRemediation(
                    f"RDS instance {db_id} already has a backup/snapshot "
                    "operation in progress; retry on the next run."
                ) from exc
            raise
        try:
            waiter = self.rds_client.get_waiter("db_snapshot_available")
            waiter.wait(DBSnapshotIdentifier=snapshot_id)
        except WaiterError as exc:
            raise ValueError(
                f"Snapshot {snapshot_id} did not become available in time: {exc}"
            ) from exc

        # 2. Copy the snapshot with KMS encryption.
        logger.info(
            "action.create_encrypted_rds_replacement.copy",
            encrypted_snapshot_id=encrypted_snapshot_id,
        )
        self.rds_client.copy_db_snapshot(
            SourceDBSnapshotIdentifier=snapshot_id,
            TargetDBSnapshotIdentifier=encrypted_snapshot_id,
            KmsKeyId="alias/aws/rds",
            CopyTags=True,
        )
        waiter.wait(DBSnapshotIdentifier=encrypted_snapshot_id)

        # 3. Restore a new encrypted instance from the encrypted snapshot.
        logger.info(
            "action.create_encrypted_rds_replacement.restore",
            new_db_id=new_db_id,
        )
        self.rds_client.restore_db_instance_from_db_snapshot(
            DBInstanceIdentifier=new_db_id,
            DBSnapshotIdentifier=encrypted_snapshot_id,
        )

        # 4. Tag the original instance for human cutover.
        self.rds_client.add_tags_to_resource(
            ResourceName=db_arn,
            Tags=[
                {"Key": "ChandraStatus", "Value": "pending-encryption-cutover"},
                {"Key": "ChandraEncryptedReplacement", "Value": new_db_id},
                {"Key": "ChandraAutoFixedBy", "Value": "chandra-auto-fix"},
            ],
        )

        logger.info(
            "action.create_encrypted_rds_replacement.done",
            original_db=db_id,
            new_db=new_db_id,
        )

    def _create_encrypted_ebs_replacement(self, volume_id: str, region: str) -> None:
        """Create an encrypted copy of an unencrypted EBS volume (COMP-004).

        Workflow:
          1. Create an EBS snapshot of the source volume.
          2. Copy the snapshot with ``Encrypted=True`` (uses aws/ebs KMS key by default).
          3. Create a new encrypted volume from the encrypted snapshot.
          4a. If the source volume is ``available`` (unattached): delete the original.
          4b. If the source volume is ``in-use``: tag the original for manual swap.

        WARNING: Steps 1-3 create a new volume with a new volume ID. If the volume
        is in-use, the application must be updated to use the new volume ID and
        the old volume must be manually swapped/detached/deleted.
        """
        logger.info(
            "action.create_encrypted_ebs_replacement",
            volume_id=volume_id,
            region=region,
        )
        # Describe source volume.
        try:
            resp = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
        except ClientError as exc:
            if _error_code(exc) == "InvalidVolume.NotFound":
                raise _SkippedRemediation(
                    f"EBS volume {volume_id!r} no longer exists; already resolved."
                ) from exc
            raise
        volumes = resp.get("Volumes", [])
        if not volumes:
            raise _SkippedRemediation(
                f"EBS volume {volume_id!r} no longer exists; already resolved."
            )
        vol = volumes[0]
        vol_state = vol.get("State", "")
        az = vol["AvailabilityZone"]
        vol_type = vol.get("VolumeType", "gp3")
        vol_size = vol["Size"]

        # 1. Create snapshot.
        snap_resp = self.ec2_client.create_snapshot(
            VolumeId=volume_id,
            Description=f"chandra-enc: encrypted copy source for {volume_id}",
            TagSpecifications=[
                {
                    "ResourceType": "snapshot",
                    "Tags": [{"Key": "CreatedBy", "Value": "chandra-auto-fix"}],
                }
            ],
        )
        snapshot_id = snap_resp["SnapshotId"]
        logger.info(
            "action.create_encrypted_ebs_replacement.snapshot",
            snapshot_id=snapshot_id,
        )
        snap_waiter = self.ec2_client.get_waiter("snapshot_completed")
        snap_waiter.wait(SnapshotIds=[snapshot_id])

        # 2. Copy snapshot with encryption.
        copy_resp = self.ec2_client.copy_snapshot(
            SourceRegion=region,
            SourceSnapshotId=snapshot_id,
            Description=f"chandra-enc: encrypted copy of {volume_id}",
            Encrypted=True,
        )
        enc_snapshot_id = copy_resp["SnapshotId"]
        logger.info(
            "action.create_encrypted_ebs_replacement.copy",
            enc_snapshot_id=enc_snapshot_id,
        )
        snap_waiter.wait(SnapshotIds=[enc_snapshot_id])

        # 3. Create encrypted volume.
        new_vol_resp = self.ec2_client.create_volume(
            AvailabilityZone=az,
            Encrypted=True,
            SnapshotId=enc_snapshot_id,
            VolumeType=vol_type,
            Size=vol_size,
            TagSpecifications=[
                {
                    "ResourceType": "volume",
                    "Tags": [
                        {"Key": "ChandraReplacementFor", "Value": volume_id},
                        {"Key": "ChandraCreatedBy", "Value": "chandra-auto-fix"},
                    ],
                }
            ],
        )
        new_vol_id = new_vol_resp["VolumeId"]
        logger.info(
            "action.create_encrypted_ebs_replacement.created",
            new_vol_id=new_vol_id,
        )

        # 4. Handle original based on state.
        if vol_state == "available":
            # Unattached — safe to delete the original immediately. This is
            # best-effort cleanup: the encrypted replacement above already
            # succeeded, so a failure to delete the old volume should not be
            # reported as a remediation failure.
            try:
                self.ec2_client.delete_volume(VolumeId=volume_id)
                logger.info(
                    "action.create_encrypted_ebs_replacement.deleted_original",
                    volume_id=volume_id,
                )
            except ClientError as exc:
                code = _error_code(exc)
                if code in ("InvalidVolume.NotFound", "VolumeInUse"):
                    logger.warning(
                        "action.create_encrypted_ebs_replacement.cleanup_skipped",
                        volume_id=volume_id,
                        error=str(exc),
                    )
                else:
                    raise
        else:
            # In-use — tag original for manual swap; do NOT delete.
            self.ec2_client.create_tags(
                Resources=[volume_id],
                Tags=[
                    {"Key": "ChandraStatus", "Value": "pending-encryption-swap"},
                    {"Key": "ChandraEncryptedReplacement", "Value": new_vol_id},
                    {"Key": "ChandraAutoFixedBy", "Value": "chandra-auto-fix"},
                ],
            )

        logger.info(
            "action.create_encrypted_ebs_replacement.done",
            original_volume=volume_id,
            new_volume=new_vol_id,
            original_deleted=vol_state == "available",
        )

    def _migrate_ec2_to_asg(self, instance_id: str, region: str) -> None:
        """Create an Auto Scaling Group and attach the existing instance to it (PERF-001).

        Workflow:
          1. Describe the instance to capture its type, AMI, security groups,
             subnet, and key pair.
          2. Create a Launch Template named ``chandra-lt-<instance_id>`` from
             the instance's live configuration.
          3. Create an Auto Scaling Group named ``chandra-asg-<instance_id>``
             with MinSize=1, MaxSize=3, DesiredCapacity=1 in the instance's subnet.
          4. Attach the existing instance to the ASG so it is immediately managed.

        The instance stays running throughout — there is no downtime.
        """
        logger.info(
            "action.migrate_ec2_to_asg",
            instance_id=instance_id,
            region=region,
        )
        # 1. Describe instance.
        try:
            resp = self.ec2_client.describe_instances(InstanceIds=[instance_id])
        except ClientError as exc:
            if _error_code(exc) == "InvalidInstanceID.NotFound":
                raise _SkippedRemediation(
                    f"EC2 instance {instance_id!r} no longer exists; already resolved."
                ) from exc
            raise
        reservations = resp.get("Reservations", [])
        if not reservations or not reservations[0].get("Instances"):
            raise _SkippedRemediation(
                f"EC2 instance {instance_id!r} no longer exists; already resolved."
            )
        inst = reservations[0]["Instances"][0]
        instance_state = inst.get("State", {}).get("Name", "")
        if instance_state in ("terminated", "shutting-down"):
            raise _SkippedRemediation(
                f"Instance {instance_id} is {instance_state!r}; nothing to "
                "migrate into an Auto Scaling group."
            )

        image_id = inst.get("ImageId", "")
        instance_type = inst.get("InstanceType", "")
        key_name = inst.get("KeyName", "")
        subnet_id = inst.get("SubnetId", "")
        sg_ids = [sg["GroupId"] for sg in inst.get("SecurityGroups", [])]

        if not image_id or not instance_type:
            raise ValueError(
                f"Instance {instance_id} is missing ImageId or InstanceType; "
                "cannot create a Launch Template."
            )

        # 2. Create Launch Template.
        lt_name = f"chandra-lt-{instance_id}"
        lt_data: dict[str, Any] = {
            "ImageId": image_id,
            "InstanceType": instance_type,
        }
        if sg_ids:
            lt_data["SecurityGroupIds"] = sg_ids
        if key_name:
            lt_data["KeyName"] = key_name

        try:
            lt_resp = self.ec2_client.create_launch_template(
                LaunchTemplateName=lt_name,
                LaunchTemplateData=lt_data,
                TagSpecifications=[
                    {
                        "ResourceType": "launch-template",
                        "Tags": [{"Key": "CreatedBy", "Value": "chandra-auto-fix"}],
                    }
                ],
            )
            lt_id = lt_resp["LaunchTemplate"]["LaunchTemplateId"]
        except self.ec2_client.exceptions.ClientError as exc:
            if "AlreadyExists" in str(exc):
                # Re-use existing template if we already created one for this instance.
                lt_resp = self.ec2_client.describe_launch_templates(
                    LaunchTemplateNames=[lt_name]
                )
                lt_id = lt_resp["LaunchTemplates"][0]["LaunchTemplateId"]
            else:
                raise

        logger.info(
            "action.migrate_ec2_to_asg.lt_created",
            lt_id=lt_id,
            lt_name=lt_name,
        )

        # 3. Create Auto Scaling Group.
        asg_name = f"chandra-asg-{instance_id}"
        try:
            self.asg_client.create_auto_scaling_group(
                AutoScalingGroupName=asg_name,
                LaunchTemplate={"LaunchTemplateId": lt_id, "Version": "$Latest"},
                MinSize=1,
                MaxSize=3,
                DesiredCapacity=1,
                VPCZoneIdentifier=subnet_id,
                Tags=[
                    {
                        "ResourceId": asg_name,
                        "ResourceType": "auto-scaling-group",
                        "Key": "CreatedBy",
                        "Value": "chandra-auto-fix",
                        "PropagateAtLaunch": True,
                    }
                ],
            )
        except self.asg_client.exceptions.AlreadyExistsFault:
            logger.info(
                "action.migrate_ec2_to_asg.asg_exists",
                asg_name=asg_name,
            )

        logger.info(
            "action.migrate_ec2_to_asg.asg_created",
            asg_name=asg_name,
        )

        # 4. Attach existing instance to the ASG.
        self.asg_client.attach_instances(
            InstanceIds=[instance_id],
            AutoScalingGroupName=asg_name,
        )

        logger.info(
            "action.migrate_ec2_to_asg.done",
            instance_id=instance_id,
            asg_name=asg_name,
            lt_id=lt_id,
        )


# ---------------------------------------------------------------------------
# LangGraph node — consumes auto_fixed, emits action_results
# ---------------------------------------------------------------------------


@traced_node
def action_executor_node(state: ChandraState) -> dict[str, Any]:
    """Execute low-risk auto-fixes for each ProposedWrite in state["auto_fixed"].

    Iterates ``state["auto_fixed"]`` (a list[ProposedWrite] emitted by
    ``decision_router``) and dispatches each write through a detector-id -> handler
    registry co-located in this module. Emits one ``ActionResult`` per write to
    ``state["action_results"]`` — in input order, so audit logs are deterministic.

    Behaviour:
      - Unknown detector id        -> ``ActionResult(status="skipped", ...)`` generic message.
      - Observation-only handler   -> ``ActionResult(status="skipped", ...)`` with rich reason.
      - ARN parse failure          -> ``ActionResult(status="failure", ...)``.
      - Handler exception          -> ``ActionResult(status="failure", ...)``.
      - ``dry_run`` from ``state["dry_run"]`` (default ``True``); the
        ``ActionExecutor`` records the requested mode on every result so
        consumers don't have to infer it from ``status``.

    Region is read from each ``ProposedWrite.region`` (not from
    ``state["region"]``), so multi-region auto-fixes work correctly.
    """
    auto_fixed: list[ProposedWrite] = list(state.get("auto_fixed", []) or [])
    dry_run: bool = bool(state.get("dry_run", True))
    run_id = state.get("run_id")

    if not auto_fixed:
        logger.info(
            "graph.action_executor",
            run_id=run_id,
            skipped=0,
            executed=0,
            dry_run=dry_run,
        )
        return {"action_results": []}

    results: list[ActionResult] = []
    for write in auto_fixed:
        detector_id = write.action.removeprefix("remediate_")
        handler = _HANDLERS.get(detector_id)

        # ── Unknown detector — no handler registered at all ──────────────
        if handler is None:
            results.append(
                ActionResult(
                    action=write.action,
                    target_arn=write.target_arn,
                    region=write.region,
                    status="skipped",
                    message=(
                        f"No handler registered for detector {detector_id}; "
                        "auto-fix is observation-only for this detector."
                    ),
                    dry_run=dry_run,
                )
            )
            continue

        # ── Observation-only handler — emit skipped with rich reason ──────
        if isinstance(handler, _ObservationOnlyHandler):
            results.append(
                ActionResult(
                    action=write.action,
                    target_arn=write.target_arn,
                    region=write.region,
                    status="skipped",
                    message=handler.skip_reason,
                    dry_run=dry_run,
                )
            )
            continue

        # ── Auto-remediable handler — extract resource id then execute ────
        try:
            resource_id = handler.extract_resource_id(write.target_arn)
        except ValueError as exc:
            results.append(
                ActionResult(
                    action=write.action,
                    target_arn=write.target_arn,
                    region=write.region,
                    status="failure",
                    message="Failed to extract resource id from ARN",
                    error=str(exc),
                    dry_run=dry_run,
                )
            )
            continue

        try:
            executor = ActionExecutor(dry_run=dry_run, region=write.region)
            outcome = executor.run(
                {
                    "action_type": write.action,
                    "resource_id": resource_id,
                    "region": write.region,
                    "problem_type": handler.problem_type,
                }
            )
        except Exception as exc:
            results.append(
                ActionResult(
                    action=write.action,
                    target_arn=write.target_arn,
                    region=write.region,
                    status="failure",
                    message=f"ActionExecutor raised: {exc!r}",
                    error=str(exc),
                    dry_run=dry_run,
                )
            )
            continue

        results.append(
            ActionResult(
                action=write.action,
                target_arn=write.target_arn,
                region=write.region,
                status=outcome["status"],
                message=outcome["message"],
                error=outcome.get("error"),
                audit_log=outcome.get("audit_log"),
                dry_run=dry_run,
            )
        )

    logger.info(
        "graph.action_executor",
        run_id=run_id,
        executed=len(results),
        dry_run=dry_run,
        breakdown={r.status: sum(1 for x in results if x.status == r.status) for r in results},
    )
    return {"action_results": results}
