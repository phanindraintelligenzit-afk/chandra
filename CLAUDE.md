# Chandra — Project context for Claude Code

This file is **auto-loaded into every Claude Code session running in this repo**. Read it once at session start. Treat the rules in it as immutable — they're the architectural invariants the team agreed on, not preferences.

---

## What Chandra is

Chandra is a LangGraph-orchestrated autonomous agent that observes one AWS account and emits a daily Cloud Health Briefing across **five KRAs**: cost, security, compliance, performance, reliability.

```
START → onboard_account → fanout_observers
                             ├─► observe_cost
                             ├─► observe_security
                             ├─► observe_compliance        → analyze (LLM rank + dedup)
                             ├─► observe_performance         → compose_briefing (LLM narrative)
                             └─► observe_reliability            → persist → END
```

Deterministic boto3 detectors gather findings. Claude Sonnet 4.5 (via Amazon Bedrock) ranks and narrates. Results persist to Postgres. Streamlit (today; FastAPI + Next.js per FE-01) renders the briefing.

**The LLM never invents findings.** It only runs in `analyze` (ranking + rationale) and `compose_briefing` (narrative). This separation is a hard architectural invariant.

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

## Quality gates

```bash
make install     # uv sync --all-extras
make db-up       # docker compose up -d postgres
make migrate     # alembic upgrade head
make check       # ruff + mypy --strict + pytest  ← must be green before PR
make run         # one full Chandra run (requires SYNTHETIC_ACCOUNT_ID + AWS creds)
make eval        # eval harness against the synthetic env
make dashboard   # streamlit on :8501
```

The first three commands need to pass on Day 1 of any new engineer's setup.

---

## CODEOWNERS — who reviews what

| Path | Team / owner |
|---|---|
| `src/chandra/graphs/`, `briefing/`, `prompts/`, `kras.py` | LangGraph team |
| `src/chandra/aws/`, `tools/`, `iac/`, `Dockerfile`, `docker-compose.yml` | AWS team |
| `src/chandra/dashboard/`, `api/` | Frontend team |
| `src/chandra/db/`, `observability.py` | AWS + LangGraph jointly |
| `evals/`, `tests/` | LangGraph team |
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
| **Aishani** | Frontend Engineer | Streamlit dashboard today, FastAPI + Next.js next |
| **Kshiraja** | Intern | Docs, demo runbook, fixtures, well-scoped starter tickets |
| **Phani** | PM / LangGraph reviewer | Project lead, escalation, decision authority on Coordination |
| **PVR** | CEO | Product norms, escalation |

---

## Norms (set by PVR — non-negotiable)

- **Full-code only.** No low-code, no drag-and-drop. Streamlit is a temporary placeholder.
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

---

## Reference links

- **Kanban (live)**: https://www.notion.so/b67c36091c9f426ab6d49c4b6e54b789
- **Onboarding Resource Pack**: https://www.notion.so/3604baec816581b1910dff95427c76be
- **Latest project status report (for PVR)**: https://www.notion.so/3674baec8165810fbf1af038d9607f93
- **GitHub repo**: https://github.com/phanindraintelligenzit-afk/chandra
- **Engineer master prompts**: `docs/agent-prompts/` (paste the relevant one at session start)
