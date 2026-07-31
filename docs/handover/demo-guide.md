# Chandra — Demo Guide for Leadership Presentation

> **Enterprise AI Cloud Operations Platform — Complete Demo Script**
> **Demo Duration:** ~20 minutes
> **Audience:** CEO (PVR), Engineering Leadership, AWS Team
> **Last Updated:** 2026-07-30

---

## Table of Contents

1. [Demo Overview](#1-demo-overview)
2. [Prerequisites & Setup](#2-prerequisites--setup)
3. [Demo Script — Step by Step](#3-demo-script--step-by-step)
4. [Sample Data](#4-sample-data)
5. [Verification Commands](#5-verification-commands)
6. [Success Criteria](#6-success-criteria)
7. [Fallback Plans](#7-fallback-plans)

---

## 1. Demo Overview

### What the audience will see

Chandra is an **enterprise AI cloud operations platform** that:

1. **Observes** AWS accounts across 5 KRAs (Cost, Security, Compliance, Performance, Reliability)
2. **Analyzes** findings via LLM (Claude Sonnet 4.5 on Bedrock or local Gemma 4-12B on vLLM)
3. **Governs** actions through a human-in-the-loop approval workflow
4. **Executes** approved remediation plans deterministically
5. **Verifies** that remediations succeeded
6. **Documents** everything in a complete audit trail

### Demo flow

```
System Startup → Monitoring Dashboard → AWS Tasks → Permissions
→ Execution Review → Deploy → Local LLM → Human Approval
→ AWS Execution → Verification → History
```

### Key talking points

| Moment | Message |
|--------|---------|
| System Startup | "Chandra runs as a set of Docker containers — backend, frontend, Postgres, and optionally vLLM for local LLM inference." |
| Monitoring Dashboard | "The dashboard shows live infrastructure observations across all 5 KRAs — Cost, Security, Compliance, Performance, Reliability." |
| AWS Tasks | "We submit a task through the Jira channel. The Digital Worker normalizes the request and routes it through the governed pipeline." |
| Permissions | "The analyzer agent resolves the minimum IAM permissions required for each task — nothing more, nothing less." |
| Execution Review | "The system generates a structured execution plan with risk scoring. Low-risk actions proceed automatically; others need approval." |
| Deploy | "The execution engine generates Terraform code, validates it, and presents the plan for review." |
| Local LLM | "All code generation runs through a local Gemma 4-12B model — no data leaves our infrastructure." |
| Human Approval | "Every destructive action pauses for human approval. This is the safety gate." |
| AWS Execution | "Once approved, the executor applies the plan against the AWS account. Everything is logged." |
| Verification | "We verify the resource was created correctly — encryption enabled, public access blocked, versioning enabled." |
| History | "The complete audit trail is available — who submitted, what was done, who approved, what was created." |

---

## 2. Prerequisites & Setup

### Before the demo (do these at least 30 minutes before)

#### Terminal 1: Postgres + Backend

```bash
cd ~/projects/chandra

# Ensure Postgres is running
docker compose up -d postgres

# Wait for Postgres to be healthy
docker compose ps postgres
# → should show "healthy"

# Start the backend
uvicorn fastapi_app:app --host 0.0.0.0 --port 6001
```

> **Verify:** `curl http://localhost:6001/health` → `{"status":"ok"}`

#### Terminal 2: Frontend

```bash
cd ~/projects/chandra/frontend
npm run dev
```

> **Verify:** Open `http://localhost:3000` in a browser — the onboarding wizard should appear.

#### Terminal 3: vLLM (if using local LLM)

```bash
vLLM_USE_FLASHINFER_SAMPLER=0 vllm serve google/gemma-4-12B-it-qat-w4a16-ct \
  --gpu-memory-utilization 0.90 \
  --max-model-len 16384 \
  --enable-prefix-caching \
  --enforce-eager \
  --host 0.0.0.0 \
  --port 8000
```

> **Verify:** `curl http://localhost:8000/v1/models` → model list returned

#### Terminal 4: Verification (keep this open)

```bash
cd ~/projects/chandra

# Watch backend logs during the demo
curl -s http://localhost:6001/health
```

### Final pre-demo checklist

- [ ] All 4 terminals open and ready
- [ ] Postgres is healthy
- [ ] Backend returns 200 on `/health` and `/health/ready`
- [ ] Frontend loads at `http://localhost:3000`
- [ ] vLLM is serving (if using local LLM)
- [ ] AWS credentials configured (`aws sts get-caller-identity`)
- [ ] `.env` has `LLM_PROVIDER` set correctly (bedrock or vllm)
- [ ] Browser is maximized and shows the frontend
- [ ] Screen recording is ready (optional)
- [ ] Speaker notes printed (see Narrator Script sections below)

---

## 3. Demo Script — Step by Step

---

### Step 1: System Startup (1 min)

**Action:** Open all three services.

**Screen:** Show the three terminals running (Postgres, Backend, Frontend).

**Narrator script:**
> "Chandra is an enterprise AI cloud operations platform. It runs as a set of Docker containers — a FastAPI backend, a Next.js operations console, PostgreSQL for persistence, and optionally a local LLM via vLLM for air-gapped operation. Everything you're about to see runs on our infrastructure — no external API calls beyond AWS."

**Verification:**
```bash
curl http://localhost:6001/health
# → {"status":"ok"}

curl http://localhost:6001/health/ready
# → {"status":"ok","components":{"copilot_agent":"ok","digital_worker":"ok","postgres":"ok"}}
```

**Success criteria:** ✅ All services return healthy status.

---

### Step 2: Onboarding — Create a Digital Worker (2 min)

**Action:** Navigate to `http://localhost:3000` and complete the onboarding wizard.

**Screen:** Show the Chrome browser with the Chandra onboarding wizard.

**Narrator script:**
> "The onboarding wizard provisions a new Digital Worker agent. It's a five-step flow: Name, Avatar, Role, Maturity, KRAs, Permissions, and Deploy."

**Step-by-step UI actions:**

| Step | Field | Value |
|------|-------|-------|
| 1 | Agent Name | `AWS-Demo-Agent` |
| 2 | Avatar | Select a holographic agent portrait |
| 3 | Role | `Cloud Engineer` |
| 4 | Maturity | `Auto` (autonomous operations) |
| 5 | KRAs | Enable all five: Cost, Security, Compliance, Performance, Reliability |
| 6 | Permissions | AWS read-only + S3 write |
| 7 | Deploy | Click "Deploy Agent" |

**Expected result:** Redirect to the Chandra operations dashboard.

**Narrator script:**
> "The agent is now provisioned. Behind the scenes, the LangGraph pipeline loaded, the Postgres checkpointer initialized, and the Digital Worker graph compiled — all in under two seconds."

**Verification:**
```bash
# Backend registers the new session
curl http://localhost:6001/health
# → {"status":"ok"}
```

**Success criteria:** ✅ Onboarding completes, frontend redirects to dashboard.

---

### Step 3: Monitoring Dashboard (1 min)

**Action:** Show the dashboard after onboarding.

**Screen:** The Chandra Experience dashboard is visible.

**Narrator script:**
> "This is the operations dashboard. It provides a unified view of the entire infrastructure. On the left, the live ops stream shows observations as they happen. The active incidents panel surfaces any ongoing issues. The cost monitoring chart shows daily trends. And the KRA performance bars give an at-a-glance health score for each of the five Key Result Areas."

**Expected UI elements:**
- **Ops Stream** — live feed of infrastructure observations
- **Active Incidents** — ongoing issues detected
- **Cost Monitoring** — daily cost trends
- **Infrastructure Health** — per-service health status
- **Performance Scoring** — KRA performance bars
- **Approval Center** — pending approvals (0 initially)

**Narrator script:**
> "Every observable fact on this dashboard is gathered by deterministic boto3 detectors — the LLM never invents findings. It only runs downstream to rank and prioritize what the detectors found."

**Success criteria:** ✅ Dashboard renders all panels.

---

### Step 4: AWS Tasks — Submit a Task (2 min)

**Action:** Submit a task via the REST API.

**Screen:** Terminal showing the curl command and response.

**Narrator script:**
> "Let's submit a real task. We'll create an S3 bucket for a data lake team. The request comes in through the Jira channel — but Chandra accepts requests from 10 channels: Jira, Slack, Teams, Email, REST API, and five monitoring webhooks."

**Execute:**
```bash
curl -X POST http://localhost:6001/requests \
  -H 'Content-Type: application/json' \
  -d '{
    "source": "jira",
    "payload": {
      "title": "Create S3 bucket acme-data-lake with SSE-S3 encryption",
      "description": "Need a new S3 bucket for the data lake team. Must have SSE-S3 encryption enabled, public access blocked, and versioning enabled.",
      "priority": "P2",
      "resource_id": "acme-data-lake",
      "ticket_id": "SEC-123",
      "requested_by": "platform-team@company.com"
    },
    "dry_run": false
  }'
```

**Expected response:**
```json
{
  "status": "accepted",
  "job_id": "dw-<uuid>",
  "message": "Request submitted for processing"
}
```

**Narrator script:**
> "The Digital Worker intake normalizes the Jira payload into a standard CloudRequest envelope. Every channel produces the same internal format — 10 channels, one pipeline."

**Save the `job_id` value** — you'll need it in later steps.

**Success criteria:** ✅ Request accepted with a job_id.

---

### Step 5: Permissions — Role Resolution (1 min)

**Action:** Show the permissions resolution.

**Screen:** Terminal or dashboard showing the permissions breakdown.

**Narrator script:**
> "The analyzer agent identifies the resource type — S3 — and resolves the minimum IAM permissions needed. This is deterministic: no LLM call. It's a simple lookup in the registered ActionExecutor handler registry."

**Expected permissions resolved:**
```json
{
  "permissions": [
    "s3:CreateBucket",
    "s3:PutBucketEncryption",
    "s3:PutBucketPublicAccessBlock",
    "s3:PutBucketVersioning",
    "s3:GetBucketLocation",
    "s3:ListBucket"
  ],
  "resources": ["arn:aws:s3:::acme-data-lake"]
}
```

**Narrator script:**
> "Notice it only requests the minimum permissions needed — no wildcards, no extra privileges. This is defense in depth."

**Success criteria:** ✅ Permissions resolved to the minimum set.

---

### Step 6: Execution Review — Plan Generation (1 min)

**Action:** Check the execution plan.

**Screen:** Terminal showing the plan status.

**Narrator script:**
> "The system generates a structured execution plan. Let's see what it decided."

**Execute:**
```bash
# Replace <job_id> with the actual job ID
curl http://localhost:6001/requests/<job_id>
```

**Expected output:**
The plan includes:
- **Resource:** `acme-data-lake`
- **Platform:** AWS S3
- **Category:** Storage Provisioning
- **Risk Score:** Low (standard S3 bucket creation)
- **Steps:**
  1. Create bucket `acme-data-lake` in `us-east-1`
  2. Enable SSE-S3 encryption
  3. Block all public access
  4. Enable versioning

**Narrator script:**
> "The plan is clear: four steps, low risk. The LLM (Gemma 4-12B running locally) generated this plan based on the original request context and AWS best practices. But we don't execute yet — first, the system validates every step and presents it for approval."

**Success criteria:** ✅ Execution plan generated with clear steps and risk score.

---

### Step 7: Deploy — Execution Pipeline (1 min)

**Action:** Show the pipeline stages progressing.

**Screen:** Terminal or dashboard showing pipeline status.

**Narrator script:**
> "The execution pipeline has six stages before anything touches AWS. Let's watch it progress."

**Pipeline stages:**
```
1. ✅ Read Existing — Checks if bucket already exists
2. ✅ Read Reference — Loads runbook/KB for S3 best practices
3. ✅ Analyze — Confirms no conflicts
4. ✅ Generate — LLM generates Terraform code
5. ✅ Validate — Syntax check on generated code
6. ✅ Plan — Dry-run against the AWS account
7. ⏳ Plan Review — Ready for human approval
```

**Narrator script:**
> "Stage four is where the LLM generates the Terraform code. The local Gemma model produces this entirely on our infrastructure — no data ever leaves our network."

**Success criteria:** ✅ Pipeline reaches "Plan Review" stage.

---

### Step 8: Local LLM — Code Generation (2 min)

**Action:** Show the generated Terraform code.

**Screen:** Terminal or dashboard showing the generated code.

**Narrator script:**
> "Here's the Terraform code the local LLM generated. Notice it includes encryption configuration, public access blocking, and versioning — exactly what was requested."

**Show generated code:**
```hcl
resource "aws_s3_bucket" "data_lake" {
  bucket = "acme-data-lake"
  force_destroy = false
  tags = {
    Name        = "acme-data-lake"
    Environment = "production"
    ManagedBy   = "chandra"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id
  versioning_configuration {
    status = "Enabled"
  }
}
```

**Narrator script:**
> "The LLM operates in a sandbox — it generates code, but the deterministic validator checks every line. If the code doesn't compile, the pipeline fails before anything touches AWS."

**Success criteria:** ✅ Valid Terraform code generated.

---

### Step 9: Human Approval — HITL Gate (1 min)

**Action:** Navigate to the Human Approval Center and approve.

**Screen:** Frontend showing the approval card.

**Narrator script:**
> "This is the human-in-the-loop gate. Every destructive or infrastructure-modifying action pauses here. The card shows the original request, the resolution plan, the risk assessment, and the generated code."

**Expected UI:** Approval card showing:
- **Source:** Jira (SEC-123)
- **Summary:** Create S3 bucket acme-data-lake with SSE-S3 encryption
- **Risk Score:** Low
- **RCA Summary:** Standard S3 provisioning request
- **Resolution Plan:** Creates bucket, enables encryption, blocks public access, enables versioning
- **Actions:** `[Approve]` `[Reject]` `[Escalate]`

**Action:** Click **Approve**.

**Narrator script:**
> "I'm approving this because it's low-risk, well-scoped, and properly generated. In production, this gate ensures that every modification — especially security-related ones — has explicit human sign-off."

**Behind the scenes:**
```bash
# This is what the frontend calls
curl -X POST http://localhost:6001/requests/<job_id>/approve \
  -H 'Content-Type: application/json' \
  -d '{"approved": true, "reason": "Approved for demo presentation"}'
```

**Success criteria:** ✅ Approval accepted, execution begins.

---

### Step 10: AWS Execution — Apply (1 min)

**Action:** Watch the execution progress.

**Screen:** Terminal showing execution status.

**Narrator script:**
> "The executor applies the approved plan against the AWS account. Every API call is logged and audited."

**Execute:**
```bash
curl http://localhost:6001/requests/<job_id>
```

**Expected response:**
```json
{
  "status": "completed",
  "job_id": "dw-<uuid>",
  "result": {
    "resource_created": "acme-data-lake",
    "arn": "arn:aws:s3:::acme-data-lake",
    "steps_completed": 4,
    "steps_failed": 0
  }
}
```

**Narrator script:**
> "Four steps, zero failures. The bucket was created, encryption was enabled, public access was blocked, and versioning was turned on — all in under 45 seconds from approval."

**Success criteria:** ✅ Execution completes with zero failures.

---

### Step 11: Verification — Confirm the Resource (1 min)

**Action:** Verify the S3 bucket was created correctly.

**Screen:** Terminal with AWS CLI verification commands.

**Narrator script:**
> "We don't just trust the executor's word. The verifier stage checks every aspect of the created resource against the original plan."

**Execute:**
```bash
# Check bucket exists
aws s3api head-bucket --bucket acme-data-lake

# Check encryption
aws s3api get-bucket-encryption --bucket acme-data-lake

# Check public access block
aws s3api get-public-access-block --bucket acme-data-lake

# Check versioning
aws s3api get-bucket-versioning --bucket acme-data-lake
```

**Expected results:**
```bash
# head-bucket → HTTP 200
# encryption → SSEAlgorithm: AES256
# public-access-block → all four flags = true
# versioning → Status: Enabled
```

**Narrator script:**
> "All four checks pass. The bucket has encryption enabled, public access is completely blocked, and versioning is on. This is exactly what the original ticket requested."

**Success criteria:** ✅ All four verification checks pass.

---

### Step 12: History & Audit Trail (1 min)

**Action:** View the complete history.

**Screen:** Terminal showing the audit trail.

**Narrator script:**
> "Chandra maintains a complete, immutable audit trail. Let's review what happened end-to-end."

**Execute:**
```bash
curl http://localhost:6001/requests?status=completed
```

**Expected output:** A list of completed requests showing:
- **Who submitted:** platform-team@company.com (via Jira SEC-123)
- **What was executed:** S3 bucket acme-data-lake creation
- **Plan:** 4 steps (create, encrypt, block public access, version)
- **Code generated:** Validated Terraform (4 resources)
- **Risk score:** Low
- **Approved by:** Demo presenter
- **Approval time:** [timestamp]
- **Execution completed:** [timestamp]
- **Verification passed:** All 4 checks
- **Resources created:** `arn:aws:s3:::acme-data-lake`

```bash
# Also check progress
curl http://localhost:6001/jobs/status/<job_id>
```

**Expected:**
```json
{
  "status": "completed",
  "progress": {
    "intake": "completed",
    "analysis": "completed",
    "planning": "completed",
    "approval": "completed",
    "execution": "completed",
    "verification": "completed"
  },
  "duration_seconds": 45
}
```

**Narrator script:**
> "Every stage is logged — from intake to verification, with timestamps. This is a complete audit trail that answers who, what, when, where, and why for every action Chandra takes."

---

### Step 13: Final Dashboard View (1 min)

**Action:** Return to the Chandra dashboard.

**Screen:** Frontend showing the updated dashboard.

**Narrator script:**
> "Back on the dashboard, we can see the completed task in the history panel. The approval center shows zero pending approvals — everything has been cleared. The audit trail entry captures the full lifecycle."

**Expected UI:**
- ✅ **Completed** status for the S3 bucket creation task
- **Audit trail** entry in the History panel
- **Updated metrics** reflecting the new S3 resource
- **Approval center** showing 0 pending approvals

---

### Closing (1 min)

**Narrator script:**
> "This demo showed the full lifecycle of a Chandra Digital Worker task:
>
> 1. **Multi-channel intake** — Jira, Slack, Teams, REST — 10 channels, one pipeline
> 2. **Deterministic detection** — No LLM involvement in gathering facts
> 3. **LLM-powered planning** — Local Gemma 4-12B generates structured plans
> 4. **Human governance** — Every destructive action requires approval
> 5. **Deterministic execution** — Validated, logged, audited
> 6. **Post-execution verification** — Confirm the resource matches the plan
>
> The key differentiator is the separation between LLM-powered reasoning (planning, analysis, narrative) and deterministic execution (detection, routing, execution, verification). The LLM never touches infrastructure directly — it only generates plans and code that are validated and gated before execution."

---

## 4. Sample Data

### Sample task: Create S3 bucket

```json
{
  "source": "jira",
  "payload": {
    "title": "Create S3 bucket acme-data-lake with SSE-S3 encryption",
    "description": "New S3 bucket for data lake team. Must have SSE-S3 encryption, public access blocked, versioning enabled.",
    "priority": "P2",
    "resource_id": "acme-data-lake",
    "ticket_id": "SEC-123",
    "requested_by": "platform-team@company.com"
  },
  "dry_run": false
}
```

### Sample task: EC2 right-sizing (alternative)

```json
{
  "source": "rest_api",
  "payload": {
    "title": "Right-size over-provisioned EC2 instances in production",
    "description": "Several m5.xlarge instances have CPU utilization below 10% for the past 30 days. Recommend downsizing to m5.large.",
    "priority": "P3",
    "resource_id": "i-1234567890abcdef0",
    "requested_by": "cost-optimization@company.com"
  },
  "dry_run": true
}
```

### Sample task: Security group cleanup (alternative)

```json
{
  "source": "slack",
  "payload": {
    "title": "Remove overly permissive security group rule",
    "description": "Security group sg-12345 has SSH (port 22) open to 0.0.0.0/0. Restrict to corporate IP range.",
    "priority": "P1",
    "resource_id": "sg-12345",
    "requested_by": "security-team@company.com"
  },
  "dry_run": false
}
```

### Permission set: S3 bucket permissions

```json
{
  "permissions": [
    "s3:CreateBucket",
    "s3:PutBucketEncryption",
    "s3:PutBucketPublicAccessBlock",
    "s3:PutBucketVersioning",
    "s3:GetBucketLocation",
    "s3:ListBucket"
  ],
  "resources": ["arn:aws:s3:::acme-data-lake"]
}
```

### Jira ticket: SEC-123

```json
{
  "ticket_id": "SEC-123",
  "summary": "Provision S3 bucket for data lake - acme-data-lake",
  "description": "Platform team needs a new S3 bucket. Requirements: SSE-S3 encryption, no public access, versioning enabled.",
  "priority": "P2",
  "assignee": "Chandra Digital Worker",
  "status": "Open"
}
```

---

## 5. Verification Commands

### Quick health checks (run before demo)

```bash
# 1. Backend health
curl http://localhost:6001/health

# 2. Backend readiness
curl http://localhost:6001/health/ready

# 3. vLLM model list (if using local LLM)
curl http://localhost:8000/v1/models

# 4. Frontend status
curl -s -o /dev/null -w "%{http_code}" http://localhost:3000

# 5. Postgres (via Docker)
docker compose exec postgres pg_isready -U chandra

# 6. AWS credentials
aws sts get-caller-identity
```

### Demo flow commands

```bash
# 1. Submit a test request
curl -X POST http://localhost:6001/requests \
  -H 'Content-Type: application/json' \
  -d '{"source":"rest_api","payload":{"title":"Test","priority":"P3","resource_id":"test-123"},"dry_run":true}'

# 2. List all requests
curl http://localhost:6001/requests

# 3. Check a specific request status
curl http://localhost:6001/jobs/status/<job_id>

# 4. Approve a request
curl -X POST http://localhost:6001/requests/<job_id>/approve \
  -H 'Content-Type: application/json' \
  -d '{"approved":true}'

# 5. Verify AWS resource
aws s3api head-bucket --bucket acme-data-lake
aws s3api get-bucket-encryption --bucket acme-data-lake
aws s3api get-public-access-block --bucket acme-data-lake
aws s3api get-bucket-versioning --bucket acme-data-lake

# 6. Check pipeline run
uv run chandra run --account $SYNTHETIC_ACCOUNT_ID
```

---

## 6. Success Criteria

### Master checklist

| # | Criterion | Target | Verification Method |
|---|-----------|--------|-------------------|
| 1 | All services start | ✅ | `curl localhost:6001/health` returns 200 |
| 2 | Backend API responds | ✅ | `GET /health/ready` returns all components "ok" |
| 3 | Frontend loads | ✅ | Browser shows dashboard at localhost:3000 |
| 4 | Onboarding completes | ✅ | Redirects to dashboard after wizard |
| 5 | vLLM serves models | ✅ | `GET /v1/models` returns model list |
| 6 | Task is submitted | ✅ | `POST /requests` returns job_id |
| 7 | Permissions resolved | ✅ | Minimum IAM permissions identified |
| 8 | Execution plan generated | ✅ | Plan shows 4 steps with risk score |
| 9 | LLM generates valid code | ✅ | Terraform passes syntax validation |
| 10 | Code is correct | ✅ | Encryption, public access block, versioning all present |
| 11 | Human approval works | ✅ | Approval gate pauses execution |
| 12 | Approval accepted | ✅ | Pipeline resumes after Approve click |
| 13 | AWS resource created | ✅ | S3 bucket exists with correct configuration |
| 14 | Encryption enabled | ✅ | `get-bucket-encryption` returns AES256 |
| 15 | Public access blocked | ✅ | All four flags = true |
| 16 | Versioning enabled | ✅ | Status = "Enabled" |
| 17 | Audit trail complete | ✅ | Request history shows full lifecycle |
| 18 | End-to-end pipeline | ✅ | Task submitted → code gen → approved → executed → verified |
| 19 | Dashboard updates | ✅ | UI shows completed task, audit entry, cleared approvals |
| 20 | All 5 KRAs observed | ✅ | Cost, Security, Compliance, Performance, Reliability all report |

### What failure looks like (and how to recover)

| Failure | Recovery |
|---------|----------|
| Backend won't start | Check `.env`, check Postgres, check `uv sync --all-extras` |
| Frontend blank | Check `NEXT_PUBLIC_API_URL`, clear `.next` cache |
| vLLM not responding | Check GPU, restart with `--enforce-eager` |
| AWS credentials error | Run `aws sts get-caller-identity` — reconfigure if needed |
| Task submission fails | Check backend logs for errors |
| Approval stuck | Post to `/requests/job_id/approve` directly via curl |

---

## 7. Fallback Plans

### Plan B: No AWS access

If AWS credentials aren't available for the demo:

1. Submit a task with `"dry_run": true` — the full pipeline runs including code generation, validation, and approval, but the executor simulates the API calls
2. Show the generated Terraform code as evidence of successful planning
3. Show the verification step would use `aws s3api` commands

### Plan C: vLLM unavailable

If the local LLM isn't responding:

1. Switch to Bedrock: `LLM_PROVIDER=bedrock` in `.env`, restart backend
2. The fallback mechanism automatically routes through Bedrock if vLLM is down
3. The demo looks identical — only the LLM backend changes

### Plan D: Frontend issues

If the Next.js frontend isn't working:

1. Demonstrate everything via curl commands — the backend API is the real surface
2. Key endpoints to show:
   - `POST /requests` — submit a task
   - `GET /requests` — list all requests
   - `GET /jobs/status/<id>` — check progress
   - `POST /requests/<id>/approve` — approve
   - `GET /health/ready` — component health

### Plan E: Complete system failure

If nothing works:

1. Show the architecture diagram (`docs/architecture-diagram.html`)
2. Walk through the code — show `src/chandra/llm/providers.py` (the LLM factory), `src/chandra/graphs/chandra_graph.py` (the pipeline graph), `src/chandra/digital_worker/graph.py` (the Digital Worker graph)
3. Show the CLAUDE.md for architectural invariants
4. Point to the Deployed demo at: https://aishanic12.github.io/chandra_extended/