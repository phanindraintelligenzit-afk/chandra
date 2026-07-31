# Database Documentation — Chandra Enterprise Digital Cloud Engineer

**Date:** 2026-07-30  
**Branch:** `feature/local-llm`

---

## 1. Database Architecture

| Component | Technology | Details |
|-----------|-----------|---------|
| Database Engine | PostgreSQL 16 | Alpine container in Docker |
| ORM | SQLAlchemy 2.x | Declarative base, async-compatible |
| Migrations | Alembic 1.x | Auto-generated, idempotent, 3 migrations |
| Connection | psycopg (2.x/3.x) | Synchronous driver in SQLAlchemy URL |
| Port (host) | 5434 | Mapped to container port 5432 |
| Default credentials | chandra / chandra | From .env.example |

### Docker Compose Configuration
```yaml
postgres:
  image: postgres:16-alpine
  environment:
    POSTGRES_USER: chandra
    POSTGRES_PASSWORD: chandra
    POSTGRES_DB: chandra
  ports:
    - "5434:5432"
  volumes:
    - chandra-pgdata:/var/lib/postgresql/data
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U chandra"]
    interval: 5s
    timeout: 5s
    retries: 10
  restart: unless-stopped
```

### Connection URL Format
```
postgresql+psycopg://chandra:chandra@localhost:5432/chandra    # Local dev
postgresql+psycopg://chandra:chandra@postgres:5432/chandra     # Docker Compose
```

---

## 2. Database Schema

### Entity Relationship Diagram

```mermaid
erDiagram
    Run ||--o{ Finding : contains
    Run ||--o{ Briefing : contains
    Run ||--o{ CloudRequestRecord : triggers

    Run {
        uuid run_id PK "UUID, primary key"
        text account_id "AWS account ID"
        jsonb regions "Active regions array"
        text status "run_status enum"
        jsonb selected_kras "KRA selection array"
        jsonb scorecard "KRA scorecard"
        jsonb pending_writes "Pending remediation writes"
        numeric bedrock_cost_usd "Bedrock API cost"
        timestamp started_at "Run start time"
        timestamp completed_at "Run completion time"
        timestamp created_at "Record creation"
    }

    Finding {
        uuid id PK "UUID, primary key"
        uuid run_id FK "References Run.run_id"
        text kra "KRA category"
        text severity "severity enum (critical|high|medium|low|info)"
        text detector_id "Detector identifier"
        text resource_arn "AWS resource ARN"
        text resource_type "AWS resource type"
        text title "Finding title"
        text evidence "Detection evidence"
        text recommendation "Remediation recommendation"
        integer rank "LLM-assigned rank"
        text rationale "LLM ranking rationale"
        timestamp created_at "Record creation"
    }

    Briefing {
        uuid id PK "UUID, primary key"
        uuid run_id FK "References Run.run_id"
        text briefing_md "Markdown-formatted briefing"
        jsonb briefing_json "Structured JSON briefing"
        text summary "Executive summary (3 bullets)"
        timestamp created_at "Record creation"
    }

    CloudRequestRecord {
        uuid id PK "UUID, primary key"
        text source "origin channel (slack|teams|email|jira|webhook|api)"
        text source_channel_id "Source channel identifier"
        text source_user_id "Source user identifier"
        text title "Request title"
        text intent "Request intent/description"
        text context "Additional context"
        text status "request_status enum"
        text classification "Request classification"
        jsonb request_payload "Original request payload"
        jsonb workflow_result "Workflow execution result"
        integer attempt "Execution attempt number"
        timestamp received_at "Request receipt time"
        timestamp completed_at "Workflow completion time"
        timestamp created_at "Record creation"
        timestamp updated_at "Record update"
    }
```

### Table: `runs`
| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `run_id` | uuid | PK, default uuid4 | Unique run identifier |
| `account_id` | text | NOT NULL | AWS account ID being observed |
| `regions` | JSONB | DEFAULT '[]' | Active regions array |
| `status` | run_status | NOT NULL, DEFAULT 'running' | Enum: running, completed, failed |
| `selected_kras` | JSONB | | KRA selection array |
| `scorecard` | JSONB | | KRA scorecard: {kra: {score, passed}} |
| `pending_writes` | JSONB | | Pending remediation writes |
| `bedrock_cost_usd` | numeric | | Bedrock API cost in USD |
| `started_at` | timestamp | | Run start time |
| `completed_at` | timestamp | | Run completion time |
| `created_at` | timestamp | DEFAULT now() | Record creation |

