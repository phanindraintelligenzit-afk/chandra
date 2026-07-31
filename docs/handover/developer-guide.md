# Chandra — Developer Guide

> **Enterprise AI Cloud Operations Platform**
> **Branch:** `feature/local-llm`
> **Last Updated:** 2026-07-30

---

## Table of Contents

1. [Development Environment Setup](#1-development-environment-setup)
2. [Coding Standards](#2-coding-standards)
3. [Project Structure](#3-project-structure)
4. [Import Conventions](#4-import-conventions)
5. [LLM Provider Switching](#5-llm-provider-switching)
6. [Adding a New KRA](#6-adding-a-new-kra)
7. [Adding a New Digital Worker Node](#7-adding-a-new-digital-worker-node)
8. [Writing Tests](#8-writing-tests)
9. [Running Tests](#9-running-tests)
10. [Debugging](#10-debugging)

---

## 1. Development Environment Setup

### Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.12+ | Backend runtime |
| Node.js | 22+ | Frontend (Next.js 16) |
| Docker | 24+ | Postgres + containerized deployment |
| Docker Compose | v2 | Service orchestration |
| uv | latest | Python package manager |
| Git | any | Version control |
| NVIDIA GPU (optional) | 24GB+ VRAM | Local LLM (vLLM) |

### One-time setup

```bash
# 1. Clone the repository
git clone https://github.com/phanindraintelligenzit-afk/chandra.git
cd chandra

# 2. Switch to the feature branch
git checkout feature/local-llm

# 3. Configure environment
cp .env.example .env
# Edit .env — at minimum set:
#   AWS_PROFILE=your-sandbox
#   SYNTHETIC_ACCOUNT_ID=123456789012
#   LLM_PROVIDER=bedrock  (or vllm for local)

# 4. Install backend dependencies
make install
# or: uv sync --all-extras

# 5. Start Postgres
make db-up
# or: docker compose up -d postgres

# 6. Apply database migrations
make migrate
# or: uv run alembic upgrade head

# 7. Install frontend dependencies
cd frontend
npm install
cd ..

# 8. Verify everything works
make check
```

### Day-to-day workflow

```bash
# Terminal 1: Backend API
uvicorn fastapi_app:app --host 0.0.0.0 --port 6001 --reload

# Terminal 2: Frontend dev server
cd frontend && npm run dev

# Terminal 3: (optional) Local LLM
vLLM_USE_FLASHINFER_SAMPLER=0 vllm serve google/gemma-4-12B-it-qat-w4a16-ct \
  --gpu-memory-utilization 0.90 --max-model-len 16384 \
  --enable-prefix-caching --enforce-eager \
  --host 0.0.0.0 --port 8000
```

### Full quality gate (run before every PR)

```bash
make check
# Runs: ruff check → ruff format --check → mypy --strict → pytest
```

---

## 2. Coding Standards

### Hard architectural rules (from CLAUDE.md)

These are **immutable** — do not violate without explicit signoff from Phani:

| Rule | Enforced by |
|------|-----------|
| **LangGraph is the only orchestration framework.** No `AgentExecutor`, no `create_react_agent`. Use `StateGraph` + `Send(...)`. | `make check` |
| **`src/chandra/llm` is the LLM abstraction layer.** Every LLM call goes through `get_llm()` or `get_llm_with_tools()`. Never import `ChatBedrockConverse` or `ChatOpenAI` directly. | Code review |
| **Read-only by default.** Detectors never call mutating AWS APIs. Write actions go through `action_executor_node` + `escalation` + `approval_node`. | Architecture |
| **`decision_router`, `action_executor`, `escalation` are deterministic.** No LLM calls in these nodes. | Code review |
| **Postgres writes only in the `persist` node and Alembic migrations.** | Code review |
| **Every boto3 list/describe call uses a paginator.** No silent truncation. | Code review |
| **AWS clients are created via `chandra.aws.client_factory.get_default_factory()`.** Never `boto3.client(...)` directly. | Code review |
| **Frontend is Next.js-only for new work.** The Streamlit dashboard is being sunset (FE-01). | Project rule |
| **New backend code goes under `src/chandra/`.** Don't add new top-level Python files. | Project rule |

### Language conventions

| Convention | Rule |
|------------|------|
| No `# TODO: implement` | Use `raise NotImplementedError("<msg>; tracked in <TICKET-ID>")` |
| No `print()` | Use `chandra.logging.get_logger(__name__)` |
| No bare `except Exception` | Always re-raise or log with structured context |
| Type annotations | Required on all function signatures — `mypy --strict` enforced |
| Docstrings | Google-style docstrings on all public functions/classes |
| Imports | `from __future__ import annotations` at the top of every file |
| Line length | 120 characters (ruff default) |
| String quotes | Double quotes for docstrings, single quotes for strings |

### Team conventions

| Convention | Rule |
|------------|------|
| Branch naming | `<ticket-id-lowercase>/<short-slug>` — e.g. `lg-03/traced-node-decorator` |
| Commit format | `<TICKET-ID>: <imperative summary>` — e.g. `LG-03: add @traced_node decorator` |
| PR body | Must include: Notion ticket link, one-paragraph "what/why", acceptance checklist |
| `make check` must pass | Before opening a PR. CI runs the same gate. |
| One ticket = one PR | Keep PRs small. No week-long branches. |
| Don't self-merge | CODEOWNERS auto-routes reviewers. |

### CODEOWNERS quick reference

| Path | Owner |
|------|-------|
| `src/chandra/graphs/`, `briefing/`, `prompts/`, `escalation/` | LangGraph team |
| `src/chandra/aws/`, `tools/`, `iac/`, `Dockerfile`, `docker-compose.yml` | AWS team |
| `src/chandra/dashboard/`, `frontend/`, `fastapi_app.py`, `digitalworker_agents/`, `copilot_agents/` | Frontend team |
| `src/chandra/db/`, `observability/` | AWS + LangGraph jointly |
| `evals/`, `tests/` | LangGraph team |
| `docs/` | Kshiraja |
| `.github/`, `CODEOWNERS`, `pyproject.toml`, `Makefile` | Phani |

---

## 3. Project Structure

### Backend — canonical pipeline (`src/chandra/`)

```
src/chandra/
├── aws/                     # AWS client factory, region discovery, IAM/audit helpers
│   ├── client_factory.py   # AwsClientFactory — must use this, never boto3.client(...)
│   ├── regions.py          # Region enumeration
│   ├── organizations.py    # OU/account traversal
│   ├── cloudtrail_audit.py # CloudTrail event correlation
│   ├── config_compliance.py# AWS Config rule evaluation
│   ├── encryption_checks.py# KMS/EBS/S3 encryption checks
│   └── helpers.py
├── tools/                   # KRA detectors — deterministic boto3, never calls LLM
│   ├── base.py             # BaseDetector interface
│   ├── cost.py             # Cost Explorer wrapper
│   ├── security.py         # Security hub, GuardDuty, IAM analysis
│   ├── compliance.py       # Config rules, encryption checks
│   ├── performance.py      # CloudWatch metrics, utilization
│   └── reliability.py      # Multi-AZ, failover, backup validation
├── graphs/                  # LangGraph orchestration
│   ├── state.py            # ChandraState (TypedDict) with reducers
│   ├── chandra_graph.py    # StateGraph construction + Send(...) fan-out
│   ├── action_nodes.py     # ALL node functions (observe_*, analyze, decision_router, …)
│   ├── checkpointer.py     # Postgres/Memory checkpointer factory
│   └── nodes.py            # Legacy duplicate — do not edit
├── prompts/                 # LLM prompt templates (consumed only by composer)
│   ├── observer.md, analyzer.md, briefer.md, kra_context.md
├── briefing/                # LLM interaction + narrative composition
│   ├── composer.py         # Only place that calls the LLM
│   ├── schemas.py          # Finding, AnalyzedFinding, Briefing pydantic models
│   └── org_summary.py
├── escalation/              # Action queue + approval workflow (deterministic)
│   ├── schemas.py          # Action, ApprovalDecision, EscalationEnvelope
│   ├── formatter.py        # Render escalation payloads
│   └── publisher.py        # Publish to SNS / Postgres
├── digital_worker/          # DW-01: omnichannel Digital Worker
│   ├── intake.py           # 10-channel payload normalization
│   ├── classifier.py       # Deterministic category/platform/priority
│   ├── context.py          # CloudWatch alarms + runbooks collector
│   ├── planner.py          # Memory cache → composer LLM → playbooks
│   ├── risk.py             # Deterministic risk scoring
│   ├── memory.py           # Resolution memory read/write
│   ├── graph.py            # 15-node workflow graph
│   ├── notifications.py    # Slack/Teams/email/SNS
│   ├── tracker.py          # Jira comment/create/transition
│   ├── guidance.py         # Engineer hand-off markdown
│   ├── schemas.py          # CloudRequest, ResolutionPlan, RiskAssessment
│   ├── state.py            # DigitalWorkerState TypedDict
│   └── verifier.py         # Post-execution verification
├── execution/               # Typed execution pipeline (planner → executor → terraform → validator → verification)
│   ├── planner.py
│   ├── executor.py
│   ├── terraform.py
│   ├── validator.py
│   ├── verification.py
│   ├── bridge.py
│   └── schemas.py
├── llm/                     # LLM abstraction layer — single factory
│   ├── __init__.py          # build_chat_model(), get_llm(), get_llm_with_tools()
│   ├── providers.py         # BaseLLM, VLLMProvider, BedrockProvider, etc.
│   └── token_counter.py     # Token budget estimation
├── db/                      # SQLAlchemy ORM + Alembic migrations
│   ├── models.py           # Run, Briefing, Finding, EvalRun, Action tables
│   ├── session.py          # session_scope context manager
│   └── migrations/         # Alembic versions
├── dashboard/               # Streamlit (temporary — being sunset)
│   └── app.py
├── observability/           # OpenTelemetry + pricing telemetry
│   ├── callbacks.py        # LangGraph → OTEL instrumentation
│   └── pricing.py          # LLM token tracking
├── config.py               # Pydantic Settings (env-driven)
├── logging.py              # structlog + OTEL setup
└── cli.py                  # Typer CLI: chandra {run, eval, render, …}
```

### Frontend — Next.js ops console (`frontend/`)

```
frontend/
├── app/
│   ├── layout.tsx           # Root HTML shell + OnboardingProvider
│   ├── providers.tsx        # Client-side context wrappers
│   ├── page.tsx             # Root → /onboarding redirect
│   ├── globals.css          # Theme tokens + Tailwind base
│   ├── onboarding/page.tsx  # OnboardingWizard host
│   ├── dashboard/page.tsx   # ChandraExperience host
│   ├── aws-tasks/page.tsx   # AWS task submission UI
│   ├── aws-permissions/page.tsx
│   ├── execution-review/page.tsx
│   ├── deployment/page.tsx
│   └── executions/page.tsx
├── components/
│   ├── AppNav.tsx
│   ├── OnboardingWizard.tsx        # Five-step provisioning flow
│   ├── ChandraExperience.tsx       # Full ops dashboard
│   ├── HumanApprovalCenter.tsx     # Approval center (polls /requests)
│   ├── WorkerActionExecutionCenter.tsx
│   └── PermissionsPage.tsx
├── services/
│   ├── api.ts               # REST client to FastAPI
│   └── mapping.ts           # Backend payload → UI shape
├── store/
│   ├── OnboardingContext.tsx # Identity + KRA + permissions
│   ├── agentProfile.ts      # Avatar catalog, employee ID
│   └── kraCatalog.ts        # KRA definitions + metrics
```

### FastAPI backend (at repo root)

```
├── fastapi_app.py             # FastAPI app — orchestrator + digital worker + copilot
├── app.py                     # Alternate entrypoint / Gradio app
├── run.py                     # Local dev launcher
├── digitalworker_agents/      # Multi-agent orchestrator
│   ├── observation_agent.py
│   ├── analyzer_agent.py
│   └── aws_execution_agent.py
├── copilot_agents/            # LangGraph chat surface
│   ├── graph.py
│   └── call_tools.py
├── tools/aws_cloud_tools/     # CloudWatch, Cost Explorer, GuardDuty, etc.
├── tools/jira_tools/          # Jira create/read
└── database/                  # SQLite checkpointer store
```

### Shared infrastructure

```
├── iac/
│   ├── synthetic_env/         # Terraform — seeds 10 known misconfigs
│   └── runtime/               # Terraform — runtime infrastructure
├── evals/
│   ├── seed_manifest.yaml     # Ground truth for eval scoring
│   ├── harness.py             # terraform apply → run → score → report
│   └── fixtures/              # Baseline JSONL fixtures
├── tests/
│   ├── conftest.py            # Shared fixtures (moto mocks)
│   ├── unit/                  # Fast unit tests (~1-2s)
│   └── integration/           # Need Docker/Postgres
├── scripts/
│   ├── smoke.{sh,ps1}         # End-to-end demo runner
│   ├── healthcheck.py         # Docker HEALTHCHECK probe
│   └── benchmark_llm.py       # LLM latency benchmark
└── docs/
    └── handover/              # Handover documentation
```

---

## 4. Import Conventions

### File header (every `.py` file)

```python
"""One-line module description.

Extended docstring with usage notes, invariants, and references.
"""

from __future__ import annotations

from typing import Any

from src.chandra.config import settings
from src.chandra.logging import get_logger

logger = get_logger(__name__)
```

### Import order (enforced by ruff)

1. `from __future__ import annotations`
2. Standard library (`os`, `json`, `datetime`, `uuid`, etc.)
3. Third-party (`pydantic`, `langgraph`, `sqlalchemy`, `boto3`, etc.)
4. Internal (`src.chandra.*`, `tools.*`, `digitalworker_agents.*`)
5. Blank line, then `logger = get_logger(__name__)`

### What NOT to import directly

| ❌ Don't import | ✅ Use instead |
|----------------|---------------|
| `boto3.client(...)` | `chandra.aws.client_factory.get_default_factory()` |
| `ChatBedrockConverse` | `src.chandra.llm.get_llm()` |
| `ChatOpenAI` | `src.chandra.llm.get_llm()` |
| `langchain_aws` (in detectors) | Nothing — detectors never call LLM |
| `os.environ` | `chandra.config.settings` |

---

## 5. LLM Provider Switching

### Architecture

The LLM abstraction layer lives in `src/chandra/llm/` and is the **single seam** through which every LLM call routes. The factory supports five provider aliases:

| Provider alias | Backend | LangChain class | Use case |
|----------------|---------|-----------------|----------|
| `bedrock` | Amazon Bedrock | `ChatBedrockConverse` | Production default (Claude Sonnet 4.5) |
| `vllm` | vLLM (OpenAI-compatible) | `ChatOpenAI` | Local inference, air-gapped |
| `openai` / `openai_compatible` | Any OpenAI-compatible server | `ChatOpenAI` | Together, Groq, TGI, LM Studio |
| `ollama` | Ollama daemon | `ChatOpenAI` (via `/v1` API) | Local dev, small models |

### Switching providers

Change one environment variable:

```bash
# .env — switch to local vLLM
LLM_PROVIDER=vllm
LLM_MODEL=google/gemma-4-12B-it-qat-w4a16-ct
VLLM_API_BASE=http://localhost:8000/v1
VLLM_API_KEY=not-needed

# Adjust token budgets for local LLM (16K context)
CHANDRA_TF_DOCS_MAX_CHARS=8000
CHANDRA_AWS_CTX_MAX_CHARS=6000
CHANDRA_MEMORY_MAX_CHARS=3000
CHANDRA_AGENT_MAX_INPUT_CHARS=30000

# Use json_schema for structured output (vLLM guided decoding)
CHANDRA_STRUCTURED_OUTPUT_METHOD=json_schema
```

### Factory call chain

```
build_chat_model(model, provider, **kwargs)
  ├─ provider="bedrock"  → ChatBedrockConverse(model_id, region_name, **kwargs)
  ├─ provider="vllm"     → ChatOpenAI(base_url=VLLM_API_BASE, model=VLLM_MODEL, **kwargs)
  ├─ provider="openai"   → ChatOpenAI(base_url=OPENAI_API_BASE, model=OPENAI_MODEL_NAME, **kwargs)
  └─ provider="ollama"   → ChatOpenAI(base_url={OLLAMA_HOST}/v1, model=OLLAMA_MODEL, **kwargs)

get_llm(model, **kwargs) → build_chat_model(...)              # legacy compatibility
get_llm_with_tools(tools, model, **kwargs) → bind_tools()     # tool-calling agents

build_chat_model_with_fallback(model, provider, **kwargs)
  → attempts primary provider, falls back to Bedrock on failure
```

### Provider layer (`providers.py`)

```python
from src.chandra.llm.providers import get_provider, VLLMProvider, BedrockProvider

# Business logic calls this:
llm = get_provider()  # reads LLM_PROVIDER from settings
response = llm.complete(system="You are a helpful assistant.", user="Hello!")
is_healthy = llm.health_check()
```

### Automatic fallback

`build_chat_model_with_fallback()` provides resilience:

1. Try the configured provider (from `LLM_PROVIDER`)
2. If it raises (connection refused, timeout, auth error), log a warning
3. If the failed provider was **not** Bedrock, try Bedrock as fallback
4. If Bedrock also fails, re-raise the original exception

---

## 6. Adding a New KRA

### Overview

Each KRA (Key Result Area) is a parallel observation branch in the LangGraph pipeline. The five existing KRAs are: Cost, Security, Compliance, Performance, Reliability.

### Step-by-step

#### 1. Create the detector module

Add a new file under `src/chandra/tools/`:

```python
"""src/chandra/tools/my_new_kra.py — Detector for MyNewKRA."""

from __future__ import annotations

from typing import Any

from src.chandra.aws.client_factory import get_default_factory
from src.chandra.logging import get_logger
from src.chandra.tools.base import BaseDetector

logger = get_logger(__name__)


class MyNewKRADetector(BaseDetector):
    """Detects findings for MyNewKRA."""

    def detect(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        client = get_default_factory().get_client("service_name")
        paginator = client.get_paginator("list_resources")
        findings = []
        for page in paginator.paginate():
            for resource in page.get("Resources", []):
                # Deterministic check — never call the LLM here
                findings.append({
                    "kra": "my_new_kra",
                    "severity": "high",
                    "resource_id": resource["Id"],
                    "message": "Description of the finding",
                    "evidence": resource,
                })
        return findings


def detect_my_new_kra_findings(context: dict[str, Any]) -> list[dict[str, Any]]:
    """Entry point called by the observation node."""
    return MyNewKRADetector().detect(context)
```

#### 2. Register the KRA in the graph

Edit `src/chandra/graphs/action_nodes.py`:

```python
# Add a new observation function
def observe_my_new_kra(state: ChandraState) -> dict[str, Any]:
    """Observe findings for MyNewKRA."""
    from src.chandra.tools.my_new_kra import detect_my_new_kra_findings
    findings = detect_my_new_kra_findings(state.get("context", {}))
    return {"raw_findings": {"my_new_kra": findings}}
```

#### 3. Add the node to the graph

Edit `src/chandra/graphs/chandra_graph.py`:

```python
# 1. Import the new node function
from src.chandra.graphs.action_nodes import (
    ...,
    observe_my_new_kra,
)

# 2. Add the node
graph.add_node("observe_my_new_kra", observe_my_new_kra)

# 3. Add it to the kra_supervisor fan-out targets
graph.add_conditional_edges(
    "kra_supervisor",
    _route_kra_workers,
    [
        "observe_cost",
        "observe_security",
        "observe_compliance",
        "observe_performance",
        "observe_reliability",
        "observe_my_new_kra",  # <-- add here
    ],
)

# 4. Add edge from the new observer to analyze
graph.add_edge("observe_my_new_kra", "analyze")
```

#### 4. Update the KRA routing logic

Edit `_route_kra_workers` in `action_nodes.py` to include the new KRA in the `Send(...)` fan-out.

#### 5. Add optional KRA context

If the new KRA needs LLM context, add it to `src/chandra/prompts/kra_context.md`.

#### 6. Write tests

```python
# tests/unit/test_my_new_kra_tools.py
def test_my_new_kra_detector(aws, client_factory, detector_context):
    """Test MyNewKRA detector with mocked AWS."""
    ...
```

#### 7. Update configuration

- Add the new KRA name to `selected_kras` in `ChandraState` if needed
- Update `src/chandra/config.py` if KRA-specific settings are needed
- Update `frontend/store/kraCatalog.ts` to display the new KRA in the UI

---

## 7. Adding a New Digital Worker Node

### Overview

The Digital Worker graph is a 15-node StateGraph in `src/chandra/digital_worker/graph.py`. Adding a new node follows a consistent pattern.

### Step-by-step

#### 1. Define the node function

Add to `src/chandra/digital_worker/graph.py`:

```python
def my_new_node(state: DigitalWorkerState) -> dict[str, Any]:
    """Description of what this node does.

    Invariants:
    - Deterministic (no LLM calls) unless explicitly approved
    - Reads from state, returns partial state update
    """
    logger.info("graph.my_new_node", request_id=state.get("request", {}).get("request_id"))
    # TODO: implement your logic
    return {"key": "value"}
```

#### 2. Register the node in the graph

In `build_digital_worker_graph()`:

```python
graph.add_node("my_new_node", my_new_node)
graph.add_edge("previous_node", "my_new_node")
graph.add_edge("my_new_node", "next_node")
```

#### 3. Update the state schema

If the node needs new state fields, add them to `DigitalWorkerState` in `src/chandra/digital_worker/state.py`.

#### 4. Write tests

```python
# tests/unit/test_digital_worker_graph.py
def test_my_new_node():
    """Test the new node's behavior."""
    state = DigitalWorkerState(...)
    result = my_new_node(state)
    assert "key" in result
```

### Determinism contract

Only `plan_resolution` may (indirectly, via the composer) invoke the LLM. `decision`, `execute_automation`, and every router in the Digital Worker graph are **deterministic**, mirroring the core graph's `decision_router` / `action_executor` / `escalation` invariant.

---

## 8. Writing Tests

### Test framework

Chandra uses **pytest** with **moto** for AWS mocking (no real AWS calls). Unit tests run in ~1-2 seconds. Integration tests require Docker/Postgres and are marked with `@pytest.mark.integration`.

### Test structure

```
tests/
├── conftest.py              # Shared fixtures
├── unit/                    # Fast unit tests (moto-mocked)
│   ├── test_cost_tools.py
│   ├── test_security_tools.py
│   ├── test_compliance_tools.py
│   ├── test_performance_tools.py
│   ├── test_reliability_tools.py
│   ├── test_decision_router.py
│   ├── test_kra_supervisor.py
│   ├── test_approval.py
│   ├── test_composer.py
│   ├── test_analyze_ranking.py
│   ├── test_action_executor_node.py
│   ├── test_action_executor_handlers.py
│   ├── test_observation_ingestion.py
│   ├── test_llm_providers.py
│   ├── test_digital_worker_graph.py
│   ├── test_digital_worker_intake.py
│   ├── test_digital_worker_planning.py
│   ├── test_execution_bridge.py
│   ├── test_execution_executor.py
│   ├── test_execution_planner.py
│   ├── test_execution_validator.py
│   ├── test_fastapi_intake.py
│   ├── test_cloudwatch_metrics.py
│   ├── test_assume_role.py
│   ├── test_organizations.py
│   ├── test_kra_context.py
│   ├── test_observability.py
│   ├── test_timeout.py
│   └── test_compliance_tools.py
└── integration/             # Need Docker/Postgres
    ├── test_chaos.py
    ├── test_cloudtrail_audit.py
    ├── test_compliance_models.py
    ├── test_config_compliance.py
    ├── test_encryption_checks.py
    └── test_security_models.py
```

### Available fixtures (from `tests/conftest.py`)

| Fixture | Type | Purpose |
|---------|------|---------|
| `aws` | `None` | moto `@mock_aws` context (all AWS calls mocked) |
| `cloudwatch` | boto3 client | Mocked CloudWatch client |
| `s3` | boto3 client | Mocked S3 client |
| `iam` | boto3 client | Mocked IAM client |
| `ec2` | boto3 client | Mocked EC2 client |
| `rds` | boto3 client | Mocked RDS client |
| `detector_context` | `DetectorContext` | Context for tool testing |
| `client_factory` | `ClientFactory` | Mocked AWS client factory |

### Writing a test

```python
"""Test the my_new_kra detector."""

from __future__ import annotations

from unittest.mock import patch

import pytest


def test_my_new_kra_detection(aws, client_factory, detector_context):
    """Test that the detector finds misconfigurations."""
    from src.chandra.tools.my_new_kra import detect_my_new_kra_findings

    # Arrange: set up mocked AWS resources
    # (moto automatically mocks boto3 calls within the `aws` fixture)

    # Act
    findings = detect_my_new_kra_findings(detector_context)

    # Assert
    assert len(findings) > 0
    assert findings[0]["kra"] == "my_new_kra"
```

### Best practices

- **One assertion per test** where practical — use parametrize for multiple inputs
- **Use `moto`** for all AWS calls — never hit real AWS in unit tests
- **Test edge cases** — empty results, pagination, throttling, errors
- **Test determinism** — verify `decision_router`, `action_executor`, `escalation` produce the same output for the same input
- **Test the LLM factory** — verify provider switching works with `test_llm_providers.py`

---

## 9. Running Tests

### Quick reference

```bash
# Full quality gate
make check

# All unit tests
uv run pytest tests/unit/ -v

# Single test file
uv run pytest tests/unit/test_decision_router.py -v

# Single test function
uv run pytest tests/unit/test_decision_router.py::TestDecisionRouter::test_critical_escalated -v

# By name pattern
uv run pytest tests/unit/ -k "decision_router" -v

# Skip integration tests (default for local dev)
uv run pytest tests/unit/ -m "not integration" -v

# Coverage for one module
uv run pytest tests/unit/ --cov=src/chandra/tools --cov-report=term-missing

# Stop on first failure
uv run pytest tests/unit/ -x -v

# Show print statements
uv run pytest tests/unit/ -v -s

# Run with pdb debugger
uv run pytest tests/unit/test_decision_router.py -v --pdb

# Show 10 slowest tests
uv run pytest tests/unit/ -v --durations=10

# Run in parallel
uv run pytest tests/unit/ -n auto

# Run integration tests only
uv run pytest tests/unit/ -m "integration" -v

# Run all tests (unit + integration)
uv run pytest tests/ -v

# Offline eval (no AWS/Terraform needed)
uv run python -m chandra.cli eval --fixture evals/fixtures/baseline_v1.jsonl
```

### Quality checks

```bash
# Linting
uv run ruff check src

# Formatting
uv run ruff format --check src

# Type checking
uv run mypy src --strict

# All checks
uv run ruff check src && uv run ruff format --check src && uv run mypy src --strict && uv run pytest -m "not integration"
```

---

## 10. Debugging

### Enable verbose logging

```bash
# .env
LOG_LEVEL=DEBUG
UVICORN_LOG_LEVEL=debug

# Or at runtime
uvicorn fastapi_app:app --host 0.0.0.0 --port 6001 --log-level debug
```

### Common debugging workflows

#### Backend won't start

```bash
# Check if .env is configured
grep -E "^(DATABASE_URL|LLM_PROVIDER|POSTGRES_URL)" .env

# Check if Postgres is running
docker compose ps postgres

# Check if dependencies are installed
uv sync --all-extras

# Check the error traceback
uvicorn fastapi_app:app --host 0.0.0.0 --port 6001
```

#### LLM calls failing

```bash
# Test the LLM endpoint directly
# For vLLM:
curl http://localhost:8000/v1/models

# For Bedrock:
aws bedrock list-foundation-models --region us-east-1

# Test a completion
curl http://localhost:8000/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"google/gemma-4-12B-it-qat-w4a16-ct","prompt":"Hello","max_tokens":10}'
```

#### Graph execution issues

```bash
# Check if the graph is paused at approval gate
curl http://localhost:6001/requests?status=awaiting_approval

# Check LangGraph checkpointer state
# Look for CheckpointError or fallback to MemorySaver in logs

# Test with a simpler graph configuration
# Compile with MemorySaver instead of Postgres checkpointer
```

#### Frontend debugging

```bash
# Check browser console for API errors
# Open DevTools → Network tab → look for failed requests to localhost:6001

# Verify NEXT_PUBLIC_API_URL
echo $NEXT_PUBLIC_API_URL

# Check if the backend is reachable from the browser
curl http://localhost:6001/health

# Clear Next.js cache
rm -rf frontend/.next
cd frontend && npm run dev
```

#### Postgres debugging

```bash
# Check if Postgres is accepting connections
docker compose exec postgres pg_isready -U chandra

# Connect to the database
docker compose exec -it postgres psql -U chandra

# \dt — list tables
# SELECT * FROM runs ORDER BY started_at DESC LIMIT 5;
# SELECT * FROM findings LIMIT 10;

# Check migration status
uv run alembic check
uv run alembic current
```

#### AWS debugging

```bash
# Verify AWS credentials
aws sts get-caller-identity

# List available profiles
aws configure list-profiles

# Test SNS topic
aws sns list-topics

# Check CloudWatch dashboards
aws cloudwatch list-dashboards
```

### Inspection with pdb

```bash
# Drop into debugger on test failure
uv run pytest tests/unit/test_decision_router.py -v --pdb

# Or add a breakpoint in code
breakpoint()  # Python 3.7+
```

### Log structure

The system uses `structlog` with JSON output. Key log fields:

| Field | Description |
|-------|-------------|
| `event` | Short event name (e.g., `graph.analyze.completed`) |
| `logger` | Module path (e.g., `src.chandra.graphs.action_nodes`) |
| `level` | `info`, `warning`, `error`, `debug` |
| `timestamp` | ISO 8601 timestamp |
| `exception` | Full traceback (on error) |

### Performance profiling

```bash
# Show 10 slowest tests
uv run pytest tests/unit/ -v --durations=10

# Coverage report
uv run pytest tests/unit/ --cov=src/chandra --cov-report=term-missing

# LLM benchmark
uv run python scripts/benchmark_llm.py
```

---

## Appendix: Quick Command Reference

| Task | Command |
|------|---------|
| Install deps | `make install` |
| Start Postgres | `make db-up` |
| Stop Postgres | `make db-down` |
| Run migrations | `make migrate` |
| Format code | `make fmt` |
| Lint code | `make lint` |
| Type check | `make type` |
| Run tests | `make test` |
| Full gate | `make check` |
| Run pipeline | `make run` |
| Run eval | `make eval` |
| Start dashboard | `make dashboard` |
| Start backend | `uvicorn fastapi_app:app --host 0.0.0.0 --port 6001 --reload` |
| Start frontend | `cd frontend && npm run dev` |
| Start vLLM | `vLLM_USE_FLASHINFER_SAMPLER=0 vllm serve ...` |
| Apply synthetic TF | `make tf-apply` |
| Destroy synthetic TF | `make tf-destroy` |
| Smoke test | `make smoke` |
| Clean caches | `make clean` |