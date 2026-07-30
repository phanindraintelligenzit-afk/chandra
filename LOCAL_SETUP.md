# LOCAL_SETUP.md — Local Development Setup Guide

This guide walks you through setting up the **Chandra Digital Cloud Engineer Platform** for local development with a local LLM (vLLM) backend.

> **Prerequisite note:** This guide assumes you are setting up for **local LLM development** (using `LLM_PROVIDER=vllm`). If you need the production pipeline with Amazon Bedrock, see the main [README.md](./README.md) §9.

---

## Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| **Python** | 3.12+ | Backend runtime |
| **uv** | ≥ 0.4 | Python package manager (`pip install uv`) |
| **Docker** | ≥ 24 | Local Postgres database |
| **Node.js** | 20+ | Frontend (Next.js) |
| **npm** | 10+ | Frontend package manager |
| **NVIDIA GPU** | ≥ 24 GB VRAM | Local vLLM inference (e.g., T4, A10G, RTX 4090) |
| **CUDA** | 12.1+ | GPU acceleration for vLLM |
| **Git** | ≥ 2.40 | Version control |

---

## Step 1: Clone & Branch

```bash
# Clone the repository
git clone https://github.com/phanindraintelligenzit-afk/chandra.git
cd chandra

# Check out the local-llm feature branch
git checkout feature/local-llm
```

---

## Step 2: Environment Configuration

```bash
# Copy the example environment file
cp .env.example .env
```

Edit `.env` with your preferred editor and configure the following:

### Essential settings

```ini
# ── LLM Provider: set to vllm for local inference ──
LLM_PROVIDER=vllm
LLM_MODEL=google/gemma-4-12B-it-qat-w4a16-ct
LLM_TEMPERATURE=0.0

# ── vLLM Endpoint ──
VLLM_API_BASE=http://localhost:8000/v1
VLLM_MODEL=google/gemma-4-12B-it-qat-w4a16-ct
VLLM_API_KEY=not-needed

# ── Database ──
POSTGRES_URL=postgresql+psycopg://chandra:chandra@localhost:5434/chandra
DATABASE_URL=postgresql+psycopg://chandra:chandra@localhost:5434/chandra

# ── Frontend / API ──
NEXT_PUBLIC_API_URL=http://localhost:6001
FRONTEND_URL=http://localhost:3000

# ── AWS (for synthetic env) ──
AWS_DEFAULT_REGION=us-east-1
AWS_ACCESS_KEY_ID=YOUR_AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY=YOUR_AWS_SECRET_ACCESS_KEY
# AWS_PROFILE=my-sandbox
```

> **Note:** When using a local LLM, you do NOT need Bedrock model access. The `LLM_PROVIDER=vllm` setting routes all LLM calls through the vLLM OpenAI-compatible endpoint.

---

## Step 3: Backend Setup

```bash
# 3a. Install Python dependencies
uv sync --all-extras

# 3b. Start Postgres
docker compose up -d postgres

# Verify Postgres is healthy
docker compose ps postgres
# → State should show "healthy"

# 3c. Run database migrations
alembic upgrade head

# 3d. Start the FastAPI backend server
uvicorn fastapi_app:app --host 0.0.0.0 --port 6001
```

The backend starts on **http://localhost:6001**. Key endpoints:

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Liveness check |
| `GET /health/ready` | Readiness check (DB + LLM) |
| `POST /requests` | Submit a Digital Worker request |
| `GET /requests` | List all requests |
| `POST /webhooks/{source}` | Channel-native webhook intake |

---

## Step 4: Frontend Setup

Open a **new terminal** and run:

```bash
cd frontend

# Install Node dependencies
npm install

# Start the Next.js dev server
npm run dev
```

The frontend starts on **http://localhost:3000**.

---

## Step 5: vLLM Local Inference Server

Open a **third terminal** on the GPU machine and run:

```bash
vLLM_USE_FLASHINFER_SAMPLER=0 vllm serve \
  google/gemma-4-12B-it-qat-w4a16-ct \
  --gpu-memory-utilization 0.90 \
  --max-model-len 16384 \
  --enable-prefix-caching \
  --enforce-eager \
  --host 0.0.0.0 \
  --port 8000
```

**Explanation of flags:**

| Flag | Purpose |
|------|---------|
| `vLLM_USE_FLASHINFER_SAMPLER=0` | Disables FlashInfer sampler (required for Gemma 4 QAT) |
| `--gpu-memory-utilization 0.90` | Uses 90% of available GPU VRAM |
| `--max-model-len 16384` | Maximum context window: 16K tokens |
| `--enable-prefix-caching` | Caches common prefixes (speeds up repeated prompts) |
| `--enforce-eager` | Disables CUDA graph optimization (compatibility) |
| `--host 0.0.0.0 --port 8000` | Listens on all interfaces, port 8000 |

Wait for the output to show:

```
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000
```

---

## Step 6: Verify Everything

### 6a. Check vLLM is responding

```bash
curl http://localhost:8000/v1/models
```

Expected output: a JSON array containing `google/gemma-4-12B-it-qat-w4a16-ct`.

### 6b. Check backend health

```bash
curl http://localhost:6001/health
```

Expected output: `{"status":"ok"}` (or similar healthy response).

### 6c. Check backend readiness (DB + LLM)

```bash
curl http://localhost:6001/health/ready
```

### 6d. Open the frontend

Open **http://localhost:3000** in your browser. You should see the Chandra Onboarding wizard.

### 6e. Verify the LLM integration

```bash
curl -X POST http://localhost:6001/requests \
  -H 'Content-Type: application/json' \
  -d '{
    "source": "rest_api",
    "payload": {
      "title": "Test request",
      "priority": "P3",
      "resource_id": "test-123"
    },
    "dry_run": true
  }'
```

Expected: a `202 Accepted` response with a `job_id`.

---

## Running the Full Pipeline

Once all services are running:

```bash
# Run the canonical Chandra pipeline (creates synthetic env + runs detection)
make run

# Or run the demo pipeline end-to-end
make smoke
```

---

## Stopping Services

```bash
# Stop Postgres
docker compose down

# Stop vLLM
# Press Ctrl+C in the vLLM terminal

# Stop FastAPI
# Press Ctrl+C in the backend terminal

# Stop Next.js dev server
# Press Ctrl+C in the frontend terminal
```

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│  Local Development Machine                                           │
│                                                                      │
│  ┌──────────────┐     HTTP 6001     ┌───────────────────┐           │
│  │  Frontend     │ ◄──────────────► │  FastAPI Backend   │           │
│  │  localhost:3000│                  │  localhost:6001    │           │
│  │  (Next.js 16)  │                  │  (uvicorn)         │           │
│  └──────────────┘                    └───────┬───────────┘           │
│                                              │                       │
│                 ┌────────────────────────────┤                       │
│                 │                            │                       │
│                 ▼                            ▼                       │
│  ┌──────────────────────┐   ┌──────────────────────────┐            │
│  │  PostgreSQL 16        │   │  vLLM Inference Server   │           │
│  │  localhost:5434       │   │  localhost:8000           │           │
│  │  (Docker container)   │   │  (gemma-4-12B QAT)       │           │
│  └──────────────────────┘   └──────────────────────────┘            │
│                                                                      │
│  ┌──────────────────────────────────────────────────────┐           │
│  │  AWS Burner Account (synthetic env via Terraform)     │           │
│  └──────────────────────────────────────────────────────┘           │
└──────────────────────────────────────────────────────────────────────┘
```