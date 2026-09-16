"""Edge hardening: JWT RS256 auth, rate limiting, live job state (PRD L2 stage 2)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from src.chandra.api.websockets import WebSocketManager
from src.chandra.config import settings
from src.chandra.security import (
    AuthConfigError,
    AuthError,
    decode_token,
    validate_auth_configuration,
)
from src.chandra.security.auth import bearer_token
from src.chandra.security.ratelimit import RateLimiter


@pytest.fixture(scope="module")
def keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def _token(private_pem: str, **claims: Any) -> str:
    payload = {
        "sub": "priya",
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        **claims,
    }
    return jwt.encode(payload, private_pem, algorithm="RS256")


@pytest.fixture(autouse=True)
def _no_issuer_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_issuer", "", raising=False)
    monkeypatch.setattr(settings, "jwt_audience", "", raising=False)


class TestTokenVerification:
    def test_valid_token_yields_identity(self, keypair: tuple[str, str]) -> None:
        private, public = keypair
        identity = decode_token(_token(private, tenant_id="acme"), public_key=public)
        assert identity.principal_id == "priya"
        assert identity.tenant_id == "acme"

    def test_tenant_defaults_when_claim_absent(self, keypair: tuple[str, str]) -> None:
        private, public = keypair
        assert decode_token(_token(private), public_key=public).tenant_id == "default"

    def test_expired_token_is_rejected(self, keypair: tuple[str, str]) -> None:
        private, public = keypair
        stale = _token(private, exp=datetime.now(UTC) - timedelta(seconds=1))
        with pytest.raises(AuthError):
            decode_token(stale, public_key=public)

    def test_token_without_expiry_is_rejected(self, keypair: tuple[str, str]) -> None:
        """A token that never expires is a permanent credential."""
        private, public = keypair
        forever = jwt.encode({"sub": "priya"}, private, algorithm="RS256")
        with pytest.raises(AuthError):
            decode_token(forever, public_key=public)

    def test_alg_none_is_rejected(self, keypair: tuple[str, str]) -> None:
        """The unsigned-token attack: 'alg': 'none' must never verify."""
        _, public = keypair
        unsigned = jwt.encode(
            {"sub": "attacker", "exp": datetime.now(UTC) + timedelta(minutes=5)},
            key="",
            algorithm="none",
        )
        with pytest.raises(AuthError):
            decode_token(unsigned, public_key=public)

    def test_hs256_signed_with_the_public_key_is_rejected(self, keypair: tuple[str, str]) -> None:
        """The algorithm-confusion attack: an attacker who knows the public key
        (it is public) signs HS256 using it as the HMAC secret. Only a verifier
        that trusts the token's own alg header falls for it."""
        _, public = keypair
        # PyJWT refuses to *encode* this, so the attacker's token is built by
        # hand — which is exactly what an attacker would do.
        import base64
        import hashlib
        import hmac
        import json

        def b64(raw: bytes) -> bytes:
            return base64.urlsafe_b64encode(raw).rstrip(b"=")

        header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        body = b64(
            json.dumps(
                {
                    "sub": "attacker",
                    "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
                }
            ).encode()
        )
        signing_input = header + b"." + body
        signature = b64(hmac.new(public.encode(), signing_input, hashlib.sha256).digest())
        forged = (signing_input + b"." + signature).decode()
        with pytest.raises(AuthError):
            decode_token(forged, public_key=public)

    def test_token_signed_by_a_different_key_is_rejected(self, keypair: tuple[str, str]) -> None:
        _, public = keypair
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_pem = other.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        with pytest.raises(AuthError):
            decode_token(_token(other_pem), public_key=public)

    def test_garbage_is_rejected(self, keypair: tuple[str, str]) -> None:
        _, public = keypair
        for value in ("", "not-a-token", "a.b.c"):
            with pytest.raises(AuthError):
                decode_token(value, public_key=public)

    def test_token_without_principal_claim_is_rejected(self, keypair: tuple[str, str]) -> None:
        private, public = keypair
        anonymous = jwt.encode(
            {"exp": datetime.now(UTC) + timedelta(minutes=5)}, private, algorithm="RS256"
        )
        with pytest.raises(AuthError):
            decode_token(anonymous, public_key=public)

    def test_issuer_and_audience_are_verified_when_configured(
        self, keypair: tuple[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        private, public = keypair
        monkeypatch.setattr(settings, "jwt_issuer", "https://idp.example", raising=False)
        monkeypatch.setattr(settings, "jwt_audience", "chandra", raising=False)

        good = _token(private, iss="https://idp.example", aud="chandra")
        assert decode_token(good, public_key=public).principal_id == "priya"

        for bad in (
            _token(private, iss="https://evil.example", aud="chandra"),
            _token(private, iss="https://idp.example", aud="other-service"),
        ):
            with pytest.raises(AuthError):
                decode_token(bad, public_key=public)

    def test_claimed_roles_are_extracted_but_are_only_hints(self, keypair: tuple[str, str]) -> None:
        """Roles in a token are recorded, never authoritative: capability grants
        come from principal_roles, so an IdP cannot grant itself Chandra rights."""
        private, public = keypair
        identity = decode_token(_token(private, roles=["agent_spinner"]), public_key=public)
        assert identity.claimed_roles == ["agent_spinner"]
        assert not hasattr(identity, "capabilities")


class TestAuthConfiguration:
    def test_enabled_without_a_key_fails_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """'I turned auth on' must never quietly mean 'auth is off'."""
        monkeypatch.setattr(settings, "auth_required", True, raising=False)
        monkeypatch.setattr(settings, "jwt_public_key", "", raising=False)
        with pytest.raises(AuthConfigError):
            validate_auth_configuration()

    def test_disabled_configuration_is_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "auth_required", False, raising=False)
        validate_auth_configuration()

    def test_escaped_newlines_in_the_pem_are_accepted(
        self, keypair: tuple[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PEMs survive most secret managers only with literal \\n escapes."""
        private, public = keypair
        monkeypatch.setattr(settings, "auth_required", True, raising=False)
        monkeypatch.setattr(settings, "jwt_public_key", public.replace("\n", "\\n"), raising=False)
        validate_auth_configuration()
        assert decode_token(_token(private)).principal_id == "priya"


class TestBearerParsing:
    def test_valid_and_invalid_headers(self) -> None:
        assert bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"
        assert bearer_token("bearer abc") == "abc"
        assert bearer_token(None) is None
        assert bearer_token("Basic abc") is None
        assert bearer_token("Bearer") is None
        assert bearer_token("Bearer   ") is None


class TestRateLimiter:
    def test_disabled_by_default(self) -> None:
        limiter = RateLimiter(0)
        assert all(limiter.check("k")[0] for _ in range(1000))

    def test_burst_is_capped_then_refills(self) -> None:
        limiter = RateLimiter(60)  # one per second, burst 60
        now = 0.0
        assert all(limiter.check("k", now=now)[0] for _ in range(60))
        allowed, retry_after = limiter.check("k", now=now)
        assert allowed is False
        assert retry_after >= 1
        assert limiter.check("k", now=now + 1.0)[0] is True

    def test_keys_are_independent(self) -> None:
        limiter = RateLimiter(1)
        assert limiter.check("a", now=0.0)[0] is True
        assert limiter.check("a", now=0.0)[0] is False
        assert limiter.check("b", now=0.0)[0] is True

    def test_no_boundary_doubling(self) -> None:
        """A fixed window would allow 2x the rate across a window boundary. A
        token bucket must not."""
        limiter = RateLimiter(60)
        assert all(limiter.check("k", now=59.0)[0] for _ in range(60))
        # one second later only one token has refilled, not a fresh window
        assert limiter.check("k", now=60.0)[0] is True
        assert limiter.check("k", now=60.0)[0] is False


class _FakeSocket:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[Any] = []
        self.fail = fail
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: Any) -> None:
        if self.fail:
            raise RuntimeError("client gone")
        self.sent.append(data)


class TestWebSocketManager:
    def test_publish_reaches_subscribers_of_that_job_only(self) -> None:
        async def scenario() -> None:
            manager = WebSocketManager()
            a, b = _FakeSocket(), _FakeSocket()
            await manager.subscribe("job-1", a)
            await manager.subscribe("job-2", b)
            delivered = await manager.publish("job-1", {"status": "running"})
            assert delivered == 1
            assert a.sent == [{"job_id": "job-1", "status": "running"}]
            assert b.sent == []

        asyncio.run(scenario())

    def test_a_dead_client_is_dropped_and_does_not_break_delivery(self) -> None:
        """A browser tab that went away must not keep a job's publisher failing."""

        async def scenario() -> None:
            manager = WebSocketManager()
            good, dead = _FakeSocket(), _FakeSocket(fail=True)
            await manager.subscribe("job-1", good)
            await manager.subscribe("job-1", dead)
            assert await manager.publish("job-1", {"status": "running"}) == 1
            assert manager.subscriber_count("job-1") == 1
            assert await manager.publish("job-1", {"status": "completed"}) == 1

        asyncio.run(scenario())

    def test_publish_to_nobody_is_a_noop(self) -> None:
        async def scenario() -> None:
            manager = WebSocketManager()
            assert await manager.publish("nobody", {"status": "running"}) == 0

        asyncio.run(scenario())

    def test_unsubscribe_removes_the_job_entry(self) -> None:
        async def scenario() -> None:
            manager = WebSocketManager()
            socket = _FakeSocket()
            await manager.subscribe("job-1", socket)
            await manager.unsubscribe("job-1", socket)
            assert manager.subscriber_count("job-1") == 0

        asyncio.run(scenario())

    def test_threadsafe_publish_without_a_loop_is_silent(self) -> None:
        """A status broadcast failing must never fail the job it reports on."""
        manager = WebSocketManager()
        manager.publish_threadsafe("job-1", {"status": "running"})  # must not raise


class TestLogBuffer:
    """The /logs ring buffer is an operator convenience, not the audit trail."""

    def test_bounded_and_drops_oldest(self) -> None:
        from src.chandra.api.logbuffer import LogBuffer

        buffer = LogBuffer(max_entries=3)
        for i in range(5):
            buffer.append({"message": str(i)})
        assert len(buffer) == 3
        assert [e["message"] for e in buffer.read()] == ["2", "3", "4"]

    def test_limit_and_offset_window(self) -> None:
        from src.chandra.api.logbuffer import LogBuffer

        buffer = LogBuffer()
        for i in range(10):
            buffer.append({"message": str(i)})
        assert [e["message"] for e in buffer.read(limit=3)] == ["7", "8", "9"]
        assert [e["message"] for e in buffer.read(limit=3, offset=3)] == ["4", "5", "6"]

    def test_reads_do_not_alias_the_buffer(self) -> None:
        from src.chandra.api.logbuffer import LogBuffer

        buffer = LogBuffer()
        buffer.append({"message": "a"})
        snapshot = buffer.read()
        buffer.append({"message": "b"})
        assert len(snapshot) == 1

    def test_concurrent_appends_lose_nothing(self) -> None:
        import threading

        from src.chandra.api.logbuffer import LogBuffer

        buffer = LogBuffer(max_entries=1000)

        def writer(start: int) -> None:
            for i in range(100):
                buffer.append({"message": f"{start}-{i}"})

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(buffer) == 500
