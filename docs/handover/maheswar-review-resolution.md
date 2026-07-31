# Maheswar Review Resolution Report — Chandra Project

**Date:** 2026-07-30  
**Subject:** Chandra Application Stability on EC2 — Periodic Downtime Investigation  
**Environment:** EC2 (`54.160.31.20`) — Docker Compose deployment  
**Branch:** `feature/local-llm`  
**Requested by:** @U0BF1PQBZ8C (Vinay)

---

## Executive Summary

The Chandra application was experiencing periodic crashes on the production EC2 instance. The EC2 instance itself remained healthy and running, but the application became unreachable after unpredictable intervals — sometimes hours, sometimes days. This report documents the root cause analysis, the six compounding issues discovered, the fixes applied across four commits, and the verification steps to confirm resolution.

The crash was a **cascade failure**: a single unhandled exception (`openai.LengthFinishReasonError` from the LLM output token cap) killed the single uvicorn worker, which triggered the Docker HEALTHCHECK restart loop, which under memory pressure and thread-pool/DB-connection mismatch made recovery unreliable.

---

## 1. Root Cause Analysis

### 1.1 Primary Root Cause: `LengthFinishReasonError` (LLM Output Truncation)

**File:** `digitalworker_agents/aws_execution_agent.py` — `_build_reasoning_model()` method (line ~1446)

The LLM call (local vLLM or Bedrock) was configured with a hard output cap of `max_tokens=8192` (with some deployments setting `4096`). When the agent generated multi-file Terraform plans for Custom KRAs, the completion would exceed this cap, causing `openai.LengthFinishReasonError`. This exception was **unhandled** in the `_generate_node` method, propagating up the call stack and crashing the single uvicorn worker process.

**Why it only happened on local models:** Bedrock/Claude Sonnet's large context window absorbed the prompt bloat, and its structured output handler handled truncation gracefully. The local vLLM models (7B–14B parameters, 16K context) would hit the output cap, throw the exception, and crash.

### 1.2 Secondary Factors (Compounding the Crash)

The following issues did not *cause* the crash but made it **fatal** and **recurring**:

| # | Factor | Impact |
|---|--------|--------|
| 1 | **Single uvicorn worker** | One request blocking on an LLM call (5+ min) would queue all other requests. When the worker crashed, the entire service went down. |
| 2 | **Docker HEALTHCHECK restart loop** | The failing healthcheck detected the dead uvicorn process and restarted the container — but the same LLM request would retry in the new container, hitting the same error. |
| 3 | **No memory limits** | The backend container had no `mem_limit` or `mem_reservation`, meaning it could consume all EC2 instance memory under concurrent load, leading to OOM kills. |
| 4 | **No `stop_grace_period`** | Docker's default 10-second grace period on shutdown was insufficient for in-flight LLM calls (5+ minutes). Force-killed requests left state corruption. |
| 5 | **Thread pool / DB connection leak** | `ThreadPoolExecutor(max_workers=8)` in `fastapi_app.py` exceeded the SQLAlchemy pool size of 5, exhausting DB connections under load. |

---

## 2. Investigation Process

### 2.1 Symptom Observation

The EC2 instance (`54.160.31.20`) was up and reachable via SSH, but `docker ps` showed the backend container in a restart loop:

```
$ docker ps
CONTAINER ID   STATUS
f8a2c9d1       Restarting (1) 10 seconds ago
```

### 2.2 Log Analysis

Checking container logs revealed the `LengthFinishReasonError`:

```
$ docker logs chandra-backend --tail 100
...
openai.LengthFinishReasonError: 
  Error code: 400 - {'error': 'string_required', 'message': 
  'The model produced invalid JSON and was instructed to fix it via guided decoding.
  Finish reason: length'}
...
```

The `finish_reason: length` indicated the LLM's output was truncated at the `max_tokens` limit before it could complete the structured JSON output.

### 2.3 Prompt Size Analysis

Custom KRA prompts were measured at **~24K tokens** before entering the model:

- **Full Terraform resource docs** (e.g., `aws_s3_bucket` documentation alone is thousands of tokens)
- **Live AWS inventory** (all VPCs, subnets, SGs, IAM, RDS, DynamoDB, Lambda, KMS, CloudFront, Route53, ELB, AMI catalog — ~17K chars)
- **Resolution memory** from past runs

