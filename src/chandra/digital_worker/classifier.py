"""Deterministic request understanding: category, platform, services, priority.

No LLM here by design — classification must be reproducible and free, so
the same request always routes the same way. The composer-backed nodes
downstream add LLM value where it belongs (root cause and planning).
"""

from __future__ import annotations

import re

from src.chandra.digital_worker.schemas import (
    CloudPlatform,
    CloudRequest,
    RequestCategory,
    RequestClassification,
    RequestPriority,
    RequestSource,
)
from src.chandra.logging import get_logger

logger = get_logger(__name__)

_WORD = re.compile(r"[a-z0-9][a-z0-9\-_./]*")

# Category keyword tables — first match wins in declaration order, so put
# the most specific categories before the catch-alls.
_CATEGORY_KEYWORDS: tuple[tuple[RequestCategory, frozenset[str]], ...] = (
    (
        RequestCategory.INCIDENT,
        frozenset(
            {
                "outage", "down", "unavailable", "5xx", "error-rate", "errors",
                "crash", "failing", "failure", "failed", "incident", "alarm",
                "alert", "timeout", "unreachable", "degraded", "sev1", "sev2",
            }
        ),
    ),
    (
        RequestCategory.SECURITY,
        frozenset(
            {
                "security", "vulnerability", "cve", "breach", "exposed", "public",
                "iam", "credentials", "key-rotation", "guardduty", "encryption",
                "unencrypted", "mfa", "phishing", "malware", "unauthorized",
            }
        ),
    ),
    (
        RequestCategory.COMPLIANCE,
        frozenset(
            {
                "compliance", "audit", "hipaa", "gdpr", "pci", "soc2", "cis",
                "config-rule", "governance", "policy-violation", "tagging",
            }
        ),
    ),
    (
        RequestCategory.COST_OPTIMIZATION,
        frozenset(
            {
                "cost", "billing", "spend", "budget", "savings", "rightsizing",
                "rightsize", "idle", "unused", "overprovisioned", "reserved",
            }
        ),
    ),
    (
        RequestCategory.PERFORMANCE,
        frozenset(
            {
                "slow", "latency", "performance", "throughput", "cpu", "memory",
                "saturation", "throttling", "throttled", "p99", "p95", "iops",
            }
        ),
    ),
    (
        RequestCategory.RELIABILITY,
        frozenset(
            {
                "reliability", "backup", "snapshot", "failover", "redundancy",
                "multi-az", "disaster-recovery", "dr", "replication", "restore",
            }
        ),
    ),
    (
        RequestCategory.CHANGE_REQUEST,
        frozenset(
            {
                "deploy", "provision", "create", "upgrade", "migrate", "resize",
                "scale", "terraform", "rollout", "release", "change", "update",
                "configure", "enable", "disable", "delete", "remove", "install",
            }
        ),
    ),
    (
        RequestCategory.QUESTION,
        frozenset({"how", "what", "why", "question", "help", "explain", "clarify"}),
    ),
)

# Service vocabulary → owning platform. Keys are the tokens matched in text.
_SERVICE_PLATFORM: dict[str, CloudPlatform] = {
    # AWS
    "ec2": CloudPlatform.AWS, "s3": CloudPlatform.AWS, "rds": CloudPlatform.AWS,
    "lambda": CloudPlatform.AWS, "iam": CloudPlatform.AWS, "vpc": CloudPlatform.AWS,
    "cloudwatch": CloudPlatform.AWS, "cloudtrail": CloudPlatform.AWS,
    "dynamodb": CloudPlatform.AWS, "eks": CloudPlatform.AWS, "ecs": CloudPlatform.AWS,
    "elb": CloudPlatform.AWS, "alb": CloudPlatform.AWS, "sqs": CloudPlatform.AWS,
    "sns": CloudPlatform.AWS, "ebs": CloudPlatform.AWS, "efs": CloudPlatform.AWS,
    "route53": CloudPlatform.AWS, "cloudfront": CloudPlatform.AWS,
    "guardduty": CloudPlatform.AWS, "redshift": CloudPlatform.AWS,
    "elasticache": CloudPlatform.AWS, "kms": CloudPlatform.AWS,
    # Azure
    "aks": CloudPlatform.AZURE, "cosmosdb": CloudPlatform.AZURE,
    "app-service": CloudPlatform.AZURE, "blob": CloudPlatform.AZURE,
    "azure-functions": CloudPlatform.AZURE, "vnet": CloudPlatform.AZURE,
    "azure-sql": CloudPlatform.AZURE, "keyvault": CloudPlatform.AZURE,
    # GCP
    "gke": CloudPlatform.GCP, "bigquery": CloudPlatform.GCP,
    "cloud-run": CloudPlatform.GCP, "gcs": CloudPlatform.GCP,
    "cloud-functions": CloudPlatform.GCP, "cloud-sql": CloudPlatform.GCP,
    "pubsub": CloudPlatform.GCP,
    # Kubernetes
    "kubernetes": CloudPlatform.KUBERNETES, "k8s": CloudPlatform.KUBERNETES,
    "pod": CloudPlatform.KUBERNETES, "pods": CloudPlatform.KUBERNETES,
    "deployment": CloudPlatform.KUBERNETES, "helm": CloudPlatform.KUBERNETES,
    "kubectl": CloudPlatform.KUBERNETES, "namespace": CloudPlatform.KUBERNETES,
    "ingress": CloudPlatform.KUBERNETES, "statefulset": CloudPlatform.KUBERNETES,
}

