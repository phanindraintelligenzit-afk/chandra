# Chandra Architecture Document

> **Project:** Chandra — Enterprise AI Cloud Operations Platform
> **Branch:** feature/local-llm
> **Last Updated:** 2026-07-30

---

## Table of Contents

1. [High-Level Architecture](#1-high-level-architecture)
2. [Component Diagram](#2-component-diagram)
3. [Deployment Diagram](#3-deployment-diagram)
4. [Sequence Diagram (Request → Approval → Execution)](#4-sequence-diagram)
5. [Runtime Flow](#5-runtime-flow)
6. [Data Flow](#6-data-flow)
7. [API Flow](#7-api-flow)
8. [LangGraph Flow (Core Graph)](#8-langgraph-flow-core-graph)
9. [Digital Worker Flow (15-Node Graph)](#9-digital-worker-flow)
10. [Execution Flow (Planner → Executor → Terraform → Validator → Verification)](#10-execution-flow)
11. [Local LLM Flow (Factory → Provider → Fallback → Token Budget)](#11-local-llm-flow)
12. [AWS Flow (Client Factory → Paginator → Detectors → SNS → Bedrock)](#12-aws-flow)
13. [Verification Flow](#13-verification-flow)
14. [Database Schema](#14-database-schema)
15. [Configuration & Environment](#15-configuration--environment)

---

## 1. High-Level Architecture

Chandra is a multi-layered enterprise AI cloud operations platform deployed as a set of Docker containers. The system comprises six major components communicating over HTTP/HTTPS and internal Docker networking.

```mermaid
graph TB
    subgraph Internet["Internet"]
        USER["👤 End User / Operator"]
        WEBHOOK_SRC["🌐 External Webhooks<br/>Slack / Teams / Jira / Email"]
    end

    subgraph Docker["Docker Compose — chandra-network"]
        subgraph FrontendTier["Frontend Tier"]
            NGINX["Nginx<br/>Port 80 / 443<br/>Reverse Proxy"]
            FE["Next.js 16 + React 18<br/>Operations Console<br/>Port 3000"]
        end

        subgraph BackendTier["Backend Tier"]
            API["FastAPI Backend<br/>Port 6001<br/>2412 lines<br/>CORS enabled"]
            GRADIO["Gradio UI<br/>Port 7861<br/>Legacy Streamlit"]
        end

        subgraph LangGraphTier["LangGraph Orchestration"]
            CORE_GRAPH["Core KRA Pipeline<br/>StateGraph<br/>12 nodes<br/>Postgres checkpointer"]
            DW_GRAPH["Digital Worker<br/>15-node StateGraph<br/>Omnichannel intake"]
        end

        subgraph LLMTier["LLM Layer"]
            LLM_FACTORY["LLM Factory<br/>src/chandra/llm/"]
            BEDROCK["Amazon Bedrock<br/>Claude Sonnet 4.5"]
            OPENAI["OpenAI / vLLM<br/>Compatible API"]
            OLLAMA["Ollama<br/>Local LLM"]
        end

        subgraph AWSTier["AWS Cloud"]
            SNS["SNS Topic<br/>Escalations & Notifications"]
            CW["CloudWatch<br/>Alarms & Metrics"]
            DETECTORS["Detector Suite<br/>5 KRA Detectors"]
        end

        subgraph DataTier["Data Tier"]
            PG["PostgreSQL 16<br/>Port 5432<br/>SQLAlchemy ORM"]
            REDIS["Redis (future)<br/>Caching"]
        end
    end

    USER -->|HTTP 80/443| NGINX
    NGINX --> FE
    NGINX -->|/api/backend/*| API
    WEBHOOK_SRC -->|Webhooks| API

    FE -->|REST / WebSocket| API
    API --> CORE_GRAPH
    API --> DW_GRAPH

    CORE_GRAPH --> LLM_FACTORY
    DW_GRAPH --> LLM_FACTORY
    LLM_FACTORY --> BEDROCK
    LLM_FACTORY --> OPENAI
    LLM_FACTORY --> OLLAMA

    CORE_GRAPH --> DETECTORS
    CORE_GRAPH --> SNS
    CORE_GRAPH --> PG
    DW_GRAPH --> PG
    DETECTORS --> CW

    API --> PG
    API --> SNS
```

### Key Design Principles

| Principle | Description |
|-----------|-------------|
| **LangGraph-only orchestration** | No LangChain `AgentExecutor` or `create_react_agent`. All workflows are `StateGraph` + `Send(...)` fan-out. |
| **Deterministic routing nodes** | `decision_router`, `action_executor`, `escalation` are purely deterministic — no LLM calls. |
| **Single LLM abstraction** | Every LLM call goes through `src.chandra.llm.get_llm()` factory. Never import providers directly. |
| **Read-only detectors** | Detectors never call mutating AWS APIs. Writes go through `action_executor_node` + `escalation` queue. |
| **Postgres writes only in persist** | Only the `persist` node writes to the database. All other nodes are read-only. |
| **Paginated AWS calls** | Every boto3 list/describe call uses a paginator — no silent truncation. |

---

## 2. Component Diagram

### 2.1 FastAPI Backend (`fastapi_app.py` — 2412 lines)

```mermaid
graph TB
    subgraph FastAPI["FastAPI Application — Port 6001"]
        LIFESPAN["Lifespan<br/>Alembic migrations"]
        CORS["CORS Middleware"]

        subgraph Routes["HTTP Routes"]
            HEALTH["GET /health<br/>Liveness probe"]
            READY["GET /health/ready<br/>Readiness probe"]
            REQUESTS["POST /requests<br/>Digital Worker intake"]
            WEBHOOKS["POST /webhooks/{source}<br/>10 channel adapters"]
            APPROVE["POST /requests/{job_id}/approve<br/>Human approval"]
            COPILOT["POST /copilot/chat<br/>LLM chat"]
            ORCHESTRATE["POST /orchestrate<br/>Execution Agents"]
            JOBS["GET /jobs/status/{job_id}<br/>Async job polling"]
            LOGS["GET /logs<br/>Capture buffer"]
            CUSTOM_KRAS["GET/PUT /customKras"]
            SETTINGS["GET/POST /settings/digital-worker"]
            AWS_TASKS["CRUD /aws-tasks<br/>AWS task definitions"]
        end

        subgraph Workers["Background Workers"]
            TP["ThreadPoolExecutor<br/>8 workers"]
            BG_L["Background Event Loop<br/>Shared asyncio loop"]
        end

        subgraph JobStore["In-Memory Job Store"]
            JS["_job_store<br/>Dict[str, JobStatus]<br/>RLock synchronized"]
            LB["_log_buffer<br/>Last 2000 log entries"]
        end

        subgraph Agents["Agent Orchestrators"]
            OBS_AGENT["AwsObservabilityAgent<br/>KRA pipeline"]
            ANALYZER["AnalyzerAgent<br/>Action analysis"]
            EXEC_AGENT["ExecutionAgents<br/>3709 lines<br/>Terraform executor"]
            COPILOT_AGENT["Copilot Agent<br/>LangGraph chat"]
            DW["Digital Worker<br/>15-node graph"]
        end
    end

    HEALTH --> LIFESPAN
    READY --> LIFESPAN
    REQUESTS --> DW
    WEBHOOKS --> DW
    APPROVE --> DW
    COPILOT --> COPILOT_AGENT
    ORCHESTRATE --> EXEC_AGENT
    ORCHESTRATE --> OBS_AGENT
    OBS_AGENT --> TP
    EXEC_AGENT --> TP
    DW --> TP
```

### 2.2 Core LangGraph Pipeline (`src/chandra/graphs/`)

```mermaid
graph TB
    subgraph CoreGraph["Core KRA Observation Pipeline"]
        subgraph State["State Management"]
            CS["ChandraState<br/>TypedDict<br/>total=False"]
            REDUCERS["Reducers<br/>merge_raw_findings<br/>merge_inventory<br/>add (list concat)"]
            CP["Checkpointer<br/>PostgresSaver<br/>interrupt_before→approval_node"]
        end

        subgraph Nodes["12 Graph Nodes"]
            OA["onboard_account<br/>Resolves account + regions"]
            IO["ingest_observations<br/>CloudWatch + EventBridge"]
            KS["kra_supervisor<br/>Send() fan-out"]
            OC["observe_cost<br/>Cost Explorer"]
            OS["observe_security<br/>GuardDuty, Config..."]
            OCP["observe_compliance<br/>AWS Config, IAM..."]

            OP["observe_performance<br/>CloudWatch metrics"]
            OR["observe_reliability<br/>Health, Trusted Advisor"]
            AN["analyze<br/>LLM ranking + dedup"]
            DR["decision_router<br/>Deterministic classify"]
            AE["action_executor<br/>Auto-fix low risk"]
            ES["escalation<br/>SNS publish high risk"]
            CB["compose_briefing<br/>LLM narrative"]
            AP["approval_node<br/>Human interrupt gate"]
            PS["persist<br/>Postgres write"]
        end

        subgraph Edges["Edge Topology"]
            direction LR
            START --> OA --> IO --> KS
            KS -->|Send| OC
            KS -->|Send| OS
            KS -->|Send| OCP
            KS -->|Send| OP
            KS -->|Send| OR
            OC --> AN
            OS --> AN
            OCP --> AN
            OP --> AN
            OR --> AN
            AN --> DR --> AE --> ES
            ES -->|pending? yes| AP --> PS
            ES -->|pending? no| PS
            PS --> END
        end
    end
```

### 2.3 Digital Worker (`src/chandra/digital_worker/`)

```mermaid
graph TB
    subgraph DW["Digital Worker — 15-Node LangGraph"]
        subgraph Intake["Intake Layer"]
            RR["receive_request<br/>Normalize payload"]
            UR["understand_request<br/>Extract intent"]
            CR["classify_request<br/>Category + priority"]
            IP["identify_platform<br/>AWS / Azure / GCP"]
        end

        subgraph Analysis["Analysis Layer"]
            CC["collect_context<br/>Context bundle"]
            RCA["root_cause_analysis<br/>Derive root cause"]
            PR["plan_resolution<br/>Memory ▸ LLM ▸ plan"]
            RA["risk_analysis<br/>Assess risk score"]
        end

        subgraph Decision["Decision Layer"]
            DE["decision<br/>Execute / Approve / Guidance"]
            AG["approval_gate<br/>Human interrupt"]
            EA["execute_automation<br/>ExecutionAgents"]
            GG["generate_guidance<br/>Engineer guidance"]
        end

        subgraph Finalization["Finalization Layer"]
            VR["validate_result<br/>Verify execution"]
            UT["update_tracker<br/>Jira update"]
            NT["notify<br/>SNS + channels"]
            AU["audit<br/>WorkflowResult"]
            PE["persist<br/>Postgres"]
        end
    end

    subgraph IntakeChannels["Omnichannel Intake"]
        SLACK["Slack Events API"]
        TEAMS["Microsoft Teams"]
        EMAIL["Email"]
        JIRA["Jira Webhook"]
        WEBHOOK["Generic Webhook"]
        REST["REST API"]
        CW2["CloudWatch Alarm"]
        AZURE["Azure Monitor"]
        GCP["GCP Monitoring"]
    end

    SLACK --> RR
    TEAMS --> RR
    EMAIL --> RR
    JIRA --> RR
    WEBHOOK --> RR
    REST --> RR
    CW2 --> RR
    AZURE --> RR
    GCP --> RR

    RR --> UR --> CR --> IP
    IP --> CC --> RCA --> PR --> RA
    RA --> DE
    DE -->|AUTO_EXECUTE| EA
    DE -->|AWAIT_APPROVAL| AG
    DE -->|GENERATE_GUIDANCE| GG
    AG -->|approved| EA
    AG -->|rejected| GG
    EA --> VR
    GG --> VR
    VR --> UT --> NT --> AU --> PE
```

### 2.4 LLM Layer (`src/chandra/llm/`)

```mermaid
graph TB
    subgraph LLMLayer["LLM Abstraction Layer"]
        FACTORY["build_chat_model()<br/>Factory function"]
        GET["get_llm()<br/>get_llm_with_tools()"]
        FALLBACK["build_chat_model_with_fallback()<br/>Auto-fallback to Bedrock"]

        subgraph Providers["Supported Providers"]
            BEDROCK["bedrock<br/>ChatBedrockConverse<br/>Sonnet 4.5<br/>Region: us-east-1<br/>Timeout: 60s"]
            VLLM["openai / vllm<br/>ChatOpenAI<br/>Configurable base_url<br/>Max retries: 2"]
            OLLAMA["ollama<br/>ChatOpenAI (/v1)<br/>Configurable host<br/>Local inference"]
        end

        subgraph Config["Configuration"]
            LLM_PROVIDER["LLM_PROVIDER env var<br/>Default: bedrock"]
            MODEL_ID["bedrock_model_id<br/>vllm_model<br/>ollama_model"]
            API_BASE["vllm_api_base<br/>ollama_host<br/>openai_api_base"]
            API_KEY["vllm_api_key<br/>openai_api_key"]
        end
    end

    FACTORY --> BEDROCK
    FACTORY --> VLLM
    FACTORY --> OLLAMA
    FALLBACK --> FACTORY
    FALLBACK -->|on failure| BEDROCK
    FACTORY --> Config
```

---

## 3. Deployment Diagram

```mermaid
graph TB
    subgraph EC2["AWS EC2 Instance<br/>t3.large / m6i.large<br/>Ubuntu 22.04"]
        subgraph DockerCompose["Docker Compose"]

            subgraph NginxContainer["nginx:alpine"]
                NGINX_P["Port 80 → 3000 (frontend)<br/>Port 443 → 6001 (backend API)<br/>SSL termination<br/>Reverse proxy"]
            end

            subgraph FrontendContainer["node:22-alpine"]
                FE_P["Next.js 16<br/>Port 3000<br/>Static export + SSR<br/>NEXT_PUBLIC_API_URL"]
            end

            subgraph BackendContainer["chandra-app<br/>(multi-stage build)"]
                API_P["Uvicorn<br/>4 workers<br/>Port 6001<br/>FastAPI app"]
                EXEC_AGENTS["ExecutionAgents<br/>Terraform executor<br/>LLM orchestrator"]
                LANGGRAPH["LangGraph graphs<br/>Core + Digital Worker"]
            end

            subgraph GradioContainer["chandra-app"]
                GRADIO_P["Gradio UI<br/>Port 7861<br/>Legacy dashboard"]
            end

            subgraph PostgresContainer["postgres:16-alpine"]
                PG_P["PostgreSQL 16<br/>Port 5432<br/>chandra DB<br/>Alembic managed"]
            end

        end

        subgraph AWS_Services["AWS Services"]
            SNS_TOPIC["SNS Topic<br/>chandra-escalations<br/>Port 443 (HTTPS)"]
            BEDROCK_SVC["Amazon Bedrock<br/>Claude Sonnet 4.5<br/>Port 443 (HTTPS)"]
            CLOUDWATCH["CloudWatch<br/>Alarms + Metrics<br/>Port 443 (HTTPS)"]
            IAM_ROLE["IAM Role<br/>Instance Profile<br/>Read-only access"]
        end
    end

    subgraph External["External"]
        USERS["Operators / Engineers"]
        WEBHOOKS["Jira / Slack / Teams"]
    end

    USERS -->|HTTP| NGINX_P
    WEBHOOKS -->|HTTPS| NGINX_P
    NGINX_P --> FE_P
    NGINX_P --> API_P
    API_P --> PG_P
    API_P --> SNS_TOPIC
    API_P --> BEDROCK_SVC
    API_P --> CLOUDWATCH
    API_P --> IAM_ROLE
    FE_P --> API_P
    GRADIO_P --> API_P
```

### Docker Compose Services

| Service | Image | Port | Command | Depends On | Limits |
|---------|-------|------|---------|------------|--------|
| `postgres` | postgres:16-alpine | 5434:5432 | default | — | — |
| `backend` | chandra-app (build .) | 6001:6001 | uvicorn 4 workers | postgres (healthy) | mem: 4g / 2g |
| `frontend` | node:22-alpine | 3000:3000 | npm start | backend | — |
| `nginx` | nginx:alpine | 80, 443 | default | backend, frontend | — |
| `gradio` | chandra-app | 7861:7861 | uv run app.py | backend | — |

### Multi-Stage Dockerfile

| Stage | Base | Purpose |
|-------|------|---------|
| `frontend-builder` | node:22-alpine | `npm ci` + `npm run build` |
| `backend-builder` | python:3.12-slim | `uv sync --frozen --no-dev` |
| `runtime` | python:3.12-slim | Combines both builds + nginx + healthcheck |

---

## 4. Sequence Diagram

A complete request-to-approval-to-execution flow through the entire system.

```mermaid
sequenceDiagram
    participant User as 👤 Operator
    participant Nginx as Nginx
    participant FE as Next.js Console
    participant API as FastAPI Backend
    participant DW as Digital Worker Graph
    participant LLM as LLM Factory
    participant PG as PostgreSQL
    participant Jira as Jira / Tracker
    participant SNS as SNS Topic
    participant Exec as ExecutionAgents
    participant Terraform as Terraform / AWS

    User->>Nginx: HTTP Request
    Nginx->>FE: Proxy to frontend
    Nginx->>API: /api/backend/*
    User->>API: POST /webhooks/jira (or POST /requests)

    Note over API,DW: === DIGITAL WORKER INTAKE ===
    API->>DW: invoke(source, payload, job_id)
    DW->>DW: receive_request (normalize)
    DW->>DW: understand_request (extract intent)
    DW->>DW: classify_request
    DW->>DW: identify_platform

    Note over DW,LLM: === ANALYSIS PHASE ===
    DW->>DW: collect_context (AWS resources)
    DW->>DW: root_cause_analysis
    DW->>LLM: plan_resolution (LLM planning)
    LLM-->>DW: ResolutionPlan
    DW->>DW: risk_analysis

    Note over DW: === DECISION PHASE ===
    DW->>DW: decision (deterministic)
    DW->>DW: approval_gate → interrupt()
    API-->>User: 202 Accepted, poll /jobs/status/{id}

    User->>API: GET /jobs/status/{id}
    API-->>User: {"status": "awaiting_approval", "approval_request": {...}}

    Note over User,API: === HUMAN APPROVAL ===
    User->>API: POST /requests/{job_id}/approve {"approved": true}
    API->>DW: Command(resume=approval)
    DW->>DW: approval_gate resumes

    Note over DW,Terraform: === EXECUTION PHASE ===
    DW->>Exec: execute_automation (ExecutionAgents)
    Exec->>Exec: Planning (LLM generates Terraform)
    Exec->>Terraform: terraform init & apply
    Terraform-->>Exec: stdout/stderr
    Exec-->>DW: ExecutionOutcome

    Note over DW: === FINALIZATION ===
    DW->>DW: validate_result (verifier)
    DW->>Jira: update_tracker (comment + status)
    DW->>SNS: notify (push notification)
    DW->>DW: audit (WorkflowResult)
    DW->>PG: persist (CloudRequestRecord)

    API-->>User: Approval completed, result available
```

---

## 5. Runtime Flow

```mermaid
flowchart LR
    subgraph Startup["Application Startup"]
        A1["FastAPI lifespan starts"]
        A2["Alembic upgrade head"]
        A3["Build Copilot Agent graph"]
        A4["Build Digital Worker graph"]
        A5["Start bg event loop thread"]
        A6["Ready on port 6001"]
    end

    subgraph RequestFlow["Request Processing Flow"]
        B1["HTTP request arrives"]
        B2["CORS check + routing"]
        B3{"Async job?"}
        B4["Queue in _job_store"]
        B5["Submit to ThreadPoolExecutor"]
        B6["Return 202 Accepted"]
        B7["Worker thread runs"]
        B8["Update job progress"]
        B9["Complete / fail → store result"]
    end

    subgraph GraphFlow["LangGraph Execution Flow"]
        C1["graph.invoke(initial_state)"]
        C2["Checkpointer serializes"]
        C3["Node function runs"]
        C4["State updated via reducers"]
        C5{"interrupt_before?"}
        C6["Halt, store checkpoint"]
        C7["Wait for Command(resume=...)"]
        C8["Continue from checkpoint"]
    end

    A1 --> A2 --> A3 --> A4 --> A5 --> A6
    A6 --> B1 --> B2 --> B3
    B3 -->|yes| B4 --> B5 --> B6
    B3 -->|no| B7
    B5 --> B7 --> B8 --> B9
    B7 --> C1 --> C2 --> C3 --> C4 --> C5
    C5 -->|yes| C6 --> C7 --> C8 --> C4
    C5 -->|no| C9["Continue to next node"]
```

### Threading Model

```
FastAPI Thread Pool (uvicorn workers)
        │
        ├── ThreadPoolExecutor (max: 8)
        │       ├── Worker 1: _run_observations_task
        │       ├── Worker 2: _run_detector_task
        │       ├── Worker 3: _run_orchestration_task
        │       ├── Worker 4: _run_digital_worker_task
        │       ├── Worker 5: _resume_digital_worker_task
        │       └── Worker 6-8: available
        │
        └── Background Event Loop Thread (daemon)
                └── asyncio.run_coroutine_threadsafe()
                    for async AWS operations
```

---

## 6. Data Flow

```mermaid
flowchart TB
    subgraph Input["Data Sources"]
        AWS_IN["AWS APIs<br/>CloudWatch, Config,<br/>GuardDuty, Cost Explorer,<br/>Health, Trusted Advisor"]
        WEBHOOK_IN["Webhook Payloads<br/>Jira / Slack / Teams<br/>Email / REST"]
    end

    subgraph Processing["Processing Pipeline"]
        DET["Detectors<br/>5 KRA modules<br/>Paginated boto3 calls"]
        OBS["Observations<br/>CloudWatch alarms<br/>EventBridge rules"]
        CLASS["Classifier<br/>Deterministic<br/>ML-based"]
        LLM_P["LLM Processing<br/>Analyze (rank + dedup)<br/>Compose (narrative)"]
        ROUTER["Router<br/>Deterministic<br/>Low-risk ↔ High-risk"]
    end

    subgraph Storage["Data Stores"]
        PG_STORE["PostgreSQL 16<br/>• runs<br/>• findings<br/>• briefings<br/>• eval_runs<br/>• cloud_requests<br/>• resolution_memory"]
        MEM["In-Memory<br/>• _job_store<br/>• _log_buffer<br/>• LangGraph state"]
        FILE["File System<br/>• customKras.json<br/>• aws_tasks.json<br/>• aws_permissions.json<br/>• digital_worker_config.json<br/>• Sandbox directories"]
    end

    subgraph Output["Data Outputs"]
        BRIEFING["Daily Cloud Health<br/>Briefing (MD + JSON)"]
        SNS_OUT["SNS Notifications<br/>Escalations<br/>Chatbot messages"]
        NOTIF["Multi-channel<br/>Notifications"]
        TICKET["Jira Ticket<br/>Updates"]
    end

    AWS_IN --> DET
    AWS_IN --> OBS
    WEBHOOK_IN --> CLASS
    DET --> LLM_P
    OBS --> LLM_P
    CLASS --> LLM_P
    LLM_P --> ROUTER
    ROUTER -->|auto_fixed| AE["action_executor"]
    ROUTER -->|pending| ES["escalation → SNS"]
    LLM_P --> BRIEFING
    AE --> PG_STORE
    ES --> SNS_OUT
    LLM_P --> PG_STORE
    CLASS --> PG_STORE
    SNS_OUT --> NOTIF
    PROCESSING_RESULT["Workflow Result"] --> TICKET
```

### State Shape — `ChandraState` (Core Graph)

| Field | Type | Reducer | Description |
|-------|------|---------|-------------|
| `run_id` | `str` | — | Unique run identifier |
| `account_id` | `str` | — | AWS account ID |
| `regions` | `list[str]` | — | Active AWS regions |
| `selected_kras` | `list[str]` | — | KRA modules to execute |
| `sns_topic_arn` | `str \| None` | — | SNS topic for escalations |
| `dry_run` | `bool` | — | When False, mutations execute |
| `raw_findings` | `dict[str, list[Finding]]` | `merge_raw_findings` | Per-KRA findings |
| `observations` | `list[Observation]` | `add` | CloudWatch + EventBridge |
| `analyzed_findings` | `list[AnalyzedFinding]` | — | Ranked/deduped findings |
| `scorecard` | `dict[str, int]` | — | Per-KRA scores |
| `pending_writes` | `list[ProposedWrite]` | `add` | High-risk → escalate |
| `auto_fixed` | `list[ProposedWrite]` | `add` | Low-risk → auto-execute |
| `action_results` | `list[ActionResult]` | `add` | Execution results |
| `approvals` | `list[ApprovalDecision]` | — | Human decisions |
| `briefing_md` | `str` | — | Markdown briefing |
| `briefing_json` | `dict` | — | JSON briefing |
| `errors` | `list[dict]` | `add` | Error records |
| `bedrock_cost_usd` | `float` | — | LLM cost tracker |

### State Shape — `DigitalWorkerState`

| Field | Type | Reducer | Description |
|-------|------|---------|-------------|
| `source` | `str` | — | Channel source name |
| `payload` | `dict` | — | Raw channel payload |
| `job_id` | `str` | — | Async job ID |
| `dry_run` | `bool` | — | When False, mutations execute |
| `request` | `CloudRequest` | — | Normalized request |
| `intent` | `str` | — | One-line intent |
| `classification` | `RequestClassification` | — | Category, platform, priority |
| `context` | `ContextBundle` | — | Collected resource context |
| `root_cause` | `RootCause` | — | Identified root cause |
| `plan` | `ResolutionPlan` | — | Execution plan |
| `risk` | `RiskAssessment` | — | Risk score + level |
| `decision` | `ExecutionDecision` | — | Execute / Approve / Guidance |
| `approval` | `ApprovalRecord` | — | Human approval decision |
| `execution` | `ExecutionOutcome` | — | Execution result |
| `guidance_md` | `str` | — | Engineer guidance markdown |
| `validation` | `ValidationResult` | — | Verification result |
| `tracker_updates` | `list[TrackerUpdate]` | `add` | Jira ticket updates |
| `notifications` | `list[NotificationResult]` | `add` | Notification results |
| `audit_trail` | `list[AuditEvent]` | `add` | Audit log |
| `errors` | `list[dict]` | `add` | Error records |
| `status` | `str` | — | Workflow status |
| `result` | `dict` | — | Terminal WorkflowResult |

---

## 7. API Flow

### Complete Endpoint Inventory

```mermaid
graph TB
    subgraph FastAPI["FastAPI — Port 6001"]

        subgraph Health["Health & Readiness"]
            H1["GET /health<br/>→ {status: ok}<br/>Liveness probe"]
            H2["GET /health/ready<br/>→ Component status<br/>Readiness probe<br/>200 ok / 503 degraded"]
        end

        subgraph DigitalWorker["Digital Worker — Omnichannel Intake"]
            DW1["POST /requests<br/>→ CloudRequestSubmission<br/>→ 202 Accepted<br/>REST API channel"]
            DW2["POST /webhooks/{source}<br/>→ Dict payload<br/>→ 202 Accepted<br/>source: jira, slack, teams,<br/>email, monitoring,<br/>cloudwatch, azure_monitor,<br/>gcp_monitoring, webhook"]
            DW3["POST /requests/{job_id}/approve<br/>→ ApprovalSubmission<br/>→ 202 Accepted<br/>Human approval gate"]
            DW4["GET /requests<br/>?status=&kraCode=<br/>→ List of requests<br/>With counts per status"]
            DW5["GET /requests/{job_id}<br/>→ Full request detail<br/>Including approval payload"]
            DW6["GET /jobs/status/{job_id}<br/>→ JobStatusResponse<br/>Shared polling endpoint"]
        end

        subgraph ExecutionAgents["Execution Agents"]
            EA1["POST /orchestrate<br/>→ OrchestrateRequest<br/>→ 202 Accepted<br/>Long-running orchestration"]
            EA2["GET /orchestrate/status/{job_id}<br/>→ JobStatusResponse<br/>Poll execution progress"]
            EA3["POST /orchestrate/{job_id}/resume<br/>→ ResumeRequest<br/>Resume HITL pause"]
            EA4["POST /orchestrate/stop/{job_id}<br/>→ Stop orchestration<br/>Kill threads + clean sandbox"]
            EA5["GET /orchestrate/logs/{job_id}<br/>→ Log file download"]
        end

        subgraph Observations["KRA Observations"]
            O1["POST /getAgentObservations<br/>→ PipelineRequest<br/>Full KRA pipeline<br/>11 AWS tools"]
            O2["GET /getDetectorIssues<br/>→ Async scan<br/>All detectors"]
            O3["POST /getPredefinedKraIssues<br/>→ selected_kras<br/>Specific KRA detectors"]
            O4["POST /getCostMetrics<br/>→ cost summary<br/>Cost Explorer"]
            O5["POST /getCloudWatchMetrics<br/>→ metrics fetch<br/>Async polling"]
        end

        subgraph Analysis["Action Analysis"]
            A1["POST /analyzeActions<br/>→ AnalyzerRequest<br/>LLM analyze + Jira tickets"]
        end

        subgraph Copilot["Copilot Chat"]
            C1["POST /copilot/chat<br/>→ sessionId + message<br/>→ reply string"]
        end

        subgraph Misc["Management"]
            M1["GET/PUT /customKras<br/>→ custom KRAs CRUD"]
            M2["GET/POST /settings/digital-worker<br/>→ Digital Worker config"]
            M3["GET /logs<br/>?limit=&offset=<br/>→ In-memory log buffer"]
            M4["GET /download_sandbox<br/>?path=<br/>→ ZIP download"]
            M5["POST /destroy_sandbox<br/>→ terraform destroy"]
            M6["POST /delete_sandbox<br/>→ Delete sandbox dir"]
            M7["GET /aws/regions<br/>→ Available regions"]
            M8["CRUD /aws-tasks<br/>→ Task definitions"]
            M9["GET /aws-tasks/resource-types<br/>→ Resource type catalog"]
            M10["CRUD /aws-permissions<br/>→ Permission management"]
        end
    end

    subgraph Auth["Authentication & CORS"]
        CORS["CORS Middleware<br/>Origins: localhost:3000,<br/>127.0.0.1:3000, FRONTEND_URL<br/>Methods: *<br/>Headers: *"]
        TOKEN["Optional webhook auth<br/>CHANDRA_WEBHOOK_TOKEN<br/>Header: X-Chandra-Webhook-Token"]
    end
```

### API Response Patterns

| Pattern | Status | Description |
|---------|--------|-------------|
| **Sync** | 200 | Immediate result |
| **Async** | 202 | `{job_id, status: "accepted", poll_url}` |
| **Awaiting Approval** | 200 | `{status: "awaiting_approval", approval_request: {...}}` |
| **HITL Pause** | 202 | `{statusCode: 202, status: "needs_clarification", questions: [...]}` |
| **Poll** | 200 | `{job_id, status, progress, result, error, ...}` |
| **Error** | 4xx/5xx | `{status: "error", message/exception}` |
| **Degraded** | 503 | `{status: "degraded", components: {postgres: "unavailable", ...}}` |

### Supported Webhook Sources

| Source | Adapter | Key Fields |
|--------|---------|-----------|
| `jira` | `_from_jira()` | issue.key, fields.summary, fields.description |
| `slack` | `_from_slack()` | event.text, event.user, event.ts |
| `teams` | Teams adapter | text, from |
| `email` | Email adapter | subject, body, from |
| `monitoring` | Generic | title, description, severity |
| `cloudwatch` | CW adapter | AlarmName, NewStateValue |
| `azure_monitor` | Azure adapter | — |
| `gcp_monitoring` | GCP adapter | — |
| `webhook` | Generic | — |
| `rest_api` | Direct | title, description, priority |

---

## 8. LangGraph Flow (Core Graph)

### Complete Graph Topology

```mermaid
graph TB
    START((START))
    END_TERM((END))

    subgraph Init["Initialization"]
        OA["onboard_account<br/>• Validate run<br/>• Resolve account_id<br/>• Discover regions<br/>• Seed inventory"]
        IO["ingest_observations<br/>• Describe CloudWatch alarms<br/>• List EventBridge rules<br/>• Paginated calls"]
    end

    subgraph FanOut["KRA Fan-Out (parallel via Send)"]
        KS["kra_supervisor<br/>• Returns list[Send()]<br/>• Projection: run_id, account_id, regions"]
        OC["observe_cost<br/>• Cost Explorer API<br/>• Budget alerts<br/>• Paginated"]
        OS["observe_security<br/>• GuardDuty findings<br/>• Security Hub<br/>• IAM audit<br/>• Paginated"]
        OCP["observe_compliance<br/>• AWS Config rules<br/>• Encryption checks<br/>• CloudTrail audit<br/>• Paginated"]
        OP["observe_performance<br/>• CloudWatch metrics<br/>• EC2 CPU/Network<br/>• Paginated"]
        OR["observe_reliability<br/>• Health events<br/>• Trusted Advisor<br/>• Paginated"]
    end

    subgraph Analysis["Analysis"]
        AN["analyze<br/>• deterministic_rank()<br/>• score_findings()<br/>• LLM ranks + dedups<br/>• Per-KRA scorecard"]
    end

    subgraph Routing["Deterministic Routing"]
        DR["decision_router<br/>• Classifies each finding<br/>• low→auto_fixed<br/>• high→pending_writes<br/>• severity-based decision"]
    end

    subgraph AutoFix["Auto-Fix (Low Risk)"]
        AE["action_executor_node<br/>• Consumes auto_fixed<br/>• Registered handlers<br/>• dry_run by default<br/>• Returns ActionResult"]
    end

    subgraph Escalation["Escalation"]
        ES["escalation_node<br/>• SNS Publisher<br/>• Chatbot format<br/>• Per-finding payload<br/>• Success/skip/fail"]
    end

    subgraph Briefing["Briefing Generation"]
        CB["compose_briefing<br/>• LLM executive summary<br/>• Markdown renderer<br/>• JSON output<br/>• Metadata injection"]
    end

    subgraph Gate["Human-in-the-Loop"]
        COND{"route_to_approval<br/>pending_writes<br/>non-empty?"}
        AP["approval_node<br/>• interrupt() halts graph<br/>• Returns ApprovalDecision<br/>• resumed via Command"]
    end

    subgraph Persist["Persistence"]
        PS["persist<br/>• Write Run row<br/>• Write Finding rows<br/>• Write Briefing row<br/>• Idempotent"]
    end

    START --> OA --> IO --> KS
    KS --> OC
    KS --> OS
    KS --> OCP
    KS --> OP
    KS --> OR
    OC --> AN
    OS --> AN
    OCP --> AN
    OP --> AN
    OR --> AN
    AN --> DR --> AE --> ES
    ES --> COND
    COND -->|yes| AP --> PS
    COND -->|no| PS
    PS --> END_TERM
```

### Node Details

| # | Node | Type | Description | AI Call | Writes DB |
|---|------|------|-------------|---------|-----------|
| 1 | `onboard_account` | Deterministic | Validate run, resolve account + regions | No | No |
| 2 | `ingest_observations` | Deterministic | Pull CloudWatch + EventBridge state | No | No |
| 3 | `kra_supervisor` | Deterministic | Fan-out dispatcher via `Send()` | No | No |
| 4 | `observe_cost` | Deterministic | Cost Explorer + Budgets | No | No |
| 5 | `observe_security` | Deterministic | GuardDuty, Security Hub, IAM | No | No |
| 6 | `observe_compliance` | Deterministic | Config rules, encryption, CloudTrail | No | No |
| 7 | `observe_performance` | Deterministic | CloudWatch metrics | No | No |
| 8 | `observe_reliability` | Deterministic | Health events, Trusted Advisor | No | No |
| 9 | `analyze` | **LLM-powered** | Rank + dedup + scorecard | **Yes** | No |
| 10 | `decision_router` | Deterministic | Severity-based classification | No | No |
| 11 | `action_executor_node` | Deterministic | Execute registered fix handlers | No | No |
| 12 | `escalation_node` | Deterministic | Publish to SNS | No | No |
| 13 | `compose_briefing` | **LLM-powered** | Executive summary + narrative | **Yes** | No |
| 14 | `approval_node` | Deterministic | Human interrupt gate | No | No |
| 15 | `persist` | Deterministic | Write to Postgres | No | **Yes** |

### Key Architectural Invariant

> **Only `analyze` and `compose_briefing` may call the LLM.**
> `decision_router`, `action_executor_node`, and `escalation` are **strictly deterministic**.
> Only the `persist` node writes to PostgreSQL.

### Checkpoint Flow

```mermaid
flowchart LR
    subgraph PostgresCheckpointer["PostgresSaver Checkpoint Flow"]
        A["Node starts<br/><br/>"] --> B["State read from<br/>checkpointer<br/>(thread_id + checkpoint_ns)"]
        B --> C["Node function<br/>executes"]
        C --> D["State written to<br/>checkpointer<br/>(serialized to checkpoints table)"]
        D --> E{"interrupt_before?"}
        E -->|yes| F["Halt execution<br/>Store pending checkpoint<br/>Return control to caller"]
        E -->|no| G["Continue to<br/>next node"]
    end

    F --> H["External caller invokes<br/>Command(resume=...)"]
    H --> I["Read checkpoint<br/>Resume execution<br/>Node re-runs"]
    I --> C
```

### Slim Projection (LG-07 Optimization)

```
Full ChandraState (before LG-07):
  {run_id, account_id, regions, raw_findings: {...500 items...}, 
   inventory: {...200 resources...}, observations: [...], ...}

Slim Projection (after LG-07):
  {run_id, account_id, regions}
  → Only 3 fields on the wire per Send()
  → ~4-5x smaller checkpoint rows for 1k-resource accounts
  → Reducers merge results back into full state
```

---

## 9. Digital Worker Flow

### 15-Node Graph Topology

```mermaid
graph TB
    START((START))
    END_TERM((END))

    subgraph Phase1["Phase 1: Intake & Understanding"]
        RR["receive_request<br/>• Normalize channel payload<br/>• Create CloudRequest<br/>• Deterministic adapters"]
        UR["understand_request<br/>• Extract intent<br/>• Identify explicit resources<br/>• Deterministic"]
        CR["classify_request<br/>• Category determination<br/>• Priority mapping (P1/P2/P3)<br/>• Deterministic classifier"]
        IP["identify_platform<br/>• Resolve AWS/Azure/GCP<br/>• Fallback from unknown<br/>• Deterministic"]
    end

    subgraph Phase2["Phase 2: Context & Analysis"]
        CC["collect_context<br/>• ContextCollector<br/>• Resource discovery<br/>• May call AWS APIs"]
        RCA["root_cause_analysis<br/>• Derive root cause<br/>• Deterministic ruleset<br/>• ML enhancement"]
        PR["plan_resolution<br/>• Memory lookup<br/>• LLM planning<br/>• Deterministic fallback"]
        RA["risk_analysis<br/>• assess_risk()<br/>• Score + level<br/>• requires_approval flag"]
    end

    subgraph Phase3["Phase 3: Decision & Execution"]
        DE["decision<br/>• evaluate_decision()<br/>• 3 modes<br/>• Deterministic"]
        COND1{"route_decision"}
        AG["approval_gate<br/>• interrupt()<br/>• Human decision<br/>• Approved → execute<br/>• Rejected → guidance"]
        EA["execute_automation<br/>• ExecutionAgents<br/>• Terraform + scripts<br/>• Dry run support"]
        GG["generate_guidance<br/>• render_guidance()<br/>• Engineer instructions<br/>• Skip execution"]
        COND2{"route_approval"}
    end

    subgraph Phase4["Phase 4: Finalization"]
        VR["validate_result<br/>• verify_execution()<br/>• ValidationChecks<br/>• Pass/fail status"]
        UT["update_tracker<br/>• Jira ticket update<br/>• Comment + status<br/>• Deterministic"]
        NT["notify<br/>• dispatch_all()<br/>• Slack/Teams/Email<br/>• SNS for P1/critical"]
        AU["audit<br/>• WorkflowResult<br/>• Terminal summary<br/>• Full audit trail"]
        PS["persist<br/>• CloudRequestRecord<br/>• resolution_memory<br/>• Only Postgres write"]
    end

    START --> RR --> UR --> CR --> IP
    IP --> CC --> RCA --> PR --> RA
    RA --> DE
    DE --> COND1
    COND1 -->|AUTO_EXECUTE| EA
    COND1 -->|AWAIT_APPROVAL| AG
    COND1 -->|GENERATE_GUIDANCE| GG
    AG --> COND2
    COND2 -->|approved| EA
    COND2 -->|rejected| GG
    EA --> VR
    GG --> VR
    VR --> UT --> NT --> AU --> PS
    PS --> END_TERM
```

### Decision Modes

| Mode | Condition | Action |
|------|-----------|--------|
| `AUTO_EXECUTE` | Low risk, automation available | Execute immediately |
| `AWAIT_APPROVAL` | High risk or destructive operation | Pause for human approval |
| `GENERATE_GUIDANCE` | No automation, or rejected | Produce engineer guidance |

### Notification Channels

| Channel | Implementation | Trigger |
|---------|---------------|---------|
| SNS | `notify_sns()` via `SNSPublisher` | P1 or CRITICAL/HIGH risk |
| Slack | `channels.dispatch_all()` | All workflows |
| Teams | `channels.dispatch_all()` | All workflows |
| Email | `channels.dispatch_all()` | All workflows |
| Jira Comment | `update_request_ticket()` | Terminal state |

---

## 10. Execution Flow

### Planner → Executor → Terraform → Validator → Verification

```mermaid
flowchart TB
    subgraph Planning["Planning Phase"]
        PLAN["plan_resolution<br/>• Memory check (fingerprint)<br/>• LLM generates steps<br/>• Deterministic fallback<br/>• ResolutionPlan"]
        RISK["risk_analysis<br/>• Score: 0.0 – 1.0<br/>• Level: LOW / MED / HIGH / CRITICAL<br/>• requires_approval flag"]
        DECIDE["decision<br/>• Execute / Approve / Guidance<br/>• Based on risk + automation"]
    end

    subgraph Execution["Execution Phase"]
        AG_EXEC["approval_gate<br/>• interrupt() with plan details<br/>• Wait for human decision"]
        EXEC["execute_automation<br/>• Instantiate ExecutionAgents<br/>• Map plan → ActionInput<br/>• Thread-safe integration"]
        DICT["ActionInput<br/>• actionName, actionDescription<br/>• service, priorityLevel<br/>• steps, jiraUrl"]
    end

    subgraph Orchestrator["ExecutionAgents Orchestrator (3709 lines)"]
        OA_INIT["Initialize<br/>• max_iterations<br/>• job_id<br/>• Thread registration"]
        OA_LOOP["Main Loop<br/>• Generate → Execute → Review<br/>Up to max_iterations"]
        OA_GEN["LLM Generation<br/>• Write Terraform/scripts<br/>• Code review pass"]
        OA_EXEC["Shell Execution<br/>• Terraform init & apply<br/>• CLI commands<br/>• timeout: 300s"]
        OA_HITL["HITL Check<br/>• Pause for clarification<br/>• Return 202<br/>• Store action_dict"]
    end

    subgraph Terraform["Terraform Lifecycle"]
        TF_INIT["terraform init<br/>• Provider setup<br/>• Module download"]
        TF_PLAN["terraform plan<br/>• Dry-run preview<br/>• Resource diff"]
        TF_APPLY["terraform apply<br/>• -auto-approve<br/>• Real resources<br/>• State persisted"]
        TF_DESTROY["terraform destroy<br/>• Cleanup<br/>• -auto-approve<br/>• POST /destroy_sandbox"]
    end

    subgraph Validation["Validation & Finalization"]
        VERIFY["verify_execution<br/>• ValidateResult<br/>• Check passed/failed"]
        TRACKER["update_tracker<br/>• Jira comment<br/>• Status update"]
        SANDBOX["Sandbox<br/>• ZIP download<br/>• Auto-clean on stop<br/>• Manual delete"]
    end

    PLAN --> RISK --> DECIDE
    DECIDE -->|await_approval| AG_EXEC -->|approved| EXEC
    DECIDE -->|auto_execute| EXEC
    EXEC --> DICT --> OA_INIT --> OA_LOOP
    OA_LOOP --> OA_GEN --> OA_EXEC
    OA_EXEC --> OA_HITL
    OA_HITL -->|needs clarification| HITL["Return 202 + questions"]
    OA_HITL -->|continue| OA_LOOP
    OA_EXEC --> TF_INIT --> TF_PLAN --> TF_APPLY
    OA_LOOP --> VERIFY --> TRACKER
    TF_APPLY --> SANDBOX
```

### Orchestration Job Lifecycle

```
Pending ──► Running ──► Completed
                │
                ├──► Failed
                │
                ├──► Stopped (by user)
                │
                └──► needs_clarification (HITL pause)
                        │
                        └──► Resume with answers
                                │
                                ├──► Completed
                                └──► needs_clarification (again)
```

---

## 11. Local LLM Flow

### Factory → Provider → Fallback → Token Budget

```mermaid
flowchart TB
    subgraph Entry["Entry Points"]
        GET_LLM["get_llm(model, **kwargs)"]
        GET_LLM_TOOLS["get_llm_with_tools(tools, model, **kwargs)"]
        GET_FALLBACK["build_chat_model_with_fallback(model, provider, **kwargs)"]
    end

    subgraph Factory["LLM Factory — build_chat_model()"]
        CFG["Read config:<br/>settings.llm_provider<br/>settings.bedrock_model_id<br/>settings.vllm_api_base<br/>settings.ollama_host"]
        SWITCH{"Provider match"}
    end

    subgraph Bedrock["Bedrock Provider"]
        BR_CREATE["ChatBedrockConverse<br/>model_id<br/>region_name: us-east-1<br/>timeout: 60s"]
        BR_OK["✓ Return model"]
    end

    subgraph OpenAI["OpenAI / vLLM Provider"]
        OAI_CHECK{"Has base_url?"}
        OAI_ERR["✗ ValueError<br/>Requires VLLM_API_BASE"]
        OAI_CREATE["ChatOpenAI<br/>base_url<br/>api_key<br/>model<br/>timeout: 60s<br/>max_retries: 2"]
        OAI_OK["✓ Return model"]
    end

    subgraph Ollama["Ollama Provider"]
        OLL_CHECK{"Has model?"}
        OLL_ERR["✗ ValueError<br/>Requires OLLAMA_MODEL"]
        OLL_CREATE["ChatOpenAI(/v1)<br/>base_url + /v1<br/>api_key: ollama<br/>model"]
        OLL_OK["✓ Return model"]
    end

    subgraph Fallback["Auto-Fallback"]
        FALL_TRIGGER{"Provider init failed?"}
        FALL_BEDROCK["Fallback to Bedrock<br/>build_chat_model(bedrock)"]
        FALL_OK["✓ Return fallback model"]
        FALL_ERR["✗ Bedrock also failed<br/>Raise exception"]
    end

    subgraph TokenBudget["Token Budget Management"]
        TB_INPUT["bedrock_input_tokens<br/>Tracked in state"]
        TB_OUTPUT["bedrock_output_tokens<br/>Tracked in state"]
        TB_COST["bedrock_cost_usd<br/>Accumulated in state"]
        TB_PERSIST["Persisted to<br/>runs.bedrock_cost_usd"]
    end

    GET_LLM --> CFG
    GET_LLM_TOOLS --> |bind_tools| GET_LLM
    GET_FALLBACK --> CFG

    CFG --> SWITCH
    SWITCH -->|"bedrock"| BR_CREATE --> BR_OK
    SWITCH -->|"openai / openai_compatible / vllm"| OAI_CHECK
    OAI_CHECK -->|yes| OAI_CREATE --> OAI_OK
    OAI_CHECK -->|no| OAI_ERR
    SWITCH -->|"ollama"| OLL_CHECK
    OLL_CHECK -->|yes| OLL_CREATE --> OLL_OK
    OLL_CHECK -->|no| OLL_ERR

    BR_CREATE --> FALL_TRIGGER
    OAI_CREATE --> FALL_TRIGGER
    OLL_CREATE --> FALL_TRIGGER
    FALL_TRIGGER -->|no failure| DONE["✓ Provider ready"]
    FALL_TRIGGER -->|exception| FALL_BEDROCK
    FALL_BEDROCK --> FALL_OK
    FALL_BEDROCK --> FALL_ERR

    BR_OK --> TB_INPUT --> TB_OUTPUT --> TB_COST --> TB_PERSIST
```

### Supported Provider Configurations

| Provider | Env Var | Model Env | API Base Env | Notes |
|----------|---------|-----------|-------------|-------|
| `bedrock` | `LLM_PROVIDER=bedrock` | `BEDROCK_MODEL_ID` | — | Default, Claude Sonnet 4.5 |
| `openai` | `LLM_PROVIDER=openai` | `OPENAI_MODEL_NAME` | `OPENAI_API_BASE` | Direct OpenAI API |
| `vllm` | `LLM_PROVIDER=vllm` | `VLLM_MODEL` | `VLLM_API_BASE` | Self-hosted vLLM |
| `ollama` | `LLM_PROVIDER=ollama` | `OLLAMA_MODEL` | `OLLAMA_HOST` | Local via `/v1` endpoint |

### Fallback Behavior

```python
# Pseudocode for fallback chain
try:
    model = build_chat_model(provider=user_provider)
except Exception:
    if user_provider != "bedrock":
        model = build_chat_model(provider="bedrock")  # Fallback
    else:
        raise  # Bedrock itself failed
```

---

## 12. AWS Flow

### Client Factory → Paginator → Detectors → SNS → Bedrock

```mermaid
flowchart TB
    subgraph ClientFactory["AwsClientFactory"]
        CF_INIT["AwsClientFactory()<br/>• profile<br/>• default_region<br/>• max_attempts (adaptive retry)<br/>• retry_mode"]
        CF_CACHE["Client Cache<br/>• Per (service, region)<br/>• Thread-safe (Lock)<br/>• Singleton session"]
        CF_ROLE["assume_role()<br/>• STS AssumeRole<br/>• Returns new factory<br/>• Cached per (role_arn, name)"]
        CF_CLIENT["client(service, region)<br/>• boto3.client()<br/>• Adaptive retry config<br/>• user_agent: chandra/0.1"]
    end

    subgraph Paginator["Paginator Pattern"]
        PAG_CALL["paginate(client, method)<br/>• Generic generator<br/>• Wraps get_paginator()"]
        PAG_LOOP["for page in paginator.paginate(**kwargs):<br/>    yield from page[...]"]
        PAG_YIELD["Yields items<br/>• No silent truncation<br/>• Handles all services"]
    end

    subgraph Detectors["KRA Detector Suite"]
        DET_COST["cost.run_all()<br/>• Cost Explorer<br/>• Budgets<br/>• Paginated"]
        DET_SEC["security.run_all()<br/>• GuardDuty<br/>• Security Hub<br/>• IAM Credential Report<br/>• Paginated"]
        DET_COMP["compliance.run_all()<br/>• AWS Config<br/>• Encryption checks<br/>• CloudTrail audit<br/>• Paginated"]
        DET_PERF["performance.run_all()<br/>• CloudWatch metrics<br/>• EC2 CPU/Network<br/>• RDS connections<br/>• Paginated"]
        DET_REL["reliability.run_all()<br/>• Health events<br/>• Trusted Advisor<br/>• Paginated"]
    end

    subgraph SNSFlow["SNS Escalation Flow"]
        SNS_PUB["SNSPublisher<br/>• topic_arn<br/>• region<br/>• AwsClientFactory"]
        SNS_MSG["Chatbot message format<br/>• version: 1.0<br/>• source: custom<br/>• title + description<br/>• nextSteps"]
        SNS_SEND["sns.publish()<br/>• TopicArn<br/>• Message (JSON)<br/>• Subject"]
        SNS_RESULT["Result<br/>• success → message_id<br/>• skipped → topic not found<br/>• failed → exception"]
    end

    subgraph BedrockFlow["Bedrock LLM Flow"]
        BR_MODEL["ChatBedrockConverse<br/>• model_id<br/>• region_name<br/>• timeout: 60s"]
        BR_INVOKE["LLM invocation<br/>• analyze (ranking + dedup)<br/>• compose_briefing (narrative)<br/>• plan_resolution (Digital Worker)"]
        BR_COST["Cost tracking<br/>• input_tokens<br/>• output_tokens<br/>• cost_usd"]
    end

    subgraph AWS_Services["AWS Services"]
        CE["AWS Cost Explorer"]
        BUDGETS["AWS Budgets"]
        GD["Amazon GuardDuty"]
        SH["AWS Security Hub"]
        IAM_SVC["AWS IAM"]
        CONFIG["AWS Config"]
        CT["AWS CloudTrail"]
        CW_METRICS["Amazon CloudWatch"]
        HEALTH["AWS Health"]
        TA["Trusted Advisor"]
        ORG["AWS Organizations"]
    end

    CF_CLIENT --> CE
    CF_CLIENT --> BUDGETS
    CF_CLIENT --> GD
    CF_CLIENT --> SH
    CF_CLIENT --> IAM_SVC
    CF_CLIENT --> CONFIG
    CF_CLIENT --> CT
    CF_CLIENT --> CW_METRICS
    CF_CLIENT --> HEALTH
    CF_CLIENT --> TA
    CF_CLIENT --> ORG

    CE --> PAG_CALL
    BUDGETS --> PAG_CALL
    GD --> PAG_CALL
    SH --> PAG_CALL
    IAM_SVC --> PAG_CALL
    CONFIG --> PAG_CALL
    CT --> PAG_CALL
    CW_METRICS --> PAG_CALL
    HEALTH --> PAG_CALL
    TA --> PAG_CALL
    ORG --> PAG_CALL

    PAG_CALL --> PAG_LOOP --> PAG_YIELD

    PAG_YIELD --> DET_COST
    PAG_YIELD --> DET_SEC
    PAG_YIELD --> DET_COMP
    PAG_YIELD --> DET_PERF
    PAG_YIELD --> DET_REL

    DET_COST --> BR_MODEL
    DET_SEC --> BR_MODEL
    DET_COMP --> BR_MODEL
    DET_PERF --> BR_MODEL
    DET_REL --> BR_MODEL
    BR_MODEL --> BR_INVOKE --> BR_COST

    DET_SEC --> SNS_PUB
    DET_COMP --> SNS_PUB
    SNS_PUB --> SNS_MSG --> SNS_SEND --> SNS_RESULT
```

### AwsClientFactory Internal

```python
class AwsClientFactory:
    _profile: str | None       # AWS profile name
    _default_region: str        # Default region
    _max_attempts: int          # Retry attempts (adaptive)
    _retry_mode: str            # "adaptive" or "legacy"
    _lock: Lock                 # Thread safety
    _session: boto3.Session     # Cached session
    _clients: dict[(str, str), BaseClient]  # (service, region) → client
```

### Paginator Contract

```python
def paginate(client, method: str, **kwargs):
    paginator = client.get_paginator(method)
    for page in paginator.paginate(**kwargs):
        yield page
    # Used by all detectors — ensures no silent truncation
```

### DetectorGuard

```python
@contextmanager
def detector_guard(ctx, detector_id, region):
    try:
        yield
    except Exception as e:
        ctx.errors.append({"detector_id": detector_id,
                          "region": region,
                          "error": str(e)})
    # Never crashes the pipeline — individual detector failures
    # are recorded and processing continues
```

---

## 13. Verification Flow

### End-to-End Verification Strategy

```mermaid
flowchart TB
    subgraph UnitTests["Unit Tests"]
        UT1["test_decision_router.py<br/>• Deterministic classifications"]
        UT2["test_action_executor_node.py<br/>• Handler dispatch"]
        UT3["test_action_executor_handlers.py<br/>• Individual handlers"]
        UT4["test_approval.py<br/>• Human-in-the-loop"]
        UT5["test_analyze_ranking.py<br/>• LLM ranking"]
        UT6["test_composer.py<br/>• Briefing generation"]
        UT7["test_kra_supervisor.py<br/>• Fan-out routing"]
        UT8["test_observation_ingestion.py<br/>• CloudWatch + EventBridge"]

        UT9["test_cost_tools.py<br/>Cost Explorer"]
        UT10["test_security_tools.py<br/>GuardDuty, IAM"]
        UT11["test_compliance_tools.py<br/>Config, encryption"]
        UT12["test_performance_tools.py<br/>CloudWatch metrics"]
        UT13["test_reliability_tools.py<br/>Health events"]
        UT14["test_kra_context.py<br/>Detector context"]

        UT15["test_digital_worker_intake.py<br/>Channel normalization"]
        UT16["test_digital_worker_planning.py<br/>Memory ▸ LLM ▸ plan"]
        UT17["test_digital_worker_graph.py<br/>Full graph topology"]
        UT18["test_fastapi_intake.py<br/>API endpoints"]
    end

    subgraph IntegrationTests["Integration Tests"]
        IT1["test_nodes/*.py<br/>• onboard_account<br/>• kra_supervisor<br/>• observe_security<br/>• observe_reliability<br/>• persist<br/>• run_all"]
        IT2["test_org_demo.py<br/>Organizations demo"]
        IT3["test_tokens_demo.py<br/>Token tracking"]
        IT4["test_tokens_mock.py<br/>Mocked token counts"]
    end

    subgraph CICD["CI/CD Pipeline"]
        GH_ACTIONS["GitHub Actions<br/>• .github/workflows/check.yml<br/>• .github/workflows/eval-offline.yml"]
        CHECK["check.yml<br/>• Lint (ruff)<br/>• Type check (mypy)<br/>• Unit tests (pytest)<br/>• Security scan"]
        EVAL["eval-offline.yml<br/>• Offline evaluation<br/>• Recall/precision<br/>• Per-KRA metrics"]
    end

    subgraph QualityGates["Quality Gates"]
        QG1["• No print() — use logger<br/>• No bare except — narrow types<br/>• No TODO — track in ticket<br/>• Paginator on every list/describe"]
        QG2["• Deterministic invariants verified<br/>• LLM calls only in analyze/compose<br/>• Factory only for boto3 clients<br/>• Postgres writes only in persist node"]
    end

    subgraph ManualVerification["Manual Verification"]
        MV1["• Approval center testing<br/>• Webhook channel testing<br/>• Terraform execution testing<br/>• Frontend integration"]
        MV2["• Security review<br/>• Performance benchmarking<br/>• Cost impact assessment<br/>• Disaster recovery"]
    end

    UT1 --> CHECK
    UT2 --> CHECK
    UT15 --> CHECK
    IT1 --> CHECK
    IT2 --> EVAL
    IT3 --> EVAL

    CHECK --> QG1
    CHECK --> QG2
    EVAL --> QG1
    EVAL --> QG2

    QG1 --> MV1
    QG2 --> MV2
```

### Verification Implementation

```python
# Verification Flow (Digital Worker) — verify_execution()
def verify_execution(request, classification, plan, execution) -> ValidationResult:
    checks = []
    
    # 1. Execution status check
    checks.append(ValidationCheck(
        name="execution_status",
        passed=execution.status == "executed",
        detail=f"Status: {execution.status}"
    ))
    
    # 2. Dry run awareness
    if execution.dry_run:
        checks.append(ValidationCheck(
            name="dry_run",
            passed=True,
            detail="Dry run — no real mutations"
        ))
    
    # 3. Plan vs execution alignment
    # ... (per-step validation)
    
    return ValidationResult(
        passed=all(c.passed for c in checks),
        checks=checks
    )
```

### Core Graph Verification (via `deterministic_rank`)

```python
# The analyze node uses deterministic_rank() which:
# 1. Groups findings by severity (critical → high → medium → low → info)
# 2. Within each group, sorts by detector_id alphabetically
# 3. Deduplicates by (detector_id, resource_arn, region)
# 4. Returns AnalyzedFinding list
# LLM only called for composing rationale text — ranking is deterministic
```

---

## 14. Database Schema

### Entity Relationship Diagram

```mermaid
erDiagram
    runs ||--o{ findings : "has"
    runs ||--o| briefings : "generates"
    runs ||--o| eval_runs : "evaluated by"

    runs {
        uuid id PK
        datetime started_at
        datetime finished_at
        string account_id UK
        string status
        jsonb errors_json
        float bedrock_cost_usd
    }

    findings {
        uuid id PK
        uuid run_id FK
        string kra
        string severity
        string detector_id
        text resource_arn
        string resource_type
        string region
        text title
        jsonb evidence_jsonb
        text recommendation
        datetime created_at
    }

    briefings {
        uuid id PK
        uuid run_id FK UK
        jsonb scorecard_jsonb
        text markdown_text
        int findings_count
        datetime created_at
    }

    eval_runs {
        uuid id PK
        uuid run_id FK UK
        float recall_overall
        jsonb recall_per_kra_jsonb
        float precision_overall
        int fp_count
        datetime created_at
    }

    cloud_requests {
        uuid id PK
        string request_id UK
        string source
        text external_id
        text title
        string category
        string platform
        string priority
        string risk_level
        string decision_mode
        string status
        jsonb result_jsonb
        jsonb audit_jsonb
        datetime received_at
        datetime completed_at
        datetime created_at
    }

    resolution_memory {
        uuid id PK
        string fingerprint UK
        string category
        string platform
        text title
        jsonb plan_jsonb
        int hit_count
        string last_outcome
        datetime created_at
        datetime updated_at
    }
```

### Migration Chain

| Revision | Name | Description |
|----------|------|-------------|
| `0001` | Initial schema | `runs`, `findings`, `briefings`, `eval_runs` |
| `c6f417c05ab8` | Add bedrock_cost_usd | +`bedrock_cost_usd` to `runs`, drop old LangGraph checkpoint tables |
| `a1d9e2f4b7c1` | Add Digital Worker tables | `cloud_requests`, `resolution_memory` |

### Index Summary

| Table | Index | Column(s) | Purpose |
|-------|-------|-----------|---------|
| `runs` | `ix_runs_account_id` | `account_id` | Filter runs by account |
| `findings` | `ix_findings_run_id` | `run_id` | JOIN to runs |
| `findings` | `ix_findings_kra` | `kra` | Filter by KRA |
| `findings` | `ix_findings_severity` | `severity` | Filter by severity |
| `findings` | `ix_findings_detector_id` | `detector_id` | Filter by detector |
| `cloud_requests` | `ix_cloud_requests_request_id` | `request_id` | Lookup by request ID |
| `cloud_requests` | `ix_cloud_requests_source` | `source` | Filter by channel source |
| `cloud_requests` | `ix_cloud_requests_category` | `category` | Filter by category |
| `cloud_requests` | `ix_cloud_requests_platform` | `platform` | Filter by platform |
| `resolution_memory` | `ix_resolution_memory_fingerprint` | `fingerprint` | Fast memory lookup |
| `resolution_memory` | `ix_resolution_memory_category` | `category` | Filter by category |
| `resolution_memory` | `ix_resolution_memory_platform` | `platform` | Filter by platform |

---

## 15. Configuration & Environment

### Key Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `bedrock` | LLM backend: bedrock, openai, vllm, ollama |
| `BEDROCK_MODEL_ID` | — | Bedrock model (e.g. anthropic.claude-sonnet-4-5-v1) |
| `AWS_DEFAULT_REGION` | `us-east-1` | Default AWS region |
| `AWS_PROFILE` | — | AWS profile name |
| `VLLM_API_BASE` | — | vLLM/OpenAI-compatible endpoint |
| `VLLM_API_KEY` | — | API key for vLLM |
| `VLLM_MODEL` | — | Model name for vLLM |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | — | Ollama model name |
| `OPENAI_API_BASE` | — | OpenAI-compatible API base |
| `OPENAI_API_KEY` | — | OpenAI API key |
| `OPENAI_MODEL_NAME` | — | OpenAI model name |
| `POSTGRES_URL` | `postgresql+psycopg://chandra:chandra@localhost:5432/chandra` | Database URL |
| `SNS_TOPIC_ARN` | — | SNS topic for escalations |
| `FRONTEND_URL` | — | Frontend URL for CORS |
| `NEXT_PUBLIC_API_URL` | — | API URL for frontend |
| `CHANDRA_WEBHOOK_TOKEN` | — | Optional webhook auth token |
| `BOTO_MAX_ATTEMPTS` | `5` | Max retry attempts for boto3 |
| `BOTO_RETRY_MODE` | `adaptive` | Retry mode (adaptive/legacy) |

### Settings (`src/chandra/config.py`)

The `Settings` class (Pydantic `BaseSettings`) loads from environment variables with a `.env` override system. Every component references `settings.*` rather than reading `os.getenv()` directly.

---

## Appendix: File Layout

```
~/projects/chandra/
├── fastapi_app.py              # 2412 lines — FastAPI backend
├── app.py                      # Gradio dashboard
├── run.py                      # Entry point
├── pyproject.toml              # Python project config
├── Dockerfile                  # Multi-stage build
├── docker-compose.yml          # 5 services
├── nginx.conf                  # Reverse proxy config
├── alembic.ini                 # Migration config
│
├── src/chandra/
│   ├── graphs/
│   │   ├── chandra_graph.py    # Core graph builder (build_graph)
│   │   ├── nodes.py            # 12+ node implementations
│   │   ├── state.py            # ChandraState definition
│   │   ├── checkpointer.py     # Postgres checkpointer builder
│   │   └── action_nodes/       # Action executor sub-package
│   │
│   ├── digital_worker/
│   │   ├── graph.py            # Digital Worker graph builder (15 nodes)
│   │   ├── state.py            # DigitalWorkerState definition
│   │   ├── intake.py           # 10 channel adapters
│   │   ├── classifier.py       # Request classification
│   │   ├── context.py          # Context collector
│   │   ├── planner.py          # Resolution planning
│   │   ├── risk.py             # Risk assessment
│   │   ├── decision_engine.py  # Execution decision logic
│   │   ├── guidance.py         # Engineer guidance renderer
│   │   ├── verifier.py         # Execution verification
│   │   ├── tracker.py          # Jira ticket update
│   │   ├── notifications.py    # Multi-channel dispatch
│   │   ├── memory.py           # Plan memory (fingerprint-based)
│   │   └── schemas.py          # Pydantic models
│   │
│   ├── llm/
│   │   ├── __init__.py         # build_chat_model, get_llm, fallback
│   │   └── ...                 # (flat factory — no subpackage)
│   │
│   ├── aws/
│   │   ├── client_factory.py   # AwsClientFactory (cached boto3 clients)
│   │   ├── regions.py          # Active region discovery
│   │   ├── helpers.py          # Utility functions
│   │   ├── organizations.py    # AWS Organizations support
│   │   ├── security_models.py  # Security data models
│   │   ├── compliance_models.py
│   │   ├── config_compliance.py
│   │   ├── encryption_checks.py
│   │   └── cloudtrail_audit.py
│   │
│   ├── tools/
│   │   ├── cost/               # Cost Explorer, Budgets
│   │   ├── security/           # GuardDuty, Security Hub, IAM
│   │   ├── compliance/         # Config, CloudTrail, encryption
│   │   ├── performance/        # CloudWatch metrics
│   │   ├── reliability/        # Health, Trusted Advisor
│   │   └── base.py             # DetectorContext, detector_guard, paginate
│   │
│   ├── briefing/
│   │   ├── composer.py         # LLM narrative + deterministic_rank
│   │   └── schemas.py          # Finding, AnalyzedFinding, ProposedWrite
│   │
│   ├── escalation/
│   │   ├── publisher.py        # SNSPublisher
│   │   └── schemas.py          # EscalationPayload, EscalationResult
│   │
│   ├── db/
│   │   ├── models.py           # SQLAlchemy ORM models
│   │   ├── session.py          # session_scope context manager
│   │   └── migrations/         # Alembic migrations (3 revisions)
│   │
│   ├── config.py               # Pydantic Settings
│   └── logging.py              # Structured logging
│
├── digitalworker_agents/
│   ├── aws_execution_agent.py   # ExecutionAgents (3709 lines)
│   ├── observation_agent.py     # AwsObservabilityAgent
│   ├── analyzer_agent.py        # AnalyzerAgent
│   └── ...
│
├── copilot_agents/
│   ├── graph.py                 # Copilot LangGraph
│   └── ...
│
├── frontend/
│   ├── package.json             # Next.js 16 + React 18 + TS
│   ├── src/
│   │   ├── app/                 # App router pages
│   │   ├── components/          # React components
│   │   └── lib/                 # Utilities
│   └── ...
│
├── tests/
│   ├── unit/                    # 20+ unit test modules
│   └── test_nodes/              # Integration test nodes
│
├── tools/
│   └── aws_cloud_tools/         # AWS fetcher tools
│
└── docs/
    └── handover/
        └── architecture-document.md  # This file
```