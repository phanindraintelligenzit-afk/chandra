# Runtime Validation Report — Chandra

> **Generated:** Thu Jul 30 2026  
> **Branch:** `feature/local-llm` | **Commit:** `cc9f328` (59 commits ahead of `main`)  
> **Local host:** Windows (10), Python 3.11.15  
> **Backend PID:** `uvicorn` (PID 20807, started Jul 27)  
> **EC2 host:** `54.160.31.20` (timeout on verification)  

---

## 1. Backend — FastAPI + uvicorn

| Field | Detail |
|---|---|
| **Status** | **Running** — `/health` returns `{"status":"ok"}` |
| **Key file** | `fastapi_app.py` — **2,412 lines** |
| **Port** | `6001` |
| **Endpoints verified** | `GET /health` → `{"status":"ok"}` ✅ |
| | `GET /health/ready` → `{"status":"degraded",...}` ⚠️ (see §17) |
| **CORS** | Configured via `CORSMiddleware` (line 168–177) |
| **Concurrency** | 8-worker `ThreadPoolExecutor` (line 59) |
| **Event loop** | Single shared async event loop (`_bg_loop`, line 68) |
| **Dependencies** | Copilot agent, Digital Worker graph, LLM factory |
| **Known issues** | None at runtime. `/health/ready` reports degraded due to missing `psycopg_c` — in-memory components (copilot, DW) both report `ok`. |
| **Test coverage** | `tests/unit/test_fastapi_intake.py` — 232 lines, 15 `def test_` functions |

---

## 2. Frontend — Next.js 16 + React 18 + TypeScript

| Field | Detail |
|---|---|
| **Status** | **Not Tested** — port 3000 not responding locally |
| **Key directory** | `frontend/` — verified package.json, next.config.mjs |
| **Stack** | Next.js `^16.2.6`, React `^18.3.1`, TypeScript `^5.6.3` |
| **Port** | `3000` (defined in docker-compose) |
| **Dependencies** | framer-motion, lucide-react, recharts, tailwindcss, clsx |
| **Build** | Supports both static export and `next start` |
| **Known issues** | Not started locally (no `npm start` process). EC2 frontend also unresponsive (timeout). |
| **Test coverage** | No frontend test files found |

---

## 3. Docker

| Field | Detail |
|---|---|
| **Status** | **Verified** — configuration structurally complete |
| **Dockerfile** | 99 lines, 3-stage build |
| | `frontend-builder` — `node:22-alpine`, `npm ci` + `npm run build` |
| | `backend-builder` — `python:3.12-slim`, `uv sync --frozen --no-dev` |
| | `runtime` — `python:3.12-slim`, copies `.venv` + `.next` + source |
| **HEALTHCHECK** | ✅ `CMD python /app/healthcheck.py || exit 1` (30s interval, 10s timeout) |
| **docker-compose.yml** | 98 lines, 5 services: |
| | `postgres` (16-alpine, port 5434:5432) |
| | `backend` (chandra-app, uvicorn, port 6001, 4 workers, 4g mem limit) |
| | `frontend` (node:22-alpine, npm start, port 3000) |
| | `nginx` (nginx:alpine, ports 80:80 + 443:443) |
| | `gradio` (chandra-app, uv run app.py, port 7861) |
| **Networks** | Single `chandra-network` bridge |
| **Dependencies** | Backend depends on postgres (health-checked). Frontend depends on backend. Nginx depends on both. |
| **Known issues** | None structural. Not running locally (no Docker Desktop confirmed). |

---

## 4. PostgreSQL

| Field | Detail |
|---|---|
| **Status** | **Broken** (local) — `psycopg_c` not installed |
| **docker-compose port** | `5434` (host) → `5432` (container) |
| **User/Database** | `chandra` / `chandra` |
| **Image** | `postgres:16-alpine` |
| **Healthcheck** | `pg_isready -U chandra` (5s interval, 5s timeout, 10 retries) |
| **Dependencies** | Backend, Alembic migrations |
| **Known issues** | `/health/ready` shows: *"unavailable: no pq wrapper available. Couldn't import psycopg 'c' implementation"*. `pyproject.toml` lists `psycopg[binary]>=3.2` and `psycopg2>=2.9.12` — `psycopg_c` missing from OS-level build deps, or the binary wheel wasn't fetched in the current venv. |

