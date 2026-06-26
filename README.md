# Chandra — Digital Cloud Engineer Platform

> **Project context for any agent / engineer / Claude session working in this repo.**
> Read this file first. It is the canonical entry point.

Chandra is a **multi-layered enterprise AI cloud operations platform** built around
an autonomous AWS observation agent. It emits a daily **Cloud Health Briefing**
across five KRAs (**cost, security, compliance, performance, reliability**), exposes a
premium Next.js ops console with a HITL approval center, and ships with a FastAPI
multi-agent backend that powers the web UI.

---

## Table of contents

1. [What Chandra is](#1-what-chandra-is)
2. [Platform components](#2-platform-components)
3. [Canonical LangGraph topology](#3-canonical-langgraph-topology)
4. [Forward-looking: Digital Worker design](#4-forward-looking-digital-worker-design)
5. [Tech stack](#5-tech-stack)
6. [Hard architectural invariants — DO NOT VIOLATE](#6-hard-architectural-invariants--do-not-violate)
7. [Repository layout](#7-repository-layout)
8. [Prerequisites](#8-prerequisites)
9. [Day-1 setup](#9-day-1-setup)
10. [Development commands](#10-development-commands)
11. [Demo run-through](#11-demo-run-through)
12. [Quality gates](#12-quality-gates)
13. [CODEOWNERS — who reviews what](#13-codeowners--who-reviews-what)
14. [Team](#14-team)
15. [Operating norms](#15-operating-norms)
16. [When you (any agent) are stuck](#16-when-you-any-agent-are-stuck)
17. [What NOT to do](#17-what-not-to-do)
18. [Reference links](#18-reference-links)

---

## 1. What Chandra is

Chandra is **four cooperating things** under one repository:

| # | Surface | Stack | Purpose |
|---|---------|-------|---------|
| 1 | **Backend orchestration** (`src/chandra/`) | Python · LangGraph · Bedrock | The canonical, production read-only observation + briefing pipeline. |
| 2 | **Frontend operations console** (`frontend/`) | Next.js 16 · React 18 · TypeScript · Tailwind · framer-motion · recharts | Premium ops UI: onboarding wizard, dashboard, approval center, audit trail. |
| 3 | **FastAPI backend service** (repo root) | FastAPI · LangGraph · async aioboto3 | HTTP + WebSocket surface for the Next.js console. Wraps `digitalworker_agents/` + `copilot_agents/`. |
| 4 | **Streamlit dashboard** (`src/chandra/dashboard/`) | Streamlit | Temporary read-only briefing viewer. **Being sunset (FE-01). Do not add features here.** |

The two backends (canonical `src/chandra/` and FastAPI-root `digitalworker_agents/` + `copilot_agents/`) coexist by design — the canonical pipeline is the source of truth for the briefing, the FastAPI surface is the runtime the Next.js console is wired to.

### What Chandra is NOT

- Not a write-only tool. Chandra is **read-only by default**. Write actions only flow through `action_executor_node` (auto-fix, `dry_run=True`) + the `escalation` queue (publishes to SNS) + `approval_node` (HITL interrupt).
- Not a single-agent ReAct loop. The canonical pipeline is a **multi-node LangGraph `StateGraph`** with deterministic nodes and `Send(...)` fan-out.
- Not a low-code/drag-and-drop tool. Full code, only.

---

## 2. Platform components

### 2.1 Canonical backend — `src/chandra/`

A LangGraph-orchestrated autonomous agent that observes one AWS account and emits a
daily Cloud Health Briefing across five KRAs.

- **Orchestration:** LangGraph `StateGraph` with `PostgresSaver` checkpointer (falls back to `MemorySaver`).
- **LLM:** Claude Sonnet 4.5 via Amazon Bedrock (`langchain_aws.ChatBedrockConverse`).
- **AWS SDK:** boto3 with adaptive retry, paginated everywhere.
- **State:** Postgres (RDS in prod, container in dev).
- **Detectors:** deterministic boto3 modules — **never call the LLM**.

### 2.2 Frontend ops console — `frontend/`

A premium, futuristic operations console (HTML/CSS/TypeScript) for observing, triaging, and approving remediations under continuous human supervision.

- **Onboarding flow:** five-step provisioning ceremony (name → avatar → role → maturity → KRAs → permissions → deploy).
- **Dashboard surface:** live ops stream, active incidents table, approval center, cost monitoring, audit trail, performance scoring, infrastructure health.
- **Governance model:** every destructive remediation is gated by human approval (`Approve / Reject / Escalate`).
- **Deployment:** ships as a static export to GitHub Pages today; will connect to the FastAPI backend for real-time WebSocket streams.

### 2.3 FastAPI service — repo root

A second service — `fastapi_app.py` + `app.py` + `run.py` — exposes HTTP and WebSocket endpoints consumed by the Next.js console and (read-only) the Streamlit dashboard. It wraps:

- **Multi-agent orchestrator** (`digitalworker_agents/`): observation → analyzer → generator → executor, with a self-healing orchestrator loop and HITL handling.
- **LangGraph chat surface** (`copilot_agents/`): the in-product copilot.

All write actions routed from the FE approval center flow through this service and ultimately land in the `escalation` queue described above.

### 2.4 Streamlit dashboard — `src/chandra/dashboard/`

Temporary. Renders the latest briefing, findings explorer, eval trend. **Being replaced by the Next.js console (FE-01). Don't add new features here — migrate them to Next.js.**

---

## 3. Canonical LangGraph topology

The single source of truth for node names and edges is
`src/chandra/graphs/chandra_graph.py:build_graph`. Fan-out uses LangGraph's
`Send(...)` from the `kra_supervisor`.

```
START
  └─► onboard_account
        └─► ingest_observations
              └─► kra_supervisor ── Send(...) per KRA ──┐
                     ├─► observe_cost                   │
                     ├─► observe_security               │
                     ├─► observe_compliance             │
                     ├─► observe_performance            │
                     └─► observe_reliability            │
                                       ↓
                                  analyze        (LLM: rank + dedup — Bedrock)
                                       ↓
                              decision_router  (deterministic split → pending_writes + auto_fixed)
                                       ↓
                              action_executor  (consumes auto_fixed; dry_run=True by default)
                                       ↓
                                  escalation    (publishes pending_writes to SNS)
                                       ↓
                              compose_briefing (LLM: narrative — Bedrock)
                                       ↓
                       conditional: pending_writes non-empty
                                       ├─► approval_node  (HITL interrupt)
                                       │       └─► persist  → END
                                       └─► persist        → END
```

**Hard separation:** `decision_router`, `action_executor`, and `escalation` are deterministic. They sit between the LLM-powered `analyze` and the LLM-powered `compose_briefing`. If a future change introduces an LLM call into any of these three, it is a rule violation.

---

## 4. Forward-looking: Digital Worker design

`architecture.txt` contains a 1192-line detailed comparison of the current Chandra
architecture against the proposed **Digital Worker platform** — a self-healing,
sandboxed, 7-agent pipeline (Observability → Analyzer → Planner → Generator →
Executor → Validation → Reporter) with HITL gating, two reconciliation loops
(Loop A code-fix, Loop B state reconciliation), and circuit breakers.

**Read `architecture.txt` before proposing any "agent that writes code" feature.**
The plan there is the team's agreed direction.

---

## 5. Tech stack

| Layer | Technology |
|-------|-----------|
| Orchestration | `langgraph>=0.2.50` + `langgraph-checkpoint-postgres` (with `MemorySaver` fallback) |
| LLM | `langchain-aws` → `ChatBedrockConverse` (Claude Sonnet 4.5) |
| AWS SDK | `boto3>=1.34.0` + `aioboto3>=15.5.0` (FastAPI surface) |
| Validation | `pydantic>=2.7` + `pydantic-settings>=2.3` |
| Database | PostgreSQL via `SQLAlchemy>=2.0` + `psycopg[binary]>=3.2` + `alembic>=1.13` |
| Multi-agent chat | `langchain-core>=0.3` + `langchain-community>=0.4.2` |
| HTTP / WS | `fastapi>=0.111.0` + `uvicorn[standard]>=0.30.0` |
| CLI | `typer>=0.12` + `rich>=13.7` |
| Logging / OTEL | `structlog>=24.1` + `opentelemetry-*>=1.42.1` |
| IaC | Terraform ≥ 1.5 (synthetic env only) |
| Frontend | Next.js 16 · React 18 · TypeScript · Tailwind · framer-motion · recharts |
| Dashboard (temp) | Streamlit ≥ 1.36 + plotly ≥ 5.22 + pandas ≥ 2.2 |
| Linting / types | `ruff>=0.5` + `mypy --strict` (Python 3.12) |
| Tests | `pytest>=8.2` + `moto[all]>=5.0` + `freezegun>=1.5` |
| Browser automation (test) | `playwright>=1.60.0` |
| Ticketing integration | `jira>=3.10.5` |

---

## 6. Hard architectural invariants — DO NOT VIOLATE

These are not preferences. They are the team's architectural contract. Violations need an explicit signoff from Phani.

1. **LangGraph is the only orchestration framework.** No LangChain `AgentExecutor`. No `create_react_agent`. Use `StateGraph` + `Send(...)` for fan-out. The canonical topology lives in `src/chandra/graphs/chandra_graph.py:build_graph`.
2. **Amazon Bedrock is the only LLM provider** — specifically `langchain_aws.ChatBedrockConverse` with Sonnet 4.5. Do not import `openai`, `anthropic` direct SDK, or any other provider. (Note: the FastAPI surface adds `openai`, `anthropic`, `langchain-openai`, and `openai-agents` for `copilot_agents/` chat — these are scoped to that surface only and do not affect the canonical pipeline.)
3. **Read-only by default.** Detectors never call mutating AWS APIs. Write actions go through `action_executor_node` (auto-fix for low-risk `auto_fixed` writes — `dry_run=True` by default) + the `escalation` queue (publishes high-risk `pending_writes` to SNS) + `approval_node` (interrupts for human approval when `pending_writes` is non-empty).
4. **`chandra.briefing.composer` is the only module in the canonical pipeline that may call Bedrock.** Detector modules in `src/chandra/tools/` MUST NOT import `langchain_aws`.
5. **`decision_router`, `action_executor`, and `escalation` are deterministic.** They sit between the LLM-powered `analyze` and the LLM-powered `compose_briefing`. If a future change introduces an LLM call into any of these three, it is a rule violation — surface it on the ticket.
6. **Postgres writes only in the `persist` node and Alembic migrations.** Nowhere else.
7. **Every boto3 list/describe call uses a paginator.** No silent truncation.
8. **AWS clients are created via `chandra.aws.client_factory.get_default_factory()`.** Never `boto3.client(...)` directly. The factory handles region discovery, caching, and IAM role assumption.
9. **No `# TODO: implement` in committed code.** If something is deferred, `raise NotImplementedError("<msg>; tracked in <TICKET-ID>")`.
10. **No `print()`.** Use `chandra.logging.get_logger(__name__)`.
11. **No `except Exception` without re-raising or structured logging.** Narrow exception classes only.
12. **Frontend is Next.js-only for new work.** The Streamlit dashboard is being sunset (FE-01). Don't add features to Streamlit; migrate them to Next.js.
13. **Don't add new top-level Python files / dirs at the repo root.** The root has drifted (FastAPI app, `digitalworker_agents/`, `database/`, `tools/`, `fix/`, ad-hoc demos). New backend code goes under `src/chandra/`.

---

## 7. Repository layout

### 7.1 Canonical backend — `src/chandra/`

```
src/chandra/
├── __init__.py                     # __version__ = "0.1.0"
├── cli.py                          # Typer: chandra {run, eval, render, …}
├── config.py                       # pydantic Settings (env-driven singleton)
├── logging.py                      # structlog + OTEL setup
│
├── aws/                            # AWS client factory + region discovery
│   ├── client_factory.py           # AwsClientFactory — must use, never boto3.client(...)
│   ├── regions.py                  # region enumeration + helpers
│   ├── organizations.py            # OU / account traversal
│   ├── cloudtrail_audit.py         # CloudTrail event correlation for compliance KRA
│   ├── config_compliance.py        # AWS Config rule evaluation
│   ├── encryption_checks.py        # KMS / EBS / S3 encryption checks
│   ├── security_models.py          # Pydantic models for security findings
│   ├── compliance_models.py        # Pydantic models for compliance findings
│   └── helpers.py
│
├── tools/                          # KRA detectors — deterministic boto3 (NO LLM)
│   ├── base.py                     # BaseDetector interface
│   ├── cost.py                     # detect_cost_findings()
│   ├── security.py                 # detect_security_findings()
│   ├── compliance.py               # compliance state checks
│   ├── performance.py              # latency / throughput / saturation
│   └── reliability.py              # uptime / failover / redundancy
│
├── graphs/                         # ⭐ LangGraph orchestration (single source of truth)
│   ├── state.py                    # ChandraState (TypedDict) with reducers
│   ├── chandra_graph.py            # StateGraph construction + Send(...) fan-out
│   ├── nodes.py                    # Legacy duplicate of nodes/__init__.py (do not edit; consolidation pending)
│   ├── nodes/
│   │   ├── __init__.py             # All node functions: observe_*, analyze, decision_router, action_executor, …
│   │   └── action_executor.py      # action_executor_node + ActionExecutor class + handler registry
│   └── action_nodes/
│       └── __init__.py
│
├── prompts/                        # LLM prompt templates (consumed only by composer)
│   ├── observer.md
│   ├── analyzer.md
│   ├── briefer.md
│   └── kra_context.md
│
├── briefing/                       # LLM interaction + narrative composition
│   ├── composer.py                 # ⭐ ONLY canonical module that calls Bedrock; deterministic fallback at line 103
│   ├── schemas.py                  # Finding, AnalyzedFinding, Briefing
│   └── org_summary.py              # organization-wide roll-ups
│
├── escalation/                     # Action queue + approval workflow (deterministic)
│   ├── schemas.py                  # Action, ApprovalDecision, EscalationEnvelope
│   ├── formatter.py                # render escalation payloads for the FE approval center
│   └── publisher.py                # publish approvals to the Next.js WS stream / Postgres
│
├── db/                             # SQLAlchemy ORM + Alembic migrations
│   ├── models.py                   # Run, Briefing, Finding, EvalRun, Action
│   ├── session.py                  # session_scope context manager
│   └── migrations/                 # alembic versions
│
├── dashboard/                      # ⏳ Streamlit read-only console (temporary, FE-01 sunset)
│   └── app.py                      # Latest briefing, findings table, eval trend
│
└── observability/                  # OpenTelemetry + pricing telemetry
    ├── callbacks.py                # LangGraph → OTEL instrumentation
    └── pricing.py                  # LLM token tracking
```

### 7.2 Frontend ops console — `frontend/`

```
frontend/
├── app/
│   ├── layout.tsx                  # Root HTML shell + OnboardingProvider
│   ├── providers.tsx               # Client-side context wrappers
│   ├── page.tsx                    # Root → /onboarding redirect
│   ├── globals.css                 # Theme tokens + animations (Tailwind base)
│   ├── onboarding/page.tsx         # OnboardingWizard host
│   └── dashboard/page.tsx          # ChandraExperience host + completion guard
│
├── components/
│   ├── OnboardingWizard.tsx        # Five-step provisioning flow
│   ├── ChandraExperience.tsx       # Full operations dashboard composition
│   └── WorkerActionExecutionCenter.tsx  # Approval center + actions
│
├── services/                       # Frontend HTTP / WS clients
│   ├── api.ts                      # REST client to FastAPI app
│   └── mapping.ts                  # Backend payload → UI shape
│
├── store/                          # Client state (sessionStorage-backed)
│   ├── OnboardingContext.tsx       # Identity + KRA + permissions
│   ├── agentProfile.ts             # Avatar catalog, employee ID generation
│   └── kraCatalog.ts               # KRA definitions + operational metrics
│
├── public/
│   ├── avatars/                    # Six holographic agent portraits (PNG)
│   ├── icons/                      # Role SVGs (AWS, Azure, DevOps, K8s, Security, Java)
│   └── intelligenz-it-logo.png.png
│
├── tailwind.config.ts              # Tailwind config (theme tokens, animations)
├── postcss.config.mjs
├── next.config.mjs                 # basePath + assetPrefix for GitHub Pages static export
├── package.json                    # next 16, react 18, framer-motion, recharts
└── tsconfig.json
```

### 7.3 FastAPI multi-agent backend — repo root

```
├── fastapi_app.py                  # FastAPI app: orchestrates digital worker agents
├── app.py                          # Alternate entrypoint / middleware wiring
├── run.py                          # Local dev launcher (uvicorn)
│
├── digitalworker_agents/           # Multi-agent orchestrator (observation → analyzer → generator → executor)
│   ├── observation_agent.py        # LangGraph-based AWS observability (5 KRAs)
│   ├── analyzer_agent.py           # Risk assessment + routing decisions
│   ├── generator_agent.py          # LLM writes .tf / .py / .sh to sandbox workspace
│   ├── executor_agent.py           # Applies DAG steps against live infra
│   ├── aws_execution_agent.py      # AWS-specific execution wrapper
│   └── orchestrator_agent.py       # Combines Generator + Executor in self-healing loop (max 5 iterations) + HITL + Jira
│
├── copilot_agents/                 # LangGraph chat / copilot surface
│   ├── graph.py
│   └── call_tools.py
│
├── tools/                          # AWS CloudWatch / Cost Explorer / Jira helpers used by the FastAPI app
│   ├── langchain_tools.py          # LangChain @tool wrappers (12 active)
│   ├── aws_cloud_tools/            # 13 async aioboto3 fetchers (cost_explorer, cloudwatch_alarms, …)
│   └── jira_tools/                 # create_jira_ticket + add_comment_to_ticket
│
├── database/                       # SQLite checkpointer store (langgraph checkpoints)
└── fix/                            # One-off repair scripts (ad-hoc; do not add new ones)
```

### 7.4 Shared infrastructure

```
├── iac/
│   ├── synthetic_env/              # Terraform — seeds 10 known misconfigs in a burner AWS account
│   └── runtime/                    # Terraform — runtime infrastructure
├── evals/
│   ├── seed_manifest.yaml          # Ground truth: what misconfigs should Chandra find?
│   ├── harness.py                  # terraform apply → run → score → report
│   ├── detected.json               # Latest detected findings snapshot
│   └── reports/                    # JSON + Markdown reports per run
├── tests/
│   ├── conftest.py                 # Shared fixtures (aws, client_factory, detector_context, …)
│   ├── unit/                       # moto-driven unit tests; fast (~1–2s)
│   └── integration/                # Marked @pytest.mark.integration; need Docker/Postgres
├── scripts/
│   ├── smoke.sh                    # Linux/macOS one-shot demo runner
│   ├── smoke.ps1                   # Windows PowerShell one-shot demo runner
│   └── healthcheck.py              # Container healthcheck
├── markdown_files/
│   └── worker_graph.md             # Worker graph documentation
├── docker-compose.yml              # Postgres + LocalStack for local dev
├── Dockerfile                      # Multi-stage slim runtime image
├── railway.toml                    # Railway deploy config
├── nginx.conf                      # Reverse proxy in front of FastAPI
├── start.sh                        # Container entrypoint
├── pyproject.toml                  # Python deps, tool config (ruff, mypy, pytest)
├── Makefile                        # Quality gates + CLI targets (run `make help`)
├── CLAUDE.md                       # Claude Code session guidance
├── CODEOWNERS                      # Reviewer routing
├── TESTING.md                      # Testing conventions
├── TODO.md                         # Build tracker (D1–D15, all complete)
├── architecture.txt                # Forward-looking Digital Worker design (1192 lines)
└── .github/workflows/
    ├── check.yml                   # CI gate: lint + type + test
    └── eval-offline.yml            # Nightly offline eval against fixtures
```

---

## 8. Prerequisites

- Python 3.12
- [uv](https://github.com/astral-sh/uv) (Python package manager)
- Docker (for local Postgres + LocalStack)
- Terraform ≥ 1.5
- AWS CLI with credentials to a **burner / sandbox** account (the synthetic env applies real AWS resources)
- Amazon Bedrock model access for `anthropic.claude-sonnet-4-5-20250929-v1:0` in your default region
- Node.js 20+ (for the Next.js console)

---

## 9. Day-1 setup

```bash
# 1. Clone + branch
git clone https://github.com/phanindraintelligenzit-afk/chandra.git
cd chandra

# 2. Backend environment
cp .env.example .env
# Edit .env to set AWS_PROFILE and SYNTHETIC_ACCOUNT_ID (your burner account id)

# 3. Initialize backend
make install      # uv sync --all-extras — all runtime + dev deps
make db-up        # docker compose up -d postgres
make migrate      # alembic upgrade head

# 4. Initialize frontend (separate package.json)
cd frontend
npm install
cd ..

# 5. Stand up the synthetic env (real AWS resources in your burner)
make tf-apply

# 6. Verify everything works
make check        # lint + type + test
```

---

## 10. Development commands

### 10.1 Backend (Python)

```bash
make install          # uv sync --all-extras
make db-up            # docker compose up -d postgres
make db-down          # docker compose down
make migrate          # alembic upgrade head
make fmt              # ruff format
make lint             # ruff check
make type             # mypy src --strict
make test             # pytest
make test -v          # pytest -v (verbose, debugging)
make check            # lint + type + test — the commit gate
make run              # chandra run --account $SYNTHETIC_ACCOUNT_ID
make eval             # chandra eval --account $SYNTHETIC_ACCOUNT_ID
make eval-offline     # chandra eval --fixture evals/fixtures/baseline_v1.jsonl
make dashboard        # streamlit run src/chandra/dashboard/app.py
make tf-apply         # terraform apply on synthetic env
make tf-destroy       # terraform destroy on synthetic env
make smoke            # bash scripts/smoke.sh — end-to-end (Linux/macOS)
make smoke-windows    # pwsh scripts/smoke.ps1 — end-to-end (Windows)
make chaos            # integration chaos tests (nightly; needs Docker/Postgres)
make clean            # remove .pytest_cache, .ruff_cache, etc.
make help             # full target list
```

#### Running a single test

```bash
# Single test file
uv run pytest tests/unit/test_decision_router.py -v

# Single test function
uv run pytest tests/unit/test_decision_router.py::TestDecisionRouter::test_critical_escalated -v

# By name pattern
uv run pytest tests/unit/ -k "decision_router" -v

# Skip integration tests
uv run pytest tests/unit/ -m "not integration" -v

# Coverage for one module
uv run pytest tests/unit/ --cov=src/chandra/tools --cov-report=term-missing

# Stop on first failure / debug
uv run pytest tests/unit/ -x --pdb
```

`tests/conftest.py` exposes `aws`, `cloudwatch`, `s3`, `iam`, `ec2`, `rds`,
`client_factory`, and `detector_context` fixtures; all AWS calls are mocked via
`moto` (no real AWS).

### 10.2 FastAPI service (separate launcher)

```bash
uvicorn fastapi_app:app --reload --port 8000   # local dev
python run.py                                   # alt launcher
```

### 10.3 Frontend (Next.js)

From `frontend/` directory:

```bash
npm install      # install Node dependencies
npm run dev      # start dev server on http://localhost:3000
npm run build    # compile + static export to ./out/
npm run start    # serve production build
npm run lint     # ESLint check
```

---

## 11. Demo run-through (≈ 10 minutes)

```bash
# 1. Configure environment.
cp .env.example .env
# Then edit .env to set AWS_PROFILE and SYNTHETIC_ACCOUNT_ID.

# 2. Bring up Postgres + install deps + migrate.
make db-up
make install
make migrate

# 3. Stand up the synthetic env (real AWS resources in your burner).
make tf-apply

# 4. One Chandra run end-to-end.
make run
# → writes briefing-{run_id}.md and briefing-{run_id}.json to evals/reports/

# 5. Score recall vs seed_manifest.yaml.
CHANDRA_STALE_KEY_DAYS_OVERRIDE=0 make eval
# → exit 0 only if recall_overall ≥ 0.80 AND every per-KRA recall ≥ 0.70.

# 6. Open the dashboard.
make dashboard
# → http://localhost:8501
```

Or, one shot:

```bash
make smoke           # Linux/macOS
make smoke-windows   # PowerShell
```

When you're done with the burner:

```bash
make tf-destroy
```

### Success bar

- ≥ 80% recall overall on the 10 seeded misconfigurations.
- ≥ 70% recall per individual KRA.
- End-to-end run under 8 minutes on a fresh burner.
- Briefing renders in Streamlit and exports clean Markdown + JSON.

---

## 12. Quality gates

`make check` runs **ruff + mypy --strict + pytest**. Don't commit on red. The repo enforces:

- No `# TODO: implement` in committed code — use `raise NotImplementedError(...)` instead.
- Tools never call Bedrock — only `chandra.briefing.composer` does.
- Writes to Postgres only inside the `persist` node and migrations.
- Boto3 paginators on every list/describe call.
- No raw `boto3.client(...)` — always use `AwsClientFactory`.
- Every boto3 list/describe uses a paginator — no silent truncation.
- No `print()` — use `chandra.logging.get_logger(__name__)`.

---

## 13. CODEOWNERS — who reviews what

| Path | Team / owner |
|------|--------------|
| `src/chandra/graphs/`, `src/chandra/briefing/`, `src/chandra/prompts/`, `src/chandra/escalation/` | LangGraph team |
| `src/chandra/aws/`, `src/chandra/tools/`, `iac/`, `Dockerfile`, `docker-compose.yml` | AWS team |
| `src/chandra/dashboard/`, `frontend/`, `fastapi_app.py`, `app.py`, `digitalworker_agents/`, `copilot_agents/`, `tools/` (root) | Frontend team |
| `src/chandra/db/`, `src/chandra/observability/` | AWS + LangGraph jointly |
| `evals/`, `tests/` | LangGraph team |
| `docs/` | Kshiraja |
| `.github/`, `CODEOWNERS`, `pyproject.toml`, `Makefile` | Chandra leads (Phani) |

If your change touches another team's path, open a draft PR and tag them. Don't merge silently.

---

## 14. Team

| Person | Role | Workstream |
|--------|------|-----------|
| **Maheshwar** | AWS Engineer | AWS infra, IaC, CI |
| **Siva** | LangGraph Engineer | Graph core, Cost/Performance KRA workers, observability primitives |
| **Nagendra** | LangGraph Engineer | Security/Compliance KRA workers, eval harness, fixture-replay |
| **Aishani** | Frontend Engineer | Next.js ops console, onboarding wizard, approval center |
| **Kshiraja** | Intern | Docs, demo runbook, fixtures, well-scoped starter tickets |
| **Phani** | PM / LangGraph reviewer | Project lead, escalation, decision authority on Coordination |
| **PVR** | CEO | Product norms, escalation |

---

## 15. Operating norms

Set by PVR — non-negotiable.

- **Full-code only.** No low-code, no drag-and-drop. Streamlit is temporary; Next.js is the real thing.
- **Ship every day.** Small PRs. Fast review.
- **State lives in Notion + repo. Not in DMs.**
- **Push back if a plan has a flaw.** Don't hedge to be polite.
- **Building-in-public is OFF.** Internal-only until PVR explicitly green-lights.

### Branch / commit / PR conventions

- **Branch naming:** `<ticket-id-lowercase>/<short-slug>` — e.g. `lg-03/traced-node-decorator`.
- **Commit / PR title format:** `<TICKET-ID>: <imperative summary>` — e.g. `LG-03: add @traced_node decorator (OTEL + structlog + metrics)`.
- **PR body must include:** link to the Notion ticket, a one-paragraph "what / why", and a checklist of acceptance items from the ticket.
- **`make check` must pass locally before opening a PR.** CI runs the same gate (`.github/workflows/check.yml`).
- **CODEOWNERS auto-routes reviewers.** Don't self-merge.
- **One ticket = one PR.** Keep PRs small. No week-long branches.

---

## 16. When you (any agent) are stuck

- **Ambiguous spec:** comment on the Notion ticket; Phani clarifies. Don't guess.
- **Architecture question:** the rules in §6 are the source of truth. If you think a rule is wrong, raise it explicitly — don't quietly route around it.
- **Test failure:** run the affected test with `-v` and read the trace. If it's a flake, document it; don't paper over.
- **Bedrock unavailable / throttling:** the composer's deterministic fallback exists for exactly this (see `composer.py:103`). Confirm it's hit (look for `llm.bedrock_unavailable_fallback_to_deterministic` log entries) and continue.
- **Frontend question:** the Next.js codebase is in `frontend/`, separate from the backend. State lives in `OnboardingContext` (sessionStorage). The WS surface is served by the FastAPI app at the repo root (`fastapi_app.py`); the Next.js console is being wired to it, but timers simulate the stream until that lands.
- **LangGraph topology question:** read `src/chandra/graphs/chandra_graph.py:build_graph` first. It is the single source of truth for node names and edges. The `Send(...)` fan-out is wired from `kra_supervisor` via `_route_kra_workers`.
- **Multi-agent pipeline question:** read `digitalworker_agents/orchestrator_agent.py` for the self-healing loop; `digitalworker_agents/observation_agent.py` for the LangGraph observability surface.
- **Forward-looking "agent that writes code":** read `architecture.txt` first.

---

## 17. What NOT to do

- Don't merge your own PRs.
- Don't push directly to `main`.
- Don't modify `CODEOWNERS`, `.github/workflows/*`, `pyproject.toml`, or `Makefile` without flagging Phani first.
- Don't touch GitHub repo settings, branch protection, or security configurations.
- Don't add new third-party dependencies without justifying in the PR description.
- Don't refactor outside the scope of your current ticket — open a separate ticket for it.
- Don't import any LLM provider other than `langchain_aws` in the canonical pipeline (no `openai`, `anthropic`, `cohere`, etc.).
- Don't instantiate `boto3.client(...)` directly — always go through `AwsClientFactory`.
- **Don't add features to the Streamlit dashboard.** Migrate existing Streamlit surfaces to Next.js instead (FE-01).
- **Don't add new top-level Python dirs at the repo root.** New backend code goes under `src/chandra/`.

---

## 18. Reference links

- **Kanban (live):** https://www.notion.so/b67c36091c9f426ab6d49c4b6e54b789
- **Onboarding Resource Pack:** https://www.notion.so/3604baec816581b1910dff95427c76be
- **Latest project status report (for PVR):** https://www.notion.so/3674baec8165810fbf1af038d9607f93
- **GitHub repo:** https://github.com/phanindraintelligenzit-afk/chandra
- **Frontend repo** (Next.js console): https://github.com/aishanic12/chandra_extended
- **Frontend live demo:** https://aishanic12.github.io/chandra_extended/
- **Engineer master prompts:** `docs/agent-prompts/` (paste the relevant one at session start)
- **Forward-looking design:** `architecture.txt` (in this repo)
- **Claude Code session guidance:** `CLAUDE.md` (in this repo)

---

## License

Proprietary — internal use only.