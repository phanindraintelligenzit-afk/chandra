"""Shared infrastructure for KRA detector modules.

Every detector receives a :class:`DetectorContext` so it can:

* Reach AWS through the cached :class:`~chandra.aws.client_factory.AwsClientFactory`.
* Iterate ``ctx.regions`` instead of guessing.
* Append structured error records to ``ctx.errors`` when boto3 raises
  ``ClientError`` (the master prompt forbids silent ``except`` blocks).

Detector functions MUST:

* Use boto3 paginators on every list/describe call.
* Catch :class:`botocore.exceptions.ClientError` per region and per resource,
  append to ``ctx.errors``, and continue — they MUST NOT raise out.
* Be pure of LLM calls. Tools are the trust layer.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from src.chandra.aws.client_factory import AwsClientFactory, get_default_factory
from src.chandra.briefing.schemas import Finding
from structlog import get_logger

logger = get_logger(__name__)


@dataclass
class DetectorContext:
    """Per-run state handed to every detector."""

    run_id: str
    account_id: str
    regions: list[str]
    factory: AwsClientFactory = field(default_factory=get_default_factory)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def record_error(
        self,
        *,
        detector_id: str,
        region: str | None,
        error: BaseException,
        resource_arn: str | None = None,
    ) -> None:
        """Append a structured error record. Detectors continue after this."""
        record: dict[str, Any] = {
            "run_id": self.run_id,
            "detector_id": detector_id,
            "region": region,
            "resource_arn": resource_arn,
            "error_type": type(error).__name__,
            "error_message": str(error),
        }
        if isinstance(error, ClientError):
            record["aws_error_code"] = error.response.get("Error", {}).get("Code")
        self.errors.append(record)
        logger.warning("detector.error", **record)


@contextmanager
def detector_guard(
    ctx: DetectorContext,
    *,
    detector_id: str,
    region: str | None = None,
    resource_arn: str | None = None,
) -> Iterator[None]:
    """Context manager: swallow AWS errors into ``ctx.errors`` and continue."""
    try:
        yield
    except (ClientError, BotoCoreError) as exc:
        ctx.record_error(
            detector_id=detector_id,
            region=region,
            error=exc,
            resource_arn=resource_arn,
        )


def paginate(client: Any, operation: str, **kwargs: Any) -> Iterator[dict[str, Any]]:
    """Yield every page from a boto3 paginator.

    Raises if the operation does not support pagination so the developer fixes
    the detector rather than silently dropping data.
    """
    paginator = client.get_paginator(operation)
    yield from paginator.paginate(**kwargs)


DetectorFn = Callable[[DetectorContext], list[Finding]]