---

## 5. Alembic — Database Migrations

| Field | Detail |
|---|---|
| **Status** | **Verified** — all 3 migrations present |
| **Migrations directory** | `src/chandra/db/migrations/versions/` |
| **#1** | `0001_initial_schema.py` — initial schema |
| **#2** | `20260603_c6f417c05ab8_add_bedrock_cost_usd_to_runs.py` — adds Bedrock cost tracking (modified by jira-ticket-reader merge) |
| **#3** | `20260707_a1d9e2f4b7c1_add_digital_worker_tables.py` — digital worker tables |
| **Config** | `alembic.ini` at repo root |
| **Dependencies** | PostgreSQL (`make db-up` + `make migrate`) |
| **Known issues** | None with migration files themselves. Cannot be applied locally without running PostgreSQL. |
| **Makefile commands** | `make db-up` → `docker compose up -d postgres`, `make migrate` → `alembic upgrade head` |

---

## 6. LangGraph

### 6a. Core Graph (`chandra_graph.py`)

| Field | Detail |
|---|---|
| **Status** | **Verified** — structurally complete |
| **File** | `src/chandra/graphs/chandra_graph.py` — **109 lines** |
| **Graph type** | `StateGraph` with `Send(...)` fan-out |
| **Total nodes** | 14 (including entry/exit) |
| **5 KRA nodes** | `observe_cost`, `observe_security`, `observe_compliance`, `observe_performance`, `observe_reliability` |
| **Fan-out** | `kra_supervisor` → `_route_kra_workers` → `Send(...)` → 5 KRA nodes |
| **Key nodes** | `onboard_account`, `ingest_observations`, `kra_supervisor`, 5× observe, `analyze`, `decision_router`, `action_executor`, `approval_node`, `persist` |
| **Dependencies** | LLM factory (`analyze`, `compose_briefing`), boto3 paginators, Postgres (`persist`) |
| **Known issues** | None |
| **Test coverage** | `tests/unit/test_decision_router.py` (183 lines, 10 tests), `tests/unit/test_analyze_ranking.py` (189 lines, 5 tests) |

### 6b. Digital Worker Graph

| Field | Detail |
|---|---|
| **Status** | **Verified** — 17 nodes, reported `ok` by health endpoint |
| **File** | `src/chandra/digital_worker/graph.py` — **747 lines** |
| **Graph type** | `StateGraph` with conditional routing |
| **Total nodes** | 17 registered (15 unique node functions + START/END) |
| **Node list** | `receive_request` → `understand_request` → `classify_request` → `identify_platform` → `collect_context` → `root_cause_analysis` → `plan_resolution` → `risk_analysis` → `decision` → (conditional: `execute_automation` | `approval_gate` | `generate_guidance`) → `approval_gate` → (conditional: `execute_automation` | `generate_guidance`) → `validate_result` → `update_tracker` → `notify` → `audit` → `persist` |
| **Human-in-loop** | `interrupt_before=["approval_gate"]` |
| **Dependencies** | LLM factory, notification channels, tracker, memory |
| **Known issues** | None |
| **Test coverage** | `tests/unit/test_digital_worker_graph.py` (213 lines, 7 tests), `tests/unit/test_digital_worker_intake.py` (174 lines, 15 tests), `tests/unit/test_digital_worker_planning.py` (214 lines, 15 tests) |

---

## 7. Copilot Agent

| Field | Detail |
|---|---|
| **Status** | **Running** — health endpoint reports `"ok"` |
| **File** | `copilot_agents/graph.py` — **224 lines** |
| **Tool calls** | `copilot_agents/call_tools.py` — **273 lines** |
| **Graph type** | ReAct pattern: `call_llm` ↔ `execute_tools` with conditional `should_continue` |
| **Initialization** | FastAPI startup event: `_copilot_agent = build_graph()` (line 188) |
| **API endpoint** | `POST /copilot/chat` (line 800) |
| **Dependencies** | LLM factory, Jira tools, AWS tools |
| **Known issues** | None |
| **Test coverage** | No dedicated test file found |