### Table: `findings`
| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `id` | uuid | PK, default uuid4 | Finding ID |
| `run_id` | uuid | FK → runs(run_id), NOT NULL | Parent run |
| `kra` | text | NOT NULL | KRA category |
| `severity` | severity | NOT NULL | Enum: critical, high, medium, low, info |
| `detector_id` | text | NOT NULL | Detector function name |
| `resource_arn` | text | | AWS resource ARN |
| `resource_type` | text | | AWS resource type |
| `title` | text | NOT NULL | Finding title |
| `evidence` | text | | Detection evidence |
| `recommendation` | text | | Remediation recommendation |
| `rank` | integer | | LLM-assigned rank |
| `rationale` | text | | LLM ranking rationale |
| `created_at` | timestamp | DEFAULT now() | Record creation |

### Table: `briefings`
| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `id` | uuid | PK, default uuid4 | Briefing ID |
| `run_id` | uuid | FK → runs(run_id), NOT NULL, UNIQUE | Parent run (one briefing per run) |
| `briefing_md` | text | | Markdown-formatted briefing |
| `briefing_json` | JSONB | | Structured JSON briefing |
| `summary` | text | | Executive summary (3 bullets) |
| `created_at` | timestamp | DEFAULT now() | Record creation |

### Table: `cloud_request_records`
| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `id` | uuid | PK, default uuid4 | Request ID |
| `source` | text | NOT NULL | Origin channel |
| `source_channel_id` | text | | Channel identifier |
| `source_user_id` | text | | User identifier |
| `title` | text | NOT NULL | Request title |
| `intent` | text | NOT NULL | Request description |
| `context` | text | | Additional context |
| `status` | request_status | NOT NULL, DEFAULT 'received' | Enum: received, classifying, planning, executing, approved, rejected, completed, failed |
| `classification` | text | | Request classification |
| `request_payload` | JSONB | | Original request |
| `workflow_result` | JSONB | | Execution result |
| `attempt` | integer | DEFAULT 1 | Execution attempt |
| `received_at` | timestamp | | Receipt time |
| `completed_at` | timestamp | | Completion time |
| `created_at` | timestamp | DEFAULT now() | Record creation |
| `updated_at` | timestamp | DEFAULT now() | Record update |

---

## 3. Migrations

| File | Version | Description |
|------|---------|-------------|
| `0001_initial_schema.py` | 0001 | Create runs, findings, briefings tables |
| `20260603_add_bedrock_cost.py` | c6f417c05ab8 | Add bedrock_cost_usd to runs |
| `20260707_add_digital_worker_tables.py` | a1d9e2f4b7c1 | Add cloud_request_records table |

### Commands
```bash
# Apply all migrations
alembic upgrade head

# Rollback one step
alembic downgrade -1

# Create new migration
alembic revision --autogenerate -m "description"

# View history
alembic history
```

---

## 4. SQLAlchemy Models

### Source: `src/chandra/db/models.py`
```python
class Run(Base):
    __tablename__ = "runs"
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    regions: Mapped[dict] = mapped_column(JSONB, default=list)
    status: Mapped[RunStatus] = mapped_column(
        SqlAlchemyEnum(RunStatus), nullable=False, default=RunStatus.RUNNING
    )
    selected_kras: Mapped[dict | None] = mapped_column(JSONB, default=None)
    scorecard: Mapped[dict | None] = mapped_column(JSONB, default=None)
    pending_writes: Mapped[dict | None] = mapped_column(JSONB, default=None)
    bedrock_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Relationships
    findings: Mapped[list["Finding"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    briefing: Mapped["Briefing | None"] = relationship(back_populates="run", uselist=False, cascade="all, delete-orphan")
```

### Session Management
```python
# src/chandra/db/session.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

engine = create_engine(
    settings.postgres_url,
    pool_size=5,        # Default; should be increased to 10
    max_overflow=10,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine)

@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

---

## 5. Database Operations

### Backup
```bash
# Docker Compose (local)
docker exec chandra-postgres-1 pg_dump -U chandra chandra > backup_$(date +%Y%m%d).sql

# Restore
cat backup.sql | docker exec -i chandra-postgres-1 psql -U chandra chandra
```

### Monitoring Queries
```sql
-- Latest runs
SELECT run_id, account_id, status, started_at, completed_at 
FROM runs ORDER BY created_at DESC LIMIT 10;

-- Findings by KRA
SELECT kra, severity, COUNT(*) as count
FROM findings GROUP BY kra, severity ORDER BY kra, severity;

-- Recent requests
SELECT id, source, title, status, received_at 
FROM cloud_request_records ORDER BY received_at DESC LIMIT 20;
```

---

## 6. Known Issues

1. **Pool size mismatch**: `ThreadPoolExecutor(max_workers=8)` but SQLAlchemy `pool_size=5` — can exhaust connections under concurrent load. Fix: set `pool_size=10`.
2. **No async connection**: All DB operations are synchronous, blocking the event loop thread when used in async contexts.
3. **No production RDS**: Currently uses Docker Postgres container. RDS should be provisioned before production deployment.