# Setup Guide — Chandra Enterprise Digital Cloud Engineer

**Date:** 2026-07-30  
**Branch:** `feature/local-llm`  
**Repository:** `https://github.com/phanindraintelligenzit-afk/chandra`

---

## 1. Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| Git | ≥2.30 | Version control |
| Python | ≥3.11 | Backend runtime |
| Node.js | ≥20 | Frontend build |
| Docker | ≥24 | Container runtime |
| Docker Compose | ≥2.20 | Multi-container orchestration |
| uv | ≥0.4 | Python package manager |
| PostgreSQL | 16 (Docker) | Database |
| Terraform | ≥1.5 | AWS infrastructure (optional) |
| AWS CLI | ≥2 | AWS operations (optional) |

---

## 2. Clone Repository

```bash
git clone https://github.com/phanindraintelligenzit-afk/chandra.git
cd chandra
git checkout feature/local-llm
```

---

## 3. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your values:

```bash
# Required: AWS credentials
AWS_DEFAULT_REGION="us-east-1"
AWS_ACCESS_KEY_ID="YOUR_ACCESS_KEY"
AWS_SECRET_ACCESS_KEY="YOUR_SECRET_KEY"
SYNTHETIC_ACCOUNT_ID="YOUR_BURNER_ACCOUNT_ID"

# Required: Database
POSTGRES_URL="postgresql+psycopg://chandra:chandra@localhost:5432/chandra"

# LLM Provider (default: bedrock)
LLM_PROVIDER=bedrock

# For local LLM (vLLM):
# LLM_PROVIDER=vllm
# VLLM_API_BASE=http://localhost:8000/v1
# VLLM_MODEL=Qwen/Qwen2.5-14B-Instruct
```

---

## 4. Install Dependencies

### Python Dependencies
```bash
# Install uv (if not installed)
pip install uv

# Install all dependencies
uv sync --all-extras

# Activate virtual environment
source .venv/bin/activate  # Linux/macOS
source .venv/Scripts/activate  # Git Bash on Windows
```

### Frontend Dependencies
```bash
cd frontend
npm install
cd ..
```

---

## 5. Start Database

```bash
# Start PostgreSQL container
docker compose up -d postgres

# Wait for healthy
docker compose ps postgres
# Expected: "healthy" in the Status column

# Apply migrations
alembic upgrade head
```

---

## 6. Run Backend

```bash
# Start FastAPI (port 6001)
uvicorn fastapi_app:app --host 0.0.0.0 --port 6001 --reload

# In another terminal, verify health
curl http://localhost:6001/health
# Expected: {"status": "ok"}

curl http://localhost:6001/health/ready
# Expected: {"status": "ok", "components": {...}}
```

---

## 7. Run Frontend

```bash
# Start Next.js dev server (port 3000)
cd frontend && npm run dev
```

---

## 8. Run Full Stack with Docker

```bash
# Build and start all services
docker compose up --build -d

# Verify all containers are running
docker compose ps

# Check logs
docker compose logs -f backend
```

---

## 9. Run Local LLM (vLLM)

### Option A: GPU Instance (Recommended)
```bash
# On EC2 g4dn.xlarge or similar
docker run --gpus all -p 8000:8000 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  vllm/vllm-openai:latest \
  --model Qwen/Qwen2.5-14B-Instruct \
  --max-model-len 16384 \
  --gpu-memory-utilization 0.9 \
  --enforce-eager
```

### Option B: Ollama (CPU fallback)
```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull model
ollama pull qwen2.5-coder:14b

# Start server
ollama serve
```

---

## 10. Health Checks

```bash
# Backend
curl http://localhost:6001/health
curl http://localhost:6001/health/ready

# Frontend
curl http://localhost:3000

# Database
docker exec chandra-postgres-1 pg_isready -U chandra

# Local LLM (vLLM)
curl http://localhost:8000/v1/health
```

---

## 11. Running Tests

```bash
# Unit tests
pytest tests/unit/ -v

# All tests (excluding integration)
pytest -m "not integration" -v

# With coverage
pytest --cov=src/chandra --cov-report=term-missing

# Lint
ruff check .
ruff format --check .

# Type check
mypy src/chandra/
```

---

## 12. Running the Pipeline

```bash
# Full observation pipeline
uv run chandra run --account $SYNTHETIC_ACCOUNT_ID

# With local LLM
LLM_PROVIDER=vllm uv run chandra run --account $SYNTHETIC_ACCOUNT_ID

# Eval
uv run chandra eval --account $SYNTHETIC_ACCOUNT_ID
```

---

## 13. LLM Benchmark

```bash
# Run benchmark (requires LLM endpoint)
python scripts/benchmark_llm.py \
  --fixture evals/fixtures/llm_benchmark_seed.jsonl \
  --provider vllm \
  --limit 1000 \
  --out evals/reports/
```

---

## 14. Troubleshooting Quick Reference

| Symptom | Solution |
|---------|----------|
| `ModuleNotFoundError: src.chandra` | Run `uv pip install -e .` |
| `Database "chandra" does not exist` | `docker compose up -d postgres` |
| Alembic: `Target database is not up to date` | `alembic upgrade head` |
| Docker: `port already allocated` | Change host port in docker-compose.yml |
| vLLM: `CUDA out of memory` | Add `--gpu-memory-utilization 0.7` |
| Bedrock: `AccessDeniedException` | Check AWS credentials and region |
| Frontend: API not reachable | Check `NEXT_PUBLIC_API_URL` in .env |
| `LengthFinishReasonError` | Unset `CHANDRA_AGENT_MAX_TOKENS` or increase it |
| Container restarting | Check `docker logs <container>` for crash reason |