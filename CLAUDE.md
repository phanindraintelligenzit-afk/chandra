# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

# Chandra — Project context for Claude Code

This file is **auto-loaded into every Claude Code session running in this repo**. Read it once at session start. Treat the rules in it as immutable — they're the architectural invariants the team agreed on, not preferences.

---

## What Chandra is

Chandra is a **multi-layered enterprise AI cloud operations platform** with three distinct parts:

### 1. **Backend orchestration** (Python + LangGraph + Bedrock)
A LangGraph-orchestrated autonomous agent that observes one AWS account and emits a daily Cloud Health Briefing across **five KRAs**: cost, security, compliance, performance, reliability.

```
START → onboard_account → fanout_observers
                             ├─► observe_cost
                             ├─► observe_security
                             ├─► observe_compliance        → analyze (LLM rank + dedup)
                             ├─► observe_performance         → compose_briefing (LLM narrative)
                             └─► observe_reliability            → persist → END
```

Deterministic boto3 detectors gather findings. Claude Sonnet 4.5 (via Amazon Bedrock) ranks and narrates. Results persist to Postgres. **The LLM never invents findings.** It only runs in `analyze` (ranking + rationale) and `compose_briefing` (narrative). This separation is a hard architectural invariant.

### 2. **Frontend operations console** (Next.js 16 + React 18 + TypeScript)
A premium, futuristic operations console (HTML/CSS/TypeScript) for observing, triaging, and approving remediations under continuous human supervision. Ships as a static export to GitHub Pages today; will connect to a FastAPI backend for real-time WebSocket streams.

**Onboarding flow:** Five-step provisioning ceremony (name → avatar → role → maturity → KRAs → permissions → deploy).

**Dashboard surface:** Live ops stream, active incidents table, approval center, cost monitoring, audit trail, performance scoring, infrastructure health.

**Governance model:** Every destructive remediation is gated by human approval (`Approve / Reject / Escalate`).

### 3. **Streamlit dashboard** (temporary)
Today's read-only analytics surface. Renders the latest briefing, findings explorer, eval trend. Being replaced by the Next.js console (FE-01).

---

## Hard architectural rules (do not violate without explicit signoff from Phani)

- **LangGraph is the only orchestration framework.** No LangChain `AgentExecutor`. No `create_react_agent`. Use `StateGraph` + `Send(...)` for fan-out.
- **Amazon Bedrock is the only LLM provider** — specifically `langchain_aws.ChatBedrockConverse` with Sonnet 4.5. Do not import `openai`, `anthropic` direct SDK, or any other provider.
- **Read-only by default.** Detectors never call mutating AWS APIs. Future write actions go through `HumanApprovalNode` (LG-01) and the `pending_writes` state field.
- **`chandra.briefing.composer` is the only module that may call Bedrock.** Detector modules MUST NOT import `langchain_aws`.
- **Postgres writes only in the `persist` node and Alembic migrations.** Nowhere else.
- **Every boto3 list/describe call uses a paginator.** No silent truncation.
- **AWS clients are created via `chandra.aws.client_factory.get_default_factory()`.** Never `boto3.client(...)` directly.
- **No `# TODO: implement` in committed code.** If something is deferred, `raise NotImplementedError("<msg>; tracked in <TICKET-ID>")`.
- **No `print()`.** Use `chandra.logging.get_logger(__name__)`.
- **No `except Exception` without re-raising or structured logging.** Narrow exception classes only.
- **Frontend is Next.js-only for new work.** The Streamlit dashboard is being sunset (FE-01). Don't add features to Streamlit; migrate them to Next.js.

---

## Development commands

### Backend (Python)

