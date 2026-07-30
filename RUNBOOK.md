# Chandra — Operations Runbook

> **Enterprise AI Cloud Operations Platform — Runbook**
> For installation, see [`INSTALL.md`](INSTALL.md). For deployment, see [`DEPLOYMENT.md`](DEPLOYMENT.md).

---

## Overview

Chandra is a multi-layered enterprise AI cloud operations platform. It observes one or more AWS accounts and emits a daily **Cloud Health Briefing** across five KRAs — **Cost, Security, Compliance, Performance, Reliability** — via a LangGraph-orchestrated autonomous pipeline. An optional **Digital Worker** module handles omnichannel request intake (Jira, Slack, Teams, email, webhooks) with human-in-the-loop approval.

### Key services

| Service | Port | Description |
|---------|------|-------------|
| FastAPI Backend | `6001` | HTTP/WS API — orchestrator + digital worker + copilot |
| Next.js Frontend | `3000` | Ops console (onboarding, dashboard, approval center) |
| PostgreSQL | `5434` | Primary database (dev Docker; RDS in prod) |
| vLLM (optional) | `8000` | Local LLM inference server |
| Gradio (legacy) | `7861` | Temporary Streamlit-equivalent (being sunset) |
| Nginx | `80` / `443` | Reverse proxy (Docker compose only) |

### Project structure (root)

```
iac/                  # Terraform: synthetic_env (eval), runtime (prod infra)
src/chandra/          # Canonical LangGraph pipeline
digitalworker_agents/ # Multi-agent orchestrator (FastAPI surface)
frontend/             # Next.js 16 ops console
scripts/              # healthcheck, smoke tests
docker-compose.yml    # Full stack: postgres + backend + frontend + nginx + gradio
```

---

## Architecture

### Pipeline flow (canonical LangGraph)

```
START → onboard_account → ingest_observations → kra_supervisor
                                                  ├── observe_cost
                                                  ├── observe_security
                                                  ├── observe_compliance
                                                  ├── observe_performance
                                                  └── observe_reliability
                                                          ↓
                                                   analyze (LLM rank + dedup)
                                                          ↓
                                                   decision_router (split → pending_writes + auto_fixed)
                                                          ↓
                                                  action_executor (consumes auto_fixed)
                                                          ↓
                                                  escalation (publishes pending_writes to SNS)
                                                          ↓
                                                  compose_briefing (LLM narrative)
                                                          ↓
                                               conditional: pending_writes → approval_node → persist → END
                                               else:                           → persist → END
```

**Key invariants:**
- `decision_router`, `action_executor`, and `escalation` are **deterministic** — no LLM calls
- `analyze` and `compose_briefing` are the only LLM-powered nodes
- All boto3 calls are paginated; all AWS clients use `chandra.aws.client_factory`
- Detector modules never call the LLM — read-only by default

### Digital Worker flow (FastAPI surface)

```
Jira · Slack · Teams · Email · REST · Monitoring · CloudWatch · Azure · GCP · Webhook
        ↓
receive_request → understand_request → classify_request → identify_platform
→ collect_context → root_cause_analysis → plan_resolution
→ risk_analysis → decision (deterministic)
→ execute_automation | approval_gate | generate_guidance
→ validate_result → update_tracker → notify → audit → persist
```

### Architecture diagram

For a full interactive architecture diagram, open `docs/architecture-diagram.html` in any browser.

---

## Quick Start

```bash
# Clone and branch
git clone https://github.com/phanindraintelligenzit-afk/chandra.git
cd chandra
git checkout feature/local-llm

# Environment
cp .env.example .env
# Edit .env: set AWS_PROFILE, SYNTHETIC_ACCOUNT_ID, LLM_PROVIDER, etc.

# Start backend
uvicorn fastapi_app:app --host 0.0.0.0 --port 6001

# Start frontend (separate terminal)
cd frontend
npm install
npm run dev
```

