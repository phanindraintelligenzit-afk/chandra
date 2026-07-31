# Chandra — Deployment Guide

> **Enterprise AI Cloud Operations Platform**
> **Branch:** `feature/local-llm`
> **Last Updated:** 2026-07-30

---

## Table of Contents

1. [EC2 Provisioning](#1-ec2-provisioning)
2. [Docker Compose Deployment](#2-docker-compose-deployment)
3. [Environment Variables](#3-environment-variables)
4. [SSL/TLS via Nginx](#4-ssltls-via-nginx)
5. [Database Migration](#5-database-migration)
6. [Backup Strategy](#6-backup-strategy)
7. [Monitoring Setup](#7-monitoring-setup)

---

## 1. EC2 Provisioning

### Runtime infrastructure (`iac/runtime/`)

The Terraform configuration at `iac/runtime/` provisions the AWS runtime infrastructure:

```bash
cd iac/runtime
terraform init
terraform plan
terraform apply
```

#### Resources created

| Resource | Purpose |
|----------|---------|
| `aws_iam_role.chandra_runtime` | IAM role for ECS tasks with ReadOnlyAccess + SecurityAudit |
| `aws_iam_policy.bedrock_policy` | Grants `bedrock:InvokeModel` for Claude Sonnet 4.5 |
| `aws_iam_policy.runtime_boundary` | Permissions boundary — denies `iam:*` |
| `aws_cloudwatch_dashboard.chandra` | CloudWatch dashboard |

#### IAM role details

The `chandra_runtime` role includes:

- **ReadOnlyAccess** — AWS managed policy (read-only across all services)
- **SecurityAudit** — AWS managed policy (security auditing)
- **Bedrock access** — Custom policy for `bedrock:InvokeModel` on Claude Sonnet 4.5
- **Permissions boundary** — Denies `iam:*` actions (defense in depth)

### GPU inference instance (`iac/runtime/inference.tf`)

> **Note:** The GPU inference module is **commented out by default**. Uncomment for local LLM inference on EC2.

#### Step 1: Edit inference.tf

Uncomment the `module "inference"` block and the related IAM resources.

#### Step 2: Set variables in `terraform.tfvars`

```hcl
inference_instance_type = "g5.2xlarge"     # 1×A10G, 24GB VRAM — $0.77/hr on-demand
inference_llm_provider  = "vllm"            # vllm | ollama | llama-cpp
inference_model_name    = "Qwen/Qwen2.5-32B-Coder-Instruct"
inference_ami_id        = "ami-0abcdef1234567890"  # Deep Learning Base AMI (Ubuntu 22.04)
vpc_id                  = "vpc-12345678"
inference_subnet_id     = "subnet-12345678"
key_name                = "my-keypair"
huggingface_token       = "hf_..."          # For gated models
```

#### Step 3: Deploy

```bash
cd iac/runtime
terraform apply
```

#### Step 4: Configure `.env` to point at the inference instance

```bash
LLM_PROVIDER=openai
OPENAI_API_BASE=http://<instance-private-ip>:8000/v1
LLM_MODEL=Qwen/Qwen2.5-32B-Coder-Instruct
```

> **Cost savings:** Use spot instances for 60–70% savings on GPU costs. Add `instance_market_options` to the Terraform module.

### Synthetic environment (`iac/synthetic_env/`)

For evaluation and testing, deploy the synthetic environment that seeds ~10 known AWS misconfigurations:

```bash
make tf-apply    # cd iac/synthetic_env && terraform apply -auto-approve
make run         # Run the Chandra pipeline against the synthetic account
make eval        # Score pipeline findings against ground-truth manifest
make tf-destroy  # Tear down synthetic resources
```

#### Synthetic environment resources

| Resource | Misconfiguration |
|----------|-----------------|
| S3 bucket | Public read access enabled |
| EC2 security group | SSH (port 22) open to 0.0.0.0/0 |
| IAM role | Overly permissive policy |
| RDS instance | Deletion protection disabled |
| CloudTrail | Not configured |
| KMS key | Rotatable key without rotation |
| EBS volume | Unencrypted |
| S3 bucket | Versioning disabled |
| IAM user | Access key not rotated |
| CloudWatch alarm | Missing critical alarm |

---

## 2. Docker Compose Deployment

### Architecture

The full Chandra stack deploys via Docker Compose with five services:

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Internet   │────▶│    Nginx     │────▶│   Frontend   │
│  (80/443)    │     │  (80/443)    │     │  (Next.js)   │
└──────────────┘     └──────┬───────┘     └──────────────┘
                            │
                            ▼
                     ┌──────────────┐     ┌──────────────┐
                     │    Backend   │────▶│  PostgreSQL  │
                     │  (FastAPI)   │     │   (Port 5432)│
                     └──────┬───────┘     └──────────────┘
                            │
                            ▼
                     ┌──────────────┐
                     │  vLLM / LLM  │
                     │  (Optional)  │
                     └──────────────┘
```

### Service details

| Service | Image | Host Port | Container Port | Entrypoint |
|---------|-------|-----------|----------------|------------|
| `postgres` | `postgres:16-alpine` | 5434 | 5432 | Default |
| `backend` | `chandra-app` (local build) | 6001 | 6001 | `uvicorn fastapi_app:app --host 0.0.0.0 --port 6001 --workers 4` |
| `frontend` | `node:22-alpine` | 3000 | 3000 | `npm start` |
| `nginx` | `nginx:alpine` | 80, 443 | 80, 443 | Default with `nginx.conf` |
| `gradio` | `chandra-app` | 7861 | 7861 | `uv run app.py` |

### Deployment steps

#### Prerequisites

- Docker Engine ≥ 24
- Docker Compose v2
- `.env` file configured (see [Environment Variables](#3-environment-variables))
- AWS credentials configured (for Bedrock or AWS operations)

#### Build and deploy

```bash
# 1. Clone the repository
git clone https://github.com/phanindraintelligenzit-afk/chandra.git
cd chandra
git checkout feature/local-llm

# 2. Configure environment
cp .env.example .env
# Edit .env with production values

# 3. Build and start all services
docker compose up -d --build

# 4. Verify deployment
docker compose ps
curl http://localhost:6001/health
curl http://localhost:6001/health/ready
curl http://localhost:3000

# 5. View logs
docker compose logs -f
```

#### Rebuild after code changes

```bash
docker compose up -d --build
```

#### Production considerations

1. **Secrets management** — Do not commit `.env`. Use Docker secrets, AWS Secrets Manager, or Parameter Store in production.
2. **Healthcheck** — The backend container has a `HEALTHCHECK` instruction that probes `/health` every 30s. The compose file sets `depends_on` with `condition: service_healthy` for Postgres.
3. **Resource limits** — Add `mem_limit` and `cpus` to each service for production deployments.
4. **Network isolation** — All services communicate over the internal `chandra-network` bridge network.
5. **Graceful shutdown** — The backend has `stop_grace_period: 120s` to allow in-flight requests to complete.

### Railway deployment

A `railway.toml` is included for Railway.app deployments:

```bash
# Install Railway CLI
npm i -g @railway/cli

# Login and deploy
railway login
railway up
```

Configure environment variables via the Railway dashboard.

---

## 3. Environment Variables

### Critical variables (must be set)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AWS_DEFAULT_REGION` | Yes | `us-east-1` | AWS region |
| `AWS_ACCESS_KEY_ID` | Conditional | — | AWS access key (if not using profile) |
| `AWS_SECRET_ACCESS_KEY` | Conditional | — | AWS secret key (if not using profile) |
| `AWS_PROFILE` | Conditional | — | Named AWS profile (alternative to keys) |
| `POSTGRES_URL` | Yes | `postgresql+psycopg://chandra:chandra@localhost:5434/chandra` | PostgreSQL connection string |
| `DATABASE_URL` | Yes | Same as `POSTGRES_URL` | Application database URL |
| `SYNTHETIC_ACCOUNT_ID` | Yes | — | AWS account ID for synthetic env |
| `SNS_TOPIC_ARN` | Yes | — | SNS topic ARN for escalation notifications |
| `NEXT_PUBLIC_API_URL` | Yes | `http://localhost:6001` | Backend URL (from browser context) |
| `FRONTEND_URL` | Yes | `http://localhost:3000` | Frontend URL (for CORS) |

### LLM provider variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LLM_PROVIDER` | Yes | `bedrock` | One of: `bedrock`, `vllm`, `openai`, `ollama` |
| `LLM_MODEL` | Yes | `anthropic.claude-sonnet-4-5-20250929-v1:0` | Model name for the active provider |
| `LLM_TEMPERATURE` | No | `0.0` | Sampling temperature |
| `BEDROCK_MODEL_ID` | Conditional | `anthropic.claude-sonnet-4-5-20250929-v1:0` | Bedrock model ID |
| `VLLM_API_BASE` | Conditional | `http://localhost:8000/v1` | vLLM endpoint |
| `VLLM_MODEL` | Conditional | — | vLLM model name |
| `VLLM_API_KEY` | No | `not-needed` | vLLM API key |
| `OPENAI_API_BASE` | Conditional | `http://localhost:8000/v1` | OpenAI-compatible endpoint |
| `OPENAI_API_KEY` | No | — | OpenAI API key |
| `OLLAMA_HOST` | Conditional | `http://localhost:11434` | Ollama host |
| `OLLAMA_MODEL` | Conditional | — | Ollama model name |

### Integration variables

| Variable | Required | Description |
|----------|----------|-------------|
| `JIRA_SERVER` | Conditional | Jira instance URL |
| `JIRA_EMAIL` | Conditional | Jira account email |
| `JIRA_API_TOKEN` | Conditional | Jira API token |
| `SLACK_WEBHOOK_URL` | No | Slack incoming webhook |
| `TEAMS_WEBHOOK_URL` | No | Teams Power Automate webhook |
| `CHANDRA_WEBHOOK_TOKEN` | No | If set, all webhooks require `X-Chandra-Webhook-Token` header |

### Observability variables

| Variable | Description |
|----------|-------------|
| `LANGCHAIN_TRACING_V2` | Enable LangSmith tracing (`true`/`false`) |
| `LANGCHAIN_API_KEY` | LangSmith API key |
| `LANGCHAIN_PROJECT` | LangSmith project name |
| `LANGFUSE_PUBLIC_KEY` | LangFuse public key |
| `LANGFUSE_SECRET_KEY` | LangFuse secret key |
| `AGENTOPS_API_KEY` | AgentOps monitoring key |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OpenTelemetry collector endpoint |
| `OTEL_ENVIRONMENT` | Environment label (`production`, `staging`, `development`) |

### Token budget variables (for local LLM)

| Variable | Default | Description |
|----------|---------|-------------|
| `CHANDRA_TF_DOCS_MAX_CHARS` | 8000 | Max chars for Terraform docs (local LLM) |
| `CHANDRA_AWS_CTX_MAX_CHARS` | 6000 | Max chars for AWS context (local LLM) |
| `CHANDRA_MEMORY_MAX_CHARS` | 3000 | Max chars for memory context (local LLM) |
| `CHANDRA_AGENT_MAX_INPUT_CHARS` | 30000 | Max input chars for agent (local LLM) |
| `CHANDRA_STRUCTURED_OUTPUT_METHOD` | `json_schema` | Structured output method: `json_schema`, `function_calling`, `json_mode` |

### Full `.env.example` reference

See `.env.example` in the repo root for the complete, annotated set of all environment variables.

---

## 4. SSL/TLS via Nginx

### Default nginx configuration

The `nginx.conf` at the repo root handles HTTP reverse proxying:

```nginx
server {
    listen 80;
    server_name localhost;

    # Frontend (Next.js)
    location / {
        proxy_pass http://frontend:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Backend APIs
    location /api/backend/ {
        proxy_pass http://backend:6001/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

### Enabling HTTPS

#### Option 1: Self-signed certificate (development)

```bash
# Generate a self-signed certificate
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout /etc/nginx/ssl/chandra.key \
  -out /etc/nginx/ssl/chandra.crt \
  -subj "/CN=chandra.example.com"

# Update nginx.conf to add HTTPS server block
```

#### Option 2: Let's Encrypt (production)

```bash
# Install certbot
sudo apt-get install certbot python3-certbot-nginx

# Obtain certificate
sudo certbot --nginx -d chandra.example.com

# Auto-renewal (certbot sets up a systemd timer by default)
sudo certbot renew --dry-run
```

#### Option 3: AWS ALB (recommended for production)

```bash
# Terminate TLS at the Application Load Balancer
# 1. Create an ALB with HTTPS listener
# 2. Use AWS Certificate Manager (ACM) for the certificate
# 3. Point the ALB target group to the ECS service or EC2 instance
# 4. Update Nginx to serve HTTP only (behind ALB)
```

### Updated nginx.conf with HTTPS

```nginx
server {
    listen 443 ssl http2;
    server_name chandra.example.com;

    ssl_certificate /etc/nginx/ssl/chandra.crt;
    ssl_certificate_key /etc/nginx/ssl/chandra.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    # HSTS
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    location / {
        proxy_pass http://frontend:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /api/backend/ {
        proxy_pass http://backend:6001/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

# Redirect HTTP to HTTPS
server {
    listen 80;
    server_name chandra.example.com;
    return 301 https://$server_name$request_uri;
}
```

### Mounting certificates in Docker

```yaml
# docker-compose.yml
services:
  nginx:
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ./ssl:/etc/nginx/ssl:ro  # Mount certificate directory
```

---

## 5. Database Migration

### Alembic setup

Alembic manages the database schema. Migrations live in `src/chandra/db/migrations/`.

```bash
# alembic.ini (at repo root)
# Uses POSTGRES_URL from environment by default
# sqlalchemy.url = postgresql+psycopg://chandra:chandra@localhost:5434/chandra
```

### Migration commands

```bash
# Apply pending migrations
uv run alembic upgrade head
# or: make migrate

# Check migration status
uv run alembic check
uv run alembic current
uv run alembic history

# Create a new migration (autogenerate)
uv run alembic revision --autogenerate -m "description_of_change"

# Review the generated file
cat src/chandra/db/migrations/versions/<revision_hash>_description_of_change.py

# Apply the new migration
uv run alembic upgrade head
```

### Rollback

```bash
# Roll back one step
uv run alembic downgrade -1

# Roll back to a specific revision
uv run alembic downgrade <revision_hash>

# Roll back all the way
uv run alembic downgrade base

# Re-apply after rollback
uv run alembic upgrade head
```

### Migration workflow

```bash
# 1. Back up the database first
docker compose exec -T postgres pg_dump -U chandra chandra > \
  pre_migration_$(date +%Y%m%d).sql

# 2. Check current state
uv run alembic check

# 3. Apply migrations
uv run alembic upgrade head

# 4. Verify
uv run alembic check
uv run alembic current

# 5. If something goes wrong, roll back
uv run alembic downgrade -1
```

### Production migration checklist

- [ ] Backup database before migration
- [ ] Test migration on a staging environment first
- [ ] Schedule during maintenance window (read-only mode)
- [ ] Disable CI/CD pipeline triggers during migration
- [ ] Monitor logs for errors during migration
- [ ] Verify migration with `alembic check` after completion
- [ ] Roll back immediately if errors occur

---

## 6. Backup Strategy

### PostgreSQL backups

#### Docker (development)

```bash
# Scheduled backup script
cat > /usr/local/bin/chandra-backup.sh << 'EOF'
#!/bin/bash
BACKUP_DIR="/backups/chandra"
DATE=$(date +%Y%m%d_%H%M%S)
cd /opt/chandra
docker compose exec -T postgres pg_dump -U chandra chandra | gzip > \
  "$BACKUP_DIR/chandra_$DATE.sql.gz"
find "$BACKUP_DIR" -name "*.sql.gz" -mtime +30 -delete
EOF

# Add to crontab (daily at 2 AM)
0 2 * * * /usr/local/bin/chandra-backup.sh
```

#### AWS RDS (production)

```bash
# Enable automated snapshots (AWS Console or CLI)
aws rds modify-db-instance \
  --db-instance-identifier chandra \
  --backup-retention-period 7 \
  --preferred-backup-window "02:00-03:00"

# Manual snapshot before migrations
aws rds create-db-snapshot \
  --db-instance-identifier chandra \
  --db-snapshot-identifier chandra-pre-migration-$(date +%Y%m%d)

# Copy snapshot to another region (DR)
aws rds copy-db-snapshot \
  --source-db-snapshot-identifier arn:aws:rds:us-east-1:123456789012:snapshot:chandra-pre-migration-20260730 \
  --target-db-snapshot-identifier chandra-pre-migration-dr \
  --region us-west-2
```

### Agent memory backup

```bash
# The agent_memory.json file contains learned patterns
cp agent_memory.json agent_memory.json.$(date +%Y%m%d)
```

### Terraform state backup

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

### Backup schedule

| Data | Method | Frequency | Retention | Location |
|------|--------|-----------|-----------|----------|
| PostgreSQL | `pg_dump` | Daily | 30 days | S3 or local disk |
| PostgreSQL (RDS) | Automated snapshot | Daily | 7-30 days | AWS RDS |
| Agent memory | File copy | Per change | Git history | GitHub |
| Docker volumes | Volume backup | Weekly | 90 days | S3 |
| Terraform state | S3 + DynamoDB | Per apply | Versioned | S3 |

### Restore procedures

#### Full restore from backup

```bash
# 1. Stop the backend
docker compose stop backend

# 2. Drop and recreate the database
docker compose exec -T postgres dropdb -U chandra chandra
docker compose exec -T postgres createdb -U chandra chandra

# 3. Restore from backup
gunzip -c chandra_backup_20260730.sql.gz | \
  docker compose exec -T postgres psql -U chandra chandra

# 4. Apply any pending migrations
uv run alembic upgrade head

# 5. Restart the backend
docker compose start backend

# 6. Verify
curl http://localhost:6001/health/ready
```

#### Point-in-time recovery (RDS)

```bash
# Restore to a specific point in time
aws rds restore-db-instance-to-point-in-time \
  --source-db-instance-identifier chandra \
  --target-db-instance-identifier chandra-restored \
  --restore-time "2026-07-29T14:00:00Z" \
  --use-latest-restorable-time
```

---

## 7. Monitoring Setup

### CloudWatch Dashboard

The runtime Terraform (`iac/runtime/main.tf`) deploys a CloudWatch dashboard.

```bash
# Deploy the dashboard
cd iac/runtime
terraform init
terraform apply
```

The dashboard (`iac/runtime/dashboards/chandra.json`) includes:

- **Pipeline runs** — Success/failure rate per run, duration
- **KRA findings** — Detection results per KRA (cost, security, compliance, performance, reliability)
- **API metrics** — Request count, latency (p50/p95/p99), error rate
- **LLM usage** — Token consumption, cost (Bedrock)
- **System health** — Memory, CPU, disk usage per container

### OpenTelemetry

```bash
# .env
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
OTEL_SERVICE_NAME=chandra
OTEL_ENVIRONMENT=production
```

#### Deploying the OpenTelemetry collector

```yaml
# docker-compose.yml addition
otel-collector:
  image: otel/opentelemetry-collector-contrib:latest
  command: ["--config=/etc/otel-collector-config.yaml"]
  volumes:
    - ./otel-collector-config.yaml:/etc/otel-collector-config.yaml
  ports:
    - "4318:4318"  # OTLP HTTP
  networks:
    - chandra-network
```

### LangSmith

```bash
# .env
LANGCHAIN_TRACING_V2=true
LANGCHAIN_ENDPOINT=https://api.smith.langchain.com
LANGCHAIN_API_KEY=ls__your-key-here
LANGCHAIN_PROJECT=chandra
```

### LangFuse

```bash
# .env
LANGFUSE_PUBLIC_KEY=pk-lf-xxxxx
LANGFUSE_SECRET_KEY=sk-lf-xxxxx
LANGFUSE_HOST=https://cloud.langfuse.com
```

### Metrics to monitor

| Metric | Source | Alert threshold | Critical threshold |
|--------|--------|-----------------|-------------------|
| Backend CPU | Docker stats | > 80% | > 90% |
| Backend memory | Docker stats | > 80% | > 90% |
| API latency (p95) | CloudWatch / OTEL | > 2s | > 5s |
| API error rate | CloudWatch / OTEL | > 1% | > 5% |
| LLM latency | LangFuse / OTEL | > 5s | > 10s |
| LLM error rate | LangFuse / OTEL | > 2% | > 10% |
| Pipeline failure rate | CloudWatch | > 1% | > 5% |
| Postgres connections | RDS monitoring | > 50 | > 80 |
| Postgres CPU | RDS monitoring | > 70% | > 90% |
| Disk space | EC2 / Docker | > 80% | > 90% |

### Health check endpoints

| Endpoint | Type | Expected | Purpose |
|----------|------|----------|---------|
| `GET /health` | Liveness | `200 {"status": "ok"}` | Process is alive |
| `GET /health/ready` | Readiness | `200` with all components `"ok"` | All dependencies available |
| `GET /v1/models` (vLLM) | LLM probe | `200` with model list | LLM is serving |

### Container health check

```bash
# Docker HEALTHCHECK
docker inspect --format='{{json .State.Health}}' chandra-backend-1

# Log health status changes
docker events --filter 'event=health_status'
```

### Alerting setup

#### CloudWatch alarms

```bash
# CPU alarm
aws cloudwatch put-metric-alarm \
  --alarm-name chandra-backend-cpu-high \
  --alarm-description "Backend CPU > 80% for 5 minutes" \
  --metric-name CPUUtilization \
  --namespace AWS/ECS \
  --statistic Average \
  --period 300 \
  --threshold 80 \
  --comparison-operator GreaterThanThreshold \
  --evaluation-periods 2 \
  --alarm-actions arn:aws:sns:us-east-1:123456789012:chandra-alerts

# API error rate alarm
aws cloudwatch put-metric-alarm \
  --alarm-name chandra-api-errors \
  --metric-name 5xxCount \
  --namespace Chandra/API \
  --statistic Sum \
  --period 300 \
  --threshold 5 \
  --comparison-operator GreaterThanThreshold \
  --evaluation-periods 2 \
  --alarm-actions arn:aws:sns:us-east-1:123456789012:chandra-alerts
```

#### SNS topics

```bash
# Create SNS topics for different severity levels
aws sns create-topic --name chandra-alerts-critical
aws sns create-topic --name chandra-alerts-warning
aws sns create-topic --name chandra-alerts-info

# Subscribe email
aws sns subscribe \
  --topic-arn arn:aws:sns:us-east-1:123456789012:chandra-alerts-critical \
  --protocol email \
  --notification-endpoint oncall@company.com
```

---

## Appendix: Deployment Checklist

### Pre-deployment

- [ ] All environment variables configured in `.env`
- [ ] AWS credentials configured (profile or keys)
- [ ] LLM provider chosen and configured
- [ ] Database connection string is correct
- [ ] SNS topic ARN is valid
- [ ] Frontend URL is correct
- [ ] SSL certificates are in place (production)
- [ ] Docker and Docker Compose are installed
- [ ] Ports are available (80, 443, 3000, 5434, 6001, 8000, 7861)
- [ ] `make check` passes locally
- [ ] Database backup exists (before migration)

### Deployment

- [ ] Pull latest code: `git pull origin feature/local-llm`
- [ ] Build images: `docker compose build`
- [ ] Start services: `docker compose up -d`
- [ ] Verify health: `curl http://localhost:6001/health`
- [ ] Verify readiness: `curl http://localhost:6001/health/ready`
- [ ] Check logs for errors: `docker compose logs --tail=50 backend`

### Post-deployment

- [ ] Run a test pipeline: `uv run chandra run --account $SYNTHETIC_ACCOUNT_ID`
- [ ] Verify frontend loads: `curl http://localhost:3000`
- [ ] Check CloudWatch dashboard for new data
- [ ] Verify SNS notifications are working
- [ ] Run eval suite: `make eval`
- [ ] Update documentation if infrastructure changed

### Rollback procedures

1. **Stop traffic** — Scale down frontend / API gateway
2. **Restore database** — From snapshot or backup
3. **Roll back application** — Git checkout + rebuild
4. **Roll back Terraform** — If IaC changed
5. **Run health checks** — Verify `/health` and `/health/ready`
6. **Resume traffic** — Scale back up

### Security checklist

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