```bash
make install     # uv sync --all-extras — install all runtime + dev deps
make db-up       # docker compose up -d postgres — start Postgres for local dev
make migrate     # alembic upgrade head — apply schema migrations
make fmt         # ruff format — fix code style
make lint        # ruff check — lint rules
make type        # mypy src — strict type checking
make test        # pytest — run unit tests
make test -v     # pytest -v — verbose, useful for debugging a single test
make check       # lint + type + test — the commit gate (must be green before PR)
make run         # chandra run --account $SYNTHETIC_ACCOUNT_ID — one full Chandra run
make eval        # chandra eval --account $SYNTHETIC_ACCOUNT_ID — score vs seed_manifest.yaml
make dashboard   # streamlit run src/chandra/dashboard/app.py — read-only briefing viewer
make tf-apply    # terraform apply on synthetic env — seeds 10 known misconfigs in a burner AWS account
make tf-destroy  # terraform destroy on synthetic env — clean up resources
make smoke       # bash scripts/smoke.sh — end-to-end: tf apply → run → eval (Linux/macOS)
make smoke-windows  # pwsh scripts/smoke.ps1 — end-to-end (Windows PowerShell)
make clean       # remove .pytest_cache, .ruff_cache, etc.
make eval-offline   # chandra eval --fixture evals/fixtures/baseline_v1.jsonl — no AWS/Terraform required
```

### Frontend (Next.js)

From `frontend/` directory:

```bash
npm install      # install Node dependencies
npm run dev      # start dev server on http://localhost:3000
npm run build    # compile + static export to ./out/
npm run start    # serve production build
npm run lint     # ESLint check
```

### Day 1 setup

```bash
# Backend environment
cp .env.example .env
# Edit .env to set AWS_PROFILE and SYNTHETIC_ACCOUNT_ID (your burner account id)

# Initialize backend
make install
make db-up
make migrate

# Initialize frontend (in frontend/ directory)
cd frontend
npm install
cd ..

# Verify everything works
make check
```

---

## Project structure

### Backend (Python)

```
src/chandra/
├── aws/                    # AWS client factory, region discovery, IAM/audit helpers
│   ├── client_factory.py  # AwsClientFactory — must use this, never boto3.client(...)
│   ├── regions.py         # Region enumeration + helpers
│   ├── organizations.py   # OU/account traversal
│   └── ...
├── tools/                  # KRA detectors — deterministic boto3 (Cost, Security, Compliance, Performance, Reliability)
│   ├── base.py            # BaseDetector interface
│   ├── cost.py            # Cost.tool_cost_trends_by_region(), etc.
│   ├── security.py        # detect_security_findings(), etc.
│   ├── compliance.py      # Compliance state checks
│   ├── performance.py     # Latency, throughput, saturation
│   └── reliability.py     # Uptime, failover, redundancy
├── graphs/                # LangGraph orchestration
│   ├── state.py           # ChandraState (TypedDict) with reducers
│   ├── chandra_graph.py   # StateGraph construction + observer fan-out
│   └── nodes/             # Individual node implementations
│       ├── action_executor.py  # (planned) execute approved remediations
│       └── ...
├── briefing/              # LLM interaction + narrative composition
│   ├── composer.py        # Only place that calls Bedrock; ranks findings + drafts narrative
│   ├── schemas.py         # Finding, AnalyzedFinding, Briefing pydantic models
│   └── org_summary.py     # Organization-wide roll-ups
├── db/                    # SQLAlchemy ORM + Alembic migrations
│   ├── models.py          # Run, Briefing, Finding, EvalRun tables
│   ├── session.py         # session_scope context manager
│   └── migrations/        # Alembic versions
├── dashboard/             # Streamlit read-only console (temporary, FE-01 sunset)
│   └── app.py             # Latest briefing, findings table, eval trend
├── observability/         # OpenTelemetry + pricing telemetry
│   ├── callbacks.py       # LangGraph → OTEL instrumentation
│   └── pricing.py         # LLM token tracking
├── config.py              # Pydantic Settings (env-driven)
├── logging.py             # structlog + OTEL setup
└── cli.py                 # Typer CLI: chandra {run, eval, render, ...}
```

### Frontend (Next.js)

