"""Shared types and helpers for observability KRA detector modules."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, TypeVar

from botocore.exceptions import BotoCoreError, ClientError

_T = TypeVar("_T")


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


def run_per_region(
    ctx: "DetectorContext",
    fn: Callable[["DetectorContext", str], list[_T]],
    *,
    max_workers: int = 10,
) -> list[_T]:
    """Run ``fn(ctx, region)`` concurrently for every region in ``ctx.regions``.

    Replaces the common ``for region in ctx.regions`` loop inside detectors.
    Errors from individual regions are recorded in ``ctx.errors`` and skipped
    so all other regions still complete.
    """
    results: list[_T] = []
    workers = min(len(ctx.regions), max_workers)
    if workers == 0:
        return results

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_region = {
            pool.submit(fn, ctx, region): region for region in ctx.regions
        }
        for future in as_completed(future_to_region):
            region = future_to_region[future]
            try:
                results.extend(future.result())
            except Exception as exc:
                ctx.record_error(
                    detector_id="run_per_region",
                    region=region,
                    error=exc,
                )
    return results
