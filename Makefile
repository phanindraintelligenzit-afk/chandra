.PHONY: help install fmt lint type test check run eval dashboard tf-apply tf-destroy db-up db-down migrate smoke clean eval-offline

PYTHON ?= python
UV ?= uv

help:
	@echo "Targets:"
	@echo "  install      Install runtime + dev deps via uv"
	@echo "  fmt          ruff format"
	@echo "  lint         ruff check"
	@echo "  type         mypy --strict"
	@echo "  test         pytest"
	@echo "  check        lint + type + test (commit gate)"
	@echo "  run          chandra run --account \$$SYNTHETIC_ACCOUNT_ID"
	@echo "  eval         chandra eval --account \$$SYNTHETIC_ACCOUNT_ID"
	@echo "  dashboard    streamlit run src/chandra/dashboard/app.py"
	@echo "  tf-apply     terraform apply on synthetic env"
	@echo "  tf-destroy   terraform destroy on synthetic env"
	@echo "  db-up        docker compose up -d postgres"
	@echo "  db-down      docker compose down"
	@echo "  migrate      alembic upgrade head"
	@echo "  smoke        end-to-end: tf apply -> run -> eval"
	@echo "  clean        remove caches"

install:
	$(UV) sync --all-extras

fmt:
	$(UV) run ruff format src tests

lint:
	$(UV) run ruff check src tests

type:
	$(UV) run mypy src

test:
	$(UV) run pytest

check: lint type test

run:
	$(UV) run chandra run --account $${SYNTHETIC_ACCOUNT_ID:?set SYNTHETIC_ACCOUNT_ID}

eval:
	$(UV) run chandra eval --account $${SYNTHETIC_ACCOUNT_ID:?set SYNTHETIC_ACCOUNT_ID}

dashboard:
	$(UV) run streamlit run src/chandra/dashboard/app.py

tf-apply:
	cd iac/synthetic_env && terraform init -upgrade && terraform apply -auto-approve

tf-destroy:
	cd iac/synthetic_env && terraform destroy -auto-approve

db-up:
	docker compose up -d postgres

db-down:
	docker compose down

migrate:
	$(UV) run alembic upgrade head

smoke:
	bash scripts/smoke.sh

smoke-windows:
	pwsh -File scripts/smoke.ps1

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache dist build *.egg-info

# Chaos resilience tests (nightly only, takes time)
chaos:
uv run pytest -m integration tests/integration/test_chaos.py -v

# Chaos resilience tests (nightly only)
chaos:
uv run pytest -m integration tests/integration/test_chaos.py -v
# Offline eval - no AWS, no Terraform required
eval-offline:
	$(UV) run python -m chandra.cli eval --fixture evals/fixtures/baseline_v1.jsonl
