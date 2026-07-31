# LangGraph Documentation — Chandra Enterprise Digital Cloud Engineer

**Date:** 2026-07-30  
**Branch:** `feature/local-llm`

---

## 1. Overview

Chandra uses **two LangGraph state machines**:

1. **Core Observation Graph** — `src/chandra/graphs/chandra_graph.py` (12 nodes)
2. **Digital Worker Graph** — `src/chandra/digital_worker/graph.py` (15 nodes)

Both use `StateGraph` with `TypedDict` state, `Send()` fan-out, and Postgres-backed checkpoints with human-in-the-loop interrupts.

---

## 2. Core Observation Graph

### Topology
```
START → onboard_account → ingest_observations → kra_supervisor
                                                  │
                                    ┌─────────────┼─────────────┐
                                    ▼             ▼             ▼
                              observe_cost  observe_security  ...
                                    │             │             │
                                    └─────────────┼─────────────┘
                                                  ▼
                                             analyze
                                                  │
                                          decision_router
                                          /              \
                                  pending_writes     auto_fixed
                                         │                │
                                    escalation      action_executor
                                         │                │
                                          └──────┬────────┘
                                                  ▼
                                            compose_briefing
                                                  │
                                           ┌──────┴──────┐
                                           ▼             ▼
                                      approval_node    persist
                                           │
                                           ▼
                                        persist
                                           │
                                           ▼
                                          END
```

### Code: `src/chandra/graphs/chandra_graph.py`
```python
def build_graph(checkpointer=None):
    graph = StateGraph(ChandraState)
    
    graph.add_node("onboard_account", onboard_account)
    graph.add_node("ingest_observations", ingest_observations)
    graph.add_node("kra_supervisor", kra_supervisor)
    graph.add_node("observe_cost", observe_cost)
    graph.add_node("observe_security", observe_security)
    graph.add_node("observe_compliance", observe_compliance)
    graph.add_node("observe_performance", observe_performance)
    graph.add_node("observe_reliability", observe_reliability)
    graph.add_node("analyze", analyze)
    graph.add_node("decision_router", decision_router)
    graph.add_node("approval_node", approval_node)
    graph.add_node("persist", persist)
    graph.add_node("action_executor", action_executor_node)
    graph.add_node("escalation", escalation_node)
    graph.add_node("compose_briefing", compose_briefing)
    
    graph.add_edge(START, "onboard_account")
    graph.add_edge("onboard_account", "ingest_observations")
    graph.add_edge("ingest_observations", "kra_supervisor")
    graph.add_conditional_edges("kra_supervisor", _route_kra_workers, [...])
    # ... (all edges defined)
```

### State: `src/chandra/graphs/state.py`
```python
class ChandraState(TypedDict):
    run_id: str
    account_id: str
    regions: list[str]
    status: RunStatus
    selected_kras: list[str]
    raw_findings: dict[str, list[dict]]
    errors: list[str]
    scorecard: dict[str, ScorecardEntry]
    pending_writes: list[dict]
    auto_fixed: list[dict]
    briefing_md: str
    briefing_json: dict
    summary: str
    dry_run: bool
```

### Key Design Decisions
1. **`Send()` fan-out**: `kra_supervisor` emits one `Send("observe_X", {kra_only_payload})` per KRA via slim projection to reduce payload
2. **Deterministic nodes**: `decision_router`, `action_executor`, `escalation` are pure Python — no LLM calls
3. **LLM nodes**: Only `analyze` (ranking) and `compose_briefing` (narrative) call the LLM
4. **Human-in-the-loop**: `approval_node` uses `interrupt()` to pause for approval, resumed via `Command(resume=decisions)`
5. **Postgres checkpointer**: `build_checkpointer()` returns `PostgresSaver` for production, `MemorySaver` for tests

---

## 3. Digital Worker Graph

### Topology
```
START → receive_request → understand_request → classify_request
      → identify_platform → collect_context → root_cause_analysis
      → plan_resolution → risk_analysis → decision
      → { execute_automation | approval_gate | generate_guidance }
      → validate_result → update_tracker → notify → audit
      → persist → END
```

### Code: `src/chandra/digital_worker/graph.py` (747 lines)