_PLATFORM_HINTS: dict[CloudPlatform, frozenset[str]] = {
    CloudPlatform.AWS: frozenset({"aws", "amazon"}),
    CloudPlatform.AZURE: frozenset({"azure", "microsoft-cloud", "entra"}),
    CloudPlatform.GCP: frozenset({"gcp", "google-cloud", "gcloud"}),
    CloudPlatform.KUBERNETES: frozenset({"cluster", "container", "containers"}),
}

# Sources that unambiguously imply a platform.
_SOURCE_PLATFORM: dict[RequestSource, CloudPlatform] = {
    RequestSource.CLOUDWATCH: CloudPlatform.AWS,
    RequestSource.AZURE_MONITOR: CloudPlatform.AZURE,
    RequestSource.GCP_MONITORING: CloudPlatform.GCP,
}

_P1_KEYWORDS = frozenset(
    {"outage", "down", "sev1", "critical", "urgent", "production", "prod", "breach", "data-loss"}
)
_P2_KEYWORDS = frozenset({"sev2", "high", "degraded", "failing", "major", "security"})


def _tokenize(request: CloudRequest) -> list[str]:
    text = " ".join([request.title, request.description, " ".join(request.labels)]).lower()
    return _WORD.findall(text)


def classify_request(request: CloudRequest) -> RequestClassification:
    """Classify a normalized request. Pure function of its inputs."""
    tokens = _tokenize(request)
    token_set = set(tokens)

    category = RequestCategory.UNKNOWN
    matched_keywords: list[str] = []
    for candidate, vocabulary in _CATEGORY_KEYWORDS:
        hits = sorted(token_set & vocabulary)
        if hits:
            category = candidate
            matched_keywords = hits
            break

    services = sorted({token for token in token_set if token in _SERVICE_PLATFORM})
    platform = identify_platform(request, services, token_set)

    priority = request.priority or _infer_priority(category, token_set)
    confidence = min(1.0, 0.3 + 0.15 * len(matched_keywords) + (0.2 if services else 0.0))

    classification = RequestClassification(
        category=category,
        platform=platform,
        services=services,
        keywords=matched_keywords,
        priority=priority,
        summary=request.title[:200],
        confidence=round(confidence, 2),
    )
    logger.info(
        "classifier.request_classified",
        request_id=request.request_id,
        category=category.value,
        platform=platform.value,
        services=services,
        priority=priority.value,
        confidence=classification.confidence,
    )
    return classification


def identify_platform(
    request: CloudRequest,
    services: list[str] | None = None,
    token_set: set[str] | None = None,
) -> CloudPlatform:
    """Identify the target cloud platform (AWS / Azure / GCP / Kubernetes)."""
    if request.source in _SOURCE_PLATFORM:
        return _SOURCE_PLATFORM[request.source]

    if token_set is None:
        token_set = set(_tokenize(request))
    if services is None:
        services = sorted({token for token in token_set if token in _SERVICE_PLATFORM})

    votes: dict[CloudPlatform, int] = {}
    for service in services:
        owner = _SERVICE_PLATFORM[service]
        votes[owner] = votes.get(owner, 0) + 2
    for platform, hints in _PLATFORM_HINTS.items():
        overlap = len(token_set & hints)
        if overlap:
            votes[platform] = votes.get(platform, 0) + overlap

    if not votes:
        return CloudPlatform.UNKNOWN
    # Deterministic tie-break: highest votes, then enum declaration order.
    order = list(CloudPlatform)
    return max(votes, key=lambda p: (votes[p], -order.index(p)))


def _infer_priority(category: RequestCategory, token_set: set[str]) -> RequestPriority:
    if token_set & _P1_KEYWORDS:
        return RequestPriority.P1
    if category in (RequestCategory.INCIDENT, RequestCategory.SECURITY) or token_set & _P2_KEYWORDS:
        return RequestPriority.P2
    return RequestPriority.P3
