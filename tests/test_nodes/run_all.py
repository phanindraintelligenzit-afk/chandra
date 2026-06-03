"""Driver: run every per-node test script in pipeline order.

This is a convenience wrapper around the 14 individual node scripts in
this folder. It invokes each node's ``test_<node>()`` function and
prints a one-line status so you can see at a glance which nodes pass
and which raise.

Pipeline order matches the topology in
``src/chandra/graphs/chandra_graph.py``:

    onboard_account -> ingest_observations -> kra_supervisor
        -> {observe_cost, observe_security, observe_compliance,
            observe_performance, observe_reliability}
        -> analyze -> decision_router -> action_executor
        -> escalation -> compose_briefing -> [approval_node] -> persist
"""

from __future__ import annotations

import importlib
import traceback
from types import ModuleType

from ._env import banner

# Each entry: (module_name, callable_name).
# The 5 KRA observers run after kra_supervisor. approval_node runs after
# compose_briefing. persist is terminal.
NODE_TESTS: list[tuple[str, str]] = [
    ("onboard_account", "test_onboardaccount"),
    ("ingest_observations", "test_ingestobservations"),
    ("kra_supervisor", "test_krasupervisor"),
    # _route_kra_workers is an internal helper; run it as a bonus check.
    ("kra_supervisor", "test_routekraworkers"),
    ("observe_cost", "test_observecost"),
    ("observe_security", "test_observesecurity"),
    ("observe_compliance", "test_observecompliance"),
    ("observe_performance", "test_observeperformance"),
    ("observe_reliability", "test_observereliability"),
    ("analyze", "test_analyze"),
    ("decision_router", "test_decisionrouter"),
    ("action_executor", "test_actionexecutor"),
    ("escalation", "test_escalation"),
    ("compose_briefing", "test_composebriefing"),
    ("approval_node", "test_approvalnode"),
    ("persist", "test_persist"),
]


def _run_one(module_name: str, fn_name: str) -> tuple[bool, str]:
    """Import a test module and call its test function. Return (ok, message)."""
    try:
        module: ModuleType = importlib.import_module(
            f"tests.test_nodes.{module_name}"
        )
        fn = getattr(module, fn_name)
        fn()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001 -- this is a top-level driver.
        return False, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"


def main() -> None:
    banner("Chandra per-node smoke test -- all 16 tests in pipeline order")
    print(f"  will run {len(NODE_TESTS)} tests")
    results: list[tuple[str, str, bool, str]] = []
    for module_name, fn_name in NODE_TESTS:
        ok, message = _run_one(module_name, fn_name)
        results.append((module_name, fn_name, ok, message))
        status = "PASS" if ok else "FAIL"
        print(f"    [{status}] {module_name}.{fn_name}")

    banner("Summary")
    passes = sum(1 for _, _, ok, _ in results if ok)
    fails = len(results) - passes
    print(f"  passed: {passes}/{len(results)}")
    print(f"  failed: {fails}")
    if fails:
        print()
        for module_name, fn_name, ok, message in results:
            if not ok:
                print(f"  --- {module_name}.{fn_name} ---")
                print(message)


if __name__ == "__main__":
    main()
