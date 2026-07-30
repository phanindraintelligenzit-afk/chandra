# TROUBLESHOOTING.md — Common Issues & Solutions

Common issues encountered when setting up and running the **Chandra Digital Cloud Engineer Platform**, along with their solutions.

---

## Table of Contents

- [Backend Won't Start](#backend-wont-start)
- [Frontend Won't Build](#frontend-wont-build)
- [Local LLM Won't Connect](#local-llm-wont-connect)
- [Database Errors](#database-errors)
- [Jira Integration Fails](#jira-integration-fails)
- [Webhook Errors](#webhook-errors)
- [Docker Issues](#docker-issues)
- [AWS Connection Issues](#aws-connection-issues)
- [LangGraph Runtime Errors](#langgraph-runtime-errors)
- [vLLM Specific Issues](#vllm-specific-issues)

---

## Backend Won't Start

### Symptom: `uvicorn` fails to bind to port 6001

```
ERROR: [Errno 10048] error while attempting to bind on address ('0.0.0.0', 6001): 
address already in use
```

**Solution:**
```bash
# Find the process using port 6001
netstat -ano | findstr :6001

# Kill the process (replace PID with the actual process ID)
taskkill /PID <PID> /F

# Or use a different port
uvicorn fastapi_app:app --host 0.0.0.0 --port 6002
```

### Symptom: `uvicorn` starts but returns 500 on health check

**Check .env configuration:**
```bash
# Verify .env exists and has the correct values
cat .env | grep -E "^(DATABASE_URL|LLM_PROVIDER|POSTGRES_URL)"

# Ensure AWS credentials are set (if using Bedrock)
# Ensure LLM_PROVIDER is set correctly (vllm vs bedrock)
```

**Common mistakes:**
- Missing `DATABASE_URL` in `.env`
- `LLM_PROVIDER=bedrock` without AWS credentials (switch to `vllm` for local dev)
- `POSTGRES_URL` pointing to wrong host/port

### Symptom: ModuleNotFoundError on startup

```
ModuleNotFoundError: No module named 'chandra'
```

**Solution:**
```bash
# The package needs to be installed in development mode
uv sync --all-extras

# Or reinstall from scratch
uv sync --reinstall --all-extras
```

### Symptom: ImportError — `pydantic` version mismatch

```
ImportError: cannot import name 'BaseModel' from 'pydantic'
```

**Solution:**
```bash
uv sync --all-extras
```

---

## Frontend Won't Build

### Symptom: `npm install` fails

```
npm ERR! code ERESOLVE
npm ERR! ERESOLVE unable to resolve dependency tree
```

**Solution:**
```bash
cd frontend

# Clear npm cache
npm cache clean --force

# Use --legacy-peer-deps if there are peer dependency conflicts
npm install --legacy-peer-deps

# Or delete node_modules and reinstall
rm -rf node_modules package-lock.json
npm install
```

### Symptom: TypeScript compilation errors

```
error TS2322: Type 'X' is not assignable to type 'Y'
```

**Solution:**
```bash
# Check TypeScript version
npx tsc --version

# Run TypeScript compiler in verbose mode to see all errors
npx tsc --noEmit

# Common fixes:
# 1. Update TypeScript
npm install typescript@latest --save-dev

# 2. Check for type mismatches in component props
# 3. Ensure @types packages are installed
npm install @types/node @types/react @types/react-dom --save-dev
```

### Symptom: Next.js build fails with cryptic error

```
Error: Page could not be resolved
```

**Solution:**
```bash
# Clear Next.js cache
rm -rf .next

# Rebuild
npm run build

# Check for missing page files in app/ directory
ls frontend/app/
```

### Symptom: Frontend dev server starts but API calls fail

```
GET http://localhost:6001/health net::ERR_CONNECTION_REFUSED
```

**Solution:**
1. Ensure the backend is running on port 6001
2. Check `NEXT_PUBLIC_API_URL` in `.env`:
   ```
   NEXT_PUBLIC_API_URL=http://localhost:6001
   ```
3. Restart the frontend dev server after changing `.env`:
   ```bash
   cd frontend && npm run dev
   ```

---

## Local LLM Won't Connect

### Symptom: vLLM server not running

```
curl: (7) Failed to connect to localhost port 8000: Connection refused
```

**Solution:**
```bash
# Start vLLM
vLLM_USE_FLASHINFER_SAMPLER=0 vllm serve google/gemma-4-12B-it-qat-w4a16-ct \
  --gpu-memory-utilization 0.90 --max-model-len 16384 \
  --enable-prefix-caching --enforce-eager \
  --host 0.0.0.0 --port 8000

# Wait for "Application startup complete" before testing
curl http://localhost:8000/v1/models
```

### Symptom: `LLM_PROVIDER` not set to `vllm`

The backend is trying to use Bedrock instead of vLLM.

**Check:**
```bash
grep LLM_PROVIDER .env
```

**Solution:** Set in `.env`:
```
LLM_PROVIDER=vllm
VLLM_API_BASE=http://localhost:8000/v1
VLLM_MODEL=google/gemma-4-12B-it-qat-w4a16-ct
VLLM_API_KEY=not-needed
```

### Symptom: Firewall blocking port 8000

```
curl: (28) Connection timed out after 10000 milliseconds
```

**Solution:**
```bash
# Windows: Check Windows Defender Firewall
# Open "Windows Defender Firewall with Advanced Security"
# Check Inbound Rules for port 8000

# Temporarily allow the port (run as Administrator)
netsh advfirewall firewall add rule name="vLLM Port 8000" \
  dir=in action=allow protocol=TCP localport=8000

# Or verify the port is listening
netstat -ano | findstr :8000
```

### Symptom: GPU out of memory

```
torch.cuda.OutOfMemoryError: CUDA out of memory.
```

**Solution:**
```bash
# Reduce GPU memory utilization
vLLM_USE_FLASHINFER_SAMPLER=0 vllm serve google/gemma-4-12B-it-qat-w4a16-ct \
  --gpu-memory-utilization 0.70 \  # Lower from 0.90 to 0.70
  --max-model-len 8192 \            # Reduce context window
  --enable-prefix-caching \
  --enforce-eager \
  --host 0.0.0.0 --port 8000

# Check available GPU memory
nvidia-smi

# If still OOM, try a smaller model like Qwen 2.5-7B
```

### Symptom: vLLM crashes on startup

```
ValueError: The model's max seq length is too large
```

**Solution:**
```bash
# Reduce max-model-len
vLLM_USE_FLASHINFER_SAMPLER=0 vllm serve google/gemma-4-12B-it-qat-w4a16-ct \
  --max-model-len 8192 \
  ...rest of flags
```

### Symptom: vLLM returns empty responses

```
{"choices":[{"index":0,"message":{"content":"","role":"assistant"},"finish_reason":"stop"}]}
```

**Solution:**
- Check if context window is being exceeded (reduce `--max-model-len` or input size)
- Ensure `vLLM_USE_FLASHINFER_SAMPLER=0` is set (required for Gemma 4 QAT)
- Try restarting the vLLM server

---

## Database Errors

### Symptom: Cannot connect to PostgreSQL

```
psycopg.OperationalError: connection to server at "localhost" (127.0.0.1), port 5434 failed: Connection refused
```

**Solution:**
```bash
# Check if Postgres container is running
docker ps | grep postgres

# If not running, start it
docker compose up -d postgres

# Wait for it to become healthy
docker compose ps postgres

# Check Postgres logs
docker compose logs postgres
```

### Symptom: `DATABASE_URL` incorrect

```
sqlalchemy.exc.OperationalError: (psycopg.OperationalError) 
FATAL: password authentication failed for user "chandra"
```

**Solution:** Verify `.env` values match `docker-compose.yml`:
```ini
# .env
POSTGRES_URL=postgresql+psycopg://chandra:chandra@localhost:5434/chandra
DATABASE_URL=postgresql+psycopg://chandra:chandra@localhost:5434/chandra
```

Note the port: **5434** (not the default 5432), because Docker maps 5434 → 5432 inside the container.

### Symptom: Alembic migration fails

```
ERROR [alembic.util.messaging] Target database is not up to date.
```

**Solution:**
```bash
# Run migrations
alembic upgrade head

# Check current migration status
alembic current

# View migration history
alembic history

# If stuck, stamp to the latest revision
alembic stamp head
alembic upgrade head

# Rollback one step and re-apply
alembic downgrade -1
alembic upgrade head
```

### Symptom: Alembic detects no changes

```
INFO  [alembic.runtime.migration] No migrations to apply.
```

**Solution:**
```bash
# Force autogenerate a new migration
alembic revision --autogenerate -m "description_of_changes"

# Then apply it
alembic upgrade head
```

### Symptom: Database is corrupted or stale

**Solution:**
```bash
# Stop and remove the container + volume
docker compose down -v

# Start fresh
docker compose up -d postgres

# Wait for healthy, then migrate
alembic upgrade head
```

---

## Jira Integration Fails

### Symptom: Jira connection refused

```
jira.exceptions.JiraError: HTTP 401: Unauthorized
```

**Solution:**
```bash
# Verify Jira credentials in .env
grep -E "^(JIRA_SERVER|JIRA_EMAIL|JIRA_API_TOKEN)" .env
```

**Check:**
```ini
JIRA_SERVER=https://your-domain.atlassian.net
JIRA_EMAIL=your-email@company.com
JIRA_API_TOKEN=your-api-token  # NOT your password
```

> **Important:** The JIRA API token is **not** your Atlassian password. Generate one at:
> https://id.atlassian.com/manage/api-tokens

### Symptom: Jira server unreachable

```
jira.exceptions.JiraError: HTTP 404: 
```

**Solution:**
```bash
# Verify the JIRA_SERVER is correct and accessible
curl -I https://your-domain.atlassian.net

# If behind a corporate proxy, ensure proxy settings are configured
# Add to .env if needed:
# HTTP_PROXY=http://proxy.company.com:8080
# HTTPS_PROXY=http://proxy.company.com:8080
```

### Symptom: Jira ticket creation fails

```
jira.exceptions.JiraError: Issues: Field 'project' cannot be set.
```

**Solution:**
- Ensure the project key in the Jira ticket exists
- Check that the authenticated user has permission to create issues in the project
- Verify the project key is correct (case-sensitive)

---

## Webhook Errors

### Symptom: Webhook endpoint returns 401

```
{"detail":"Unauthorized"}
```

**Solution:**
- If `CHANDRA_WEBHOOK_TOKEN` is set in `.env`, all webhook requests must include the header:
  ```
  X-Chandra-Webhook-Token: your-secret-token
  ```
- If not needed, comment out or remove `CHANDRA_WEBHOOK_TOKEN` from `.env`:
  ```ini
  # CHANDRA_WEBHOOK_TOKEN=
  ```

### Symptom: Webhook payload not processed

```
{"detail":"Unsupported webhook source"}
```

**Solution:**
- Check the webhook source is one of the supported channels:
  - `jira`, `slack`, `teams`, `email`, `cloudwatch`, `azure_monitor`, `gcp_monitoring`, `rest_api`, `webhook`
- The URL format is: `POST /webhooks/{source}` where `{source}` is the channel name

### Symptom: Webhook timeout

```
504 Gateway Timeout
```

**Solution:**
- Check if the backend is overloaded
- Reduce the payload size
- Ensure the LLM (vLLM) is responding quickly enough
- Check `docker compose logs backend` for backend-side errors

---

## Docker Issues

### Symptom: Docker daemon not running

```
Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?
```

**Solution:**
- **Windows:** Start Docker Desktop from the Start Menu
- **Linux:** `sudo systemctl start docker`
- **macOS:** Start Docker Desktop from Applications

### Symptom: Port conflict — 5434 already in use

```
Error response from daemon: driver failed programming external connectivity on endpoint
(port is already allocated)
```

**Solution:**
```bash
# Find the process using port 5434
netstat -ano | findstr :5434

# Kill it or change the port mapping in docker-compose.yml
# e.g., change "5434:5432" to "5435:5432"
```

### Symptom: Docker volume permissions

```
Error: permission denied while trying to connect to the Docker daemon socket
```

**Solution:**
- **Windows:** Ensure Docker Desktop is running with Windows containers (or Linux containers, whichever is configured)
- **Linux:** Add your user to the docker group:
  ```bash
  sudo usermod -aG docker $USER
  # Log out and back in
  ```

### Symptom: Container exits immediately

```
docker compose ps
# → State: "Exited"
```

**Solution:**
```bash
# Check logs
docker compose logs postgres

# Common issues:
# - Port conflict (change port mapping)
# - Volume permissions (reset volume with docker compose down -v)
# - Insufficient memory (increase Docker Desktop memory limit)
```

---

## AWS Connection Issues

### Symptom: AWS credentials not configured

```
botocore.exceptions.NoCredentialsError: Unable to locate credentials
```

**Solution:**
```bash
# Check if AWS CLI is configured
aws sts get-caller-identity

# Configure credentials
aws configure
# Or set environment variables in .env:
# AWS_ACCESS_KEY_ID=your-access-key
# AWS_SECRET_ACCESS_KEY=your-secret-key
# AWS_DEFAULT_REGION=us-east-1
```

### Symptom: AWS profile not found

```
botocore.exceptions.ProfileNotFound: The config profile (my-sandbox) could not be found
```

**Solution:**
```bash
# Check available profiles
aws configure list-profiles

# Set the correct profile in .env or unset to use default
# AWS_PROFILE=my-sandbox
# Comment out the line to use the default profile
```

### Symptom: Insufficient permissions

```
ClientError: An error occurred (AccessDenied) when calling the ... operation: 
User: ... is not authorized to perform: ...
```

**Solution:**
- Ensure the AWS user/role has the required permissions:
  - Read-only: `arn:aws:iam::aws:policy/ReadOnlyAccess`
  - S3 write: `arn:aws:iam::aws:policy/AmazonS3FullAccess` (for demo execution)
- Use a burner/sandbox account for development
- Never use production AWS credentials

### Symptom: Terraform apply fails (synthetic env)

```
Error: Error creating S3 bucket: AccessDenied: Access Denied
```

**Solution:**
```bash
# Verify AWS credentials
aws sts get-caller-identity

# Check that the user has permissions to create:
# - S3 buckets
# - EC2 instances
# - IAM roles
# - CloudWatch alarms
# - etc.

# Use a burner account with AdminAccess for the synthetic env
```

---

## LangGraph Runtime Errors

### Symptom: LangGraph checkpoint error

```
langgraph.errors.CheckpointError: Unable to read checkpoint
```

**Solution:**
- If using PostgresSaver, ensure the database is running and migrated
- Fallback to MemorySaver by checking if Postgres is unreachable
- Check the logs for `fallback to MemorySaver`

### Symptom: `Send()` fan-out fails

```
TypeError: Send() argument after * must be an iterable, not NoneType
```

**Solution:**
- Check that the `kra_supervisor` node returns a list of `Send(...)` objects
- Verify that KRA names match the graph node names exactly
- Ensure all five KRA workers are defined in the graph

### Symptom: Graph execution hangs

**Solution:**
- Check if the workflow is paused at `approval_gate` (waiting for human approval)
- Verify the interrupt is handled correctly:
  ```bash
  curl http://localhost:6001/requests?status=awaiting_approval
  ```
- If stuck, approve or reject the pending request via the API
- Check for infinite loops in the self-healing orchestrator

### Symptom: LLM call within LangGraph fails

- Verify the LLM provider is running (vLLM or Bedrock)
- Check the `LLM_PROVIDER` setting in `.env`
- Test the LLM endpoint directly:
  ```bash
  # For vLLM
  curl http://localhost:8000/v1/completions -H 'Content-Type: application/json' \
    -d '{"model":"google/gemma-4-12B-it-qat-w4a16-ct","prompt":"Hello","max_tokens":10}'
  ```

---

## vLLM Specific Issues

### Symptom: FlashInfer sampler error

```
RuntimeError: FlashInfer sampling is not supported for this model
```

**Solution:**
```bash
# Set the environment variable before starting vLLM
vLLM_USE_FLASHINFER_SAMPLER=0 vllm serve ...
```

### Symptom: Model download fails

```
HuggingFaceHubError: 401 Client Error: Unauthorized
```

**Solution:**
- The model `google/gemma-4-12B-it-qat-w4a16-ct` requires accepting the license on HuggingFace
- Log in to HuggingFace and accept the Gemma license:
  ```bash
  huggingface-cli login
  ```
- Then try again

### Symptom: vLLM is slow

- Check GPU utilization: `nvidia-smi -l 1`
- Ensure `--enable-prefix-caching` is set (speeds up repeated prompts)
- Reduce `--max-model-len` if the full context isn't needed
- Check for CPU fallback (if `nvidia-smi` shows 0% GPU utilization)

### Symptom: `enforce-eager` causes performance issues

- The `--enforce-eager` flag disables CUDA graphs for compatibility
- If your GPU supports it, try removing `--enforce-eager`:
  ```bash
  vLLM_USE_FLASHINFER_SAMPLER=0 vllm serve google/gemma-4-12B-it-qat-w4a16-ct \
    --gpu-memory-utilization 0.90 --max-model-len 16384 \
    --enable-prefix-caching \
    --host 0.0.0.0 --port 8000
  ```

---

## General Debugging Tips

### Enable verbose logging

```bash
# Start the backend with DEBUG level
uvicorn fastapi_app:app --host 0.0.0.0 --port 6001 --log-level debug

# Or set in .env
LOG_LEVEL=DEBUG
```

### Check all service logs

```bash
# Backend logs
docker compose logs backend

# Postgres logs
docker compose logs postgres

# Frontend logs (look at the terminal running npm run dev)
```

### System health checklist

```bash
# 1. Is Postgres running?
docker compose ps postgres

# 2. Is the backend running?
curl http://localhost:6001/health

# 3. Is vLLM running?
curl http://localhost:8000/v1/models

# 4. Is the frontend running?
curl http://localhost:3000

# 5. Are AWS credentials configured?
aws sts get-caller-identity

# 6. Is the .env file configured?
grep -E "^(DATABASE_URL|LLM_PROVIDER|VLLM_API_BASE)" .env
```

### Quick reset (nuke everything)

```bash
# Stop all services
docker compose down -v

# Remove caches
rm -rf .pytest_cache .ruff_cache .mypy_cache

# Clean frontend
cd frontend && rm -rf node_modules .next && cd ..

# Start fresh
docker compose up -d postgres
uv sync --all-extras
alembic upgrade head
cd frontend && npm install && cd ..
```

---

## Still Stuck?

If the above solutions don't resolve your issue:

1. **Check the logs** — Most issues leave a trace in the backend logs
2. **Search the docs** — `docs/` directory contains deployment guides and architecture diagrams
3. **Ask in the team channel** — Include the error message, relevant logs, and what you've tried
4. **Open a GitHub issue** — Include the full error traceback and steps to reproduce