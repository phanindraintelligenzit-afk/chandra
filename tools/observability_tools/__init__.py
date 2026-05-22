"""Shared types and helpers for observability KRA detector modules."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError


@dataclass
class DetectorContext:
    """Per-run state handed to every detector."""

    account_id: str
    regions: list[str]
    factory: Any
    errors: list[dict[str, Any]] = field(default_factory=list)

    def record_error(
        self,
        *,
        detector_id: str,
        region: str | None,
        error: BaseException,
        resource_arn: str | None = None,
    ) -> None:
        self.errors.append({
            "detector_id": detector_id,
            "region": region,
            "resource_arn": resource_arn,
            "error_type": type(error).__name__,
            "error_message": str(error),
        })


@contextmanager
def detector_guard(
    ctx: DetectorContext,
    *,
    detector_id: str,
    region: str | None = None,
    resource_arn: str | None = None,
) -> Iterator[None]:
    """Swallow AWS errors into ctx.errors and continue."""
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
    """Yield every page from a boto3 paginator."""
    paginator = client.get_paginator(operation)
    yield from paginator.paginate(**kwargs)
