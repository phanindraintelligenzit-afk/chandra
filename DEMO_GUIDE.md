# DEMO_GUIDE.md — AWS Team Review Demonstration Script

A step-by-step demonstration script of the **Chandra Digital Cloud Engineer** platform for the AWS team review. This demo walks through the full lifecycle: onboarding a digital worker, monitoring AWS infrastructure, executing tasks with LLM-generated code, and human-in-the-loop approval.

---

## Overview

**Chandra** is an enterprise AI cloud operations platform that observes AWS accounts, detects misconfigurations, generates remediation plans, and executes them through a governed human-in-the-loop approval workflow.

**Demo duration:** ~15 minutes

**Demo audience:** AWS team, engineering leadership

---

## Demo Flow

```
Onboarding → Monitoring → AWS Tasks → AWS Permissions → Execution Review
  → Deploy → Execution Engine → LLM Code Generation → Human Approval
  → AWS Execution → Verification → Progress → History → Dashboard
```

---

## Prerequisites (for the demo presenter)

Ensure all services are running before starting the demo:

```bash
# Terminal 1: Backend
uvicorn fastapi_app:app --host 0.0.0.0 --port 6001

# Terminal 2: Frontend
cd frontend && npm run dev

# Terminal 3: vLLM
vLLM_USE_FLASHINFER_SAMPLER=0 vllm serve google/gemma-4-12B-it-qat-w4a16-ct \
  --gpu-memory-utilization 0.90 --max-model-len 16384 \
  --enable-prefix-caching --enforce-eager \
  --host 0.0.0.0 --port 8000

# Terminal 4: Postgres (already running)
docker compose up -d postgres
```

---

## Step-by-Step Script

### Step 1: Onboarding (Frontend UI)

**Action:** Open **http://localhost:3000** in the browser.

**Expected UI:** The Chandra Onboarding wizard appears with a five-step provisioning flow:

1. **Name** → Enter "AWS-Demo-Agent"
2. **Avatar** → Select a holographic agent portrait
3. **Role** → Select "Cloud Engineer"
4. **Maturity** → Select "Auto" (autonomous operations)
5. **KRAs** → All five KRAs enabled: Cost, Security, Compliance, Performance, Reliability
6. **Permissions** → AWS read-only + S3 write permissions
7. **Deploy** → Click "Deploy Agent"

**Expected output:** Agent profile created, redirected to the Chandra ops dashboard.

**Verification:**
```bash
# Check the backend received the onboarding
curl http://localhost:6001/health
# → {"status":"ok"}
```

---

### Step 2: Monitoring (Live Dashboard)

**Action:** Observe the Chandra experience dashboard.

**Expected UI:** The dashboard shows:
- **Ops Stream** — live feed of infrastructure observations
- **Active Incidents** — any ongoing issues detected
- **Cost Monitoring** — daily cost trends
- **Infrastructure Health** — per-service health status
- **Performance Scoring** — KRA performance bars

**Narrator script:** "Chandra continuously monitors the AWS account across five Key Result Areas — Cost, Security, Compliance, Performance, and Reliability. Each KRA runs deterministic detectors that never call the LLM — they simply gather facts from AWS APIs."

---

### Step 3: AWS Tasks (Submit a Task)

**Action:** Submit a task via the REST API or Jira integration.

**Sample Task:** Create an S3 bucket with proper encryption.

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

**Expected output:**
```json
{
  "status": "accepted",
  "job_id": "dw-<uuid>",
  "message": "Request submitted for processing"
}
```

**Narrator script:** "We submit a task through the Jira channel. The Digital Worker intake normalizes the request into a standard CloudRequest envelope and routes it through the governed pipeline."

---

### Step 4: AWS Permissions (Role Resolution)

**Action:** The system resolves the required AWS permissions for the task.

**Behind the scenes:** The analyzer agent identifies the resource type (S3) and determines the required IAM permissions:
- `s3:CreateBucket`
- `s3:PutBucketEncryption`
- `s3:PutBucketPublicAccessBlock`
- `s3:PutBucketVersioning`

**Narrator script:** "The analyzer agent identifies the resource type — S3 — and resolves the minimum IAM permissions needed to execute this task. This is a deterministic step that maps the request to a registered ActionExecutor handler."

---

### Step 5: Execution Review (Plan Generation)

**Action:** The system generates an execution plan.

**Expected output (check via API):**
```bash
curl http://localhost:6001/requests/<job_id>
```

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

---

### Step 6: Deploy (Execution Pipeline)

**Action:** The execution engine starts processing the plan.

**Pipeline stages:**
1. ✅ **Read Existing** — Checks if bucket already exists
2. ✅ **Read Reference** — Loads runbook/KB for S3 best practices
3. ✅ **Analyze** — Confirms no conflicts
4. ✅ **Generate** — LLM generates Terraform/CloudFormation code
5. ✅ **Validate** — Syntax check on generated code
6. ✅ **Plan** — Dry-run against the AWS account
7. ⏳ **Plan Review** — Ready for human approval

---

### Step 7: Execution Engine (LLM Code Generation)

