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

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.chandra.aws.client_factory import get_default_factory
from src.chandra.briefing.schemas import ActionResult, ProposedWrite
from src.chandra.graphs.state import ChandraState
from src.chandra.logging import get_logger
from src.chandra.observability import traced_node

logger = get_logger(__name__)


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


# ---------------------------------------------------------------------------
# Detector-id -> handler registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Handler:
    """Wires a detector id to the right ActionExecutor method and ARN parser."""

    problem_type: str
    extract_resource_id: Callable[[str], str]
    run: Callable[[ActionExecutor, str, str], None]


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


_HANDLERS: dict[str, _Handler] = {
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
    # COST-002: unattached EBS volumes. Delete after verifying the
    # volume is in the ``available`` state (i.e. nothing is attached);
    # refuses otherwise. The volume's data is gone — use snapshot +
    # copy if you need a recovery path.
    "COST-002-unattached-ebs": _Handler(
        problem_type="unattached_ebs",
        extract_resource_id=_volume_id_from_arn,
        run=_fix_unattached_ebs_via,
    ),
    # COST-004: untagged billable resources. Adds placeholder tags
    # ``Environment=untagged`` and ``Owner=chandra-auto-fix`` so the
    # instance shows up under cost allocation. A human should replace
    # the placeholders with real values.
    "COST-004-untagged-billable": _Handler(
        problem_type="untagged_instance",
        extract_resource_id=_instance_id_from_arn,
        run=_fix_untagged_via,
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

    def __init__(self, dry_run: bool = True, region: str = "us-east-1"):
        """
        Args:
            dry_run: If True, show what would happen without doing it.
            region: AWS region for the executor.
        """
        self.dry_run = dry_run
        self.region = region
        factory = get_default_factory()
        self.s3_client = factory.client("s3", region=region)
        self.iam_client = factory.client("iam", region=region)
        self.ec2_client = factory.client("ec2", region=region)

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        """
        Execute a single action from state.

        Args:
            state: action spec
                {
                  "action_type": "remediate_SEC-001-public-s3",
                  "resource_id": "bucket-name",
                  "region": "us-east-1",
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
        region = state.get("region", "us-east-1")
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

            if problem_type == "public_s3":
                self._fix_public_s3(resource_id, region)
            elif problem_type == "open_security_group":
                self._fix_open_sg(resource_id, region)
            elif problem_type == "stale_iam_key":
                self._disable_iam_key(resource_id, region)
            elif problem_type == "unattached_ebs":
                self._fix_unattached_ebs(resource_id, region)
            elif problem_type == "untagged_instance":
                self._fix_untagged_instance(resource_id, region)
            else:
                raise ValueError(f"Unknown problem type: {problem_type}")

            message = f"Successfully executed {action_type} on {resource_id}"
            audit_entry = f"[{timestamp}] SUCCESS: {action_type} on {resource_id}"
            logger.info(message)

            return {
                "action_executed": True,
                "status": "success",
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

    def _fix_public_s3(self, bucket_name: str, region: str) -> None:
        """Make S3 bucket private by enabling Block Public Access (modern AWS standard)."""
        logger.info(f"Fixing public S3: {bucket_name}")
        self.s3_client.put_public_access_block(
            Bucket=bucket_name,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )

    def _fix_open_sg(self, sg_id: str, region: str) -> None:
        """Close open security group."""
        logger.info(f"Fixing security group: {sg_id}")
        self.ec2_client.revoke_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "-1",
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                }
            ],
        )

    def _disable_iam_key(self, key_id: str, region: str) -> None:
        """Disable stale IAM key."""
        logger.info(f"Disabling IAM key: {key_id}")
        self.iam_client.update_access_key_status(
            AccessKeyId=key_id,
            Status="Inactive",
        )

    def _fix_unattached_ebs(self, volume_id: str, region: str) -> None:
        """Delete an unattached EBS volume after a state check.

        Refuses to delete anything that isn't in the ``available``
        state, which is the only state that means "no attachments".
        Calling ``delete_volume`` on an in-use volume would either
        fail with ``VolumeInUse`` (good) or, if the volume was just
        detached and the state hasn't caught up, succeed and lose
        data. Verify explicitly so the failure mode is a clear
        refusal rather than a 5xx.
        """
        logger.info(f"Inspecting EBS volume: {volume_id}")
        resp = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
        if not resp["Volumes"]:
            raise ValueError(f"Volume {volume_id} not found")
        state = resp["Volumes"][0].get("State")
        if state != "available":
            raise ValueError(
                f"Volume {volume_id} is in state {state!r}, not 'available'; "
                "refusing to delete a volume that may be in use."
            )
        logger.info(f"Deleting EBS volume: {volume_id}")
        self.ec2_client.delete_volume(VolumeId=volume_id)

    def _fix_untagged_instance(self, instance_id: str, region: str) -> None:
        """Apply placeholder Environment/Owner tags flagged as missing.

        The placeholder values (``Environment=untagged`` and
        ``Owner=chandra-auto-fix``) are deliberately distinct from any
        value a human would type, so a reviewer can see at a glance
        which instances were auto-fixed and need real tags applied.
        This is a metadata-only change — no runtime impact.
        """
        logger.info(f"Applying placeholder tags to instance: {instance_id}")
        self.ec2_client.create_tags(
            Resources=[instance_id],
            Tags=[
                {"Key": "Environment", "Value": "untagged"},
                {"Key": "Owner", "Value": "chandra-auto-fix"},
            ],
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
      - Unknown detector id -> ``ActionResult(status="skipped", ...)``.
      - ARN parse failure   -> ``ActionResult(status="failure", ...)``.
      - Handler exception   -> ``ActionResult(status="failure", ...)``.
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