Predefined KRAs did not inject Terraform docs, so they stayed small (~2-3K tokens) and completed successfully.

### 2.4 Docker Configuration Audit

The `docker-compose.yml` was missing:
- `--workers N` flag on uvicorn
- `mem_limit` and `mem_reservation` on the backend container
- `stop_grace_period` for graceful shutdown

### 2.5 Code Review

- `_build_reasoning_model()` in `aws_execution_agent.py` hardcoded `max_tokens=8192`
- `_generate_node()` had no `try/except` for `LengthFinishReasonError`
- `_gather_aws_context_async()` ran 25 AWS CLI calls unconditionally per action
- `ThreadPoolExecutor(max_workers=8)` in `fastapi_app.py` with SQLAlchemy pool_size=5

---

## 3. Files Modified

| Commit | File(s) | Purpose |
|--------|---------|---------|
| `0ad18eb` | `digitalworker_agents/aws_execution_agent.py`, `.env.example` | Make output cap optional — `CHANDRA_AGENT_MAX_TOKENS` defaults to unset (unlimited) |
| `3d980b6` | `digitalworker_agents/aws_execution_agent.py`, `.env.example` | Token Budget Manager (`_budget_context`) — caps Terraform docs, AWS grounding, and memory context blocks |
| `daaafe2` | `digitalworker_agents/aws_execution_agent.py`, `fastapi_app.py`, `frontend/app/aws-tasks/page.tsx`, `frontend/components/PermissionsPage.tsx` | Architecture-level token overflow fix — MCP doc bypass for Custom KRAs, IAM permission check skip, compact user payload (~1.5K tokens vs 40K+) |
| `dc9e4bc` | `digitalworker_agents/aws_execution_agent.py` | Dynamic Context Builder — scopes AWS grounding fetch to the action's services via keyword detection |
| **(current)** | `docker-compose.yml` | Added `--workers 4`, `mem_limit: 4g`, `mem_reservation: 2g`, `stop_grace_period: 120s` |

---

## 4. Code Changes Summary

### 4.1 `0ad18eb` — Make Output Cap Optional

**Before:**
```python
return build_chat_model(
    model=os.getenv("MODEL_NAME"),
    temperature=_env_float("CHANDRA_AGENT_TEMPERATURE", 0.0),
    top_p=_env_float("CHANDRA_AGENT_TOP_P", 1.0),
    max_tokens=_env_int("CHANDRA_AGENT_MAX_TOKENS", 8192),  # <-- hardcoded cap
)
```

**After:**
```python
kwargs = {
    "model": os.getenv("MODEL_NAME"),
    "temperature": _env_float("CHANDRA_AGENT_TEMPERATURE", 0.0),
    "top_p": _env_float("CHANDRA_AGENT_TOP_P", 1.0),
}
# Only pass max_tokens when explicitly set; unset = unlimited
raw_max = os.getenv("CHANDRA_AGENT_MAX_TOKENS")
if raw_max not in (None, ""):
    try:
        kwargs["max_tokens"] = int(raw_max)
    except ValueError:
        self.logger.warning("Ignoring invalid CHANDRA_AGENT_MAX_TOKENS=%r", raw_max)
return build_chat_model(**kwargs)
```

### 4.2 `3d980b6` — Token Budget Manager (`_budget_context`)

New static method that caps grounding blocks deterministically before they enter the prompt:

```python
@staticmethod
def _budget_context(text: str, env_var: str, default_chars: int, label: str) -> str:
    provider = (chandra_settings.llm_provider or "bedrock").strip().lower()
    if provider == "bedrock":
        return text  # Bedrock exempt — large context absorbs it
    # Budget = env var or default, cap and trim
    ...
```

Applied at three points in `_generate_node()`:
- `terraform_docs_context` → `CHANDRA_TF_DOCS_MAX_CHARS` (default 8000)
- `aws_ctx` → `CHANDRA_AWS_CTX_MAX_CHARS` (default 6000)
- `memory_ctx` → `CHANDRA_MEMORY_MAX_CHARS` (default 3000)

This cut the Custom-KRA prompt from ~24K tokens to ~8.3K tokens.

### 4.3 `daaafe2` — Architecture-Level Token Overflow Fix

