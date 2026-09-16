"""Rollback and idempotency for AWS mutations (PRD §26.9).

The PRD states the principle — "revert using an approved rollback method when
safe, otherwise escalate for controlled recovery" — and leaves the mechanism
open. What is implemented here and what is deliberately not:

**Implemented: Terraform state snapshot and restore.** Before an apply the
current ``terraform.tfstate`` is captured; if the apply fails partway, the
snapshot is what an operator needs to understand and undo what landed. Restoring
the state *file* is offered as an explicit, separate action rather than
something the workflow does on its own.

**Not implemented, on purpose: automatic `terraform destroy` on failure.** A
failed apply leaves AWS in an unknown state, and an automatic destroy against a
state file that may not describe reality can delete resources the plan never
created. "Safe" in the PRD's sentence is doing a lot of work, and deciding what
qualifies is a business decision (flagged, not invented). So a failed mutation
captures evidence and escalates; the destructive step stays human-initiated.

**Idempotency** is the other half. Retrying a mutation is only safe if the
operation has not already taken effect, so a retry is gated on a fresh read of
actual AWS state rather than on the previous attempt's exit code — a Terraform
apply can create a resource and then fail on a later step, and retrying blind
would create it twice.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.chandra.logging import get_logger

logger = get_logger(__name__)

STATE_FILENAME = "terraform.tfstate"
SNAPSHOT_SUFFIX = ".chandra-snapshot"


@dataclass
class StateSnapshot:
    """A point-in-time copy of Terraform state, taken before a mutation."""

    path: Path | None
    taken_at: datetime
    resource_count: int
    existed: bool
    error: str = ""

    @property
    def available(self) -> bool:
        return self.path is not None and self.path.exists()

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path) if self.path else None,
            "taken_at": self.taken_at.isoformat(),
            "resource_count": self.resource_count,
            "existed_before_apply": self.existed,
            "available": self.available,
            "error": self.error,
        }


def _count_resources(state_path: Path) -> int:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        resources = data.get("resources", [])
        return sum(len(r.get("instances", [])) for r in resources) if resources else 0
    except (OSError, ValueError):
        return 0


def snapshot_state(workdir: str | Path) -> StateSnapshot:
    """Capture Terraform state before an apply. Never raises.

    A snapshot failure must not block the apply — it degrades the recovery story
    for that run, which is the operator's call to make, not a reason to refuse
    work that was already approved through two gates.
    """
    now = datetime.now(UTC)
    state_path = Path(workdir) / STATE_FILENAME
    if not state_path.exists():
        # First apply in this workspace: "no prior state" is itself the thing to
        # restore to, and is recorded as such.
        return StateSnapshot(path=None, taken_at=now, resource_count=0, existed=False)

    snapshot_path = state_path.with_suffix(state_path.suffix + SNAPSHOT_SUFFIX)
    try:
        shutil.copy2(state_path, snapshot_path)
        count = _count_resources(state_path)
        logger.info("rollback.snapshot_taken", path=str(snapshot_path), resources=count)
        return StateSnapshot(path=snapshot_path, taken_at=now, resource_count=count, existed=True)
    except OSError as exc:
        logger.warning("rollback.snapshot_failed", error=str(exc))
        return StateSnapshot(
            path=None, taken_at=now, resource_count=0, existed=True, error=str(exc)
        )


def restore_state(snapshot: StateSnapshot, workdir: str | Path) -> bool:
    """Restore a captured state file.

    This restores Chandra's *record* of infrastructure, not the infrastructure
    itself. Any resource the failed apply actually created still exists in AWS
    and will now be absent from state — which is precisely why this is an
    operator-initiated recovery step with escalation attached, not an automatic
    one.
    """
    if not snapshot.available or snapshot.path is None:
        logger.warning("rollback.restore_unavailable")
        return False
    try:
        shutil.copy2(snapshot.path, Path(workdir) / STATE_FILENAME)
        logger.info("rollback.state_restored", path=str(snapshot.path))
        return True
    except OSError as exc:
        logger.error("rollback.restore_failed", error=str(exc))
        return False


@dataclass
class IdempotencyVerdict:
    """Whether a mutation may be retried."""

    safe_to_retry: bool
    reason: str
    already_applied: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "safe_to_retry": self.safe_to_retry,
            "reason": self.reason,
            "already_applied": self.already_applied,
            "evidence": self.evidence,
        }


def verify_idempotency(
    workdir: str | Path,
    expected_resources: list[str] | None = None,
    runner: Any = None,
) -> IdempotencyVerdict:
    """Decide whether a failed mutation can be retried, from live state.

    Uses ``terraform plan -detailed-exitcode``, whose exit codes are the cleanest
    available answer to "does reality already match the desired state?":

    * ``0`` — no changes: the mutation already took effect. Retrying would be a
      no-op at best; the correct move is to proceed to verification.
    * ``2`` — changes pending: nothing (or not everything) was applied, so a
      retry is safe.
    * anything else — the question could not be answered, so retry is refused.

    Refusing on an unanswerable question is the point: a retry that might double
    a mutation is worse than an escalation.
    """
    workdir = Path(workdir)
    run = runner or _run_terraform_plan
    try:
        exit_code, output = run(workdir)
    except Exception as exc:
        logger.warning("idempotency.check_failed", error=str(exc))
        return IdempotencyVerdict(
            safe_to_retry=False,
            reason=f"Could not determine current state: {exc}",
            evidence={"error": str(exc)},
        )

    evidence: dict[str, Any] = {
        "plan_exit_code": exit_code,
        "expected_resources": expected_resources or [],
        "output_tail": output[-2000:] if output else "",
    }

    if exit_code == 0:
        return IdempotencyVerdict(
            safe_to_retry=False,
            reason="Desired state already matches AWS; the mutation has taken effect",
            already_applied=True,
            evidence=evidence,
        )
    if exit_code == 2:
        return IdempotencyVerdict(
            safe_to_retry=True,
            reason="Changes are still pending; the mutation did not take effect",
            evidence=evidence,
        )
    return IdempotencyVerdict(
        safe_to_retry=False,
        reason=f"Terraform plan could not determine state (exit {exit_code})",
        evidence=evidence,
    )


def _run_terraform_plan(workdir: Path) -> tuple[int, str]:
    completed = subprocess.run(
        ["terraform", "plan", "-detailed-exitcode", "-no-color", "-input=false"],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def plan_recovery(
    snapshot: StateSnapshot, verdict: IdempotencyVerdict, apply_succeeded: bool
) -> dict[str, Any]:
    """Recommend a recovery action. Recommendation only — nothing destructive is
    performed automatically (§26.9: "when safe ... otherwise escalate")."""
    if apply_succeeded:
        return {"action": "none", "detail": "Apply succeeded; no recovery required"}
    if verdict.already_applied:
        return {
            "action": "verify",
            "detail": "The change is present in AWS despite the failure; verify rather than retry",
            "escalate": True,
        }
    if verdict.safe_to_retry:
        return {
            "action": "retry",
            "detail": "No changes landed; the mutation can be retried safely",
            "escalate": False,
        }
    return {
        "action": "escalate",
        "detail": (
            "AWS state could not be determined after a failed apply. "
            "A state snapshot is available for manual recovery."
            if snapshot.available
            else "AWS state could not be determined and no state snapshot is available."
        ),
        "snapshot": snapshot.as_dict(),
        "escalate": True,
    }
