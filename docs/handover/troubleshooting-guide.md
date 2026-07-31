# Chandra — Troubleshooting Guide

> **Enterprise AI Cloud Operations Platform**
> **Branch:** `feature/local-llm`
> **Last Updated:** 2026-07-30
> **Scope:** 25+ failure scenarios organized by component with exact error messages, root causes, and solutions.

---

## Table of Contents

1. [Backend Startup Failures](#1-backend-startup-failures)
2. [Database Errors](#2-database-errors)
3. [Alembic / Migration Errors](#3-alembic--migration-errors)
4. [AWS Connection Errors](#4-aws-connection-errors)
5. [LLM Provider Errors](#5-llm-provider-errors)
6. [vLLM / Local LLM Errors](#6-vllm--local-llm-errors)
7. [Frontend / Next.js Errors](#7-frontend--nextjs-errors)
8. [Docker Errors](#8-docker-errors)
9. [LangGraph Runtime Errors](#9-langgraph-runtime-errors)
10. [Digital Worker Errors](#10-digital-worker-errors)
11. [Webhook / Integration Errors](#11-webhook--integration-errors)
12. [General Debugging](#12-general-debugging)

---

## 1. Backend Startup Failures

### F1: ModuleNotFoundError — `No module named 'chandra'`

**Error:**
```
ModuleNotFoundError: No module named 'chandra'
```

**Root Cause:** The `chandra` package is not installed in the current Python environment.

**Solution:**
```bash
# Install runtime + dev dependencies
uv sync --all-extras

# Verify the package is installed
uv run python -c "import chandra; print(chandra.__file__)"
```

**Prevention:** Always run `make install` after pulling new code or switching branches.

---

### F2: Port 6001 already in use — `address already in use`

**Error:**
```
ERROR: [Errno 10048] error while attempting to bind on address ('0.0.0.0', 6001):
address already in use
```

**Root Cause:** Another process (another uvicorn instance, or a different service) is already bound to port 6001.

**Solution:**
```bash
# Find the process using port 6001
netstat -ano | findstr :6001

# Kill the process (Windows)
taskkill /PID <PID> /F

# Kill the process (Linux/Mac)
kill -9 $(lsof -ti:6001)

# Or use a different port
uvicorn fastapi_app:app --host 0.0.0.0 --port 6002
```

**Prevention:** Check for running processes before starting the backend.

---

### F3: `psycopg.OperationalError` — connection refused

**Error:**
```
psycopg.OperationalError: connection to server at "localhost" (127.0.0.1),
port 5434 failed: Connection refused
  Is the server running on that host and accepting TCP/IP connections?
```

**Root Cause:** PostgreSQL is not running, or the connection string is wrong.

**Solution:**
```bash
# Check if Postgres container is running
docker ps | grep postgres

# If not running, start it
docker compose up -d postgres

# Wait for it to become healthy
sleep 5
docker compose ps postgres
# → Should show "healthy"

# Check the connection string in .env
grep POSTGRES_URL .env
# → postgresql+psycopg://chandra:chandra@localhost:5434/chandra
```

**Prevention:** Run `make db-up` before starting the backend.

---

### F4: `ImportError` — pydantic version mismatch

**Error:**
```
ImportError: cannot import name 'BaseModel' from 'pydantic'
```

**Root Cause:** Pydantic v1/v2 incompatibility. The wrong pydantic version is installed.

**Solution:**
```bash
# Force reinstall with correct dependency resolution
uv sync --all-extras

# Check pydantic version
uv run python -c "import pydantic; print(pydantic.__version__)"
# → Should be 2.x
```

**Prevention:** Use `uv sync --all-extras` instead of pip, which respects version pins from `pyproject.toml`.

---

### F5: Backend starts but returns 500 on `/health`

**Error:**
```
curl http://localhost:6001/health
# → 500 Internal Server Error
```

**Root Cause:** Application-level error during startup — typically missing env vars or failed imports.

**Solution:**
```bash
# Check the uvicorn terminal for the traceback

# Common causes:
# 1. Missing DATABASE_URL in .env
grep DATABASE_URL .env

# 2. LLM_PROVIDER=bedrock without AWS credentials
grep LLM_PROVIDER .env
aws sts get-caller-identity  # Verify AWS works

# 3. Import error in a module
# Check for missing dependencies or syntax errors
uv run python -c "from fastapi_app import app"
```

---

### F6: `No module named 'pydantic_settings'`

**Error:**
```
ModuleNotFoundError: No module named 'pydantic_settings'
```

**Root Cause:** The `pydantic-settings` package is not installed.

**Solution:**
```bash
uv sync --all-extras
```

**Prevention:** The dependency is listed in `pyproject.toml` under `[project.dependencies]`. `uv sync` installs it.

---

## 2. Database Errors

### F7: PostgreSQL password authentication failed

**Error:**
```
sqlalchemy.exc.OperationalError: (psycopg.OperationalError)
FATAL: password authentication failed for user "chandra"
```

**Root Cause:** The password in `POSTGRES_URL` doesn't match the password configured in `docker-compose.yml`.

**Solution:**
```bash
# Verify .env values match docker-compose.yml
grep POSTGRES_URL .env
# → postgresql+psycopg://chandra:chandra@localhost:5434/chandra

# Check docker-compose.yml
grep -A3 POSTGRES_PASSWORD docker-compose.yml
# → POSTGRES_PASSWORD: chandra

# Both must use the same password ("chandra" is the default)
```

**Note the port:** The host port is **5434** (not the default 5432), because Docker maps `5434:5432`.

---

### F8: PostgreSQL connection timeout

**Error:**
```
psycopg.OperationalError: connection to server at "localhost" (127.0.0.1),
port 5434 failed: timeout expired
```

**Root Cause:** Network issue, firewall blocking, or Postgres not fully started.

**Solution:**
```bash
# Check if Postgres is actually running
docker compose ps postgres
# → If "starting", wait longer (up to 30s for first start)

# Check Postgres logs
docker compose logs postgres --tail=20

# If using Windows, check Windows Defender Firewall
# Ensure Docker Desktop has network access

# Try increasing the healthcheck timeout in docker-compose.yml:
# healthcheck:
#   interval: 10s
#   timeout: 10s
#   retries: 20
```

---

### F9: SQLAlchemy `relation does not exist`

**Error:**
```
sqlalchemy.exc.ProgrammingError: (psycopg.errors.UndefinedTable)
relation "runs" does not exist
```

**Root Cause:** Database schema hasn't been created or migrations haven't been applied.

**Solution:**
```bash
# Apply pending migrations
uv run alembic upgrade head

# Check migration status
uv run alembic current
# → If blank, no migrations have been applied

# List tables to verify
docker compose exec postgres psql -U chandra -c "\dt"
```

**Prevention:** Always run `make migrate` after setting up a fresh database.

---

### F10: `database is locked` (SQLite)

**Error:**
```
sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) database is locked
```

**Root Cause:** The code is running against SQLite (likely in tests) and there's concurrent write contention.

**Solution:**
```bash
# This only happens with SQLite checkpointer (MemorySaver fallback)
# Check logs for "fallback to MemorySaver"

# Ensure Postgres is available — the graph should use PostgresSaver
docker compose ps postgres
```

**Prevention:** In production, always use Postgres as the checkpointer backend.

---

## 3. Alembic / Migration Errors

### F11: `Target database is not up to date`

**Error:**
```
ERROR [alembic.util.messaging] Target database is not up to date.
```

**Root Cause:** Pending migrations exist but haven't been applied.

**Solution:**
```bash
# Apply pending migrations
alembic upgrade head

# Check current migration status
alembic current

# View migration history
alembic history
```

---

### F12: `No migrations to apply` — but tables are missing

**Error:**
```
INFO  [alembic.runtime.migration] No migrations to apply.
```
But tables like `runs`, `findings`, etc. don't exist.

**Root Cause:** Migration head is set but the actual schema wasn't created. The database was wiped after the last migration.

**Solution:**
```bash
# Force autogenerate a new migration
alembic revision --autogenerate -m "force_rebuild_schema"

# Then apply it
alembic upgrade head

# Or stamp to the latest revision and re-migrate
alembic stamp head
alembic upgrade head
```

---

### F13: Alembic migration fails with foreign key error

**Error:**
```
psycopg.errors.ForeignKeyViolation: insert or update on table "findings"
violates foreign key constraint "fk_findings_run_id_runs"
```

**Root Cause:** Trying to drop or alter a table that has foreign key references from existing data.

**Solution:**
```bash
# 1. Back up the database
docker compose exec -T postgres pg_dump -U chandra chandra > backup.sql

# 2. Identify the conflicting data
docker compose exec postgres psql -U chandra -c "
  SELECT * FROM findings WHERE run_id NOT IN (SELECT id FROM runs);
"

# 3. Clean up orphaned records or handle the migration manually
# 4. Re-run the migration
alembic upgrade head
```

---

## 4. AWS Connection Errors

### F14: `NoCredentialsError` — Unable to locate credentials

**Error:**
```
botocore.exceptions.NoCredentialsError: Unable to locate credentials
```

**Root Cause:** No AWS credentials configured in `.env` or environment.

**Solution:**
```bash
# Check if AWS CLI is configured
aws sts get-caller-identity

# If not, configure credentials
aws configure

# Or set in .env:
# AWS_ACCESS_KEY_ID=your-access-key
# AWS_SECRET_ACCESS_KEY=your-secret-key
# AWS_DEFAULT_REGION=us-east-1

# Or use a named profile:
# AWS_PROFILE=my-sandbox
```

---

### F15: `ProfileNotFound` — Config profile not found

**Error:**
```
botocore.exceptions.ProfileNotFound: The config profile (my-sandbox)
could not be found
```

**Root Cause:** The `AWS_PROFILE` set in `.env` doesn't exist in `~/.aws/config`.

**Solution:**
```bash
# List available profiles
aws configure list-profiles

# Check what's in .env
grep AWS_PROFILE .env

# Fix: set AWS_PROFILE to an existing profile, or comment it out
# to use the default profile
```

---

### F16: `AccessDenied` — Insufficient permissions

**Error:**
```
ClientError: An error occurred (AccessDenied) when calling the ... operation:
User: ... is not authorized to perform: ...
```

**Root Cause:** The AWS IAM user/role doesn't have the required permissions.

**Solution:**
```bash
# Check the current user/role
aws sts get-caller-identity

# Required permissions for Chandra:
# - ReadOnlyAccess (arn:aws:iam::aws:policy/ReadOnlyAccess)
# - S3 write access (for demo execution)
# - SNS:Publish (for the escalation topic)

# For the synthetic environment, use AdminAccess
# For production, use the curated chandra_runtime role
```

**Prevention:** Use a burner/sandbox account for development. Attach the `ReadOnlyAccess` managed policy as a minimum.

---

### F17: Terraform `AccessDenied` (synthetic env)

**Error:**
```
Error: Error creating S3 bucket: AccessDenied: Access Denied
```

**Root Cause:** The AWS credentials don't have permission to create synthetic test resources.

**Solution:**
```bash
# Verify credentials
aws sts get-caller-identity

# The synthetic env needs permissions to create:
# - S3 buckets
# - EC2 instances
# - IAM roles
# - CloudWatch alarms
# - RDS instances
# - KMS keys

# Use a burner account with AdminAccess for the synthetic env
```

---

## 5. LLM Provider Errors

### F18: `Bedrock` — Model access not granted

**Error:**
```
botocore.errorfactory.AccessDeniedException: User is not authorized
to perform bedrock:InvokeModel on model
anthropic.claude-sonnet-4-5-20250929-v1:0
```

**Root Cause:** The AWS account hasn't been granted access to the Claude Sonnet 4.5 model in Amazon Bedrock.

**Solution:**
```bash
# Check available Bedrock models
aws bedrock list-foundation-models --region us-east-1 \
  --query "modelSummaries[?providerName=='Anthropic']"

# Request model access via AWS Console:
# 1. Go to AWS Bedrock Console
# 2. Click "Model access" in the left sidebar
# 3. Request access to Claude Sonnet 4.5

# Alternatively, switch to a different LLM provider:
# LLM_PROVIDER=vllm
```

---

### F19: `LLM_PROVIDER` not set correctly

**Error:**
```
ValueError: Unsupported LLM_PROVIDER 'xxx'; expected one of bedrock,
openai, openai_compatible, vllm, ollama
```

**Root Cause:** The `LLM_PROVIDER` environment variable is set to an unsupported value.

**Solution:**
```bash
# Check the current value
grep LLM_PROVIDER .env

# Set to a supported value:
# LLM_PROVIDER=bedrock    (default, production)
# LLM_PROVIDER=vllm       (local inference)
# LLM_PROVIDER=openai     (OpenAI-compatible)
# LLM_PROVIDER=ollama     (Ollama daemon)
```

---

### F20: LLM returns empty response

**Error:**
```json
{"choices":[{"index":0,"message":{"content":"","role":"assistant"},"finish_reason":"stop"}]}
```

**Root Cause:** The LLM context window is exceeded, or the input is malformed.

**Solution:**
```bash
# For vLLM: reduce input size or increase max-model-len
# Check if token budget caps are being applied

# For Bedrock: check the input token count
# Reduce the prompt size

# Check logs for truncation warnings
docker compose logs backend | grep -i truncat
```

---

## 6. vLLM / Local LLM Errors

### F21: vLLM port 8000 connection refused

**Error:**
```
curl: (7) Failed to connect to localhost port 8000: Connection refused
```

**Root Cause:** vLLM server is not running.

**Solution:**
```bash
# Start vLLM
vLLM_USE_FLASHINFER_SAMPLER=0 vllm serve google/gemma-4-12B-it-qat-w4a16-ct \
  --gpu-memory-utilization 0.90 \
  --max-model-len 16384 \
  --enable-prefix-caching \
  --enforce-eager \
  --host 0.0.0.0 \
  --port 8000

# Wait for "Application startup complete" before testing
curl http://localhost:8000/v1/models
```

---

### F22: `FlashInfer sampler` not supported

**Error:**
```
RuntimeError: FlashInfer sampling is not supported for this model
```

**Root Cause:** The Gemma 4 QAT model is incompatible with FlashInfer sampling.

**Solution:**
```bash
# Set the environment variable before starting vLLM
vLLM_USE_FLASHINFER_SAMPLER=0 vllm serve google/gemma-4-12B-it-qat-w4a16-ct ...
```

**Prevention:** Always set `vLLM_USE_FLASHINFER_SAMPLER=0` when using Gemma 4 QAT models.

---

### F23: `CUDA out of memory`

**Error:**
```
torch.cuda.OutOfMemoryError: CUDA out of memory.
Tried to allocate 256.00 MiB. GPU 0 has a total capacity of 23.65 GiB.
```

**Root Cause:** The GPU doesn't have enough VRAM to load the model.

**Solution:**
```bash
# Reduce GPU memory utilization
vLLM_USE_FLASHINFER_SAMPLER=0 vllm serve google/gemma-4-12B-it-qat-w4a16-ct \
  --gpu-memory-utilization 0.70 \   # Lower from 0.90 to 0.70
  --max-model-len 8192 \            # Reduce context window by half
  --enable-prefix-caching \
  --enforce-eager \
  --host 0.0.0.0 --port 8000

# Check available GPU memory
nvidia-smi

# If still OOM, try a smaller model:
# LLM_MODEL=Qwen/Qwen2.5-7B-Instruct
```

---

### F24: `The model's max seq length is too large`

**Error:**
```
ValueError: The model's max seq length is too large ... available model length ...
```

**Root Cause:** The `--max-model-len` setting exceeds the GPU's capacity.

**Solution:**
```bash
# Reduce the context window
vLLM_USE_FLASHINFER_SAMPLER=0 vllm serve google/gemma-4-12B-it-qat-w4a16-ct \
  --max-model-len 8192 \
  ...
```

---

### F25: HuggingFace model download fails — 401 Unauthorized

**Error:**
```
HuggingFaceHubError: 401 Client Error: Unauthorized
```

**Root Cause:** The Gemma 4 QAT model requires accepting the license on HuggingFace.

**Solution:**
```bash
# Log in to HuggingFace
huggingface-cli login
# Enter your HuggingFace token when prompted

# Accept the Gemma license:
# 1. Go to https://huggingface.co/google/gemma-4-12B-it-qat-w4a16-ct
# 2. Click "Agree and access repository"
# 3. After accepting, try downloading again
```

---

### F26: vLLM is very slow

**Error (observed):** Each LLM call takes 30+ seconds.

**Root Cause:** Missing `--enable-prefix-caching`, or CPU fallback (no GPU detected).

**Solution:**
```bash
# Check GPU utilization
nvidia-smi -l 1
# → If 0%, the model is running on CPU

# Ensure --enable-prefix-caching is set (speeds up repeated prompts)
# Ensure --enforce-eager is set (compatibility with Gemma 4 QAT)

# Check if there are other GPU processes consuming VRAM
nvidia-smi

# Reduce --max-model-len if the full context isn't needed
```

---

## 7. Frontend / Next.js Errors

### F27: `npm install` fails — `ERESOLVE`

**Error:**
```
npm ERR! code ERESOLVE
npm ERR! ERESOLVE unable to resolve dependency tree
```

**Root Cause:** Peer dependency conflicts.

**Solution:**
```bash
cd frontend

# Clear npm cache
npm cache clean --force

# Use --legacy-peer-deps to bypass strict resolution
npm install --legacy-peer-deps

# Or delete node_modules and reinstall
rm -rf node_modules package-lock.json
npm install
```

---

### F28: TypeScript compilation errors

**Error:**
```
error TS2322: Type 'X' is not assignable to type 'Y'
```

**Root Cause:** Type mismatch in component props or state.

**Solution:**
```bash
cd frontend

# Check TypeScript version
npx tsc --version

# Run TypeScript compiler in verbose mode
npx tsc --noEmit

# Common fixes:
# 1. Update TypeScript: npm install typescript@latest --save-dev
# 2. Check for type mismatches in component props
# 3. Ensure @types packages are installed:
npm install @types/node @types/react @types/react-dom --save-dev
```

---

### F29: Next.js build fails — `Page could not be resolved`

**Error:**
```
Error: Page could not be resolved
```

**Root Cause:** Missing or misnamed page file in `frontend/app/`.

**Solution:**
```bash
cd frontend

# Clear Next.js cache
rm -rf .next

# Check for missing page files
ls app/

# Ensure each route has a page.tsx:
# app/page.tsx (root → redirect to /onboarding)
# app/onboarding/page.tsx
# app/dashboard/page.tsx
# app/aws-tasks/page.tsx
# app/aws-permissions/page.tsx
# app/execution-review/page.tsx
# app/deployment/page.tsx
# app/executions/page.tsx
```

---

### F30: Frontend dev server starts but API calls fail

**Error (browser console):**
```
GET http://localhost:6001/health net::ERR_CONNECTION_REFUSED
```

**Root Cause:** The backend is not running, or `NEXT_PUBLIC_API_URL` is set wrong.

**Solution:**
```bash
# 1. Ensure the backend is running on port 6001
curl http://localhost:6001/health

# 2. Check NEXT_PUBLIC_API_URL in .env
grep NEXT_PUBLIC_API_URL .env
# → NEXT_PUBLIC_API_URL=http://localhost:6001

# 3. Restart the frontend dev server after changing .env
cd frontend && npm run dev

# 4. Check CORS configuration in fastapi_app.py
# The backend has CORS middleware — ensure it's configured correctly
```

---

### F31: Blank page — no errors in console

**Error:** Frontend loads but shows a blank white page.

**Root Cause:** React hydration error or missing provider.

**Solution:**
```bash
# Check browser console for React warnings
# Open DevTools → Console

# Common causes:
# 1. OnboardingProvider not wrapping the app — check app/layout.tsx
# 2. Missing globals.css import — check app/layout.tsx
# 3. Client component that references server-only APIs

# Fix: clear browser cache and hard reload (Ctrl+Shift+R)
# Or check if the app compiles locally
cd frontend && npm run build
```

---

## 8. Docker Errors

### F32: Docker daemon not running

**Error:**
```
Cannot connect to the Docker daemon at unix:///var/run/docker.sock.
Is the docker daemon running?
```

**Root Cause:** Docker Desktop is not running.

**Solution:**
- **Windows:** Start "Docker Desktop" from the Start Menu
- **Linux:** `sudo systemctl start docker`
- **macOS:** Start "Docker Desktop" from Applications

**Verification:**
```bash
docker info
```

---

### F33: Port conflict — `port is already allocated`

**Error:**
```
Error response from daemon: driver failed programming external connectivity
on endpoint (port is already allocated)
```

**Root Cause:** Another process is using the same port (e.g., 5434, 6001, 3000).

**Solution:**
```bash
# Find the process using the port
# Windows:
netstat -ano | findstr :5434

# Linux/Mac:
lsof -i :5434

# Kill the process, or change the port mapping in docker-compose.yml
# e.g., change "5434:5432" to "5435:5432"
```

---

### F34: Docker volume permissions

**Error:**
```
Error: permission denied while trying to connect to the Docker daemon socket
```

**Root Cause:** The current user doesn't have Docker permissions.

**Solution:**
- **Windows:** Ensure Docker Desktop is running with Windows containers
- **Linux:** Add your user to the docker group:
  ```bash
  sudo usermod -aG docker $USER
  # Log out and back in
  ```

---

### F35: Container exits immediately

**Error:**
```
docker compose ps
# → State: "Exited"
```

**Root Cause:** Port conflict, volume permissions, or insufficient memory.

**Solution:**
```bash
# Check logs for the specific error
docker compose logs backend --tail=50

# Common issues:
# 1. Port conflict — change port mapping
# 2. Volume permissions — reset with docker compose down -v
# 3. Insufficient memory — increase Docker Desktop memory limit
# 4. Missing .env file — ensure .env exists with required vars
```

---

### F36: `backend` service unable to resolve `postgres` hostname

**Error (backend logs):**
```
psycopg.OperationalError: connection to server at "postgres" (xx.xx.xx.xx),
port 5432 failed: Connection refused
```

**Root Cause:** The backend container tries to connect to Postgres before it's healthy.

**Solution:**
```yaml
# In docker-compose.yml, ensure the depends_on condition is set:
services:
  backend:
    depends_on:
      postgres:
        condition: service_healthy
```

The compose file already has this configured. If it still fails, check Postgres health:

```bash
docker compose ps postgres
docker compose logs postgres --tail=20
```

---

## 9. LangGraph Runtime Errors

### F37: `CheckpointError` — Unable to read checkpoint

**Error:**
```
langgraph.errors.CheckpointError: Unable to read checkpoint
```

**Root Cause:** PostgresSaver can't read the checkpointer table — database is down or not migrated.

**Solution:**
```bash
# Check if Postgres is running and migrated
docker compose ps postgres
uv run alembic check

# The graph will fall back to MemorySaver if Postgres is unreachable
# Check logs for "fallback to MemorySaver"

# If using MemorySaver, checkpoints are lost on restart — this is expected
# for development but not production
```

---

### F38: `Send()` fan-out fails — `NoneType not iterable`

**Error:**
```
TypeError: Send() argument after * must be an iterable, not NoneType
```

**Root Cause:** The `kra_supervisor` node returned `None` instead of a list of `Send(...)` objects.

**Solution:**
- Check the `_route_kra_workers` function in `src/chandra/graphs/action_nodes.py`
- Verify that KRA names match the graph node names exactly
- Ensure all five KRA workers are defined in the graph
- Check that `selected_kras` in the state is a non-empty list

---

### F39: Graph execution hangs — stuck at approval gate

**Error (observed):** The pipeline starts but never completes. No errors in logs.

**Root Cause:** The graph is paused at the `approval_node` waiting for human input, and no one approves or rejects.

**Solution:**
```bash
# Check if there are pending approvals
curl http://localhost:6001/requests?status=awaiting_approval

# Approve or reject via API
curl -X POST http://localhost:6001/requests/<job_id>/approve \
  -H 'Content-Type: application/json' \
  -d '{"approved": true}'

# OR via the frontend
# Navigate to http://localhost:3000 → Approval Center
```

**Prevention:** Set `CHANDRA_AUTO_APPROVE=1` in `.env` for development (never in production).

---

### F40: LangGraph `interrupt_before` not pausing

**Error (observed):** The approval gate is reached but the graph continues to `persist` without pausing.

**Root Cause:** `interrupt_before=["approval_node"]` missing from `graph.compile()` or the checkpointer is not configured.

**Solution:**
```bash
# Verify the graph compilation in chandra_graph.py:
# graph.compile(checkpointer=saver, interrupt_before=["approval_node"])

# Ensure a real checkpointer is being used (not None)
# MemorySaver is sufficient for development

# Check that the checkpointer is initialized before the graph runs
```

---

### F41: LLM call within LangGraph fails

**Error:**
```
langgraph.graph.GraphRecursionError: Maximum recursion depth reached
```

**Root Cause:** An LLM call is stuck in a retry loop, causing the graph to exceed its recursion limit.

**Solution:**
```bash
# Check which LLM provider is configured
grep LLM_PROVIDER .env

# Test the LLM endpoint directly
# For vLLM:
curl http://localhost:8000/v1/models
curl http://localhost:8000/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"google/gemma-4-12B-it-qat-w4a16-ct","prompt":"Hello","max_tokens":10}'

# For Bedrock:
aws bedrock-runtime invoke-model \
  --model-id anthropic.claude-sonnet-4-5-20250929-v1:0 \
  --body '{"anthropic_version":"bedrock-2023-05-31","max_tokens":10,"messages":[{"role":"user","content":"Hello"}]}' \
  output.json

# If the LLM is down, the graph will retry and eventually fail
# Increase recursion limit or fix the LLM connectivity
```

---

## 10. Digital Worker Errors

### F42: Request submission returns 422 — validation error

**Error:**
```json
{"detail": [{"type": "missing", "loc": ["body", "payload"], "msg": "Field required"}]}
```

**Root Cause:** The request payload doesn't match the `CloudRequest` schema.

**Solution:**
```bash
# Check the required fields:
# source: one of "jira", "slack", "teams", "email", "rest_api", "cloudwatch",
#         "azure_monitor", "gcp_monitoring", "webhook"
# payload.title: string (required)
# payload.priority: one of "P0", "P1", "P2", "P3", "P4" (optional, defaults to P3)
# payload.resource_id: string (required)

# Example valid request:
curl -X POST http://localhost:6001/requests \
  -H 'Content-Type: application/json' \
  -d '{
    "source": "rest_api",
    "payload": {
      "title": "Test task",
      "priority": "P3",
      "resource_id": "test-123"
    }
  }'
```

---

### F43: Webhook endpoint returns 401

**Error:**
```json
{"detail": "Unauthorized"}
```

**Root Cause:** `CHANDRA_WEBHOOK_TOKEN` is set in `.env`, and the webhook request is missing the auth header.

**Solution:**
```bash
# Include the auth header in webhook requests
curl -X POST http://localhost:6001/webhooks/slack \
  -H 'Content-Type: application/json' \
  -H 'X-Chandra-Webhook-Token: your-secret-token' \
  -d '{...}'

# OR disable webhook auth by commenting out CHANDRA_WEBHOOK_TOKEN in .env:
# CHANDRA_WEBHOOK_TOKEN=
```

---

### F44: Webhook payload not processed — `Unsupported webhook source`

**Error:**
```json
{"detail": "Unsupported webhook source"}
```

**Root Cause:** The webhook source in the URL is not in the list of supported channels.

**Solution:**
```bash
# Supported sources:
# - jira, slack, teams, email, cloudwatch, azure_monitor, gcp_monitoring, rest_api, webhook

# The URL format is: POST /webhooks/{source}
# e.g., POST /webhooks/slack, POST /webhooks/jira

# Check the SUPPORTED_SOURCES constant in:
# src/chandra/digital_worker/intake.py
```

---

### F45: Digital Worker graph not loaded on startup

**Error (on `/health/ready`):**
```json
{
  "status": "degraded",
  "components": {
    "copilot_agent": "ok",
    "digital_worker": "unavailable",
    "postgres": "ok"
  }
}
```

**Root Cause:** The Digital Worker graph failed to import or compile.

**Solution:**
```bash
# Check the backend logs for the import error
docker compose logs backend | grep -i "digital_worker"

# Common causes:
# 1. Missing prompt files in src/chandra/prompts/
# 2. Pydantic model validation error in schemas
# 3. Missing dependencies

# Test the import directly:
uv run python -c "from src.chandra.digital_worker.graph import build_digital_worker_graph; print('OK')"
```

---

## 11. Webhook / Integration Errors

### F46: Jira integration fails — HTTP 401

**Error:**
```
jira.exceptions.JiraError: HTTP 401: Unauthorized
```

**Root Cause:** Jira credentials are incorrect.

**Solution:**
```bash
# Verify Jira credentials in .env
grep -E "^(JIRA_SERVER|JIRA_EMAIL|JIRA_API_TOKEN)" .env

# The JIRA_API_TOKEN is NOT your Atlassian password.
# Generate one at: https://id.atlassian.com/manage/api-tokens

# Test the connection
curl -u your-email@company.com:your-api-token \
  https://your-domain.atlassian.net/rest/api/2/project
```

---

### F47: Jira server unreachable

**Error:**
```
jira.exceptions.JiraError: HTTP 404:
```

**Root Cause:** The `JIRA_SERVER` URL is incorrect or the server is behind a corporate proxy.

**Solution:**
```bash
# Verify the JIRA_SERVER is accessible
curl -I https://your-domain.atlassian.net

# If behind a corporate proxy, set proxy environment variables:
# HTTP_PROXY=http://proxy.company.com:8080
# HTTPS_PROXY=http://proxy.company.com:8080
```

---

### F48: Jira ticket creation fails — `Field 'project' cannot be set`

**Error:**
```
jira.exceptions.JiraError: Issues: Field 'project' cannot be set.
```

**Root Cause:** The authenticated Jira user doesn't have permission to create issues in the specified project, or the project key is wrong.

**Solution:**
- Ensure the project key in the Jira ticket exists
- Check that the authenticated user has permission to create issues in the project
- Verify the project key is case-sensitive

---

## 12. General Debugging

### Enabling verbose logging

```bash
# .env
LOG_LEVEL=DEBUG
UVICORN_LOG_LEVEL=debug

# Or at runtime
uvicorn fastapi_app:app --host 0.0.0.0 --port 6001 --log-level debug
```

### System health checklist

```bash
# 1. Is Postgres running?
docker compose ps postgres

# 2. Is the backend running?
curl http://localhost:6001/health

# 3. Are all components ready?
curl http://localhost:6001/health/ready

# 4. Is vLLM running (if using local LLM)?
curl http://localhost:8000/v1/models

# 5. Is the frontend running?
curl -s -o /dev/null -w "%{http_code}" http://localhost:3000

# 6. Are AWS credentials configured?
aws sts get-caller-identity

# 7. Is .env configured?
grep -E "^(DATABASE_URL|LLM_PROVIDER|VLLM_API_BASE|SYNTHETIC_ACCOUNT_ID)" .env

# 8. Are all dependencies installed?
uv run python -c "import chandra; import fastapi; import langgraph; import boto3; print('All deps OK')"
```

### Quick reset (nuke everything)

```bash
# Stop all services
docker compose down -v

# Remove Python caches
rm -rf .pytest_cache .ruff_cache .mypy_cache

# Remove frontend caches
cd frontend && rm -rf node_modules .next && cd ..

# Remove Python virtualenv and reinstall
rm -rf .venv
uv sync --all-extras

# Start fresh
docker compose up -d postgres
sleep 5
uv run alembic upgrade head
cd frontend && npm install && cd ..

# Start the backend
uvicorn fastapi_app:app --host 0.0.0.0 --port 6001
```

### Essential log patterns to grep for

| Log pattern | What it means | Action |
|-------------|---------------|--------|
| `ERROR` | Unhandled exception | Investigate immediately |
| `fallback_to_MemorySaver` | Postgres checkpointer unavailable | Check Postgres |
| `bedrock_unavailable_fallback_to_deterministic` | Bedrock API limit hit | Check AWS Bedrock quota |
| `llm.complete_attempt_failed` | LLM call retrying | Check LLM connectivity |
| `graph.*.completed` | Pipeline stage completed | Normal operation |
| `CheckpointError` | Checkpointer read/write failure | Check Postgres |
| `Send() argument after * must be an iterable` | KRA fan-out failed | Check kra_supervisor |
| `CUDA out of memory` | GPU VRAM exhausted | Restart vLLM with lower utilization |
| `Connection refused` | Service not running | Start the service |
| `relation .* does not exist` | Migration not applied | Run `alembic upgrade head` |

### Error-to-component quick reference

| Error pattern | Likely component | Section |
|---------------|-----------------|---------|
| `ModuleNotFoundError` | Python environment | F1 |
| `port 6001: address already in use` | Backend | F2 |
| `connection refused` on port 5434 | PostgreSQL | F3/F8 |
| `Unable to locate credentials` | AWS | F14 |
| `CUDA out of memory` | vLLM / GPU | F23 |
| `FlashInfer sampler` | vLLM | F22 |
| `ERESOLVE` | Frontend npm | F27 |
| `CheckpointError` | LangGraph / Postgres | F37 |
| `status: degraded` | Digital Worker / Postgres | F45 |
| `Send() ... NoneType` | LangGraph graph | F38 |
| `Field 'project' cannot be set` | Jira integration | F48 |

### Still stuck?

1. **Check the logs** — Most issues leave a trace in the backend logs (`docker compose logs backend --tail=100`)
2. **Search the docs** — `docs/` directory contains deployment guides, architecture diagrams, and the runbook
3. **Run the quality gate** — `make check` (lint + type + test) catches many issues early
4. **Try the quick reset** — If nothing else works, the "Quick reset" procedure above restores a clean state
5. **Ask in the team channel** — Include the error message, relevant logs, and what you've tried
6. **Open a GitHub issue** — Include the full error traceback and steps to reproduce