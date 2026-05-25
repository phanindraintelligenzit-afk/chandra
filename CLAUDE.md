# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

`make` targets work on Linux/macOS. On Windows, use the `uv run` equivalents below.

```bash
# Setup
cp .env.example .env          # fill in AWS_PROFILE and SYNTHETIC_ACCOUNT_ID
make db-up                    # docker compose up -d postgres localstack
make install                  # uv sync --all-extras
make migrate                  # uv run alembic upgrade head

# Quality gate (runs ruff + mypy --strict + pytest)
make check
# Windows: uv run ruff check src && uv run ruff format --check src && uv run mypy src --strict && uv run pytest -m "not integration"

# Individual checks
make fmt                      # uv run ruff format src
make lint                     # uv run ruff check src
make type                     # uv run mypy src --strict
make test                     # uv run pytest -m "not integration"

# Run a single test file
uv run pytest tests/unit/test_security_tools.py -v

# Run a single test by name
uv run pytest tests/unit/test_security_tools.py::test_public_s3_bucket -v

# App
make run                      # uv run chandra run
make eval                     # uv run python evals/harness.py
make dashboard                # uv run streamlit run src/chandra/dashboard/app.py

# Full smoke test
make smoke                    # Linux/macOS
make smoke-windows            # PowerShell (Windows)

# IaC (real burner AWS account)
make tf-apply                 # terraform -chdir=iac/synthetic_env apply -auto-approve
make tf-destroy               # terraform -chdir=iac/synthetic_env destroy -auto-approve
```

## Architecture

Chandra is an autonomous AWS observation agent: deterministic boto3 detectors produce findings; an LLM (Claude via Bedrock) ranks and narrates them; results are persisted in Postgres and surfaced via Streamlit.

### LangGraph pipeline

```
START → onboard_account → fanout_observers → [observe_cost | observe_security |
  observe_compliance | observe_performance | observe_reliability] → analyze →
  compose_briefing → persist → END
```

The five observer nodes run in parallel (`Send(...)` fan-out); their findings are merged back via reducers on `ChandraState`. `analyze` calls Bedrock; `compose_briefing` calls Bedrock for an executive summary; `persist` is the **only** node that writes to Postgres.

### Tool-first invariant

Detectors (`src/chandra/tools/`) are pure boto3 functions — they never call the LLM. The LLM never fabricates findings — it only ranks and narrates the detector output. This boundary is enforced by prompt rules and must not be crossed.

### Key modules

| Path | Role |
|---|---|
| `src/chandra/graphs/` | LangGraph state (`state.py`), nodes (`nodes.py`), compilation (`chandra_graph.py`) |
| `src/chandra/tools/` | Detector implementations (one file per KRA); `base.py` defines `DetectorContext` and `detector_guard` |
| `src/chandra/briefing/` | `composer.py` — Bedrock calls, scorecard math, Markdown/JSON render; `schemas.py` — Pydantic models |
| `src/chandra/prompts/` | `observer.md`, `analyzer.md`, `briefer.md` — LLM system prompts |
| `src/chandra/db/` | SQLAlchemy ORM (`models.py`), session (`session.py`), Alembic migrations |
| `src/chandra/aws/` | `client_factory.py` — cached, retry-configured boto3 clients; `regions.py` — region discovery |
| `src/chandra/config.py` | Pydantic `Settings` — all runtime config from env |
| `src/chandra/cli.py` | Typer CLI: `run`, `eval`, `render` sub-commands |
| `src/chandra/dashboard/app.py` | Streamlit 3-tab dashboard (no API layer — reads Postgres directly) |
| `evals/` | `harness.py` (recall/precision scorer), `seed_manifest.yaml` (ground truth), reports |
| `iac/synthetic_env/` | Terraform module seeding 10 known misconfigs in a burner AWS account |

### Detector pattern

Every detector in `src/chandra/tools/` accepts `DetectorContext` and returns `list[Finding]`. Errors are appended to `context.errors` and swallowed — detectors must never raise. Use `@detector_guard` from `tools/base.py`.

### Config reference (`.env` / environment)

| Variable | Default | Purpose |
|---|---|---|
| `AWS_PROFILE` | — | boto3 credential profile |
| `AWS_DEFAULT_REGION` | `us-east-1` | fallback region |
| `BEDROCK_MODEL_ID` | `anthropic.claude-sonnet-4-5-20250929-v1:0` | LLM |
| `POSTGRES_URL` | `postgresql+psycopg://chandra:chandra@localhost:5432/chandra` | state DB |
| `SYNTHETIC_ACCOUNT_ID` | — | burner account for eval |
| `CHANDRA_STALE_KEY_DAYS_OVERRIDE` | — | override 90-day threshold for SEC-003 (demo use) |
| `LOG_LEVEL` | `INFO` | structlog level |

### Severity scoring

Per-KRA score = `max(0, 100 − min(100, sum(severity_weights) × 5))` where weights are critical=10, high=5, medium=2, low=1, info=0. Deterministic ranking: severity weight → KRA order (security > compliance > reliability > performance > cost) → detector_id → resource_arn.

### Testing

- Unit tests use moto (`@mock_aws`) — no real AWS calls.
- CI skips integration tests (`-m "not integration"`) — no Postgres or AWS creds in CI.
- `tests/conftest.py` provides shared fixtures: `aws_context`, `client_factory`, `detector_context`, and per-service boto3 clients.
- The eval harness (`evals/harness.py`) exits non-zero if `recall_overall < 0.80` or any per-KRA recall `< 0.70`.

### CI

- **check.yml** — every PR/push to main: ruff + mypy + pytest (unit only).
- **eval-offline.yml** — nightly: offline eval against `evals/fixtures/baseline_v1.jsonl` (gated until that fixture exists).