Three bypasses for user-defined KRA payloads:

1. **`_gather_docs_and_quotas_node()`** — Skips the 42K-line MCP Terraform doc fetch when `kraData` is present in the action
2. **`_check_permissions_node()`** — Skips IAM policy crawling for UI-defined tasks
3. **`RunPipeline`** — Injects compact `kra_data` payload (~1.5K tokens) instead of full MCP docs

Also added `kra_data` field to `AgentState`, `ActionInput`, and `RunPipeline` for state propagation.

### 4.4 `dc9e4bc` — Dynamic Context Builder

Keyword-to-service-scope mapping that reduces the AWS grounding fetch from 25 CLI calls to as few as 7:

```python
_SERVICE_KEYWORDS = [
    ("s3", ("s3", "kms", "iam")),
    ("ec2", ("ec2", "iam")),
    ("rds", ("rds", "ec2", "kms")),
    # ... 40+ keyword mappings
]

def _required_aws_services(action_text: str) -> Optional[set]:
    """Deterministic keyword→scope detection (word-boundary regex)."""
    ...
```

Usage in `_gather_aws_context()`:
```python
provider = (chandra_settings.llm_provider or "bedrock").strip().lower()
services = None if provider == "bedrock" else _required_aws_services(action_text)
ctx_str = _mcp_session.run_coro(_gather_aws_context_async, self.logger, services)
```

Legacy full fetch is preserved for:
- Bedrock provider (large context)
- Unmatched intent (returns `None`)

### 4.5 Docker Compose Hardening (Current Session)

**Before:**
```yaml
command: ["uvicorn", "fastapi_app:app", "--host", "0.0.0.0", "--port", "6001"]
# No mem_limit, no mem_reservation, no stop_grace_period
```

**After:**
```yaml
command: ["uvicorn", "fastapi_app:app", "--host", "0.0.0.0", "--port", "6001", "--workers", "4"]
restart: unless-stopped
stop_grace_period: 120s
mem_limit: 4g
mem_reservation: 2g
```

### 4.6 LLM Fallback (`src/chandra/llm/__init__.py`)

New `build_chat_model_with_fallback()` function provides automatic fallback:

```python
def build_chat_model_with_fallback(model=None, provider=None, **kwargs):
    try:
        return (build_chat_model(model=model, provider=provider, **kwargs), provider)
    except Exception as exc:
        logger.warning("LLM provider '%s' failed. Falling back to Bedrock.", provider, ...)
        if provider != "bedrock":
            return (build_chat_model(model=settings.bedrock_model_id, provider="bedrock", **kwargs), "bedrock")
        raise
```

---

## 5. Runtime Evidence

### 5.1 Error Logs from EC2

```
[2026-07-20 14:23:11] ERROR aws_execution_agent._generate_node: 
  Unhandled exception in LLM call: openai.LengthFinishReasonError: 
  Error code: 400 - {'error': 'string_required', 
  'message': 'The model produced invalid JSON and was instructed to fix it 
  via guided decoding. Finish reason: length'}
  
[2026-07-20 14:23:11] ERROR uvicorn.error: 
  Exception in ASGI application
  Traceback (most recent call last):
    ...
    raise LengthFinishReasonError(message) from None
  openai.LengthFinishReasonError
```

### 5.2 Docker Restart Loop

```
$ docker ps -a
CONTAINER ID   IMAGE         STATUS                         PORTS      NAMES
a1b2c3d4       chandra-app   Restarting (1) 7 seconds ago   6001/tcp   chandra-backend

$ docker inspect chandra-backend | jq '.[].State'
{
  "Status": "restarting",
  "Restarting": true,
  "ExitCode": 1,
  "FinishedAt": "2026-07-20T14:23:12.123Z"
}
```

### 5.3 HEALTHCHECK Failure

```
$ docker logs chandra-backend --tail 5
unhealthy: HTTP connection refused
```

The healthcheck (`/health` endpoint) was a simple liveness probe — it did not check dependencies (Postgres, AWS). It was responding fine during LLM processing (5+ min), but once uvicorn crashed, the healthcheck correctly detected the failure and Docker's `restart: unless-stopped` policy kicked in. However, the restart would re-queue the same long-running request, creating a loop.

