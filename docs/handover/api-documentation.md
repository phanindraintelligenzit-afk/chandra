# API Documentation — Chandra Enterprise Digital Cloud Engineer

**Date:** 2026-07-30  
**Branch:** `feature/local-llm`  
**Base URL:** `http://localhost:6001`  
**OpenAPI:** Auto-generated at `/docs` (Swagger) and `/redoc` (ReDoc)

---

## 1. Authentication

Most endpoints are **unauthenticated** (internal network). Webhook endpoints optionally authenticate via `CHANDRA_WEBHOOK_TOKEN` header. SNS integrations use IAM-based auth.

---

## 2. Health Endpoints

### GET /health
Liveness probe. Returns immediately with no dependency checks.

**Response 200:**
```json
{"status": "ok"}
```

### GET /health/ready
Readiness probe. Checks each component independently.

**Response 200 (all healthy):**
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

**Response 503 (degraded):**
```json
{
  "status": "degraded",
  "components": {
    "copilot_agent": "unavailable",
    "digital_worker": "ok",
    "postgres": "ok"
  }
}
```

---

## 3. Digital Worker Endpoints

### POST /requests
Submit a new cloud operations request.

**Request:**
```json
{
  "intent": "Enable S3 bucket encryption for acme-prod-logs",
  "source": "api",
  "title": "Enable bucket encryption",
  "context": "Bucket acme-prod-logs has no server-side encryption enabled",
  "source_channel_id": "channel-123",
  "source_user_id": "user-456"
}
```

**Response 200:**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "accepted",
  "message": "Request submitted for processing",
  "poll_url": "/requests/550e8400-e29b-41d4-a716-446655440000/status"
}
```

### GET /requests/{job_id}/status
Poll request status.

**Response 200:**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "running",
  "progress": 65,
  "message": "Analyzing root cause",
  "result": null,
  "error": null,
  "started_at": 1698765432.123,
  "completed_at": null
}
```

### POST /requests/{job_id}/approve
Approve or reject a pending request.

**Request:**
```json
{
  "decision": "approve",
  "reviewer": "admin@example.com",
  "reason": "Standard security remediation"
}
```

**Response 200:**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "approved",
  "message": "Approved by admin@example.com"
}
```

---

## 4. Webhook Endpoints

### POST /webhooks/{source}
Receive requests from external channels.

**Supported sources:** `slack`, `teams`, `email`, `jira`, `webhook`, `api`, `github`, `gitlab`, `pagerduty`, `custom`

**Headers:**
| Header | Required | Description |
|--------|----------|-------------|
| `Content-Type` | Yes | `application/json` |
| `X-Webhook-Token` | If configured | `CHANDRA_WEBHOOK_TOKEN` env var |
| `X-Source-Channel-Id` | No | Source channel identifier |
| `X-Source-User-Id` | No | Source user identifier |

**Request (generic):**
```json
{
  "text": "Enable encryption on S3 bucket acme-prod-logs",
  "channel": "C0123456789",
  "user": "U0123456789",
  "source_metadata": {}
}
```

**Response 200:**
```json
{
  "status": "accepted",
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "message": "Request received and processing"
}
```

---

## 5. Orchestration Endpoints

### POST /orchestrate_simple
Run the simple orchestration pipeline.

**Request:**
```json
{
  "action": "Create S3 bucket",
  "description": "Create a standard S3 bucket in us-east-1",
  "service": "AWS",
  "kra_code": "compliance",
  "priority": "high",
  "reference_folder": "s3",
  "max_iterations": 4,
  "command_timeout": 600
}
```

**Response 200:**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "accepted",
  "message": "Job submitted for processing"
}
```

### GET /jobs/status/{job_id}
Poll orchestration job status.

**Response 200:**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "running",
  "progress": 50,
  "message": "Generating Terraform plan",
  "result": null,
  "error": null,
  "started_at": 1698765432.123,
  "completed_at": null,
  "sandbox_path": "/tmp/chandra/sandbox_550e8400"
}
```

---

## 6. Task Management Endpoints

### POST /execute_task
Execute a direct task.

**Request:**
```json
{
  "title": "Create S3 bucket",
  "description": "Create encrypted bucket acme-data",
  "task_type": "aws_api",
  "parameters": {
    "bucket_name": "acme-data",
    "region": "us-east-1",
    "encryption": "AES256"
  }
}
```

**Response 200:**
```json
{
  "task_id": "660e8400-e29b-41d4-a716-446655440001",
  "status": "accepted",
  "message": "Task queued for execution"
}
```

### POST /tasks/create
Create a reusable task template.

**Request:**
```json
{
  "title": "Enable S3 Encryption",
  "description": "Enable AES256 encryption on an S3 bucket",
  "task_type": "aws_api",
  "parameters": {
    "bucket_name": "",
    "region": "us-east-1"
  },
  "created_by": "admin"
}
```

**Response 200:**
```json
{
  "task_id": "770e8400-e29b-41d4-a716-446655440002",
  "status": "created",
  "message": "Task created successfully"
}
```

### GET /tasks
List all tasks.

**Query params:** `?created_by=admin&task_type=aws_api`

**Response 200:**
```json
{
  "tasks": [
    {
      "id": "770e8400-...",
      "title": "Enable S3 Encryption",
      "task_type": "aws_api",
      "created_by": "admin",
      "created_at": 1698765432
    }
  ],
  "total": 1
}
```

---

## 7. Permission Management

### POST /permissions/create
Create a permission set.

**Request:**
```json
{
  "name": "s3_full_access",
  "description": "Full S3 bucket management",
  "permissions": ["s3:CreateBucket", "s3:PutEncryptionConfig", "s3:PutBucketPolicy"],
  "created_by": "admin"
}
```

**Response 200:**
```json
{
  "permission_id": "880e8400-...",
  "status": "created",
  "message": "Permission set created"
}
```

### GET /permissions
List all permission sets.

**Response 200:**
```json
{
  "permissions": [
    {
      "id": "880e8400-...",
      "name": "s3_full_access",
      "permissions": ["s3:CreateBucket", "s3:PutEncryptionConfig"]
    }
  ],
  "total": 1
}
```

---

## 8. User Management

### POST /users/create
Create a new user.

**Request:**
```json
{
  "username": "jdoe",
  "email": "jdoe@example.com",
  "role": "operator",
  "permissions": ["s3_full_access"],
  "created_by": "admin"
}
```

**Response 200:**
```json
{
  "user_id": "990e8400-...",
  "status": "created",
  "message": "User created successfully"
}
```

### GET /users
List all users.

**Response 200:**
```json
{
  "users": [
    {
      "id": "990e8400-...",
      "username": "jdoe",
      "email": "jdoe@example.com",
      "role": "operator"
    }
  ],
  "total": 1
}
```

---

## 9. Log Endpoints

### GET /logs
Retrieve in-memory log buffer (last 2000 entries).

**Query params:** `?level=ERROR&job_id=<job_id>&limit=50`

**Response 200:**
```json
{
  "logs": [
    {
      "timestamp": 1698765432.123,
      "level": "INFO",
      "logger": "fastapi_app",
      "message": "Request received",
      "job_id": "550e8400-..."
    }
  ],
  "total": 1
}
```

---

## 10. Error Response Format

All endpoints return errors in this format:
```json
{
  "detail": "Human-readable error message",
  "status_code": 400
}
```

| Status Code | Meaning |
|-------------|---------|
| 200 | Success |
| 400 | Bad request (validation error) |
| 404 | Resource not found |
| 422 | Unprocessable entity (Pydantic validation) |
| 503 | Service unavailable (degraded component) |