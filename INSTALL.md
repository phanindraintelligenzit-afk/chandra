# Chandra — Installation Guide

> **Enterprise AI Cloud Operations Platform**
> Step-by-step installation from scratch.

---

## Prerequisites

| Dependency | Version | Notes |
|-----------|---------|-------|
| Python | 3.12+ | Required by `pyproject.toml` (`requires-python = ">=3.12"`) |
| Node.js | 22+ | Required by Next.js frontend |
| Docker | Latest | Required for local PostgreSQL and container builds |
| uv | Latest | Python package manager — install via `pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh` |
| Terraform | ≥ 1.5 | Required for synthetic environment and runtime IaC |
| AWS CLI | Latest | Required for AWS credential management |
| Git | Latest | For cloning the repository |

### Optional

| Dependency | Purpose |
|-----------|---------|
| NVIDIA GPU + CUDA | Local LLM inference via vLLM |
| Amazon Bedrock access | Default LLM provider (production) |
| HuggingFace token | For gated model downloads (vLLM) |

---

## Clone Repository

```bash
git clone https://github.com/phanindraintelligenzit-afk/chandra.git
cd chandra
```

---

## Environment Setup

```bash
# Copy the example environment file
cp .env.example .env
```

Edit `.env` with your values:

| Variable | Description | Required |
|----------|-------------|----------|
| `AWS_ACCESS_KEY_ID` | AWS access key | Yes (unless using `AWS_PROFILE`) |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key | Yes (unless using `AWS_PROFILE`) |
| `AWS_PROFILE` | Named AWS profile | Alternative to access key |
| `AWS_DEFAULT_REGION` | AWS region | Yes (default: `us-east-1`) |
| `SYNTHETIC_ACCOUNT_ID` | AWS account ID for eval harness | Yes |
| `SNS_TOPIC_ARN` | SNS topic for escalations | Yes |
| `LLM_PROVIDER` | LLM backend (`bedrock`, `vllm`, `openai`, `ollama`) | Yes (default: `bedrock`) |
| `POSTGRES_URL` | Database connection string | Default works for Docker Postgres |
| `NEXT_PUBLIC_API_URL` | Frontend API URL | Default: `http://localhost:6001` |

---

## Backend Setup

### 1. Install Python dependencies

```bash
cd ~/projects/chandra
uv sync
```

This installs all runtime dependencies from `pyproject.toml`. For development extras (pytest, ruff, mypy, moto):

```bash
uv sync --all-extras
```

> **Note:** `uv sync --all-extras` is equivalent to `make install`.

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your AWS credentials and any other custom values (see [Environment Setup](#environment-setup) above).

### 3. Start PostgreSQL

```bash
docker compose up -d postgres
```

This starts PostgreSQL 16 on port `5434` with user/password/database all set to `chandra`. To verify:

```bash
docker compose ps postgres
docker compose exec postgres pg_isready -U chandra
```

> **Tip:** Use `make db-up` as a shortcut.

### 4. Run database migrations

```bash
alembic upgrade head
```

Or via uv:

```bash
uv run alembic upgrade head
```

> **Tip:** Use `make migrate` as a shortcut.

### 5. Start the FastAPI backend

```bash
uvicorn fastapi_app:app --host 0.0.0.0 --port 6001
```

Or with auto-reload for development:

```bash
uvicorn fastapi_app:app --host 0.0.0.0 --port 6001 --reload
```

The backend serves:
- `/health` — liveness probe
- `/health/ready` — readiness probe (reports Postgres, copilot, digital worker status)
- `/requests` — Digital Worker API
- `/webhooks/{source}` — Webhook intake
- `POST /requests/{job_id}/approve` — HITL approval endpoint

---

## Frontend Setup

```bash
cd frontend
npm install
npm run dev
```

The frontend starts on `http://localhost:3000` with hot-reload enabled.

> **Note:** The frontend `package.json` is separate from the root-level `package.json`. Always run `npm install` from within `frontend/`.

### Build for production

```bash
cd frontend
npm run build
npm start
```

---

## vLLM Setup (Optional — Local LLM)

If you do not have access to Amazon Bedrock or want to run locally:

### 1. Install vLLM

```bash
pip install vllm
```

> Requires a CUDA-compatible GPU. For CPU-only setups, use Ollama instead.

### 2. Start the vLLM server

```bash
vLLM_USE_FLASHINFER_SAMPLER=0 vllm serve google/gemma-4-12B-it-qat-w4a16-ct \
  --gpu-memory-utilization 0.90 \
  --max-model-len 16384 \
  --enable-prefix-caching \
  --enforce-eager \
  --host 0.0.0.0 \
  --port 8000
```

### 3. Configure .env

```
LLM_PROVIDER=vllm
LLM_MODEL=google/gemma-4-12B-it-qat-w4a16-ct
VLLM_API_BASE=http://localhost:8000/v1
VLLM_API_KEY=not-needed
```

### Alternative: Ollama

```bash
# Install Ollama from https://ollama.com
ollama pull qwen2.5-coder:32b

# .env configuration
LLM_PROVIDER=ollama
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=qwen2.5-coder:32b
```

---

## Verification

### 1. Check backend health

```bash
curl http://localhost:6001/health
```

Expected response:
```json
{"status": "ok"}
```

### 2. Check readiness

```bash
curl http://localhost:6001/health/ready
```

Expected response:
```json
{"status": "ok", "components": {"copilot_agent": "ok", "digital_worker": "ok", "postgres": "ok"}}
```

### 3. Open the frontend

Navigate to `http://localhost:3000` in your browser.

### 4. Run the quality gate

```bash
make check
```

This runs `ruff check` (lint), `mypy` (type checking), and `pytest` (unit tests).

### 5. Run the synthetic environment (eval)

```bash
make tf-apply     # Creates ~10 known misconfigs in your burner AWS account
make run          # Runs the Chandra pipeline
make eval         # Scores against ground-truth manifest
```

---

## Common Installation Issues

| Issue | Fix |
|-------|-----|
| `uv: command not found` | Install uv: `pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| `Python 3.12 required` | Use `pyenv` or install Python 3.12 from python.org |
| `docker: command not found` | Install Docker Desktop from docker.com |
| `port 5434 already in use` | Change the host port in `docker-compose.yml` or stop the conflicting service |
| `alembic: command not found` | Use `uv run alembic` instead |
| `psycopg` connection error | Ensure `docker compose up -d postgres` is running and healthcheck passes |
| AWS credentials not found | Set `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` or `AWS_PROFILE` in `.env` |
| `Next.js build fails` | Ensure Node.js 22+ and run `npm install` from the `frontend/` directory |
| `vLLM CUDA error` | Check GPU with `nvidia-smi`; ensure CUDA toolkit is installed |

---

## Quick Reference

```bash
# Install all deps (backend)
make install          # uv sync --all-extras

# Start database
make db-up            # docker compose up -d postgres

# Run migrations
make migrate          # alembic upgrade head

# Start backend
uvicorn fastapi_app:app --host 0.0.0.0 --port 6001

# Start frontend (from frontend/ directory)
npm run dev

# Run tests
make test             # pytest

# Full quality gate
make check            # lint + type + test

# Run pipeline
make run              # uv run chandra run --account $SYNTHETIC_ACCOUNT_ID

# Tear down database
make db-down          # docker compose down
```