"""Unit tests for the refactored ``action_executor_node``.

Covers:
- empty ``auto_fixed`` -> empty ``action_results``
- unknown detector id  -> ``ActionResult(status="skipped")``
- malformed ARN        -> ``ActionResult(status="failure")``
- SEC-001-public-s3 in dry-run and real modes (uses moto)
- SEC-002-open-sg and SEC-003-stale-key (skeleton, dispatch + ARN parse only)
- mixed list ordering preserved
- ARN parser helpers in isolation
- ``@traced_node`` does not break the node
"""

from __future__ import annotations

import pytest
from src.chandra.briefing.schemas import ProposedWrite
from src.chandra.graphs.action_nodes import action_executor_node
from src.chandra.graphs.action_nodes.action_executor import (
    _iam_key_id_from_arn,
    _s3_bucket_from_arn,
    _sg_id_from_arn,
)
from src.chandra.graphs.state import ChandraState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(
    detector_id: str,
    target_arn: str,
    region: str = "us-east-1",
) -> ProposedWrite:
    """Build a ProposedWrite the way decision_router does."""
    return ProposedWrite(
        action=f"remediate_{detector_id}",
        target_arn=target_arn,
        region=region,
        payload={"recommendation": "fix it"},
        requested_by="decision_router",
        justification="auto",
        risk_level="low",
    )


def _state(auto_fixed: list[ProposedWrite] | None = None, **extra: object) -> ChandraState:
    state: ChandraState = {
        "run_id": "test-run-1",
        "account_id": "123456789012",
    }
    if auto_fixed is not None:
        state["auto_fixed"] = auto_fixed
    state.update(extra)  # type: ignore[typeddict-unknown-key]
    return state


# ---------------------------------------------------------------------------
# Empty / skipped paths
# ---------------------------------------------------------------------------


def test_empty_auto_fixed_returns_empty_results() -> None:
    result = action_executor_node(_state(auto_fixed=[]))
    assert result == {"action_results": []}


def test_missing_auto_fixed_key_treated_as_empty() -> None:
    result = action_executor_node(_state())
    assert result == {"action_results": []}


def test_unknown_detector_emits_skipped() -> None:
    """A detector with no registered handler -> ActionResult(status='skipped')."""
    write = _write("COST-001-idle-ec2", "arn:aws:ec2:us-east-1:123:i-abc")

    result = action_executor_node(_state(auto_fixed=[write]))
    assert len(result["action_results"]) == 1
    r = result["action_results"][0]
    assert r.status == "skipped"
    assert r.action == "remediate_COST-001-idle-ec2"
    assert r.dry_run is True
    assert "COST-001-idle-ec2" in r.message


def test_malformed_arn_emits_failure() -> None:
    """SEC-001 handler is registered, but a broken ARN -> failure (not skipped)."""
    # arn:aws:s3:::  -> parts[5] is empty -> ValueError in the parser
    write = _write("SEC-001-public-s3", "arn:aws:s3:::")

    result = action_executor_node(_state(auto_fixed=[write]))
    assert len(result["action_results"]) == 1
    r = result["action_results"][0]
    assert r.status == "failure"
    assert r.error is not None
    assert "Invalid S3 ARN" in r.error


# ---------------------------------------------------------------------------
# SEC-001 public-s3 (dry-run vs real run, moto)
# ---------------------------------------------------------------------------


def test_sec_001_public_s3_dry_run(s3: object) -> None:
    """Dry-run must NOT change the bucket ACL."""
    bucket = "test-bucket-dry"
    s3.create_bucket(Bucket=bucket)
    # Moto doesn't enforce ACLs the same way as real S3, but the executor
    # records a dry-run result and skips put_bucket_acl.
    write = _write("SEC-001-public-s3", f"arn:aws:s3:::{bucket}")

    result = action_executor_node(_state(auto_fixed=[write], dry_run=True))
    assert len(result["action_results"]) == 1
    r = result["action_results"][0]
    assert r.status == "dry_run"
    assert r.dry_run is True
    assert "DRY RUN" in r.message
    assert r.audit_log is not None
    assert bucket in r.audit_log


def test_sec_001_public_s3_real_run(s3: object) -> None:
    """Real run: executor calls put_bucket_acl and reports success."""
    bucket = "test-bucket-real"
    s3.create_bucket(Bucket=bucket)
    write = _write("SEC-001-public-s3", f"arn:aws:s3:::{bucket}")

    result = action_executor_node(_state(auto_fixed=[write], dry_run=False))
    assert len(result["action_results"]) == 1
    r = result["action_results"][0]
    # moto accepts put_bucket_acl(Bucket, ACL='private') without error.
    assert r.status == "success"
    assert r.dry_run is False
    assert r.audit_log is not None
    assert "SUCCESS" in r.audit_log
    assert r.error is None