```python
def build_digital_worker_graph(checkpointer=None):
    builder = StateGraph(DigitalWorkerState)
    
    builder.add_node("receive_request", receive_request)
    builder.add_node("understand_request", understand_request)
    builder.add_node("classify_request", classify_request)
    builder.add_node("identify_platform", identify_platform)
    builder.add_node("collect_context", collect_context)
    builder.add_node("root_cause_analysis", root_cause_analysis)
    builder.add_node("plan_resolution", plan_resolution)
    builder.add_node("risk_analysis", risk_analysis)
    builder.add_node("decision", decision_node)
    builder.add_node("execute_automation", execute_automation)
    builder.add_node("approval_gate", approval_gate)
    builder.add_node("generate_guidance", generate_guidance)
    builder.add_node("validate_result", validate_result)
    builder.add_node("update_tracker", update_tracker)
    builder.add_node("notify", notify_all)
    builder.add_node("audit", audit_event)
    builder.add_node("persist", persist_to_db)
    
    # Decision mode routing
    builder.add_conditional_edges(
        "decision",
        _route_decision_mode,
        {
            "execute_automation": "execute_automation",
            "approval_gate": "approval_gate",
            "generate_guidance": "generate_guidance",
        },
    )
    # ... (all edges defined)
```

### Decision Modes
| Mode | Route | Description |
|------|-------|-------------|
| `auto` | → execute_automation | Low-risk, pre-approved actions |
| `approval` | → approval_gate | High-risk actions requiring human approval |
| `guidance` | → generate_guidance | Advisory/informational only |

### Notification Channels
| Channel | Source | Implementation |
|---------|--------|---------------|
| Slack | `src/chandra/digital_worker/notifications.py` | Webhook-based |
| Teams | `src/chandra/digital_worker/notifications.py` | Webhook-based |
| Email | `src/chandra/digital_worker/notifications.py` | SMTP/SNS |
| Jira | `tools/jira_tools/` | REST API |
| Webhook | `fastapi_app.py` | POST /webhooks/{source} |
| SNS | `src/chandra/escalation/publisher.py` | AWS SNS Topic |

---

## 4. Checkpointer Architecture

### `src/chandra/graphs/checkpointer.py`
```python
def build_checkpointer():
    """Return PostgresSaver for production, MemorySaver for tests."""
    if settings.postgres_url and "localhost" not in settings.postgres_url:
        from langgraph.checkpoint.postgres import PostgresSaver
        return PostgresSaver.from_conn_string(settings.postgres_url)
    from langgraph.checkpoint.memory import MemorySaver
    return MemorySaver()
```

### Interrupt / Resume Pattern
```python
# In approval_node:
def approval_node(state: DigitalWorkerState) -> dict:
    approval_data = interrupt({
        "job_id": state.get("job_id"),
        "pending_actions": state.get("pending_actions", []),
        "risk_level": state.get("risk_level", "unknown"),
    })
    # approval_data is the Command(resume=...) payload
    return {"approval_decisions": approval_data}

# Resuming:
graph.invoke(Command(resume=decisions), config)
```

---

## 5. Key Architectural Invariants

1. **LangGraph is the only orchestrator** — No LangChain AgentExecutor, no create_react_agent
2. **LLM calls only in specific nodes** — analyze, compose_briefing, plan_resolution
3. **Deterministic routers** — decision_router, action_executor, escalation
4. **Postgres writes only in persist nodes** — No DB writes in detectors or other nodes
5. **Boto3 paginators everywhere** — No silent truncation of AWS API responses
6. **State mutations via reducers** — All list/dict merges use LangGraph reducer functions

---

## 6. Testing

### Unit Tests
```bash
# Test individual graph nodes
pytest tests/unit/test_kra_supervisor.py -v
pytest tests/unit/test_decision_router.py -v
pytest tests/unit/test_approval.py -v

# Test digital worker
pytest tests/unit/test_digital_worker_graph.py -v
pytest tests/unit/test_digital_worker_intake.py -v
pytest tests/unit/test_digital_worker_planning.py -v
```

### Test Fixtures
```python
# tests/test_nodes/ - Integration-style tests with real graph compilation
# tests/unit/ - Pure unit tests with mocked dependencies
# test_nodes/run_all.py - Runs all node tests
```

---

## 7. Dependencies

| Package | Purpose |
|---------|---------|
| `langgraph` | State machine orchestration |
| `langchain-core` | Base runnable interfaces |
| `langchain-aws` | Bedrock chat model |
| `langchain-openai` | OpenAI/vLLM/Ollama chat model |
| `langgraph-checkpoint-postgres` | Postgres checkpointer |
| `langgraph-checkpoint` | Memory checkpointer |