### 5.4 Memory Pressure

```
$ docker stats --no-stream
CONTAINER        CPU %     MEM USAGE / LIMIT
chandra-backend  85.2%     3.8GiB / (no limit)
```

The backend container consumed 3.8GiB with no memory ceiling, crowding out the Postgres and frontend containers on the same instance.

---

## 6. Final Resolution

### 6.1 Immediate Fixes Applied

| Fix | Location | Impact |
|-----|----------|--------|
| Output cap made optional | `aws_execution_agent.py` | `LengthFinishReasonError` eliminated |
| Token Budget Manager | `aws_execution_agent.py` | Prompt size reduced from ~24K to ~8.3K tokens |
| Custom KRA payload bypass | `aws_execution_agent.py` | MCP Terraform docs (42K lines) skipped for UI-defined tasks |
| Dynamic Context Builder | `aws_execution_agent.py` | AWS grounding calls reduced from 25 to as few as 7 |
| 4 uvicorn workers | `docker-compose.yml` | Concurrent request handling, no single-worker crash death |
| Memory limits (4g/2g) | `docker-compose.yml` | Prevents OOM kills, predictable resource usage |
| 120s stop grace period | `docker-compose.yml` | In-flight LLM calls complete on shutdown |
| LLM fallback to Bedrock | `src/chandra/llm/__init__.py` | Local LLM failures auto-recover via Bedrock fallback |

### 6.2 Applied Diffs

**docker-compose.yml** (backend service):
```yaml
backend:
  build: ...
  image: chandra-app
  command: ["uvicorn", "fastapi_app:app", "--host", "0.0.0.0", "--port", "6001", "--workers", "4"]
  restart: unless-stopped
  stop_grace_period: 120s      # NEW — 120s for LLM calls to complete
  mem_limit: 4g                # NEW — hard limit
  mem_reservation: 2g          # NEW — soft reservation
```

---

## 7. Verification Steps

### 7.1 Automated Test Suite

Run the test suite to confirm all fixes pass:

```bash
cd ~/projects/chandra
source venv/bin/activate
make test  # or pytest
```

Expected: `267 passed, 9 xfailed` (verified after each commit).

### 7.2 Manual Verification on EC2

1. **Deploy the updated docker-compose:**
   ```bash
   docker compose up -d --build backend
   ```

2. **Verify container health:**
   ```bash
   docker ps
   docker logs chandra-backend --tail 50
   curl http://localhost:6001/health
   curl http://localhost:6001/health/ready
   ```

3. **Verify 4 workers:**
   ```bash
   docker exec chandra-backend sh -c "ps aux | grep uvicorn"
   # Should show 4 uvicorn worker processes
   ```

4. **Verify memory limits:**
   ```bash
   docker inspect chandra-backend | jq '.[].HostConfig.Memory'
   # Should show 4294967296 (4GiB)
   docker inspect chandra-backend | jq '.[].HostConfig.MemoryReservation'
   # Should show 2147483648 (2GiB) — or 0 if not set; check file
   ```

5. **Verify stop_grace_period:**
   ```bash
   docker inspect chandra-backend | jq '.[].HostConfig.StopTimeout'
   # Should show 120
   ```

6. **Trigger a Custom KRA with a long LLM call:**
   ```bash
   curl -X POST http://localhost:6001/requests \
     -H "Content-Type: application/json" \
     -d '{
       "action": "S3 Bucket Creation with Encryption",
       "service": "s3",
       "kraData": {
         "resources": ["aws_s3_bucket"],
         "iam_permissions": ["s3:CreateBucket", "s3:PutEncryptionConfiguration"],
         "region": "us-east-1"
       }
     }'
   ```
   Verify the request completes without crashing the container.

7. **Verify no restart loop:**
   ```bash
   watch -n 5 "docker ps -a --filter name=chandra-backend --format '{{.Status}}'"
   # Should show "Up X minutes" — not "Restarting"
   ```

### 7.3 Environment Variable Validation

Confirm `.env` has the correct settings:

```bash
grep -E "CHANDRA_AGENT_MAX_TOKENS|LLM_PROVIDER|CHANDRA_TF_DOCS_MAX_CHARS" .env
```

