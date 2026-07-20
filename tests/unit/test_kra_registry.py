"""Dynamic KRA Framework registry: builtins, DB merge, matching, API."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from src.chandra.db.models import Base
from src.chandra.kra import registry
from src.chandra.kra.registry import BUILTIN_KRAS, KraDefinition, list_kras, match_kra, upsert_kra


@pytest.fixture
def sqlite_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    """Route the registry's session_scope to in-memory SQLite."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def _scope() -> Iterator[Session]:
        session = factory()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(registry, "session_scope", _scope)
    yield factory


class TestBuiltins:
    def test_five_builtins_always_present(self, sqlite_registry: object) -> None:
        codes = {k.code for k in list_kras()}
        assert {"cost", "security", "compliance", "performance", "reliability"} <= codes

    def test_builtins_survive_db_outage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sqlalchemy.exc import OperationalError

        @contextmanager
        def _broken() -> Iterator[None]:
            raise OperationalError("conn refused", None, Exception("down"))
            yield

        monkeypatch.setattr(registry, "session_scope", _broken)
        codes = {k.code for k in list_kras()}
        assert len(codes) == len(BUILTIN_KRAS)


class TestCustomerKras:
    def test_customer_kra_is_a_row_not_a_release(self, sqlite_registry: object) -> None:
        """Insert 'Business Continuity' → it participates immediately."""
        upsert_kra(
            KraDefinition(
                code="business_continuity",
                name="Business Continuity",
                description="DR readiness and RTO/RPO conformance.",
                keywords=["disaster", "recovery", "rto", "rpo", "continuity"],
                mode="llm",
            )
        )
        kras = {k.code: k for k in list_kras()}
        assert "business_continuity" in kras
        assert kras["business_continuity"].mode == "llm"
        # And free-form text now maps onto it deterministically.
        match = match_kra("we failed the disaster recovery drill, rto exceeded")
        assert match is not None and match.code == "business_continuity"

    def test_upsert_updates_in_place(self, sqlite_registry: object) -> None:
        base = KraDefinition(code="finops", name="FinOps", keywords=["chargeback"])
        upsert_kra(base)
        upsert_kra(base.model_copy(update={"name": "FinOps v2", "enabled": False}))
        merged = {k.code: k for k in list_kras(include_disabled=True)}
        assert merged["finops"].name == "FinOps v2"
        assert merged["finops"].enabled is False
        # Disabled rows are hidden by default.
        assert "finops" not in {k.code for k in list_kras()}

    def test_match_prefers_most_hits(self, sqlite_registry: object) -> None:
        match = match_kra("idle unused instances driving up spend and cost")
        assert match is not None and match.code == "cost"

    def test_no_match_returns_none(self, sqlite_registry: object) -> None:
        assert match_kra("zzzz qqqq wwww") is None


class TestClassifierIntegration:
    def test_classification_carries_kra_code(self, sqlite_registry: object) -> None:
        from src.chandra.digital_worker.classifier import classify_request
        from src.chandra.digital_worker.intake import normalize_request

        request = normalize_request("rest_api", {"title": "Reduce spend from idle rds instances"})
        classification = classify_request(request)
        assert classification.kra_code == "cost"

    def test_customer_kra_reachable_from_classifier(self, sqlite_registry: object) -> None:
        from src.chandra.digital_worker.classifier import classify_request
        from src.chandra.digital_worker.intake import normalize_request

        upsert_kra(
            KraDefinition(
                code="sustainability",
                name="Sustainability",
                keywords=["carbon", "emissions", "sustainability"],
                mode="llm",
            )
        )
        request = normalize_request(
            "rest_api", {"title": "Report carbon emissions for our workloads"}
        )
        assert classify_request(request).kra_code == "sustainability"