```
frontend/
├── app/
│   ├── layout.tsx              # Root HTML shell + OnboardingProvider
│   ├── providers.tsx           # Client-side context wrappers
│   ├── globals.css             # Theme tokens + animations (maturity rings, telemetry glow)
│   ├── page.tsx                # Root → /onboarding redirect
│   ├── onboarding/page.tsx    # OnboardingWizard host
│   └── dashboard/page.tsx      # ChandraExperience host + completion guard
├── components/
│   ├── OnboardingWizard.tsx    # Five-step provisioning flow
│   ├── ChandraExperience.tsx   # Full operations dashboard composition
│   └── WorkerActionExecutionCenter.tsx  # Approval center + actions
├── store/
│   ├── OnboardingContext.tsx   # Identity + KRA + permissions state (sessionStorage)
│   ├── agentProfile.ts         # Avatar catalog, employee ID generation
│   └── kraCatalog.ts           # KRA definitions + operational metrics
├── public/
│   ├── avatars/                # Six holographic agent portraits (PNG)
│   └── icons/                  # Role SVGs (AWS, Azure, DevOps, K8s, Security, Java)
├── package.json                # npm dependencies
├── next.config.mjs             # basePath + assetPrefix for GitHub Pages static export
└── tsconfig.json
```

### Shared infrastructure

```
├── iac/synthetic_env/          # Terraform module — seeds 10 misconfigs in a burner account
├── evals/
│   ├── seed_manifest.yaml      # Ground truth: what misconfigs should Chandra find?
│   ├── harness.py              # terraform apply → run → score → report (eval harness)
│   ├── fixtures/               # JSONL replay files for offline eval
│   └── reports/                # JSON + Markdown reports per run
├── tests/                      # pytest suite (unit tests with moto AWS mocks)
├── scripts/smoke.{sh,ps1}      # One-shot demo runner
├── Makefile                    # Quality gates + CLI targets
├── docker-compose.yml          # Postgres + LocalStack for local dev
├── Dockerfile                  # Multi-stage slim runtime image
├── pyproject.toml              # Python dependencies, tool config (ruff, mypy, pytest)
└── .github/workflows/
    ├── check.yml               # CI gate: lint + type + test
    └── nextjs.yml              # GitHub Pages deploy on every main push
```

---

## Quality gates

`make check` runs **ruff + mypy --strict + pytest**. Don't commit on red. The repo enforces:

- No `# TODO: implement` in committed code. Use `NotImplementedError` instead.
- Tools never call Bedrock — only `briefing.composer` does.
- Writes to Postgres only inside the `persist` node and migrations.
- Boto3 paginators on every list/describe call.
- No raw `boto3.client(...)` — always use `AwsClientFactory`.

---

## How the team operates

- **Single source of truth for work**: the Notion Kanban → https://www.notion.so/b67c36091c9f426ab6d49c4b6e54b789
- **Every ticket has**: a "Why / Where / Acceptance" page body + a step-by-step engineering comment with file paths, code snippets, ETA, and dependencies.
- **One ticket = one PR**. Keep PRs small. No week-long branches.
- **Branch naming**: `<ticket-id-lowercase>/<short-slug>` — e.g. `lg-03/traced-node-decorator`.
- **Commit / PR title format**: `<TICKET-ID>: <imperative summary>` — e.g. `LG-03: add @traced_node decorator (OTEL + structlog + metrics)`.
- **PR body must include**: link to the Notion ticket, a one-paragraph "what / why", and a checklist of acceptance items from the ticket.
- **`make check` must pass locally before opening a PR.** CI runs the same gate (`.github/workflows/check.yml`).
- **CODEOWNERS auto-routes reviewers**. Don't self-merge.

---

## CODEOWNERS — who reviews what

