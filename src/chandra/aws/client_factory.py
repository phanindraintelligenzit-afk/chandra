"""Single point of construction for boto3 clients and resources.

Every detector in :mod:`chandra.tools` MUST go through :class:`AwsClientFactory`
rather than calling ``boto3.client`` directly. This guarantees:

* Consistent adaptive retry / max-attempts config (botocore ``Config``).
* Profile + region resolution from a single source (:mod:`chandra.config`).
* Thread-safe per-(service, region) caching to avoid client churn during fanout.
"""

from __future__ import annotations

import threading
from typing import Any

import boto3
from botocore.client import BaseClient
from botocore.config import Config

from chandra.config import settings


class AwsClientFactory:
    """Cached boto3 client factory with retry/backoff baked in."""
    def assume_role(
    self,
    role_arn: str,
    session_name: str,
    duration_s: int = 3600
) -> "AwsClientFactory":

    cache_key = (role_arn, session_name)

    now = time.time()

    cached = _ASSUME_ROLE_CACHE.get(cache_key)

    if cached and cached["expiry"] > now:
        return cached["factory"]

    sts = self.client("sts")

    resp = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName=session_name,
        DurationSeconds=duration_s
    )

    c = resp["Credentials"]

    new = AwsClientFactory(
        profile=None,
        default_region=self._default_region,
        max_attempts=self._max_attempts,
        retry_mode=self._retry_mode
    )

    new._session = boto3.session.Session(
        aws_access_key_id=c["AccessKeyId"],
        aws_secret_access_key=c["SecretAccessKey"],
        aws_session_token=c["SessionToken"],
        region_name=self._default_region,
    )

    _ASSUME_ROLE_CACHE[cache_key] = {
        "factory": new,
        "expiry": now + duration_s - 60
    }

    return new

    def __init__(
        self,
        profile: str | None = None,
        default_region: str | None = None,
        max_attempts: int | None = None,
        retry_mode: str | None = None,
        fast: bool = False,
    ) -> None:
        self._profile = profile or settings.aws_profile
        self._default_region = default_region or settings.aws_default_region
        self._fast = fast

        if fast:
            self._max_attempts = 1
            self._retry_mode = "legacy"
        else:
            self._max_attempts = max_attempts or settings.boto_max_attempts
            self._retry_mode = retry_mode or settings.boto_retry_mode

        self._lock = threading.Lock()
        self._session: boto3.session.Session | None = None
        self._clients: dict[tuple[str, str], BaseClient] = {}

    @property
    def session(self) -> boto3.session.Session:
        """Lazily build and cache a single :class:`boto3.session.Session`."""
        if self._session is None:
            with self._lock:
                if self._session is None:
                    if self._profile:
                        self._session = boto3.session.Session(
                            profile_name=self._profile,
                            region_name=self._default_region,
                        )
                    else:
                        self._session = boto3.session.Session(
                            region_name=self._default_region,
                        )
        return self._session

    def _boto_config(self) -> Config:
        if self._fast:
            return Config(user_agent_extra="chandra/0.1")

        return Config(
            retries={"max_attempts": self._max_attempts, "mode": self._retry_mode},
            user_agent_extra="chandra/0.1",
        )

    def client(self, service: str, region: str | None = None) -> Any:
        """Return a cached boto3 client for ``(service, region)``."""
        region = region or self._default_region
        key = (service, region)
        client = self._clients.get(key)
        if client is None:
            with self._lock:
                client = self._clients.get(key)
                if client is None:
                    client = boto3.client(
                        service_name=service,
                        region_name=region,
                        config=self._boto_config(),
                    )
                    self._clients[key] = client
        return client

    def account_id(self) -> str:
        """Resolve the current AWS account id via STS."""
        sts = self.client("sts", region=self._default_region)
        identity = sts.get_caller_identity()
        return str(identity["Account"])


_default_factory: AwsClientFactory | None = None
_default_lock = threading.Lock()


def get_default_factory() -> AwsClientFactory:
    """Return the process-wide default :class:`AwsClientFactory`."""
    global _default_factory
    if _default_factory is None:
        with _default_lock:
            if _default_factory is None:
                _default_factory = AwsClientFactory()
    return _default_factory