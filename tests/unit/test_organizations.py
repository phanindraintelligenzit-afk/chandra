"""Tests for AWS Organizations support."""

import pytest
from src.chandra.aws.organizations import Account


def test_account_dataclass_creation():
    """Test that Account can be created."""

    account = Account(
        id="123456789012",
        arn="arn:aws:organizations::123456789012:account/o-abc/123456789012",
        name="Test Account",
        email="test@example.com",
        status="ACTIVE",
    )

    assert account.id == "123456789012"
    assert account.name == "Test Account"
    assert account.status == "ACTIVE"


def test_account_dataclass_frozen():
    """Test that Account is immutable."""

    account = Account(
        id="123456789012",
        arn="arn:aws:organizations::123456789012:account/o-abc/123456789012",
        name="Test Account",
        email="test@example.com",
        status="ACTIVE",
    )

    with pytest.raises(AttributeError):
        account.id = "999999999999"


def test_account_fields():
    """Test Account fields."""

    account = Account(
        id="111111111111",
        arn="arn:aws:organizations::111111111111:account/o-abc/111111111111",
        name="Production",
        email="prod@company.com",
        status="ACTIVE",
    )

    assert account.id == "111111111111"
    assert account.name == "Production"
    assert account.email == "prod@company.com"
    assert account.status == "ACTIVE"