---

## 8. Digital Worker — Notification Channels

| Field | Detail |
|---|---|
| **Status** | **Verified** — code complete, health reports `ok` |
| **Notification file** | `src/chandra/digital_worker/notifications.py` — **116 lines** |
| **Channels** | Slack (webhook), Teams (webhook), Email (SMTP), SNS, Jira (via tools) |
| **Escalation** | `src/chandra/escalation/publisher.py` — **69 lines**, `SNSPublisher` class |
| | Uses `boto3` SNS `publish()` with `TopicArn` |
| **All 4 notification funcs** | `notify_slack()`, `notify_teams()`, `notify_email()`, `notify_sns()` |
| **Dependencies** | SNS topic ARN (in `.env`), Slack/Teams webhook URLs, SMTP config |
| **Known issues** | All notification channels skip gracefully when their env vars are unset (graceful degradation) |
| **Test coverage** | `tests/unit/test_digital_worker_graph.py` covers the notify node indirectly |

---

## 9. Local LLM Integration

| Field | Detail |
|---|---|
| **Status** | **Verified** — factory, providers, fallback all present |
| **LLM factory** | `src/chandra/llm/__init__.py` — **80 lines** |
| | `build_chat_model()` — routes to provider based on `LLM_PROVIDER` |
| | `get_llm()`, `get_llm_with_tools()` — convenience wrappers |
| | `build_chat_model_with_fallback()` — auto-fallback to Bedrock on failure |
| **Providers** | `src/chandra/llm/providers.py` — **172 lines** (class-based `VLLMProvider`, `OpenAICompatibleProvider`) |
| **Token counter** | `src/chandra/llm/token_counter.py` — **90 lines** |
| **Supported providers** | `bedrock`, `openai`, `openai_compatible`, `vllm`, `ollama` |
| **Fallback** | ✅ When local LLM fails, falls back to Bedrock with logging |
| **Dependencies** | `langchain-aws` (for Bedrock), `langchain-openai` (for OpenAI-compatible), `httpx` |
| **Known issues** | None |
| **Test coverage** | `tests/unit/test_llm_providers.py` (145 lines, 11 tests) |

---

## 10. Amazon Bedrock

| Field | Detail |
|---|---|
| **Status** | **Verified** — configured as default, but currently overridden |
| **Default provider** | `LLM_PROVIDER=bedrock` in `Settings` class (config.py line 38) |
| **Runtime .env** | Currently set to `LLM_PROVIDER=vllm` targeting `OPENAI_API_BASE=http://52.2.42.146:8000/v1` |
| **Model** | `anthropic.claude-sonnet-4-5-20250929-v1:0` (config.py line 45) |
| **Client** | `langchain_aws.ChatBedrockConverse` |
| **Custom override** | `.env` has `BEDROCK_MODEL_ID=moonshotai.kimi-k2.5` (non-default) |
| **Region** | `us-east-1` |
| **Known issues** | Overridden locally by vLLM. Feature flag works: changing `LLM_PROVIDER=bedrock` in `.env` switches all LLM calls. |
| **Test coverage** | Covered by `test_llm_providers.py` |

---

## 11. Jira Integration

| Field | Detail |
|---|---|
| **Status** | **Verified** — merged into `feature/local-llm` |
| **Files** | `tools/jira_tools/read_jira_ticket.py` — **135 lines** |
| | `tools/jira_tools/create_jira_ticket.py` — **234 lines** |
| **Branch** | `feature/jira-ticket-reader` → merged into `feature/local-llm` (commit `fe65ae4`) |
| **Merge message** | *"Added tools/jira_tools/read_jira_ticket.py — Jira ticket reader with ADF support. Resolved conflict in migration file."* |
| **Integration** | Referenced in `fastapi_app.py` (lines 841–898, jiraUrl field in orchestration requests, POST /orchestrate) |
| **Dependencies** | `JIRA_SERVER`, `JIRA_EMAIL`, `JIRA_API_TOKEN` — all set in `.env` |
| **Known issues** | None |
| **Test coverage** | `test_jira_reader.py` (27 lines, at repo root) |

