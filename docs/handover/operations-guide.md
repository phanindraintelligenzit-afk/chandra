# Chandra — Operations Guide

> **Enterprise AI Cloud Operations Platform**
> **Branch:** `feature/local-llm`
> **Last Updated:** 2026-07-30

---

## Table of Contents

1. [Service Overview](#1-service-overview)
2. [Starting & Stopping Services](#2-starting--stopping-services)
3. [Docker Management](#3-docker-management)
4. [Log Collection](#4-log-collection)
5. [Database Backups](#5-database-backups)
6. [Monitoring](#6-monitoring)
7. [Alerting](#7-alerting)
8. [Health Checks](#8-health-checks)
9. [Scaling](#9-scaling)
10. [Troubleshooting Common Issues](#10-troubleshooting-common-issues)

---

## 1. Service Overview

### Component map

| Service | Port | Purpose | Docker? | Dependencies |
|---------|------|---------|---------|-------------|
| **FastAPI Backend** | 6001 | HTTP/WS API — orchestrator, digital worker, copilot | Yes | Postgres, (vLLM or Bedrock) |
| **Next.js Frontend** | 3000 | Ops console — onboarding, dashboard, approval center | Yes | Backend API |
| **PostgreSQL** | 5434 (host) / 5432 (container) | Primary database | Yes | None |
| **vLLM** (optional) | 8000 | Local LLM inference server | No (bare metal) | GPU, HuggingFace model |
| **Gradio** (legacy) | 7861 | Streamlit-equivalent (being sunset) | Yes | Backend |
| **Nginx** | 80 / 443 | Reverse proxy (Docker compose only) | Yes | Backend, Frontend |

### Key files

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Full stack service definition |
| `Dockerfile` | Multi-stage build (Python + Node.js) |
| `start.sh` | Container entrypoint — starts FastAPI, Gradio, Next.js |
| `nginx.conf` | Reverse proxy configuration |
| `.env` | Runtime environment configuration |
| `scripts/healthcheck.py` | Docker HEALTHCHECK probe |
| `digital_worker_config.json` | Max iterations and timeout settings |

---

## 2. Starting & Stopping Services

### Full stack (Docker Compose)

```bash
# Start all services
docker compose up -d

# View startup logs
docker compose logs -f

# Check service status
docker compose ps

# Stop all services
docker compose down

# Stop and remove volumes (destroys data)
docker compose down -v
```

### Individual services

```bash
# Database only (for local development without Docker)
docker compose up -d postgres

# Backend only
docker compose up -d backend

# Frontend only
docker compose up -d frontend

# Nginx only
docker compose up -d nginx
```

### Development mode (without Docker)

```bash
# Terminal 1: Backend
uvicorn fastapi_app:app --host 0.0.0.0 --port 6001 --reload

# Terminal 2: Frontend
cd frontend && npm run dev

# Terminal 3: vLLM (optional)
vLLM_USE_FLASHINFER_SAMPLER=0 vllm serve google/gemma-4-12B-it-qat-w4a16-ct \
  --gpu-memory-utilization 0.90 --max-model-len 16384 \
  --enable-prefix-caching --enforce-eager \
  --host 0.0.0.0 --port 8000
```

### Expected startup sequence

1. **Postgres** starts first (healthcheck: `pg_isready -U chandra`)
2. **Backend** waits for Postgres to be healthy, then starts FastAPI
3. **Frontend** starts after backend is up
4. **Nginx** starts after both backend and frontend
5. **vLLM** (if used) starts independently — wait for "Application startup complete" in logs

### Verification after startup

```bash
# Check all services
docker compose ps

# Backend health
curl http://localhost:6001/health
# → {"status": "ok"}

# Backend readiness
curl http://localhost:6001/health/ready
# → {"status": "ok", "components": {"copilot_agent": "ok", "digital_worker": "ok", "postgres": "ok"}}

# Frontend
curl -s -o /dev/null -w "%{http_code}" http://localhost:3000
# → 200

# vLLM (if used)
curl http://localhost:8000/v1/models
# → {"object": "list", "data": [...]}
```

---

## 3. Docker Management

### Common operations

```bash
# Rebuild and restart
docker compose up -d --build

# View logs for a specific service
docker compose logs -f --tail=100 backend

# Restart a single service
docker compose restart backend

# Scale a service
docker compose up -d --scale backend=2

# Check resource usage
docker stats

# Inspect a container
docker inspect chandra-backend-1

# Execute a command in a running container
docker compose exec backend python -c "import chandra; print('OK')"

# Execute in Postgres
docker compose exec -T postgres pg_dump -U chandra chandra > backup.sql
```

### Container health

```bash
# Check container health status
docker ps --filter "health=healthy"
docker ps --filter "health=unhealthy"

# View healthcheck logs
docker inspect --format='{{json .State.Health}}' chandra-backend-1

# Manually trigger healthcheck
docker compose exec backend python /app/healthcheck.py
```

### Image management

```bash
# Build the backend image
docker compose build backend

# List images
docker images chandra-app

# Remove unused images
docker image prune -a

# Tag for registry
docker tag chandra-app your-registry/chandra-app:latest
docker push your-registry/chandra-app:latest
```

### Network management

```bash
# List networks
docker network ls

# Inspect the Chandra network
docker network inspect chandra_chandra-network

# Check DNS resolution
docker compose exec backend ping postgres
```

### Volume management

```bash
# List volumes
docker volume ls

# Inspect Postgres data volume
docker volume inspect chandra_chandra-pgdata

# Backup a volume
docker run --rm -v chandra_chandra-pgdata:/source -v $(pwd):/backup alpine \
  tar czf /backup/pgdata-backup.tar.gz -C /source .

# Restore a volume
docker run --rm -v chandra_chandra-pgdata:/target -v $(pwd):/backup alpine \
  tar xzf /backup/pgdata-backup.tar.gz -C /target
```

---

## 4. Log Collection

### Log locations

| Source | Location | Format |
|--------|----------|--------|
| Docker containers | `docker compose logs <service>` | Container stdout/stderr |
| Backend (Docker) | `docker compose logs backend` | JSON (structlog) |
| Backend (dev mode) | Terminal stdout | JSON (structlog) |
| Frontend (Docker) | `docker compose logs frontend` | Next.js stdout |
| Frontend (dev mode) | Terminal stdout | Dev server output |
| Postgres | `docker compose logs postgres` | PostgreSQL log format |
| Application logs | `logs/*.log` (if configured) | Raw text |
| vLLM | Terminal stdout | vLLM log format |

### Viewing logs

```bash
# All services
docker compose logs -f

# Specific service with tail
docker compose logs -f --tail=200 backend

# Filter by time range
docker compose logs --since=10m backend
docker compose logs --since=2026-07-30T10:00:00 --until=2026-07-30T11:00:00 backend

# Follow and grep
docker compose logs -f backend | grep -i error

# JSON format (structlog)
docker compose logs backend | grep '"event"' | head -20
```

### Log levels

| Level | When to use |
|-------|-------------|
| `DEBUG` | Development — detailed flow tracing |
| `INFO` | Default — pipeline stages, API calls, state transitions |
| `WARNING` | Retry attempts, degraded states, non-critical failures |
| `ERROR` | Pipeline failures, database errors, LLM failures |

#### Changing log level

```bash
# Via .env
LOG_LEVEL=DEBUG
UVICORN_LOG_LEVEL=debug

# Via environment variable (no restart)
export LOG_LEVEL=DEBUG
# Restart the backend
```

### Structured log fields

```json
{
  "event": "graph.analyze.completed",
  "logger": "src.chandra.graphs.action_nodes",
  "level": "info",
  "timestamp": "2026-07-30T10:00:00.123456Z",
  "run_id": "abc-123",
  "finding_count": 15,
  "kra": "cost"
}
```

### Log aggregation setup

For production, configure one of:

```bash
# CloudWatch Logs (via awslogs driver in docker-compose.yml)
# Add to docker-compose.yml:
# logging:
#   driver: awslogs
#   options:
#     awslogs-group: /chandra/backend
#     awslogs-region: us-east-1

# ELK stack
# Forward JSON logs to Filebeat → Logstash → Elasticsearch

# Datadog
# Use datadog-agent container with Docker socket access
```

---

## 5. Database Backups

### Backup methods

#### Method 1: pg_dump (recommended for regular backups)

```bash
# Full database dump
docker compose exec -T postgres pg_dump -U chandra chandra > \
  chandra_backup_$(date +%Y%m%d_%H%M%S).sql

# Compressed dump
docker compose exec -T postgres pg_dump -U chandra chandra | gzip > \
  chandra_backup_$(date +%Y%m%d_%H%M%S).sql.gz

# Custom format (compressed, supports parallel restore)
docker compose exec -T postgres pg_dump -U chandra -Fc chandra > \
  chandra_backup_$(date +%Y%m%d_%H%M%S).dump
```

#### Method 2: AWS RDS snapshots (production)

```bash
# Automated snapshot (enable via AWS Console)
# Retention: minimum 7 days, recommended 30 days

# Manual snapshot before migrations
aws rds create-db-snapshot \
  --db-instance-identifier chandra \
  --db-snapshot-identifier chandra-pre-migration-$(date +%Y%m%d)

# Copy snapshot to another region
aws rds copy-db-snapshot \
  --source-db-snapshot-identifier arn:aws:rds:us-east-1:123456789012:snapshot:chandra-pre-migration-20260730 \
  --target-db-snapshot-identifier chandra-pre-migration-dr \
  --region us-west-2
```

### Restore

```bash
# From SQL dump
cat chandra_backup_20260730.sql | docker compose exec -T postgres psql -U chandra chandra

# From compressed dump
gunzip -c chandra_backup_20260730.sql.gz | docker compose exec -T postgres psql -U chandra chandra

# From custom format
docker compose exec -T postgres pg_restore -U chandra -d chandra \
  < chandra_backup_20260730.dump

# Full restore sequence (destructive)
docker compose exec -T postgres dropdb -U chandra chandra
docker compose exec -T postgres createdb -U chandra chandra
cat chandra_backup_20260730.sql | docker compose exec -T postgres psql -U chandra chandra
```

### Backup schedule

| Data | Method | Frequency | Retention |
|------|--------|-----------|-----------|
| PostgreSQL | `pg_dump` | Daily | 30 days |
| PostgreSQL (RDS) | Automated snapshot | Daily | 7-30 days |
| Agent memory | `agent_memory.json` | Per change | Git history |
| Terraform state | S3 + DynamoDB | Per apply | Versioned |
| Docker volumes | Volume backup | Weekly | 90 days |

### Automated backup script

```bash
#!/bin/bash
# scripts/backup.sh — Scheduled backup (add to cron)
BACKUP_DIR="/backups/chandra"
mkdir -p "$BACKUP_DIR"
DATE=$(date +%Y%m%d_%H%M%S)

# PostgreSQL backup
docker compose exec -T postgres pg_dump -U chandra chandra | gzip > \
  "$BACKUP_DIR/chandra_db_$DATE.sql.gz"

# Agent memory backup
cp agent_memory.json "$BACKUP_DIR/agent_memory_$DATE.json"

# Clean up old backups (older than 30 days)
find "$BACKUP_DIR" -name "*.sql.gz" -mtime +30 -delete
find "$BACKUP_DIR" -name "*.json" -mtime +30 -delete

echo "Backup completed: $DATE"
```

---

## 6. Monitoring

### CloudWatch Dashboard

The runtime Terraform (`iac/runtime/main.tf`) deploys a CloudWatch dashboard at `iac/runtime/dashboards/chandra.json`. It surfaces:

- Pipeline run status (success/failure per run)
- KRA detection results (findings per KRA)
- API endpoint metrics (request count, latency, error rate)
- LLM cost tracking (Bedrock tokens consumed)

```bash
# Deploy the dashboard
cd iac/runtime
terraform init
terraform apply

# View the dashboard
open https://console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards:name=chandra
```

### OpenTelemetry

Chandra emits traces and metrics via OpenTelemetry:

```bash
# .env
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
OTEL_SERVICE_NAME=chandra
OTEL_ENVIRONMENT=production
```

### LangSmith (LangChain tracing)

```bash
# .env
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=ls__...
LANGCHAIN_PROJECT=chandra
```

### LangFuse (LLM observability)

```bash
# .env
LANGFUSE_PUBLIC_KEY=pk-lf-xxxxx
LANGFUSE_SECRET_KEY=sk-lf-xxxxx
LANGFUSE_HOST=https://cloud.langfuse.com
```

### AgentOps (AI agent monitoring)

```bash
# .env
AGENTOPS_API_KEY=your-agentops-key
```

### Docker resource monitoring

```bash
# Real-time resource usage
docker stats

# Per-service CPU/memory
docker stats chandra-backend-1

# Container resource limits
docker inspect chandra-backend-1 | jq '.[0].HostConfig.Memory'
docker inspect chandra-backend-1 | jq '.[0].HostConfig.NanoCpus'

# Disk usage
docker system df
```

### API monitoring

```bash
# Monitor API endpoints with curl
watch -n 5 'curl -s http://localhost:6001/health'

# Response time
time curl -s http://localhost:6001/health

# Endpoint discovery
curl -s http://localhost:6001/openapi.json | jq '.paths | keys'
```

---

## 7. Alerting

### SNS alerts

The escalation node publishes high-risk `pending_writes` to `SNS_TOPIC_ARN`. Configure subscribers in AWS Console:

```bash
# .env
SNS_TOPIC_ARN=arn:aws:sns:us-east-1:123456789012:chandra-escalation-critical
```

**Recommended subscribers:**

- Email (team lead, on-call engineer)
- SMS (critical escalations only)
- SQS queue (for downstream automation)
- Lambda function (for Slack/Teams webhook relay)

### Health check alerts

```bash
# Monitor /health endpoint
# 200 OK → healthy
# Non-200 → unhealthy

# Monitor /health/ready endpoint
# 200 → all components healthy
# 503 → degraded (one or more components unavailable)
# Check per-component status in the response body
```

### Container health alerts

The Docker HEALTHCHECK probes `/health` every 30s. Configure Docker event monitoring:

```bash
# Watch Docker events
docker events --filter 'event=health_status'

# Set up Docker event monitoring with a webhook
# (e.g., using diun, watchtower, or custom scripts)
```

### Log-based alerting

Monitor logs for these patterns:

| Pattern | Severity | Action |
|---------|----------|--------|
| `ERROR` | Critical | Investigate immediately |
| `bedrock_unavailable_fallback_to_deterministic` | Warning | Check Bedrock quota |
| `CheckpointError` | Critical | Check database connectivity |
| `CUDA out of memory` | Critical | Restart vLLM with lower GPU utilization |
| `Connection refused` | Warning | Check service connectivity |
| `Migration failed` | Critical | Rollback migration |

---

## 8. Health Checks

### Liveness probe (`GET /health`)

```bash
curl http://localhost:6001/health
```

**Expected response:**
```json
{"status": "ok"}
```

**Purpose:** Confirms the process is alive and accepting requests. Does not check external dependencies — designed for container `HEALTHCHECK`.

### Readiness probe (`GET /health/ready`)

```bash
curl http://localhost:6001/health/ready
```

**Expected response (healthy):**
```json
{
  "status": "ok",
  "components": {
    "copilot_agent": "ok",
    "digital_worker": "ok",
    "postgres": "ok"
  }
}
```

**Expected response (degraded):**
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

**Component status mapping:**

| Component | Healthy | Unhealthy |
|-----------|---------|-----------|
| `copilot_agent` | LangGraph copilot graph loaded | Import or compilation error |
| `digital_worker` | Digital Worker graph loaded | Import or compilation error |
| `postgres` | `SELECT 1` succeeds | Connection refused or query fails |

### Docker HEALTHCHECK

The backend container includes a built-in healthcheck (see `Dockerfile`):

```
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python /app/healthcheck.py || exit 1
```

### Frontend health

```bash
# Check if frontend is serving
curl -s -o /dev/null -w "%{http_code}" http://localhost:3000

# Check for JS errors
# Open browser DevTools → Console tab
```

### vLLM health

```bash
# Check model list
curl http://localhost:8000/v1/models

# Check with a simple completion
curl http://localhost:8000/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"google/gemma-4-12B-it-qat-w4a16-ct","prompt":"OK","max_tokens":5}'
```

---

## 9. Scaling

### Vertical scaling

#### Backend (FastAPI)

```bash
# Increase workers in docker-compose.yml
command: ["uvicorn", "fastapi_app:app", "--host", "0.0.0.0", "--port", "6001", "--workers", "8"]

# Increase memory limit
mem_limit: 8g
mem_reservation: 4g
```

#### Postgres

```bash
# Increase memory and CPU in docker-compose.yml
deploy:
  resources:
    limits:
      cpus: '4'
      memory: 8G
    reservations:
      cpus: '2'
      memory: 4G
```

#### vLLM

```bash
# Increase GPU memory utilization
--gpu-memory-utilization 0.95

# Use a larger GPU instance
# g5.2xlarge (1×A10G, 24GB) → g5.12xlarge (4×A10G, 96GB)
# Or use tensor parallelism
--tensor-parallel-size 4
```

### Horizontal scaling

#### Backend

```bash
# Scale backend to multiple containers
docker compose up -d --scale backend=3

# Add a load balancer (ALB) in front
# docker-compose.yml already has nginx for basic load balancing
```

#### Database

```bash
# For production, use AWS RDS with Multi-AZ and read replicas
# Update POSTGRES_URL to point to the RDS endpoint
# POSTGRES_URL=postgresql+psycopg://chandra:chandra@chandra-db.cluster-xxxxx.us-east-1.rds.amazonaws.com:5432/chandra
```

### Resource limits configuration

```yaml
# docker-compose.yml resource limits
services:
  backend:
    mem_limit: 4g
    mem_reservation: 2g
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 4G
```

### Production recommendations

| Service | Min | Recommended | Max |
|---------|-----|-------------|-----|
| Backend | 2GB RAM, 1 CPU | 4GB RAM, 2 CPU | 8GB RAM, 8 CPU |
| Postgres | 2GB RAM, 1 CPU | 4GB RAM, 2 CPU | 16GB RAM, 8 CPU |
| Frontend | 1GB RAM, 1 CPU | 2GB RAM, 1 CPU | 4GB RAM, 2 CPU |
| vLLM (Gemma 4-12B) | 24GB VRAM | 24GB VRAM (g5.2xlarge) | 96GB VRAM (g5.12xlarge) |
| Nginx | 256MB RAM | 512MB RAM | 1GB RAM |

---

## 10. Troubleshooting Common Issues

### Container won't start

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Exited (1)` immediately | Missing env vars | Check `.env` file exists and has required values |
| `Exited (137)` | Out of memory | Increase `mem_limit` in docker-compose.yml |
| `Unhealthy` | Healthcheck failing | Check `docker compose logs backend` |
| `Port already allocated` | Port conflict | Change port mapping or kill the process using it |

### Backend API not responding

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Connection refused` | Backend not running | `docker compose up -d backend` |
| `500 Internal Server Error` | Application error | Check logs: `docker compose logs backend --tail=50` |
| `503 Service Unavailable` | Dependency unhealthy | Check `/health/ready` for component status |
| `504 Gateway Timeout` | LLM not responding | Check vLLM or Bedrock connectivity |

### Database issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `connection refused` | Postgres not running | `docker compose up -d postgres` |
| `password authentication failed` | Wrong credentials | Check `POSTGRES_URL` in `.env` matches `docker-compose.yml` |
| `relation does not exist` | Migrations not applied | `uv run alembic upgrade head` |
| `database is locked` | Concurrent write contention | Check for long-running transactions |

### LLM issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Connection refused` on port 8000 | vLLM not running | Start vLLM server |
| `CUDA out of memory` | GPU VRAM exhausted | Reduce `--gpu-memory-utilization` or use smaller model |
| Empty responses | Context window exceeded | Reduce `--max-model-len` or input size |
| `FlashInfer sampler` error | Missing env var | Set `vLLM_USE_FLASHINFER_SAMPLER=0` |

### Common error patterns and quick fixes

```bash
# 1. ModuleNotFoundError: No module named 'chandra'
uv sync --all-extras

# 2. psycopg.OperationalError: connection refused
docker compose up -d postgres
sleep 5
uv run alembic upgrade head

# 3. botocore.exceptions.NoCredentialsError
aws sts get-caller-identity
# Configure AWS credentials if this fails

# 4. LangGraph CheckpointError
# Fallback to MemorySaver — check if Postgres is up and migrated

# 5. Migration head mismatch
uv run alembic check
uv run alembic upgrade head

# 6. Port 6001 already in use
netstat -ano | findstr :6001
taskkill /PID <PID> /F

# 7. npm ERESOLVE
cd frontend
npm cache clean --force
npm install --legacy-peer-deps

# 8. Next.js build fails
rm -rf frontend/.next
cd frontend && npm run build
```

---

## Appendix: Quick Reference

### Daily operations checklist

```bash
# 1. Check all services are running
docker compose ps

# 2. Check backend health
curl http://localhost:6001/health

# 3. Check backend readiness
curl http://localhost:6001/health/ready

# 4. Check recent logs for errors
docker compose logs --since=1h backend | grep -E "(ERROR|CRITICAL)"

# 5. Check disk space
df -h

# 6. Check GPU (if using vLLM)
nvidia-smi

# 7. Run a health check on the database
docker compose exec postgres pg_isready -U chandra
```

### Weekly maintenance tasks

```bash
# 1. Backup database
docker compose exec -T postgres pg_dump -U chandra chandra | gzip > \
  backup_$(date +%Y%m%d).sql.gz

# 2. Prune unused Docker resources
docker system prune -f

# 3. Check for dependency updates
uv sync --all-extras
cd frontend && npm outdated

# 4. Run evaluation suite
uv run python -m chandra.cli eval --fixture evals/fixtures/baseline_v1.jsonl
```

### Emergency restart sequence

```bash
# 1. Graceful shutdown
docker compose down --timeout 120

# 2. Prune stale resources
docker system prune -f

# 3. Start fresh
docker compose up -d

# 4. Wait for health
sleep 10
curl http://localhost:6001/health/ready

# 5. Run migrations if needed
docker compose exec backend uv run alembic upgrade head
```