"""Standalone test for the ``kra_supervisor`` and ``_route_kra_workers`` nodes.

Pipeline position:
    ingest_observations -> kra_supervisor -> {5 KRA observers in parallel}

These two nodes do not touch AWS, so the real-env / mock-env toggle
does not change their behaviour. They are pure routing logic.
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

from langgraph.types import Send
from src.chandra.graphs.nodes import _route_kra_workers, kra_supervisor

from tests.test_nodes._env import banner, make_state, mode_banner


def test_krasupervisor() -> dict:
    """Run the ``kra_supervisor`` node.

    The supervisor is a no-op for state. It exists so the graph has a
    single fan-out point: if a future iteration wants to skip or
    re-order KRAs, the change happens here, not in the graph topology.
    """
    mode_banner()
    banner("kra_supervisor -- input state")
    state_in = make_state()
    print(f"  run_id     = {state_in['run_id']!r}")
    print(f"  account_id = {state_in['account_id']!r}")
    print(f"  regions    = {state_in['regions']!r}")

    result = kra_supervisor(state_in)

    banner("kra_supervisor -- output (state update)")
    print(f"  result = {result!r}")
    assert result == {}
    print("\n  [ok] kra_supervisor emitted an empty state update (no-op)")
    return result


def test_routekraworkers() -> list[Send]:
    """Run the ``_route_kra_workers`` router to inspect the fan-out."""
    mode_banner()
    banner("_route_kra_workers -- input state")
    state_in = make_state()
    print(f"  run_id     = {state_in['run_id']!r}")
    print(f"  regions    = {state_in['regions']!r}")

    sends = _route_kra_workers(state_in)

    banner("_route_kra_workers -- output (list[Send])")
    print(f"  count = {len(sends)}")
    for s in sends:
        print(f"    - Send(node={s.node!r:25s})  -> payload keys: {sorted(s.arg.keys())}")
    print()
    print(f"  targets = {sorted(s.node for s in sends)}")

    expected = {
        "observe_cost",
        "observe_security",
        "observe_compliance",
        "observe_performance",
        "observe_reliability",
    }
    assert {s.node for s in sends} == expected
    assert all(isinstance(s, Send) for s in sends)
    print("\n  [ok] _route_kra_workers dispatched Send() to all 5 KRA observers")
    return sends


if __name__ == "__main__":
    test_krasupervisor()
    test_routekraworkers()
