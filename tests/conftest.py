"""Shared pytest fixtures: moto-backed AWS, in-memory SQLite, detector context."""

from __future__ import annotations

import os
from collections.abc import Iterator

import boto3
import pytest

# Force a fake region BEFORE moto is imported so boto3 sessions resolve cleanly.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")

from moto import mock_aws
from src.chandra.aws.client_factory import AwsClientFactory
from src.chandra.tools.base import DetectorContext


@pytest.fixture
def aws() -> Iterator[None]:
    """Activate moto's ``mock_aws`` backend for the duration of the test."""
    with mock_aws():
        yield


@pytest.fixture
def factory(aws: None) -> AwsClientFactory:
    return AwsClientFactory(default_region="us-east-1")


@pytest.fixture
def account_id(factory: AwsClientFactory) -> str:
    sts = factory.client("sts")
    return str(sts.get_caller_identity()["Account"])


@pytest.fixture
def ctx(factory: AwsClientFactory, account_id: str) -> DetectorContext:
    return DetectorContext(
        run_id="test-run",
        account_id=account_id,
        regions=["us-east-1"],
        factory=factory,
    )


@pytest.fixture
def s3(aws: None) -> object:
    return boto3.client("s3", region_name="us-east-1")


@pytest.fixture
def ec2(aws: None) -> object:
    return boto3.client("ec2", region_name="us-east-1")


@pytest.fixture
def iam(aws: None) -> object:
    return boto3.client("iam", region_name="us-east-1")


@pytest.fixture
def rds(aws: None) -> object:
    return boto3.client("rds", region_name="us-east-1")


@pytest.fixture
def cloudtrail(aws: None) -> object:
    return boto3.client("cloudtrail", region_name="us-east-1")


@pytest.fixture
def cloudwatch(aws: None) -> object:
    return boto3.client("cloudwatch", region_name="us-east-1")


@pytest.fixture
def events(aws: None) -> object:
    return boto3.client("events", region_name="us-east-1")


# ---------------------------------------------------------------------------
# Catalogue seeding for SQLite-backed graph tests (Postgres is the system of
# record for permission sets since Phase 1; tests must seed what they rely on).
# ---------------------------------------------------------------------------
import json as _json
from pathlib import Path as _Path

_REPO_ROOT = _Path(__file__).resolve().parents[1]


def load_catalog_seed(name: str) -> object:
    """Load a catalogue seed file (``permission_sets.json`` / ``aws_tasks.json``)."""
    candidates = [
        _REPO_ROOT / "src" / "chandra" / "catalog" / "seeds" / name,
        _REPO_ROOT / {"permission_sets.json": "aws_permissions.json"}.get(name, name),
    ]
    for path in candidates:
        if path.exists():
            return _json.loads(path.read_text(encoding="utf-8"))
    raise FileNotFoundError(name)


def seed_permission_sets(session_factory: object, tenant_id: str = "default") -> None:
    from src.chandra.catalog import ConfigRepository

    repo = ConfigRepository(tenant_id=tenant_id, session_factory=session_factory)  # type: ignore[arg-type]
    repo.replace_permission_sets(load_catalog_seed("permission_sets.json"))  # type: ignore[arg-type]