**Action:** The LLM (local Gemma 4-12B) generates the infrastructure code.

**Narrator script:** "The LLM generates the Terraform code for the S3 bucket. Notice that the LLM works within a sandbox workspace — it generates code, but the deterministic validator checks every line before it touches infrastructure."

**Sample generated code:**
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

---

### Step 8: Human Approval (HITL Gate)

**Action:** The workflow pauses at the `approval_gate` waiting for human approval.

**Expected UI:** The **Human Approval Center** in the frontend dashboard shows the approval card with:
- **Source:** Jira (SEC-123)
- **Summary:** Create S3 bucket acme-data-lake with SSE-S3 encryption
- **Risk Score:** Low
- **RCA Summary:** Standard S3 provisioning request
- **Resolution Plan:** Creates bucket, enables encryption, blocks public access, enables versioning
- **Actions:** `[Approve]` `[Reject]` `[Escalate]`

**Action:** Click **Approve**.

**Behind the scenes:**
```bash
# This is what the frontend calls
curl -X POST http://localhost:6001/requests/<job_id>/approve \
  -H 'Content-Type: application/json' \
  -d '{"approved": true, "reason": "Approved for demo"}'
```

**Narrator script:** "The plan pauses at the human-in-the-loop gate. Every destructive or infrastructure-modifying action requires explicit human approval. We review the plan, the generated code, and the risk assessment, then approve."

---

### Step 9: AWS Execution (Apply)

**Action:** The executor applies the approved plan against the AWS account.

**Expected output:**
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

---

### Step 10: Verification

**Action:** Verify the S3 bucket was created correctly.

**Via AWS CLI:**
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

**Expected output:**
```bash
# head-bucket → HTTP 200
# encryption → {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}
# public-access-block → all four flags = true
# versioning → {"Status": "Enabled"}
```

---

### Step 11: Progress Tracking

**Action:** Check the progress of the Digital Worker pipeline.

```bash
curl http://localhost:6001/jobs/status/<job_id>
```

**Expected output:**
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

---

### Step 12: History & Audit Trail

**Action:** View the complete history of this Digital Worker run.

```bash
curl http://localhost:6001/requests?status=completed
```

**Expected output:** A list of completed requests with full audit trail including:
- Who submitted the request (Jira user)
- What was executed (S3 bucket creation)
- When it happened (timestamp)
- Who approved it (human approval)
- What code was generated (Terraform)
- What AWS resources were created (ARN)

---

### Step 13: Dashboard Overview

**Action:** Return to the Chandra dashboard at **http://localhost:3000**.

**Expected UI:** The dashboard now shows:
- ✅ **Completed** status for the S3 bucket creation task
- **Audit trail** entry in the History panel
- **Updated metrics** reflecting the new S3 resource
- **Approval center** showing 0 pending approvals (all cleared)

---

## Sample Data

### AWS Task: Create S3 Bucket

```json
{
  "title": "Create S3 bucket acme-data-lake with SSE-S3 encryption",
  "description": "New S3 bucket for data lake team. Must have SSE-S3 encryption, public access blocked, versioning enabled.",
  "resource_id": "acme-data-lake",
  "platform": "AWS",
  "service": "S3",
  "priority": "P2"
}
```

### Permission Set: S3 Bucket Permissions

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

### Jira Ticket: SEC-123

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

## Verification Commands

Use these commands to verify the system is working at each stage:

```bash
# 1. Backend health
curl http://localhost:6001/health

# 2. Backend readiness
curl http://localhost:6001/health/ready

# 3. vLLM model list
curl http://localhost:8000/v1/models

# 4. Submit a test request
curl -X POST http://localhost:6001/requests \
  -H 'Content-Type: application/json' \
  -d '{"source":"rest_api","payload":{"title":"Test","priority":"P3","resource_id":"test-123"},"dry_run":true}'

# 5. List all requests
curl http://localhost:6001/requests

# 6. Check a specific request status
curl http://localhost:6001/jobs/status/<job_id>

# 7. Approve a request
curl -X POST http://localhost:6001/requests/<job_id>/approve \
  -H 'Content-Type: application/json' \
  -d '{"approved":true}'

# 8. Verify AWS resource
aws s3api head-bucket --bucket acme-data-lake
```

---

## Success Criteria

| Criterion | Target | Verification |
|-----------|--------|-------------|
| Onboarding completes | ✅ | Frontend redirects to dashboard |
| Backend API responds | ✅ | `GET /health` returns 200 |
| vLLM serves models | ✅ | `GET /v1/models` returns model list |
| LLM generates valid code | ✅ | Terraform/CloudFormation passes syntax check |
| Human approval works | ✅ | Approval gate pauses execution |
| AWS resource created | ✅ | S3 bucket exists with correct configuration |
| Audit trail complete | ✅ | Request history shows full lifecycle |
| End-to-end pipeline | ✅ | Task submitted → code generated → approved → executed → verified |
| Dashboard renders | ✅ | UI shows live ops stream, incidents, metrics |
| All 5 KRAs observed | ✅ | Cost, Security, Compliance, Performance, Reliability all report |