"""SQLAlchemy engine + session factory."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from src.chandra.config import settings

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Return the cached process-wide SQLAlchemy engine."""
    global _engine
    if _engine is None:
        try:
            _engine = create_engine(
                settings.postgres_url,
                pool_pre_ping=True,
                pool_recycle=1800,
                pool_size=20,
                max_overflow=20,
                pool_timeout=30,
                future=True,
            )
        except Exception:
            from pathlib import Path
            db_dir = Path("database")
            db_dir.mkdir(parents=True, exist_ok=True)
            _engine = create_engine("sqlite:///database/fallback.db", future=True)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
            class_=Session,
        )
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional context. Commits on clean exit, rolls back on exception."""
    global _engine, _SessionLocal
    try:
        sm = get_sessionmaker()
        session = sm()
    except Exception:
        from pathlib import Path
        db_dir = Path("database")
        db_dir.mkdir(parents=True, exist_ok=True)
        _engine = create_engine("sqlite:///database/fallback.db", future=True)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, autoflush=False, class_=Session)
        session = _SessionLocal()

    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
