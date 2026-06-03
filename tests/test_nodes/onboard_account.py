"""Standalone test for the ``onboard_account`` node.

Pipeline position:
    START -> onboard_account -> ingest_observations

The node resolves the AWS account and active regions, then seeds the
state with empty ``raw_findings`` and ``errors`` so downstream observers
can rely on those keys being present.

Real-env mode uses the ``SYNTHETIC_ACCOUNT_ID`` and ``AWS_DEFAULT_REGION``
from .env and hits real ``ec2:DescribeRegions`` to enumerate regions.
Set ``CHANDRA_TEST_NODES_MOCK=1`` to run it offline (moto-backed).
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

from src.chandra.graphs.nodes import onboard_account

from tests.test_nodes._env import aws_scope, banner, make_state, mode_banner, real_account_id


def test_onboardaccount() -> dict:
    """Run the ``onboard_account`` node against the real (or mocked) AWS account."""
    mode_banner()
    banner("onboard_account -- input state")
    state_in = make_state()
    print(f"  run_id     = {state_in['run_id']!r}")
    print(f"  account_id = {state_in['account_id']!r}  (from .env SYNTHETIC_ACCOUNT_ID)")
    print(f"  regions    = {state_in['regions']!r}  (from .env AWS_DEFAULT_REGION)")

    with aws_scope():
        result = onboard_account(state_in)

    banner("onboard_account -- output (state update)")
    print(f"  account_id    = {result['account_id']!r}")
    print(f"  regions       = {result['regions']!r}")
    print(f"  raw_findings  = {result['raw_findings']!r}")
    print(f"  errors        = {result['errors']!r}")

    assert result["account_id"] == real_account_id()
    assert result["regions"]  # non-empty list
    assert result["raw_findings"] == {}
    assert result["errors"] == []
    print("\n  [ok] onboard_account emitted the expected state update")
    return result


if __name__ == "__main__":
    test_onboardaccount()
