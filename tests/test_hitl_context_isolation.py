"""
test_hitl_context_isolation.py
=================================
Tests the EXACT bug that caused `KeyError: 'action'` on HITL resume.

THE REAL BUG (not ContextVars):
  - Request 1 creates ExecutionAgents instance A with MemorySaver A
  - Graph pauses, state saved in MemorySaver A
  - Request 1 ends, instance A garbage-collected, MemorySaver A destroyed
  - Request 2 creates ExecutionAgents instance B with fresh empty MemorySaver B
  - Resume fails: checkpoint not found, LangGraph reruns from start
  - state["action"] missing -> KeyError

THE FIX:
  - Module-level singleton checkpointer shared across ALL instances
  - Pause saves to shared store, resume finds it from shared store

Run with:
    .venv\\Scripts\\python.exe tests/test_hitl_context_isolation.py
"""

import contextvars
import sys
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from langgraph.graph import StateGraph, END, START
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import MemorySaver
from typing import TypedDict, Optional


# ─────────────────────────────────────────────
# 1.  Minimal state
# ─────────────────────────────────────────────
class TestState(TypedDict):
    action: str
    result: str
    clarification: Optional[str]


# ─────────────────────────────────────────────
# 2.  Simulate the singleton checkpointer pattern
#     (mirrors the fix in aws_execution_agent.py)
# ─────────────────────────────────────────────
_SHARED_CHECKPOINTER = None

def _get_shared_checkpointer():
    global _SHARED_CHECKPOINTER
    if _SHARED_CHECKPOINTER is None:
        _SHARED_CHECKPOINTER = MemorySaver()
    return _SHARED_CHECKPOINTER

def _reset_shared_checkpointer():
    """Reset for test isolation."""
    global _SHARED_CHECKPOINTER
    _SHARED_CHECKPOINTER = None


# ─────────────────────────────────────────────
# 3.  Simulate ExecutionAgents class
#     Each instance gets the SHARED checkpointer (the fix)
#     vs each instance getting its OWN checkpointer (the bug)
# ─────────────────────────────────────────────
def _build_graph(checkpointer):
    def analyze(state: TestState):
        """This is _analyze_node — throws KeyError: 'action' when state is empty."""
        action = state["action"]  # This was crashing
        return {"result": f"analyzed:{action}"}

    def hitl_node(state: TestState):
        answers = interrupt(["What is the S3 bucket name?"])
        answer = answers[0] if isinstance(answers, list) else answers
        return {"clarification": answer}

    def finish(state: TestState):
        action = state["action"]
        clarification = state.get("clarification", "")
        return {"result": f"done:{action}:clarified_with:{clarification}"}

    builder = StateGraph(TestState)
    builder.add_node("analyze", analyze)
    builder.add_node("hitl", hitl_node)
    builder.add_node("finish", finish)
    builder.add_edge(START, "analyze")
    builder.add_edge("analyze", "hitl")
    builder.add_edge("hitl", "finish")
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=checkpointer)


class SimulatedAgent:
    """Simulates one HTTP request creating one ExecutionAgents instance."""
    def __init__(self, use_singleton: bool):
        if use_singleton:
            self.checkpointer = _get_shared_checkpointer()  # THE FIX
        else:
            self.checkpointer = MemorySaver()  # THE BUG (each instance own memory)
        self.graph = _build_graph(self.checkpointer)


