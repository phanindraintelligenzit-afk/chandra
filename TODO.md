# Chandra — Build TODO

Tracks the master prompt's DELIVERABLE ORDER. One line per task.

## Deliverables

- [x] D1 — Repo scaffold (pyproject, Makefile, Dockerfile, compose, .env.example, README, TODO)
- [x] D2 — `config.py`, `aws/client_factory.py`, `aws/regions.py`
- [x] D3 — DB models + alembic migration 0001
- [x] D4 — `tools/security.py` (5 detectors) + unit tests against moto
- [x] D5 — `tools/cost.py` + tests
- [x] D6 — `tools/compliance.py` + tests
- [x] D7 — `tools/performance.py` + tests
- [x] D8 — `tools/reliability.py` + tests
- [x] D9 — `graphs/state.py`, `graphs/nodes.py`, `graphs/chandra_graph.py`
- [x] D10 — `briefing/composer.py` + LLM prompts (`observer.md`, `analyzer.md`, `briefer.md`)
- [x] D11 — `cli.py` — `chandra run`, `chandra eval`
- [x] D12 — Terraform synthetic env + `seed_manifest.yaml`
- [x] D13 — `evals/harness.py`
- [x] D14 — Streamlit dashboard
- [x] D15 — Demo smoke script + README run-through

## Open questions / decisions

- Target for synthetic env: **real burner AWS account** (confirmed by user 2026-05-13).
- Postgres in dev: local container via `docker compose`. Prod: RDS (managed by ops).
- LLM: Claude Sonnet via Bedrock, model id sourced from `BEDROCK_MODEL_ID`.

## Quality gates (must pass before any commit)

- `make check` — `ruff` + `mypy --strict` + `pytest`.
- No `# TODO: implement` survives a commit.
- Tools never call Bedrock. Writes to Postgres only inside the `persist` node and migrations.