> **Note:** The `make install` + `make db-up` + `make migrate` sequence is the full setup path (see [INSTALL.md](INSTALL.md#backend-setup)). The `uvicorn` command above is the minimal run path once deps and DB are ready.

---

## Local LLM Setup

Chandra supports switching the LLM backend from Amazon Bedrock to a local vLLM server for development or air-gapped operation.

### Start vLLM

```bash
vLLM_USE_FLASHINFER_SAMPLER=0 vllm serve google/gemma-4-12B-it-qat-w4a16-ct \
  --gpu-memory-utilization 0.90 \
  --max-model-len 16384 \
  --enable-prefix-caching \
  --enforce-eager \
  --host 0.0.0.0 \
  --port 8000
```

### Configure .env

```
LLM_PROVIDER=vllm
LLM_MODEL=google/gemma-4-12B-it-qat-w4a16-ct
VLLM_API_BASE=http://localhost:8000/v1
VLLM_API_KEY=not-needed
```

### Supported providers

| Provider | `.env` value | Notes |
|----------|-------------|-------|
| Amazon Bedrock | `bedrock` | Default, production |
| vLLM | `vllm` | Local server (GPU required) |
| OpenAI-compatible | `openai` | Together, Groq, etc. |
| Ollama | `ollama` | Local daemon |

Adjust `CHANDRA_STRUCTURED_OUTPUT_METHOD`, `CHANDRA_TF_DOCS_MAX_CHARS`, etc. for local LLMs with smaller context windows (see `.env.example`).

---

## Docker Setup

```bash
# Full stack (postgres + backend + frontend + nginx + gradio)
docker compose up -d

# Database only (for local dev)
docker compose up -d postgres

# View logs
docker compose logs -f backend
docker compose logs -f postgres

# Tear down
docker compose down
```

The backend image is built locally via the multi-stage `Dockerfile`. See [DEPLOYMENT.md](DEPLOYMENT.md#docker-deployment) for production configuration.

---

## Health Checks

### Liveness probe
```bash
curl http://localhost:6001/health
# → {"status": "ok"}
```

Returns `200 OK` when the process is up. Does not check external dependencies — designed for container `HEALTHCHECK`.

### Readiness probe
```bash
curl http://localhost:6001/health/ready
# → {"status": "ok", "components": {"copilot_agent": "ok", "digital_worker": "ok", "postgres": "ok"}}
```

Returns `200` when all components are healthy, `503` with a `"degraded"` status and per-component detail when any dependency is unavailable.

### Frontend
```
http://localhost:3000
```

---

## Troubleshooting

### Backend won't start

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `ModuleNotFoundError` | Missing deps | `uv sync --all-extras` |
| `psycopg` connection error | Postgres not running | `docker compose up -d postgres` |
| `alembic` migration error | Schema not applied | `uv run alembic upgrade head` |
| `boto3` credential error | AWS not configured | Check `AWS_PROFILE` / `AWS_ACCESS_KEY_ID` in `.env` |
| Port conflict (6001) | Another process on port | `netstat -ano \| grep 6001` then kill or change port |

### vLLM issues

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| CUDA out of memory | GPU VRAM insufficient | Reduce `--gpu-memory-utilization` or use a smaller model |
| Model not found | Wrong model name | Verify against HuggingFace repo ID |
| Slow inference | Prefix caching off | Ensure `--enable-prefix-caching` |
| `vLLM_USE_FLASHINFER_SAMPLER` error | FlashInfer not available | Set env var to `0` as in the command above |

### Frontend issues

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Blank page / API errors | `NEXT_PUBLIC_API_URL` wrong | Set to `http://localhost:6001` in `.env` |
| Build fails | Node version mismatch | Use Node.js 22+ |
| Tailwind styles missing | PostCSS not configured | Check `postcss.config.mjs` exists |

### Database

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `psycopg` SSL error | Connection string mismatch | Verify `POSTGRES_URL` in `.env` uses `psycopg` (not `psycopg2`) driver scheme |
| Migration head mismatch | Schema drift | `uv run alembic check` to compare, then `uv run alembic upgrade head` |
| Data not persisting | Wrong checkpointer | The graph falls back to `MemorySaver` if Postgres is unreachable |

### General

- **Check logs:** `docker compose logs -f backend` or `tail -f logs/*.log`
- **Run quality gate:** `make check` (lint + type + test)
- **Verbose mode:** Set `LOG_LEVEL=DEBUG` in `.env` and restart
- **Check startup:** Review `start.sh` for PYTHONPATH and service launch order

---

## Maintenance

### Daily operations

```bash
# Check all services are running
docker compose ps

# View recent pipeline runs
uv run chandra run --account $SYNTHETIC_ACCOUNT_ID

# Run evaluation harness
uv run chandra eval --account $SYNTHETIC_ACCOUNT_ID

# View Streamlit dashboard (legacy)
uv run streamlit run src/chandra/dashboard/app.py
```

### Database migrations

```bash
# Create a new migration
uv run alembic revision --autogenerate -m "description"

# Apply pending migrations
uv run alembic upgrade head

# Check migration state
uv run alembic check

# Roll back one step
uv run alembic downgrade -1
```

### Terraform (synthetic env)

```bash
# Apply synthetic environment (seeds ~10 known misconfigs)
make tf-apply

# Destroy synthetic environment
make tf-destroy
```

### Backups

| Data | Method | Frequency |
|------|--------|-----------|
| PostgreSQL | `pg_dump` or AWS RDS automated snapshots | Daily |
| Agent memory | `agent_memory.json` — version-controlled | Per change |
| Terraform state | Remote backend (S3 + DynamoDB) | Per apply |

### Updating

```bash
git pull origin main
uv sync --all-extras
uv run alembic upgrade head
docker compose up -d --build
```

### Observability

| Tool | Configuration | Purpose |
|------|-------------|---------|
| LangSmith | `LANGCHAIN_TRACING_V2=true` in `.env` | LangGraph trace visualization |
| LangFuse | `LANGFUSE_*` keys in `.env` | LLM observability |
| AgentOps | `AGENTOPS_API_KEY` in `.env` | Agent monitoring |
| OpenTelemetry | `opentelemetry-*` deps in `pyproject.toml` | Traces + metrics export |

---

## Incident Response

### Pipeline failure
1. Check `/health` and `/health/ready` endpoints
2. Review logs: `docker compose logs backend --tail=100`
3. Check Postgres connectivity: `docker compose exec postgres pg_isready -U chandra`
4. Verify AWS credentials: `aws sts get-caller-identity`
5. For Bedrock issues: verify model access in AWS Console
6. For vLLM issues: check GPU memory with `nvidia-smi`

### HITL approval stuck
1. Verify the approval is visible in the frontend at `http://localhost:3000`
2. Check the pending requests: `curl localhost:6001/requests?status=awaiting_approval`
3. Approve/reject via API: `curl -X POST localhost:6001/requests/{job_id}/approve -d '{"approved": true}'`
4. Resume the graph thread if checkpointer supports it

### Frontend unresponsive
1. Check `docker compose logs frontend`
2. Verify `NEXT_PUBLIC_API_URL` points to a running backend
3. Clear browser cache / hard reload
4. Rebuild: `cd frontend && npm run build`

---

## Reference

- [INSTALL.md](INSTALL.md) — Step-by-step installation guide
- [DEPLOYMENT.md](DEPLOYMENT.md) — Production deployment guide
- [README.md](README.md) — Project overview
- [CLAUDE.md](CLAUDE.md) — Architectural invariants and development commands
- [TESTING.md](TESTING.md) — Testing conventions
- [TODO.md](TODO.md) — Build tracker