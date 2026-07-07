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
