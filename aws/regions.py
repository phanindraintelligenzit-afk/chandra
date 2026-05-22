"""Region discovery. Never hardcode regions — call :func:`active_regions`."""

from __future__ import annotations

from functools import lru_cache

from aws.client_factory import AwsClientFactory, get_default_factory
from logging import get_logger

logger = get_logger(__name__)


def active_regions(
    account_id: str,
    factory: AwsClientFactory | None = None,
) -> list[str]:
    """Return the list of regions the account has opted in to.

    Uses EC2 ``describe_regions`` filtered to ``opt-in-not-required`` and
    ``opted-in``. The ``account_id`` is accepted for log correlation and to
    keep the call site honest; the underlying API call is per-credential.
    """
    factory = factory or get_default_factory()
    return _active_regions_cached(account_id, id(factory))


@lru_cache(maxsize=8)
def _active_regions_cached(account_id: str, _factory_key: int) -> list[str]:
    factory = get_default_factory()
    ec2 = factory.client("ec2")
    resp = ec2.describe_regions(
        AllRegions=False,
        Filters=[
            {"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]},
        ],
    )
    regions = sorted(r["RegionName"] for r in resp.get("Regions", []))
    logger.info(
        "regions.discovered",
        account_id=account_id,
        count=len(regions),
        regions=regions,
    )
    return regions
