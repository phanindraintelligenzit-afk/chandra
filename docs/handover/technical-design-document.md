# Chandra — Technical Design Document

> **Enterprise AI Cloud Operations Platform**
> Version: 1.0.0 | Branch: `feature/local-llm`

---

## Table of Contents

1. [Backend Architecture Overview](#1-backend-architecture-overview)
2. [LangGraph State Design (ChandraState)](#2-langgraph-state-design)
3. [Node Function Signatures & Reducer Patterns](#3-node-function-signatures--reducer-patterns)
4. [Digital Worker Schemas](#4-digital-worker-schemas)
5. [LLM Abstraction Layer](#5-llm-abstraction-layer)
6. [Execution Engine Design](#6-execution-engine-design)
7. [Escalation Design](#7-escalation-design)
8. [Database Schema (models.py)](#8-database-schema)
9. [API Design Patterns](#9-api-design-patterns)
10. [Frontend Component Architecture](#10-frontend-component-architecture)
11. [Security Design](#11-security-design)
12. [Observability Design](#12-observability-design)

---

## 1. Backend Architecture Overview

### Two-Service Model

Chandra is composed of **two backend services** sharing the same Python codebase:

| Service | Entry Point | Purpose |
|---------|------------|---------|
| **LangGraph Pipeline** | `src/chandra/graphs/chandra_graph.py` | Daily AWS Cloud Health Briefing — 5 KRA observers, LLM analysis, deterministic remediation, human approval gate |
| **FastAPI Backend** | `fastapi_app.py` (port 6001) | HTTP/WebSocket API consumed by the Next.js frontend and Streamlit dashboard. Hosts Digital Worker omnichannel intake, copilot chat, async job orchestration, and CRUD endpoints |

### Core Architectural Invariants

1. **LangGraph is the only orchestration framework.** No LangChain `AgentExecutor`. No `create_react_agent`. Use `StateGraph` + `Send(...)` for fan-out.
2. **The LLM never invents findings.** It only runs in `analyze` (ranking + rationale) and `compose_briefing` (narrative). `decision_router`, `action_executor`, and `escalation` are deterministic.
3. **Postgres writes only in the `persist` node and Alembic migrations.**
4. **Every boto3 list/describe call uses a paginator.**
5. **AWS clients are created via `chandra.aws.client_factory.get_default_factory()`.** Never `boto3.client(...)` directly.
6. **All LLM calls go through `src.chandra.llm.get_llm()`** — a single env var (`LLM_PROVIDER`) switches the entire runtime.

### LangGraph Pipeline Topology

```
START
  └─► onboard_account
        └─► ingest_observations
              └─► kra_supervisor
                    ├─► observe_cost
                    ├─► observe_security
                    ├─► observe_compliance
                    ├─► observe_performance
                    └─► observe_reliability
                          └─► analyze (LLM rank + dedup)
                                └─► decision_router (splits → pending_writes + auto_fixed)
                                      └─► action_executor (consumes auto_fixed)
                                            └─► escalation (publishes pending_writes to SNS)
                                                  └─► conditional:
                                                        pending_writes non-empty → approval_node → persist → END
                                                        else                   → persist → END
```

### Digital Worker Topology

```
START → receive_request → understand_request → classify_request
      → identify_platform → collect_context → root_cause_analysis
      → plan_resolution → risk_analysis → decision
      → { execute_automation | approval_gate | generate_guidance }
      → validate_result → update_tracker → notify → audit
      → persist → END
```

---

## 2. LangGraph State Design (ChandraState)

**File:** `src/chandra/graphs/state.py`

### ChandraState TypedDict

```python
class ChandraState(TypedDict, total=False):
    # — Identity & Routing —
    assume_role_arn: str | None
    run_id: str
    account_id: str
    regions: list[str]
    selected_kras: list[str]
    sns_topic_arn: str | None         # Seeded by onboard_account
    dry_run: bool                      # When False, action_executor makes real AWS calls

    # — Parallel observer outputs (merged via reducers) —
    inventory: Annotated[dict[str, list[dict[str, Any]]], merge_inventory]
    raw_findings: Annotated[dict[str, list[Finding]], merge_raw_findings]
    observations: Annotated[list[Observation], add]

    # — Analysis & Scoring —
    analyzed_findings: list[AnalyzedFinding]
    scorecard: dict[str, int]

    # — Remediation Pipeline —
    pending_writes: Annotated[list[ProposedWrite], add]
    auto_fixed: Annotated[list[ProposedWrite], add]
    action_results: Annotated[list[ActionResult], add]
    approvals: list[ApprovalDecision]

    # — Output —
    briefing_md: str
    briefing_json: dict[str, Any]
    escalation_result: dict[str, Any]
    errors: Annotated[list[dict[str, Any]], add]

    # — Cost Tracking —
    bedrock_input_tokens: int
    bedrock_output_tokens: int
    bedrock_cost_usd: float
```

### Design Rationale

- **`total=False`** allows partial dict returns from each node — LangGraph merges them into the full state.
- **`Annotated[T, reducer_fn]`** enables LangGraph's parallel fan-out merge: when five observer branches run concurrently, the reducer deep-merges per-KRA finding lists instead of clobbering.
- **`add` reducer** (from `operator.add`) appends to lists — used for `observations`, `pending_writes`, `auto_fixed`, `action_results`, `errors`.

### DigitalWorkerState TypedDict

**File:** `src/chandra/digital_worker/state.py`

```python
class DigitalWorkerState(TypedDict, total=False):
    # — Intake —
    source: str
    payload: dict[str, Any]
    job_id: str
    dry_run: bool

    # — Workflow Artifacts —
    request: CloudRequest
    intent: str
    classification: RequestClassification
    context: ContextBundle
    root_cause: RootCause
    plan: ResolutionPlan
    risk: RiskAssessment
    decision: ExecutionDecision
    approval: ApprovalRecord
    execution: ExecutionOutcome
    guidance_md: str
    validation: ValidationResult

    # — Append-only (add reducer) —
    tracker_updates: Annotated[list[TrackerUpdate], add]
    notifications: Annotated[list[NotificationResult], add]
    audit_trail: Annotated[list[AuditEvent], add]
    errors: Annotated[list[dict[str, Any]], add]

    status: str
    result: dict[str, Any]
```

---

## 3. Node Function Signatures & Reducer Patterns

### Reducer Functions

```python
def merge_raw_findings(
    left: dict[str, list[Finding]] | None,
    right: dict[str, list[Finding]] | None,
) -> dict[str, list[Finding]]:
    """Deep-merge per-KRA finding lists from parallel observers."""
    out = {k: list(v) for k, v in (left or {}).items()}
    for kra, items in (right or {}).items():
        out.setdefault(kra, []).extend(items)
    return out

def merge_inventory(
    left: dict[str, list[dict[str, Any]]] | None,
    right: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, list[dict[str, Any]]]:
    """Deep-merge resource_type -> [resource] inventory dicts."""
    out = {k: list(v) for k, v in (left or {}).items()}
    for rtype, items in (right or {}).items():
        out.setdefault(rtype, []).extend(items)
    return out
```

### Node Function Signatures

All nodes follow the pattern `(state: ChandraState) -> dict[str, Any]`:

| Node | Signature | Returns | LLM? |
|------|-----------|---------|------|
| `onboard_account` | `onboard_account(state) -> dict` | `{account_id, regions, raw_findings, errors}` | No |
| `ingest_observations` | `ingest_observations(state) -> dict` | `{observations, errors}` | No |
| `kra_supervisor` | `kra_supervisor(state) -> dict` | `{}` (routes via `Send`) | No |
| `observe_cost` | `observe_cost(state) -> dict` | `{raw_findings, errors}` | No |
| `observe_security` | `observe_security(state) -> dict` | `{raw_findings, errors}` | No |
| `observe_compliance` | `observe_compliance(state) -> dict` | `{raw_findings, errors}` | No |
| `observe_performance` | `observe_performance(state) -> dict` | `{raw_findings, errors}` | No |
| `observe_reliability` | `observe_reliability(state) -> dict` | `{raw_findings, errors}` | No |
| `analyze` | `analyze(state) -> dict` | `{analyzed_findings, scorecard}` | **Yes** |
| `decision_router` | `decision_router(state) -> dict` | `{pending_writes, auto_fixed}` | No |
| `action_executor_node` | `action_executor_node(state) -> dict` | `{action_results}` | No |
| `escalation_node` | `escalation_node(state) -> dict` | `{escalation_result}` | No |
| `compose_briefing` | `compose_briefing(state) -> dict` | `{briefing_md, briefing_json}` | **Yes** |
| `approval_node` | `approval_node(state) -> dict` | `{approvals}` | No |
| `persist` | `persist(state) -> dict` | `{}` | No |

### Fan-out Pattern (KRA Supervisor)

```python
def _route_kra_workers(state: ChandraState) -> list[Send]:
    """LG-07: Slim projection — only run_id, account_id, regions on the wire."""
    projection = {
        "run_id": state["run_id"],
        "account_id": state["account_id"],
        "regions": list(state.get("regions", [])),
    }
    selected = state.get("selected_kras", [])
    active_kras = [k for k in selected if k in KRAS_TO_RUN]
    return [Send(f"observe_{kra}", projection) for kra in active_kras]
```

### Conditional Routing

```python
def route_to_approval(state: ChandraState) -> str:
    pending = state.get("pending_writes", []) or []
    return "approval_node" if pending else "persist"
```

---

## 4. Digital Worker Schemas

**File:** `src/chandra/digital_worker/schemas.py`

### Request Envelope

```python
class CloudRequest(BaseModel):
    request_id: str          # UUID4 default
    source: RequestSource    # jira | slack | teams | email | rest_api | monitoring | cloudwatch | azure_monitor | gcp_monitoring | webhook
    external_id: str | None  # Channel-native identifier
    title: str
    description: str = ""
    priority: RequestPriority | None = None  # P1 | P2 | P3
    requester: str | None = None
    received_at: datetime    # UTC now default
    labels: list[str] = []
    raw_payload: dict[str, Any] = {}
```

### Classification

```python
class RequestClassification(BaseModel):
    category: RequestCategory   # incident | service_request | change_request | cost_optimization | security | compliance | performance | reliability | question | unknown
    platform: CloudPlatform     # aws | azure | gcp | kubernetes | unknown
    services: list[str] = []
    keywords: list[str] = []
    priority: RequestPriority = P3
    summary: str = ""
    confidence: float = 0.0     # 0.0–1.0
```

### Planning

```python
class ResolutionPlan(BaseModel):
    plan_id: str               # UUID4 default
    generated_by: str          # memory | llm | deterministic
    fingerprint: str           # Stable hash for memory lookups
    steps: list[ResolutionStep] = []
    rollback_steps: list[ResolutionStep] = []
    detector_id: str | None = None
    automation_available: bool = False
    notes: str = ""

class ResolutionStep(BaseModel):
    order: int
    action: str
    detail: str = ""
    command: str | None = None
    expected_outcome: str | None = None
```

### Risk & Decision

```python
class RiskAssessment(BaseModel):
    level: RiskLevel           # low | medium | high | critical
    score: int = 0             # 0–100
    factors: list[str] = []
    reversible: bool = True
    requires_approval: bool = True

class ExecutionDecision(BaseModel):
    mode: DecisionMode         # auto_execute | await_approval | engineer_guidance
    reason: str
```

### Execution & Validation

```python
class ExecutionOutcome(BaseModel):
    status: str = "skipped"    # executed | dry_run | skipped | failed
    dry_run: bool = True
    detail: str = ""
    step_results: list[dict] = []
    errors: list[str] = []
    execution_code: str = ""
    execution_logs: str = ""
    rollback_code: str = ""
    rollback_logs: str = ""
    sandbox_path: str | None = None
    pipeline_response: dict | None = None

class ValidationResult(BaseModel):
    passed: bool = False
    checks: list[ValidationCheck] = []
    verification_code: str = ""
    verification_logs: str = ""
```

### Terminal WorkflowResult

```python
class WorkflowResult(BaseModel):
    request: CloudRequest
    classification: RequestClassification
    root_cause: RootCause
    plan: ResolutionPlan
    risk: RiskAssessment
    decision: ExecutionDecision
    execution: ExecutionOutcome
    validation: ValidationResult
    tracker_updates: list[TrackerUpdate] = []
    notifications: list[NotificationResult] = []
    guidance_md: str = ""
    audit_trail: list[AuditEvent] = []
    status: str = "completed"
```

---

## 5. LLM Abstraction Layer

**Files:** `src/chandra/llm/__init__.py`, `src/chandra/llm/providers.py`

### Factory (__init__.py)

All LLM calls go through a single factory:

```python
def build_chat_model(model=None, provider=None, **kwargs) -> Any:
    """Build a LangChain chat model based on LLM_PROVIDER env var."""
```

**Supported Providers** (`LLM_PROVIDER` env var):

| Provider | Backend | Default Model |
|----------|---------|---------------|
| `bedrock` | `langchain_aws.ChatBedrockConverse` | `anthropic.claude-sonnet-4-5-20250929-v1:0` |
| `openai` | `langchain_openai.ChatOpenAI` | `OPENAI_MODEL_NAME` / `VLLM_MODEL` |
| `vllm` | `langchain_openai.ChatOpenAI` | `VLLM_MODEL` |
| `ollama` | `langchain_openai.ChatOpenAI` | `OLLAMA_MODEL` |

**Convenience wrappers:**

```python
def get_llm(model=None, **kwargs) -> Any
def get_llm_with_tools(tools=None, model=None, **kwargs) -> Any
def build_chat_model_with_fallback(model=None, provider=None, **kwargs) -> Any
```

### Provider Interface (providers.py)

The `BaseLLM` abstract class provides a uniform reasoning surface:

```python
class BaseLLM(ABC):
    provider: str = "bedrock"
    
    def __init__(self, model=None, params: GenerationParams | None = None)
    def _build(self) -> Any           # Abstract — construct the LangChain model
    def complete(self, system: str, user: str, **overrides) -> str
    def health_check(self) -> bool
```

**Concrete implementations:**

| Class | Provider Value |
|-------|---------------|
| `BedrockProvider` (aliased as `ClaudeProvider`) | `bedrock` |
| `VLLMProvider` | `vllm` |
| `OpenAICompatibleProvider` | `openai` |
| `OllamaProvider` | `ollama` |

**Usage:**

```python
from src.chandra.llm.providers import get_provider

llm = get_provider()  # Uses LLM_PROVIDER env var
reply = llm.complete(
    system="You are a cloud operations assistant.",
    user="Summarize the current findings."
)
```

### Generation Params

```python
@dataclass
class GenerationParams:
    temperature: float = 0.0
    top_p: float = 1.0
    max_tokens: int = 2048
    timeout_s: float = 60.0
    max_retries: int = 2
```

---

## 6. Execution Engine Design

### Overview

The execution engine has two parallel designs:

1. **Core Graph Remediation** (action_executor_node) — deterministic, pre-registered AWS handlers
2. **Digital Worker Automation** (ExecutionAgents) — LLM-driven code generation + execution

### Core Graph: Action Executor

**File:** `src/chandra/graphs/action_nodes/action_executor.py`

The `action_executor_node` loops over `state["auto_fixed"]` and dispatches each `ProposedWrite` to a registered handler via a **detector_id → handler registry**:

```python
_HANDLERS: dict[str, _Handler | _ObservationOnlyHandler] = {
    "SEC-001-public-s3": _Handler("public_s3", _s3_bucket_from_arn, _fix_public_s3_via),
    "SEC-002-open-sg-ssh": _Handler("open_security_group", _sg_id_from_arn, _fix_open_sg_via),
    "SEC-003-stale-key": _Handler("stale_iam_key", _iam_key_id_from_arn, _disable_iam_key_via),
    "SEC-009-kms-rotation": _Handler("kms_key_rotation", _kms_key_id_from_arn, _enable_kms_rotation_via),
    "COMP-005-s3-default-enc": _Handler("s3_default_encryption", _s3_bucket_from_arn, ...),
    "COMP-008-ebs-default-enc-off": _Handler("ebs_encryption_by_default", ...),
    # ... 34 total handlers (11 original + 16 new + 7 observation-only)
}
```

**34 registered handlers** across all 5 KRAs, including:
- **S3**: public access fix, default encryption, versioning, mandatory tags
- **Security**: open security groups, stale IAM keys, KMS rotation, GuardDuty archiving, Access Analyzer
- **Compliance**: CloudTrail multi-region, Config recorder, EBS encryption, RDS encryption
- **Performance**: idle EC2/RDS stop, instance type right-sizing, Lambda memory tuning
- **Reliability**: RDS Multi-AZ, backup plans, DLM policies, EC2→ASG migration

### Error Classification

The executor distinguishes between:

| Exception Type | Status | Example |
|---------------|--------|---------|
| `ClientError` with `_ALREADY_RESOLVED_ERROR_CODES` | `skipped` | NoSuchBucket, NotFoundException |
| `ClientError` with `_SKIP_STATE_CONFLICT_ERROR_CODES` | `skipped` | VolumeInUse, IncorrectInstanceState |
| `_SkippedRemediation` | `skipped` | AWS-managed resource |
| Any other exception | `failure` | Network error, auth failure |

### Digital Worker: ExecutionAgents

**File:** `digitalworker_agents/aws_execution_agent.py` (external module)

The `ExecutionAgents` orchestrator is an LLM-driven code generation engine:
1. **Planner** — generates an `ExecutionPlan` with typed steps (ActionInput, TerraformPlan, ScriptPlan)
2. **Executor** — runs each step, captures stdout/stderr
3. **Terraform** — applies/destroys infrastructure via Terraform
4. **Validator** — validates plan schema before execution
5. **Bridge** — adapts legacy `PipelineResponse` formats
6. **Verification** — post-execution health checks

Supports **HITL (Human-in-the-Loop)** pauses: when the agent needs clarification, it returns `statusCode=202` with questions, and resumes via `POST /orchestrate/{job_id}/resume`.

---

## 7. Escalation Design

**Files:** `src/chandra/escalation/schemas.py`, `src/chandra/escalation/publisher.py`

### Escalation Payload

```python
class EscalationPayload(BaseModel):
    finding_id: str
    resource_id: str
    severity: Literal["low", "medium", "high", "critical"]
    service: str
    region: str
    summary: str
    recommended_action: str
```

### SNSPublisher

```python
class SNSPublisher:
    def __init__(self, topic_arn: str, region: str = "us-east-1", factory=None)
    def publish(self, payload: EscalationPayload) -> EscalationResult
```

The publisher wraps messages in **AWS Chatbot Custom Notification format** for Slack/Teams delivery:

```json
{
  "version": "1.0",
  "source": "custom",
  "content": {
    "title": ":rotating_light: [CRITICAL] Escalation: SEC-001",
    "description": "...",
    "nextSteps": ["..."]
  }
}
```

### Escalation Flow

1. `decision_router` classifies each finding: `critical/high` → `pending_writes`, `medium/low/info` → `auto_fixed`
2. `escalation_node` publishes `pending_writes` to the SNS topic (from `state.sns_topic_arn`)
3. When `pending_writes` is non-empty, the graph routes through `approval_node` (human-in-the-loop interrupt)
4. On approval, `action_executor_node` executes the remediation

### Escalation Result

```python
class EscalationResult(BaseModel):
    status: str                # success | skipped | failed
    message_id: str | None = None
    error: str | None = None
```

---

## 8. Database Schema

**File:** `src/chandra/db/models.py`

### Tables

#### `runs`

| Column | Type | Constraints |
|--------|------|-------------|
| `id` | UUID → String(36) | PK, default uuid4 |
| `started_at` | DateTime(tz) | server_default=CURRENT_TIMESTAMP |
| `finished_at` | DateTime(tz) | nullable |
| `account_id` | String(32) | NOT NULL, indexed |
| `status` | String(16) | NOT NULL, default "running" |
| `errors_json` | JSONB | nullable |
| `bedrock_cost_usd` | Float | server_default=0.0 |

Relationships: `findings` (1:N), `briefing` (1:1), `eval_run` (1:1)

#### `findings`

| Column | Type | Constraints |
|--------|------|-------------|
| `id` | UUID → String(36) | PK |
| `run_id` | FK → runs.id | NOT NULL, CASCADE, indexed |
| `kra` | String(16) | NOT NULL, indexed |
| `severity` | String(16) | NOT NULL, indexed |
| `detector_id` | String(64) | NOT NULL, indexed |
| `resource_arn` | Text | NOT NULL |
| `resource_type` | String(64) | NOT NULL |
| `region` | String(32) | NOT NULL |
| `title` | Text | NOT NULL |
| `evidence_jsonb` | JSONB | NOT NULL |
| `recommendation` | Text | NOT NULL |
| `created_at` | DateTime(tz) | server_default=CURRENT_TIMESTAMP |

#### `briefings`

| Column | Type | Constraints |
|--------|------|-------------|
| `id` | UUID → String(36) | PK |
| `run_id` | FK → runs.id | NOT NULL, UNIQUE, CASCADE |
| `scorecard_jsonb` | JSONB | NOT NULL |
| `markdown_text` | Text | NOT NULL |
| `findings_count` | Integer | NOT NULL |
| `created_at` | DateTime(tz) | server_default=CURRENT_TIMESTAMP |

#### `cloud_requests` (Digital Worker audit)

| Column | Type | Constraints |
|--------|------|-------------|
| `id` | UUID → String(36) | PK |
| `request_id` | String(36) | NOT NULL, UNIQUE, indexed |
| `source` | String(32) | NOT NULL, indexed |
| `external_id` | Text | nullable |
| `title` | Text | NOT NULL |
| `category` | String(32) | NOT NULL, indexed |
| `platform` | String(16) | NOT NULL, indexed |
| `priority` | String(4) | NOT NULL |
| `risk_level` | String(16) | NOT NULL |
| `decision_mode` | String(32) | NOT NULL |
| `status` | String(32) | NOT NULL, default "completed" |
| `result_jsonb` | JSONB | NOT NULL |
| `audit_jsonb` | JSONB | NOT NULL |
| `received_at` | DateTime(tz) | NOT NULL |
| `completed_at` | DateTime(tz) | nullable |
| `created_at` | DateTime(tz) | server_default=CURRENT_TIMESTAMP |

#### `resolution_memory` (Execution plan cache)

| Column | Type | Constraints |
|--------|------|-------------|
| `id` | UUID → String(36) | PK |
| `fingerprint` | String(64) | NOT NULL, UNIQUE, indexed |
| `category` | String(32) | NOT NULL, indexed |
| `platform` | String(16) | NOT NULL, indexed |
| `title` | Text | NOT NULL |
| `plan_jsonb` | JSONB | NOT NULL |
| `hit_count` | Integer | NOT NULL, default 0 |
| `last_outcome` | String(32) | NOT NULL, default "unknown" |
| `created_at` | DateTime(tz) | server_default=CURRENT_TIMESTAMP |
| `updated_at` | DateTime(tz) | nullable |

#### `eval_runs`

| Column | Type | Constraints |
|--------|------|-------------|
| `id` | UUID → String(36) | PK |
| `run_id` | FK → runs.id | NOT NULL, UNIQUE, CASCADE |
| `recall_overall` | Float | NOT NULL |
| `recall_per_kra_jsonb` | JSONB | NOT NULL |
| `precision_overall` | Float | NOT NULL |
| `fp_count` | Integer | NOT NULL |
| `created_at` | DateTime(tz) | server_default=CURRENT_TIMESTAMP |

### Write Rule

**Only the `persist` node and Alembic migrations may write to these tables.** No other code path touches Postgres directly.

---

## 9. API Design Patterns

### Async Job Pattern

Long-running operations follow a consistent async pattern:

1. **Submit** → `POST /endpoint` returns `202 Accepted` with `{job_id, poll_url}`
2. **Poll** → `GET /jobs/status/{job_id}` returns `{status, progress, message, result, error}`

### Endpoint Categories

| Category | Pattern | Examples |
|----------|---------|---------|
| **Health** | Synchronous GET | `/health`, `/health/ready` |
| **Observability** | Async POST → poll | `/getAgentObservations`, `/getCostMetrics`, `/getDetectorIssues` |
| **Orchestration** | Async POST → poll | `/orchestrate`, `/orchestrate/simple` |
| **Digital Worker** | Async POST → poll | `/requests`, `/webhooks/{source}`, `/requests/{job_id}/approve` |
| **CRUD** | Synchronous REST | `/aws-tasks`, `/api/permission-sets`, `/api/executions` |
| **Copilot** | Synchronous POST | `/copilot/chat` |
| **Settings** | GET/POST | `/settings/digital-worker` |

### Response Envelope

All API responses follow a consistent envelope:

```json
{
  "status": "ok" | "error",
  "job_id": "...",           // For async submissions
  "poll_url": "/jobs/status/...",
  "result": {...},           // For synchronous responses
  "error": "..."             // On failure
}
```

---

## 10. Frontend Component Architecture

**Tech Stack:** Next.js 16 + React 18 + TypeScript + Tailwind CSS + Framer Motion

### Key Components

| Component | File | Purpose |
|-----------|------|---------|
| `ChandraExperience` | `components/ChandraExperience.tsx` | Main dashboard — live ops stream, incidents, cost monitoring, copilot, approvals |
| `HumanApprovalCenter` | `components/HumanApprovalCenter.tsx` | Polls `GET /requests`, renders approval/reject controls for Digital Worker requests |
| `WorkerActionExecutionCenter` | `components/WorkerActionExecutionCenter.tsx` | Legacy orchestration jobs monitoring |
| `OnboardingWizard` | `components/OnboardingWizard.tsx` | 5-step provisioning ceremony (name → avatar → role → maturity → KRAs → permissions → deploy) |

### Services Layer

**File:** `services/api.ts`

- `fetchAgentObservations(payload)` — POST → poll for observability pipeline
- `fetchCostMetrics(days)` — POST → poll for cost data
- `fetchDetectorIssues(search)` — GET → poll for detector scan
- `sendCopilotMessage(sessionId, message)` — POST to `/copilot/chat`
- `fetchBackendLogs(limit, offset)` — GET `/logs`
- `submitDigitalWorkerApproval(jobId, approved, comment)` — POST `/requests/{job_id}/approve`
- `listDigitalWorkerRequests(status)` — GET `/requests`
- `pollJobStatus(jobId, extractResult)` — Generic polling loop

### Store

| Store | File | Purpose |
|-------|------|---------|
| `OnboardingContext` | `store/OnboardingContext.tsx` | Wizard state: step, agent profile, permissions, KRAs |
| `agentProfile` | `store/agentProfile.ts` | Avatar definitions, agent metadata |
| `kraCatalog` | `store/kraCatalog.ts` | KRA definitions and metrics |

### Pages

| Route | File | Purpose |
|-------|------|---------|
| `/` | `app/page.tsx` | Main dashboard (ChandraExperience) |
| `/dashboard` | `app/dashboard/page.tsx` | Dashboard (alias) |
| `/onboarding` | `app/onboarding/page.tsx` | Onboarding wizard |

---

## 11. Security Design

### CORS Configuration

```python
allowed_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    os.getenv("FRONTEND_URL", ""),  # Production frontend
]
# Falls back to ["*"] if no specific origins configured
```

### Webhook Token Authentication

**File:** `fastapi_app.py` — `POST /webhooks/{source}`

```python
expected_token = os.getenv("CHANDRA_WEBHOOK_TOKEN")
if expected_token and x_chandra_webhook_token != expected_token:
    return JSONResponse(status_code=401, content={
        "status": "error", "message": "invalid or missing X-Chandra-Webhook-Token",
    })
```

- Optional: set `CHANDRA_WEBHOOK_TOKEN` env var to enable
- Header: `X-Chandra-Webhook-Token`
- Development: unset means unauthenticated intake

### SNS Authentication

Escalation messages are published to an SNS topic configured via `SNS_TOPIC_ARN` env var. AWS IAM credentials (from environment or AWS profile) authenticate the API calls.

### Slack URL Verification

```python
if source == "slack" and payload.get("type") == "url_verification":
    return JSONResponse(status_code=200, content={"challenge": payload.get("challenge")})
```

### Admin User Model

```python
ADMIN_USERS = {"phani", "admin"}
```

Used for CRUD ownership checks on AWS tasks, permission sets, and executions.

### Dry-Run Mode

- `state.dry_run = True` (default) — action_executor makes no real AWS calls
- `dry_run: false` in `CloudRequestSubmission` — approved automations perform real mutating calls
- Each `ExecutionOutcome` carries `dry_run: bool` for audit

---

## 12. Observability Design

### Tracing: `@traced_node` Decorator

**File:** `src/chandra/observability/__init__.py`

Every LangGraph node is wrapped with `@traced_node` which provides:

- **Structured logging** — node start/complete with full state input/output
- **Timeout enforcement** — SIGALRM-based on POSIX (Linux/macOS), skipped on Windows
- **Metric emission** — `NodeLatency` (Count) and `NodeErrors` (Count) to CloudWatch via `_emit_metric()`

```python
@traced_node
def onboard_account(state: ChandraState) -> dict[str, Any]:
    ...
```

### Token & Cost Tracking

**File:** `src/chandra/observability/callbacks.py`

```python
class UsageCapture(BaseCallbackHandler):
    def on_llm_end(self, response: LLMResult, **kwargs):
        # Captures prompt_tokens, completion_tokens, calculates cost
```

Used by `compose_briefing` and `analyze` to track Bedrock usage. Costs are persisted in `runs.bedrock_cost_usd`.

### Logging Infrastructure

**File:** `src/chandra/logging.py`

- Module-level `get_logger(__name__)` replaces all `print()` calls
- Structured logging with `structlog` context variables (run_id, account_id)
- `LogCapture` handler in `fastapi_app.py` buffers last 2000 log entries in memory
- `GET /logs` endpoint exposes the buffer for frontend consumption

### Configuration

**File:** `src/chandra/config.py`

```python
class Settings(BaseSettings):
    otel_endpoint: str | None = None        # OTEL_EXPORTER_OTLP_ENDPOINT
    otel_environment: str = "production"    # OTEL_ENVIRONMENT
    log_level: str = "INFO"                 # LOG_LEVEL
```

### Metric Emission

`_emit_metric()` pushes custom metrics to CloudWatch under the `Chandra` namespace:

```python
def _emit_metric(metric_name, value, unit="None", dimensions=None):
    # Thread-safe CloudWatch put_metric_data call
    # Metrics: NodeLatency, NodeErrors
```

### Environment Variables for Observability

| Variable | Purpose |
|----------|---------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OpenTelemetry collector endpoint |
| `OTEL_ENVIRONMENT` | Environment tag (production/staging/dev) |
| `LOG_LEVEL` | DEBUG/INFO/WARNING/ERROR |
| `LANGCHAIN_TRACING_V2` | LangSmith tracing toggle |
| `LANGFUSE_*` | LangFuse LLM observability |
| `AGENTOPS_API_KEY` | AgentOps monitoring |