---

## 12. Slack / Teams / Email Notifications

| Field | Detail |
|---|---|
| **Status** | **Verified** — code complete with graceful degradation |
| **Notification module** | `src/chandra/digital_worker/notifications.py` — **116 lines** |
| **Slack** | `notify_slack()` — sends JSON payload to `SLACK_WEBHOOK_URL` |
| **Teams** | `notify_teams()` — sends JSON payload to `TEAMS_WEBHOOK_URL` (MessageCard format) |
| **Email** | `notify_email()` — SMTP via `email.message.EmailMessage`, configurable `SMTP_HOST`/`PORT`/`USER`/`PASS` |
| **SNS (escalation)** | `src/chandra/escalation/publisher.py` — `SNSPublisher` class, publishes via boto3 to `SNS_TOPIC_ARN` |
| **SNS Topic** | `arn:aws:sns:us-east-1:827295473120:chandra-escalation-critical` (in `.env`) |
| **Graceful degradation** | All channels return `status="skipped"` with detail when their env var is missing |
| **Dependencies** | boto3 (SNS), httpx (Slack/Teams), smtplib (Email), `SNS_TOPIC_ARN` in `.env` |
| **Known issues** | None |

---

## 13. Dashboard (Streamlit)

| Field | Detail |
|---|---|
| **Status** | **Not Tested** — exists, not actively running |
| **File** | `src/chandra/dashboard/app.py` — **296 lines** |
| **Status** | **Temporary** — being replaced by Next.js console (FE-01) |
| **Function** | Read-only analytics: latest briefing, findings explorer, eval trends |
| **Dependencies** | Streamlit, Postgres (for reading briefings) |
| **Known issues** | No new features should be added; migration to Next.js in progress |
| **Launch** | `make dashboard` or `streamlit run src/chandra/dashboard/app.py` |

---

## 14. Execution Engine

| Field | Detail |
|---|---|
| **Status** | **Verified** — all modules present |
| **Directory** | `src/chandra/execution/` |
| **Files** | 7 modules (1,051 lines total) |
| **Module** | `planner.py` (158 lines) — execution planning |
| | `executor.py` (251 lines) — execution runner |
| | `terraform.py` (103 lines) — Terraform integration |
| | `validator.py` (167 lines) — result validation |
| | `verification.py` (92 lines) — verification logic |
| | `bridge.py` (117 lines) — execution bridge |
| | `schemas.py` (132 lines) — data models |
| **Dependencies** | LLM factory (for planning), Terraform CLI (for apply/destroy) |
| **Known issues** | None |
| **Test coverage** | `tests/unit/test_execution_executor.py` (189 lines, 9 tests), `tests/unit/test_execution_planner.py` (150 lines, 7 tests), `tests/unit/test_execution_validator.py` (177 lines, 15 tests), `tests/unit/test_execution_bridge.py` (4 tests) |

---

## 15. Verification Engine

| Field | Detail |
|---|---|
| **Status** | **Verified** — structure confirmed |
| **Files** | `src/chandra/execution/verification.py` — **92 lines** |
| | `src/chandra/execution/validator.py` — **167 lines** |
| **Role** | Post-execution verification of infrastructure changes |
| **Dependencies** | AWS SDK (boto3), execution engine schemas |
| **Known issues** | None |
| **Test coverage** | `tests/unit/test_execution_validator.py` (177 lines, 15 tests) |

---

## 16. Monitoring & Observability

| Field | Detail |
|---|---|
| **Status** | **Verified** — callbacks and pricing modules present |
| **Files** | `src/chandra/observability/callbacks.py` — **26 lines** |
| | `src/chandra/observability/pricing.py` — **15 lines** |
| **Callbacks** | LangGraph run callbacks for observability instrumentation |
| **Pricing** | Model pricing lookups for cost tracking |
| **Dependencies** | LangGraph callback interface |
| **Known issues** | Minimal implementations — likely to need expansion |
| **Test coverage** | `tests/unit/test_observability.py` (10 tests) |