# ---------------------------------------------------------------------------
# SEC-002 / SEC-003 dispatch (skeleton coverage)
# ---------------------------------------------------------------------------


def test_sec_002_open_sg_dispatches_through_registry(ec2: object) -> None:
    """Smoke: a valid SG ARN routes through the registry to _fix_open_sg."""
    # moto: create a VPC + SG so revoke_security_group_ingress has a target.
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")
    vpc_id = vpc["Vpc"]["VpcId"]
    sg = ec2.create_security_group(GroupName="sg-test", Description="test", VpcId=vpc_id)
    sg_id = sg["GroupId"]
    sg_arn = f"arn:aws:ec2:us-east-1:123:security-group/{sg_id}"

    write = _write("SEC-002-open-sg-ssh", sg_arn)
    result = action_executor_node(_state(auto_fixed=[write], dry_run=True))
    r = result["action_results"][0]
    assert r.status == "dry_run"
    assert r.region == "us-east-1"


def test_sec_003_stale_iam_key_dispatches_through_registry(iam: object) -> None:
    """Smoke: a valid IAM key ARN routes through the registry to _disable_iam_key."""
    user = iam.create_user(UserName="alice")
    key = iam.create_access_key(UserName=user["User"]["UserName"])
    key_id = key["AccessKey"]["AccessKeyId"]
    key_arn = f"arn:aws:iam::123:key/{key_id}"

    write = _write("SEC-003-stale-key", key_arn)
    result = action_executor_node(_state(auto_fixed=[write], dry_run=True))
    r = result["action_results"][0]
    assert r.status == "dry_run"


# ---------------------------------------------------------------------------
# Mixed list — ordering preserved
# ---------------------------------------------------------------------------


def test_mixed_writes_continue_on_failure() -> None:
    """One skipped + one failure + one real = 3 results, in input order."""
    writes = [
        _write("COST-001-idle-ec2", "arn:aws:ec2:us-east-1:123:i-1"),  # skipped
        _write("SEC-001-public-s3", "arn:aws:s3:::"),  # failure (bad ARN)
        _write("SEC-001-public-s3", "arn:aws:s3:::ok-bucket", region="us-west-2"),
    ]

    result = action_executor_node(_state(auto_fixed=writes, dry_run=True))
    results = result["action_results"]
    assert len(results) == 3
    assert results[0].status == "skipped"
    assert results[1].status == "failure"
    assert results[2].status == "dry_run"
    # Order matches input — important for audit log determinism.
    assert [r.target_arn for r in results] == [
        "arn:aws:ec2:us-east-1:123:i-1",
        "arn:aws:s3:::",
        "arn:aws:s3:::ok-bucket",
    ]


# ---------------------------------------------------------------------------
# ARN parser helpers — direct unit tests
# ---------------------------------------------------------------------------


class TestArnParsers:
    def test_s3_bucket(self) -> None:
        assert _s3_bucket_from_arn("arn:aws:s3:::my-bucket") == "my-bucket"

    def test_s3_bucket_with_dots_and_dashes(self) -> None:
        assert _s3_bucket_from_arn("arn:aws:s3:::logs.prod-2024") == "logs.prod-2024"

    def test_s3_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid S3 ARN"):
            _s3_bucket_from_arn("arn:aws:s3:::")

    def test_s3_too_few_parts_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid S3 ARN"):
            _s3_bucket_from_arn("arn:aws:s3")

    def test_sg_id(self) -> None:
        assert _sg_id_from_arn("arn:aws:ec2:us-east-1:123:security-group/sg-abc123") == "sg-abc123"

    def test_sg_id_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid security-group ARN"):
            _sg_id_from_arn("arn:aws:ec2:us-east-1:123:security-group")

    def test_iam_key(self) -> None:
        assert _iam_key_id_from_arn("arn:aws:iam::123:key/ABCDEFGHIJKL") == "ABCDEFGHIJKL"

    def test_iam_key_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid IAM key ARN"):
            _iam_key_id_from_arn("arn:aws:iam::123:key")


# ---------------------------------------------------------------------------
# dry_run default and explicit
# ---------------------------------------------------------------------------


def test_dry_run_defaults_to_true(s3: object) -> None:
    """If state doesn't carry dry_run, the node defaults to dry-run mode."""
    s3.create_bucket(Bucket="default-dry")
    write = _write("SEC-001-public-s3", "arn:aws:s3:::default-dry")

    # NB: no dry_run in state
    state = _state(auto_fixed=[write])
    result = action_executor_node(state)
    r = result["action_results"][0]
    assert r.status == "dry_run"
    assert r.dry_run is True