| Path | Team / owner |
|---|---|
| `src/chandra/graphs/`, `briefing/`, `prompts/`, `kras.py` | LangGraph team |
| `src/chandra/aws/`, `tools/`, `iac/`, `Dockerfile`, `docker-compose.yml` | AWS team |
| `src/chandra/dashboard/`, `api/` | Frontend team |
| `src/chandra/db/`, `observability.py` | AWS + LangGraph jointly |
| `evals/`, `tests/` | LangGraph team |
| `frontend/` | Frontend team (Aishani) |
| `docs/` | Kshiraja |
| `.github/`, `CODEOWNERS`, `pyproject.toml`, `Makefile` | Chandra leads (Phani) |

If your change touches another team's path, open a draft PR and tag them. Don't merge silently.

---

## Team

| Person | Role | Workstream |
|---|---|---|
| **Maheshwar** | AWS Engineer | AWS infra, IaC, CI |
| **Siva** | LangGraph Engineer | Graph core, Cost/Performance KRA workers, observability primitives |
| **Nagendra** | LangGraph Engineer | Security/Compliance KRA workers, eval harness, fixture-replay |
| **Aishani** | Frontend Engineer | Next.js ops console, onboarding wizard, approval center |
| **Kshiraja** | Intern | Docs, demo runbook, fixtures, well-scoped starter tickets |
| **Phani** | PM / LangGraph reviewer | Project lead, escalation, decision authority on Coordination |
| **PVR** | CEO | Product norms, escalation |

---

## Norms (set by PVR — non-negotiable)

- **Full-code only.** No low-code, no drag-and-drop. Streamlit is temporary; Next.js is the real thing.
- **Ship every day.** Small PRs. Fast review.
- **State lives in Notion + repo. Not in DMs.**
- **Push back if a plan has a flaw.** Don't hedge to be polite.
- **Building-in-public is OFF.** Internal-only until PVR explicitly green-lights.

---

## When you (Claude) are stuck or unsure

- **Ambiguous spec**: comment on the Notion ticket; Phani clarifies. Don't guess.
- **Architecture question**: the rules above are the source of truth. If you think a rule is wrong, raise it explicitly — don't quietly route around it.
- **Test failure**: run the affected test with `-v` and read the trace. If it's a flake, document it; don't paper over.
- **Bedrock unavailable / throttling**: the composer's deterministic fallback exists for exactly this (see `composer.py:103`). Confirm it's hit (look for `llm.bedrock_unavailable_fallback_to_deterministic` log entries) and continue.
- **Frontend question**: the Next.js codebase is in `frontend/`, separate from the backend. State lives in `OnboardingContext` (sessionStorage). WebSocket integration is planned; for now, timers simulate the stream.

---

## What NOT to do

- Don't merge your own PRs.
- Don't push directly to `main`.
- Don't modify `CODEOWNERS`, `.github/workflows/*`, `pyproject.toml`, or `Makefile` without flagging Phani first.
- Don't touch GitHub repo settings, branch protection, or security configurations.
- Don't add new third-party dependencies without justifying in the PR description.
- Don't refactor outside the scope of your current ticket — open a separate ticket for it.
- Don't import any LLM provider other than `langchain_aws` (no `openai`, `anthropic`, `cohere`, etc.).
- Don't instantiate `boto3.client(...)` directly — always go through `AwsClientFactory`.
- **Don't add features to the Streamlit dashboard.** Migrate existing Streamlit surfaces to Next.js instead (FE-01).

---

## Reference links

- **Kanban (live)**: https://www.notion.so/b67c36091c9f426ab6d49c4b6e54b789
- **Onboarding Resource Pack**: https://www.notion.so/3604baec816581b1910dff95427c76be
- **Latest project status report (for PVR)**: https://www.notion.so/3674baec8165810fbf1af038d9607f93
- **GitHub repo**: https://github.com/phanindraintelligenzit-afk/chandra
- **Frontend repo** (Next.js console): https://github.com/aishanic12/chandra_extended
- **Frontend live demo**: https://aishanic12.github.io/chandra_extended/
- **Engineer master prompts**: `docs/agent-prompts/` (paste the relevant one at session start)