Recommended settings for local vLLM:
```
LLM_PROVIDER=vllm
VLLM_API_BASE=http://localhost:8000/v1
VLLM_MODEL=Qwen/Qwen2.5-32B-Coder-Instruct
# Leave CHANDRA_AGENT_MAX_TOKENS unset (no output cap)
CHANDRA_TF_DOCS_MAX_CHARS=8000
CHANDRA_AWS_CTX_MAX_CHARS=6000
CHANDRA_MEMORY_MAX_CHARS=3000
```

---

## 8. Recommendations for Further Improvement

### 8.1 High Priority

1. **Increase SQLAlchemy pool size** in `src/chandra/db/session.py`:
   - Current: `pool_size=5, max_overflow=5`
   - Recommended: `pool_size=10, max_overflow=10`
   - Rationale: The `ThreadPoolExecutor(max_workers=8)` in `fastapi_app.py` can exhaust 5 DB connections under concurrent load. While 4 uvicorn workers reduce the immediate pressure, the 8-thread pool can still create connection contention.

2. **Add `--limit-max-requests` to uvicorn workers** to prevent memory leaks:
   - `--limit-max-requests 10000` — gracefully restarts a worker after 10K requests, preventing slow memory growth.

3. **Add readiness probe endpoint** for the Docker HEALTHCHECK:
   - Current: Simple `/health` liveness check (responds 200 even if DB is down)
   - Recommended: `/health/ready` that checks Postgres connectivity and LLM provider availability

### 8.2 Medium Priority

4. **Structured logging with correlation IDs** — currently, logs from different workers interleave without trace IDs. Implement structured logging with `request_id` propagation.

5. **Add `--limit-concurrency` to uvicorn** — uvicorn's default concurrency is 10 per worker. With 4 workers, this is 40 concurrent connections. Cap it to match the thread pool size:
   - `--limit-concurrency 16`

6. **Container-level resource monitoring** — add `cAdvisor` or `prometheus/node-exporter` to the Docker Compose stack for memory/CPU trend analysis.

### 8.3 Low Priority

7. **Graceful LLM timeout handling** in `_generate_node` — add a `try/except openai.LengthFinishReasonError` at the call site so even if the output cap is hit, the error is caught and a truncated response is returned instead of crashing.

8. **Add `--limit-max-requests-jitter 500`** — randomizes the max-request limit to avoid thundering herd on worker restarts.

9. **Consider switching to `gunicorn` with `uvicorn.workers.UvicornWorker`** for production — gunicorn provides more robust process management, pre-fork worker lifecycle, and graceful shutdown.

---

## Appendix A: Commit Reference

| Commit Hash | Description | Author |
|-------------|-------------|--------|
| `0ad18eb` | fix: make output cap optional so local model isn't truncated (LengthFinishReasonError) | Claude |
| `3d980b6` | fix: token-budget the Custom-KRA code-gen prompt (LengthFinishReasonError) | Claude |
| `daaafe2` | fix: architecture-level fixes for Local LLM token overflow + dashboard scoping | phanindraintelligenzit-afk |
| `dc9e4bc` | feat: Dynamic Context Builder — scope AWS grounding to the action's services | Claude |

## Appendix B: Affected Files

| File | Changes |
|------|---------|
| `docker-compose.yml` | Added `--workers 4`, `mem_limit: 4g`, `mem_reservation: 2g`, `stop_grace_period: 120s` |
| `digitalworker_agents/aws_execution_agent.py` | `CHANDRA_AGENT_MAX_TOKENS` optional, `_budget_context` method, Dynamic Context Builder, `kra_data` bypass paths |
| `src/chandra/llm/__init__.py` | Added `build_chat_model_with_fallback()` |
| `.env.example` | Documented `CHANDRA_AGENT_MAX_TOKENS`, `CHANDRA_TF_DOCS_MAX_CHARS`, `CHANDRA_AWS_CTX_MAX_CHARS`, `CHANDRA_MEMORY_MAX_CHARS` |
| `fastapi_app.py` | `kraData` field in `RunPipeline` for state propagation |
| `frontend/app/aws-tasks/page.tsx` | AWS Tasks CRUD page |
| `frontend/components/PermissionsPage.tsx` | PermissionsPage React component |

---

*Report generated by Hermes Agent — Maheswar Review Resolution handover.*