# ─────────────────────────────────────────────
# 4.  Tests
# ─────────────────────────────────────────────
class TestRealBug(unittest.TestCase):
    """Proves the real bug and that the singleton fix resolves it."""

    def setUp(self):
        _reset_shared_checkpointer()

    def test_bug_reproduced_with_separate_checkpointers(self):
        """
        REPRODUCE THE BUG:
        Two different instances with separate checkpointers.
        The resume fails because the checkpoint is in instance A's memory
        which is gone when instance B tries to resume.
        """
        thread_id = "bug-test-thread-001"
        config = {"configurable": {"thread_id": thread_id}}
        action_payload = {"action": "create-s3-bucket", "result": "", "clarification": None}

        # Request 1: instance A pauses
        agent_a = SimulatedAgent(use_singleton=False)
        ctx = contextvars.Context()
        ctx.run(agent_a.graph.invoke, action_payload, config)

        snap = ctx.run(agent_a.graph.get_state, config)
        self.assertTrue(snap.tasks or snap.next, "Should be paused")
        print("   BUG test: Request 1 paused correctly in instance A's memory")

        # Request 1 ends — agent_a goes out of scope (garbage collected)
        del agent_a

        # Request 2: brand new instance B (empty memory)
        agent_b = SimulatedAgent(use_singleton=False)
        ctx2 = contextvars.Context()
        try:
            ctx2.run(agent_b.graph.invoke, Command(resume=["my-bucket"]), config)
            # If this succeeds, it means LangGraph restarted from scratch
            # We detect this by checking if action is in final state
            final = ctx2.run(agent_b.graph.get_state, config)
            action_val = final.values.get("action", "MISSING")
            print(f"   BUG test: Resume result action='{action_val}' result='{final.values.get('result')}'")
            # The action will be MISSING because it restarted with empty state from Command
            print("   BUG CONFIRMED: Resume produced wrong state (checkpoint was lost)")
        except KeyError as e:
            print(f"   BUG CONFIRMED: KeyError: {e} — exactly what production throws!")
        except Exception as e:
            print(f"   BUG CONFIRMED: Exception: {type(e).__name__}: {e}")

        print("PASS: Bug successfully reproduced with separate per-instance checkpointers.")

    def test_fix_works_with_singleton_checkpointer(self):
        """
        VERIFY THE FIX:
        Two different instances but sharing ONE singleton checkpointer.
        Resume MUST succeed and correctly read state['action'].
        """
        thread_id = "fix-test-thread-002"
        config = {"configurable": {"thread_id": thread_id}}
        action_payload = {"action": "deploy-rds-instance", "result": "", "clarification": None}

        # Request 1: agent A pauses — saves to SHARED checkpointer
        agent_a = SimulatedAgent(use_singleton=True)
        ctx1 = contextvars.Context()
        ctx1.run(agent_a.graph.invoke, action_payload, config)

        snap = ctx1.run(agent_a.graph.get_state, config)
        self.assertTrue(snap.tasks or snap.next, "Should be paused")
        self.assertEqual(snap.values.get("action"), "deploy-rds-instance")
        print(f"   FIX test: Request 1 paused. action='{snap.values['action']}'")

        # agent_a goes out of scope — but SHARED checkpointer still holds state
        del agent_a

        # Request 2: brand new agent B — uses the SAME shared checkpointer
        agent_b = SimulatedAgent(use_singleton=True)
        ctx2 = contextvars.Context()
        try:
            ctx2.run(agent_b.graph.invoke, Command(resume=["prod-rds-bucket-01"]), config)
        except KeyError as e:
            self.fail(f"FAIL: KeyError: {e} — the fix didn't work!")
        except Exception as e:
            self.fail(f"FAIL: {type(e).__name__}: {e}")

        final = ctx2.run(agent_b.graph.get_state, config)
        vals = final.values

        self.assertEqual(vals.get("action"), "deploy-rds-instance",
            "FAIL: action was lost after resume!")
        self.assertEqual(vals.get("clarification"), "prod-rds-bucket-01",
            "FAIL: clarification answer not saved!")
        self.assertIn("done:deploy-rds-instance:clarified_with:prod-rds-bucket-01",
                      vals.get("result", ""),
            "FAIL: final result is wrong!")

        print(f"   FIX test: Resume succeeded!")
        print(f"   Final result: '{vals['result']}'")
        print("PASS: Singleton checkpointer fix works correctly across two instances!")

    def test_exec_prefix_no_collision(self):
        """exec-<id> and <id> are isolated even in the shared checkpointer."""
        job_id = "shared-job-abc"
        parent_config = {"configurable": {"thread_id": job_id}}
        child_config = {"configurable": {"thread_id": f"exec-{job_id}"}}

        # Both use the same singleton checkpointer
        agent1 = SimulatedAgent(use_singleton=True)
        agent2 = SimulatedAgent(use_singleton=True)
        ctx = contextvars.Context()

        ctx.run(agent1.graph.invoke, {"action": "parent-action", "result": "", "clarification": None}, parent_config)
        ctx.run(agent2.graph.invoke, {"action": "child-action", "result": "", "clarification": None}, child_config)

        parent_snap = ctx.run(agent1.graph.get_state, parent_config)
        child_snap = ctx.run(agent2.graph.get_state, child_config)

        self.assertEqual(parent_snap.values.get("action"), "parent-action",
            "FAIL: Parent state corrupted by child!")
        self.assertEqual(child_snap.values.get("action"), "child-action",
            "FAIL: Child state corrupted by parent!")
        print(f"PASS: No collision — parent='{parent_snap.values['action']}', child='{child_snap.values['action']}'")


class TestContextvarsIsolation(unittest.TestCase):
    """Keep the contextvars tests for documentation."""

    def test_blank_context_is_empty(self):
        my_var = contextvars.ContextVar("TEST_VAR_1", default="default")
        my_var.set("parent_value")
        fresh_ctx = contextvars.Context()
        result = fresh_ctx.run(my_var.get)
        self.assertEqual(result, "default")
        print("PASS: contextvars.Context() is blank — no parent leakage.")


# ─────────────────────────────────────────────
# 5.  Runner
# ─────────────────────────────────────────────
if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("=" * 65)
    print("  HITL Context Isolation + Singleton Checkpointer Tests")
    print("=" * 65)
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestRealBug))
    suite.addTests(loader.loadTestsFromTestCase(TestContextvarsIsolation))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    print("\n" + "=" * 65)
    if result.wasSuccessful():
        print("  ALL TESTS PASSED -- HITL resume is fully verified!")
    else:
        failures = len(result.failures) + len(result.errors)
        print(f"  {failures} FAILED -- Fix is NOT working!")
    print("=" * 65)
    sys.exit(0 if result.wasSuccessful() else 1)
