# Chandra — Production Deployment Guide

> **Enterprise AI Cloud Operations Platform**
> Covers Docker deployment, EC2 GPU deployment, environment configuration, database migrations, health checks, monitoring, backups, and rollback procedures.

---

## Table of Contents

1. [Docker Deployment](#docker-deployment)
2. [EC2 / GPU Deployment](#ec2--gpu-deployment)
3. [Environment Variables](#environment-variables)
4. [Database Migrations](#database-migrations)
5. [Health Checks](#health-checks)
6. [Monitoring](#monitoring)
7. [Backup](#backup)
8. [Rollback](#rollback)

---

## Docker Deployment

The full Chandra stack can be deployed via Docker Compose with five services: Postgres, Backend (FastAPI), Frontend (Next.js), Nginx (reverse proxy), and Gradio (legacy dashboard).

### Prerequisites

- Docker Engine ≥ 24
- Docker Compose v2
- `.env` file configured (see [Environment Variables](#environment-variables))

### Build and start

```bash
# Build and start all services
docker compose up -d

# View logs
docker compose logs -f

# Check service status
docker compose ps
```

### Service details

| Service | Image | Port(s) | Entrypoint |
|---------|-------|---------|------------|
| `postgres` | `postgres:16-alpine` | `5434:5432` | Default |
| `backend` | `chandra-app` (local build) | `6001` | `uvicorn fastapi_app:app --host 0.0.0.0 --port 6001` |
| `frontend` | `node:22-alpine` | `3000` | `npm start` |
| `nginx` | `nginx:alpine` | `80`, `443` | Default with `nginx.conf` |
| `gradio` | `chandra-app` | `7861` | `uv run app.py` |

### Rebuild after changes

```bash
docker compose up -d --build
```

### Production considerations

1. **Environment overrides** — The backend container overrides `POSTGRES_URL` internally to use the compose network hostname (`postgres:5432`). For external databases, set the URL in `.env` and the override will be replaced.
2. **Secrets** — Do not commit `.env`. Use Docker secrets or your orchestration platform's secret store in production.
3. **Healthcheck** — The backend container has a `HEALTHCHECK` instruction that probes `/health` every 30s. The compose file sets `depends_on` with `condition: service_healthy` for Postgres.
4. **Resource limits** — Consider adding `mem_limit` and `cpus` to each service in `docker-compose.yml` for production deployments.

### Railway deployment

A `railway.toml` is included for Railway.app deployments. Configure environment variables via the Railway dashboard.

---

## EC2 / GPU Deployment

### Runtime infrastructure (`iac/runtime/`)

The Terraform configuration at `iac/runtime/` provisions the AWS runtime infrastructure for Chandra:

```bash
cd iac/runtime
terraform init
terraform plan
terraform apply
```

This creates:

| Resource | Purpose |
|----------|---------|
| `aws_iam_role.chandra_runtime` | IAM role for ECS tasks with ReadOnlyAccess + SecurityAudit |
| `aws_iam_policy.bedrock_policy` | Grants `bedrock:InvokeModel` for Claude Sonnet 4.5 |
| `aws_iam_policy.runtime_boundary` | Permissions boundary — denies `iam:*` |
| `aws_cloudwatch_dashboard.chandra` | CloudWatch dashboard |

### GPU inference instance (`iac/runtime/inference.tf`)

> **Note:** The GPU inference module is **commented out by default**. Uncomment and configure for local LLM inference on EC2.

To deploy a GPU inference instance:

1. Edit `iac/runtime/inference.tf` and uncomment the `module "inference"` block and the IAM resources.

2. Set variables in `terraform.tfvars`:

```hcl
inference_instance_type = "g5.2xlarge"     # 1×A10G, 24GB VRAM
inference_llm_provider  = "vllm"            # vllm | ollama | llama-cpp
inference_model_name    = "Qwen/Qwen2.5-32B-Coder-Instruct"
inference_ami_id        = "ami-..."         # Deep Learning Base AMI (Ubuntu 22.04)
vpc_id                  = "vpc-..."
inference_subnet_id     = "subnet-..."
key_name                = "my-keypair"
huggingface_token       = "hf_..."          # For gated models
```

3. After deployment, set these `.env` variables to point Chandra at the inference instance:

```
LLM_PROVIDER=openai
OPENAI_API_BASE=http://<instance-private-ip>:8000/v1
LLM_MODEL=Qwen/Qwen2.5-32B-Coder-Instruct
```

> **Cost:** g5.2xlarge on-demand is ~$0.77/hr. Use spot instances for 60–70% savings.

### Synthetic environment (`iac/synthetic_env/`)

For evaluation and testing, deploy the synthetic environment that seeds ~10 known AWS misconfigurations:

```bash
make tf-apply    # cd iac/synthetic_env && terraform apply -auto-approve
make run         # Run the Chandra pipeline against the synthetic account
make eval        # Score pipeline findings against ground-truth manifest
make tf-destroy  # Tear down synthetic resources
```

---

## Environment Variables

The full set of environment variables is documented in `.env.example`. Below are the **critical** ones for production:

### AWS

| Variable | Description |
|----------|-------------|
| `AWS_DEFAULT_REGION` | AWS region (default: `us-east-1`) |
| `AWS_ACCESS_KEY_ID` | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key |
| `AWS_PROFILE` | Named AWS profile (alternative to access key) |
| `SYNTHETIC_ACCOUNT_ID` | AWS account ID for the synthetic env (eval) |
| `SNS_TOPIC_ARN` | SNS topic ARN for escalation notifications |

### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_URL` | `postgresql+psycopg://chandra:chandra@localhost:5434/chandra` | PostgreSQL connection string |
| `DATABASE_URL` | Same as `POSTGRES_URL` | Application database URL |

### LLM Provider

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `bedrock` | One of: `bedrock`, `vllm`, `openai`, `ollama` |
| `LLM_MODEL` | `anthropic.claude-sonnet-4-5-20250929-v1:0` | Model name for the active provider |
| `LLM_TEMPERATURE` | `0.0` | Sampling temperature |
| `BEDROCK_MODEL_ID` | `anthropic.claude-sonnet-4-5-20250929-v1:0` | Bedrock model ID |
| `VLLM_API_BASE` | `http://localhost:8000/v1` | vLLM endpoint |
| `OPENAI_API_BASE` | `http://localhost:8000/v1` | OpenAI-compatible endpoint |

### Frontend

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXT_PUBLIC_API_URL` | `http://localhost:6001` | Backend URL (from browser/client context) |
| `FRONTEND_URL` | `http://localhost:3000` | Frontend URL (for CORS configuration) |

### Webhook / Integration

| Variable | Description |
|----------|-------------|
| `CHANDRA_WEBHOOK_TOKEN` | If set, all webhooks require `X-Chandra-Webhook-Token` header |
| `SLACK_WEBHOOK_URL` | Slack incoming webhook |
| `TEAMS_WEBHOOK_URL` | Teams Power Automate webhook |
| `JIRA_SERVER` | Jira instance URL |
| `JIRA_EMAIL` | Jira account email |
| `JIRA_API_TOKEN` | Jira API token |

### Observability

| Variable | Description |
|----------|-------------|
| `LANGCHAIN_TRACING_V2` | Enable LangSmith tracing |
| `LANGFUSE_PUBLIC_KEY` | LangFuse public key |
| `LANGFUSE_SECRET_KEY` | LangFuse secret key |
| `AGENTOPS_API_KEY` | AgentOps monitoring key |

---

## Database Migrations

Alembic manages the database schema. Migrations live in `src/chandra/db/migrations/`.

### Apply pending migrations

```bash
# Via uv
uv run alembic upgrade head

# Or via make
make migrate
```

### Check migration status

```bash
uv run alembic check
uv run alembic current
uv run alembic history
```

### Create a new migration

```bash
uv run alembic revision --autogenerate -m "description_of_change"
```

Review the generated file in `src/chandra/db/migrations/versions/` before applying.

### Rollback

```bash
# Roll back one step
uv run alembic downgrade -1

# Roll back to a specific revision
uv run alembic downgrade <revision_hash>

# Roll back all the way
uv run alembic downgrade base
```

> **Important:** In production, always back up the database before running destructive downgrade operations.

---

## Health Checks

### Liveness (`GET /health`)

```bash
curl http://localhost:6001/health
```

Expected: `200 OK` with `{"status": "ok"}`.

Used by Docker `HEALTHCHECK` and container orchestrators. Does not depend on external services — confirms the process is alive and accepting requests.

### Readiness (`GET /health/ready`)

```bash
curl http://localhost:6001/health/ready
```

Expected: `200 OK` with `{"status": "ok", "components": {"copilot_agent": "ok", "digital_worker": "ok", "postgres": "ok"}}`.

Reports per-component status:
- `copilot_agent` — LangGraph chat agent loaded
- `digital_worker` — Digital Worker graph loaded
- `postgres` — Database connectivity (`SELECT 1`)

Returns `503` with `"status": "degraded"` when any component is unavailable.

### Docker HEALTHCHECK

The backend container includes a built-in healthcheck (see `Dockerfile` and `scripts/healthcheck.py`):

```
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python /app/healthcheck.py || exit 1
```

### Frontend verification

```
http://localhost:3000
```

---

## Monitoring

### CloudWatch Dashboard

The runtime Terraform (`iac/runtime/main.tf`) deploys a CloudWatch dashboard at `iac/runtime/dashboards/chandra.json`. It surfaces:

- Pipeline run status
- KRA detection results
- API endpoint metrics (via CloudWatch agent or container insights)

### OpenTelemetry

Chandra emits traces and metrics via OpenTelemetry. Configure exporters in `.env`:

```
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
OTEL_SERVICE_NAME=chandra
```

### LangSmith (LangChain tracing)

```bash
# .env
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=ls__...
LANGCHAIN_PROJECT=chandra
```

### Logging

- **Structured logging:** Uses `structlog` with JSON output for log aggregation tools (CloudWatch Logs, ELK, Datadog)
- **Log levels:** Set `LOG_LEVEL=INFO` (default), `LOG_LEVEL=DEBUG` for verbose output
- **Uvicorn log level:** Set `UVICORN_LOG_LEVEL=info`

### Container monitoring

```bash
# Docker stats
docker stats

# Service logs
docker compose logs -f --tail=100 backend

# Resource usage
docker inspect chandra-backend-1 | jq '.[0].State'
```

---

## Backup

### PostgreSQL

```bash
# Dump the database
docker compose exec -T postgres pg_dump -U chandra chandra > chandra_backup_$(date +%Y%m%d).sql

# Restore
cat chandra_backup_20250101.sql | docker compose exec -T postgres psql -U chandra chandra
```

For production (AWS RDS):
- Enable automated snapshots (daily, 7-day retention minimum)
- Manual snapshot before migrations: `aws rds create-db-snapshot --db-instance-identifier chandra --db-snapshot-identifier chandra-pre-migration-$(date +%Y%m%d)`

### Agent memory

The `agent_memory.json` file at the repo root contains learned patterns and resolution memory. Back it up alongside the database:

```bash
cp agent_memory.json agent_memory.json.$(date +%Y%m%d)
```

### Terraform state

> **Production requirement:** Use a remote backend (S3 + DynamoDB) for all production Terraform state. The local `terraform.tfstate` at the repo root is for development only.

```hcl
# backend.tf
terraform {
  backend "s3" {
    bucket         = "chandra-terraform-state"
    key            = "runtime/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "chandra-terraform-locks"
    encrypt        = true
  }
}
```

---

## Rollback

### Application rollback

```bash
# Docker: rebuild and restart previous image tag
docker compose up -d --build   # rebuilds from current code
docker compose up -d           # uses previously built images (no --build)

# Git: revert to a known-good commit
git log --oneline -10
git checkout <known-good-hash>
docker compose up -d --build
```

### Database rollback

```bash
# Alembic downgrade
uv run alembic downgrade -1

# Full restore from backup
docker compose exec -T postgres dropdb -U chandra chandra
docker compose exec -T postgres createdb -U chandra chandra
cat chandra_backup_20250101.sql | docker compose exec -T postgres psql -U chandra chandra
```

### Terraform rollback

```bash
# Destroy the latest apply
cd iac/runtime
terraform destroy -auto-approve

# Or roll back to a previous state version
terraform state pull > /tmp/current.tfstate
# Restore from S3 versioning or DynamoDB backup
```

### Rollback sequence (full)

1. **Stop traffic** — Scale down frontend / API gateway
2. **Restore database** — From snapshot or backup
3. **Roll back application** — Git checkout + rebuild
4. **Roll back Terraform** — If IaC changed
5. **Run health checks** — Verify `/health` and `/health/ready`
6. **Resume traffic** — Scale back up

---

## Security Checklist

| Item | Recommendation |
|------|---------------|
| IAM permissions boundary | `iam:*` is denied by `runtime_boundary` policy |
| Secrets management | Use AWS Secrets Manager or Parameter Store; never commit `.env` |
| Network isolation | Inference instances use private subnets |
| Webhook auth | Set `CHANDRA_WEBHOOK_TOKEN` to require header on all webhook endpoints |
| Dry-run by default | All write actions set `dry_run=True` unless explicitly configured |
| HITL approvals | Destructive remediations require human approval |
| TLS termination | Nginx or ALB in front of the backend handles HTTPS |
| Container security | Run as non-root user (`chandra` UID 10001) in Docker |