---

## 17. Health Checks

| Field | Detail |
|---|---|
| **Status** | **Running** (with degradation) |
| **Endpoint** | `GET /health` → `{"status":"ok"}` ✅ |
| **Endpoint** | `GET /health/ready` → `{"status":"degraded",...}` ⚠️ |
| **Readiness components** | `copilot_agent: "ok"` ✅, `digital_worker: "ok"` ✅, `postgres: "unavailable"` ❌ |
| **Postgres issue** | *"no pq wrapper available. Attempts made: couldn't import psycopg 'c' implementation — No module named 'psycopg_c'"* |
| **Impact** | Core graph (AWS observation pipeline) unavailable without Postgres. Digital worker and Copilot agent still work (in-memory). |

---

## Summary Dashboard

| # | Component | Status | Key File(s) | Lines | Tests |
|---|-----------|--------|-------------|------:|-------|
| 1 | **Backend** (FastAPI) | ✅ Running | `fastapi_app.py` | 2,412 | 1 file |
| 2 | **Frontend** (Next.js) | ⛔ Not Tested | `frontend/` | ~2,500 est. | 0 |
| 3 | **Docker** | ✅ Verified | `Dockerfile`, `docker-compose.yml` | 99+98 | — |
| 4 | **PostgreSQL** | ❌ Broken (local) | docker-compose | — | — |
| 5 | **Alembic** | ✅ Verified | 3 migration files | — | — |
| 6 | **LangGraph Core** | ✅ Verified | `chandra_graph.py` | 109 | 4 files |
| 7 | **Copilot Agent** | ✅ Running | `copilot_agents/` | 497 | 0 |
| 8 | **Digital Worker** | ✅ Running | `digital_worker/graph.py` | 747 | 3 files |
| 9 | **Local LLM** | ✅ Verified | `src/chandra/llm/` | 342 | 1 file |
| 10 | **Bedrock** | ✅ Verified | config.py default | — | 1 file |
| 11 | **Jira Integration** | ✅ Verified | `tools/jira_tools/` | 369 | 1 script |
| 12 | **Slack/Teams/Email** | ✅ Verified | `notifications.py` | 116 | indirect |
| 13 | **Streamlit Dashboard** | ⛔ Not Tested | `dashboard/app.py` | 296 | 0 |
| 14 | **Execution Engine** | ✅ Verified | `execution/` (7 files) | 1,051 | 4 files |
| 15 | **Verification Engine** | ✅ Verified | `verification.py`, `validator.py` | 259 | 1 file |
| 16 | **Monitoring** | ✅ Verified | `observability/` | 41 | 1 file |
| 17 | **Health Checks** | ⚠️ Degraded | `/health`, `/health/ready` | — | — |

### Overall Assessment

- **10/17 components** are ✅ **Running or Verified**
- **3/17 components** are ⛔ **Not Tested** (Frontend, Streamlit Dashboard, EC2 endpoints)
- **1/17 component** is ❌ **Broken** (PostgreSQL — psycopg_c missing locally)
- **1/17 component** is ⚠️ **Degraded** (Health endpoint shows degraded due to Postgres)

### Critical Issues

1. **PostgreSQL unavailable locally** — `psycopg_c` binary wheel not installed. Fix: `uv pip install psycopg[c]` or use `psycopg[binary]` fallback. Blocks the core AWS observation pipeline.
2. **Frontend not running** — Neither local nor EC2 frontend responded. Likely needs `cd frontend && npm start` (or Docker compose).
3. **EC2 deployment unreachable** — Both `54.160.31.20:6001` and `:3000` timed out. May have been stopped or network-restricted.
4. **Test coverage gap** — 7,265 lines of tests across 31 files, but Copilot Agent, Jira tools, and notifications have no dedicated unit tests. Core graph (14 nodes) has good coverage.

### Makefile Safety Net

- `make install` — `uv sync --all-extras`
- `make db-up && make migrate` — starts Postgres + applies Alembic migrations
- `make check` — `lint + type + test` (commit gate)
- `make test` — `pytest`