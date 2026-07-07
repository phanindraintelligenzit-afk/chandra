"""Standalone test for the ``persist`` node.

Pipeline position:
    [approval_node] -> persist -> END

The persist node is the ONLY place in the codebase (outside Alembic
migrations) that writes to Postgres. It writes:
* one ``Run`` row per run
* one ``Finding`` row per detected finding
* one ``Briefing`` row per run (markdown + scorecard)

Real-env mode uses the ``POSTGRES_URL`` from .env. If it's not set the
script falls back to an in-memory SQLite database so the demo can still
run. Set ``CHANDRA_TEST_NODES_MOCK=1`` to force the in-memory fallback
even when a real Postgres URL is configured.
"""

from __future__ import annotations

# --- sys.path bootstrap: lets this script run both as a module
#     (uv run python -m tests.test_nodes.x) and as a script
#     (uv run tests/test_nodes/x.py). ---
import sys as _sys
from pathlib import Path as _Path

_REPO_ROOT = _Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_REPO_ROOT))

# Resolve which engine to use: real POSTGRES_URL when in real mode,
# in-memory SQLite when in MOCK_MODE or when no URL is configured.

import src.chandra.db.session as _session
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from src.chandra.briefing.schemas import Finding
from src.chandra.db.models import Base

from tests.test_nodes._env import MOCK_MODE, banner, make_state, mode_banner, real_postgres_url

if MOCK_MODE or not real_postgres_url():
    _test_engine: Engine = create_engine("sqlite:///:memory:", future=True)
    engine_label = "sqlite:///:memory:  (mock / no POSTGRES_URL set)"
else:
    _test_engine = create_engine(real_postgres_url(), future=True)
    engine_label = "POSTGRES_URL  (real, from .env)"

_session._engine = _test_engine  # type: ignore[attr-defined]
_session._SessionLocal = sessionmaker(  # type: ignore[attr-defined]
    bind=_test_engine, expire_on_commit=False, autoflush=False
)
# Only auto-create tables for the in-memory engine; the real DB already
# has Alembic-managed schema.
if "sqlite" in engine_label:
    Base.metadata.create_all(_test_engine)

from src.chandra.db.models import Briefing, Run
from src.chandra.db.models import Finding as FindingRow
from src.chandra.graphs.nodes import persist


def _finding(*, kra: str, severity: str, detector_id: str) -> Finding:
    return Finding(
        kra=kra,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        resource_arn=f"arn:aws:test:::{detector_id}",
        resource_type="Test",
        region="us-east-1",
        title=f"finding {detector_id}",
        evidence={"key": "value"},
        recommendation="fix it",
        detector_id=detector_id,
    )


def test_persist() -> None:
    """Run ``persist`` against the real Postgres (or in-memory SQLite)."""
    mode_banner()
    print(f"  [db] using engine: {engine_label}")
    banner("persist -- input state")
    raw = {
        "security": [
            _finding(kra="security", severity="critical", detector_id="SEC-1"),
            _finding(kra="security", severity="high", detector_id="SEC-2"),
        ],
        "cost": [
            _finding(kra="cost", severity="medium", detector_id="COST-1"),
        ],
    }
    state_in = make_state(
        raw_findings=raw,
        analyzed_findings=[],
        scorecard={
            "cost": 90,
            "security": 25,
            "compliance": 100,
            "performance": 100,
            "reliability": 100,
            "overall": 83,
        },
        briefing_md="# Daily briefing\n\nEverything is fine.\n",
    )
    print(f"  run_id        = {state_in['run_id']!r}")
    print(f"  raw_findings  = {sorted(raw.keys())}")
    print(f"  briefing_md   = {len(state_in['briefing_md'])} chars")

    result = persist(state_in)

    banner("persist -- output (state update)")
    print(f"  result = {result!r}  (persist is a terminal no-op on the state)")

    banner("persist -- database contents (read-back)")
    with _session.session_scope() as sess:
        runs = sess.query(Run).all()
        findings = sess.query(FindingRow).all()
        briefings = sess.query(Briefing).all()

        print(f"  runs        : {len(runs)}")
        for r in runs:
            print(f"    - id={r.id}  account_id={r.account_id}  status={r.status}")

        print(f"  findings    : {len(findings)}")
        for f in findings:
            print(
                f"    - {f.kra:10s} {f.severity:8s} {f.detector_id:8s}  resource={f.resource_arn}"
            )

        print(f"  briefings   : {len(briefings)}")
        for b in briefings:
            print(
                f"    - run_id={b.run_id}  findings_count={b.findings_count}"
                f"  scorecard_overall={b.scorecard_jsonb['overall']}"
            )

    assert len(runs) == 1
    assert len(findings) == 3
    assert len(briefings) == 1
    assert briefings[0].findings_count == 3
    assert briefings[0].scorecard_jsonb["overall"] == 83
    print("\n  [ok] persist wrote 1 run, 3 findings, 1 briefing to the DB")


if __name__ == "__main__":
    test_persist()
