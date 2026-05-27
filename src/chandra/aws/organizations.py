"""AWS Organizations support for multi-account fan-out."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import boto3
from chandra.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class Account:
    """Represents an AWS account in an Organization."""

    id: str
    arn: str
    name: str
    email: str
    status: str


def list_accounts_in_ou(ou_id: str) -> Iterator[Account]:
    """
    List all ACTIVE accounts in an Organizational Unit.

    Args:
        ou_id: Organization Unit ID (e.g., "ou-xxxx-yyyyyyyy")

    Yields:
        Account objects for each active account in the OU
    """
    org = boto3.client("organizations")

    paginator = org.get_paginator("list_accounts_for_parent")

    logger.info("organizations.list_accounts_in_ou", ou_id=ou_id)

    for page in paginator.paginate(ParentId=ou_id):

        for account in page["Accounts"]:

            if account["Status"] != "ACTIVE":
                logger.info(
                    "organizations.skip_inactive_account",
                    account_id=account["Id"],
                    status=account["Status"],
                )
                continue

            yield Account(
                id=account["Id"],
                arn=account["Arn"],
                name=account["Name"],
                email=account["Email"],
                status=account["Status"],
            )
