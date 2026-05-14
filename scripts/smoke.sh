#!/usr/bin/env bash
# Chandra demo smoke script.
#
# End-to-end: verify prereqs -> docker compose Postgres -> alembic migrate ->
# terraform apply synthetic env -> chandra run -> chandra eval -> open dashboard.
#
# Idempotent. Exits non-zero on any step failure.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

: "${SYNTHETIC_ACCOUNT_ID:?Set SYNTHETIC_ACCOUNT_ID in .env (burner AWS account id)}"

step() {
  printf '\n\033[1;36m▶ %s\033[0m\n' "$*"
}

ok() {
  printf '\033[1;32m✓ %s\033[0m\n' "$*"
}

step "Verifying prerequisites"
for bin in uv docker terraform aws; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "Missing required tool: $bin" >&2
    exit 1
  fi
done
ok "uv, docker, terraform, aws CLI present"

step "Confirming caller identity"
aws sts get-caller-identity --output table

CURRENT_ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
if [[ "$CURRENT_ACCOUNT" != "$SYNTHETIC_ACCOUNT_ID" ]]; then
  echo "WARNING: AWS_PROFILE points at $CURRENT_ACCOUNT but SYNTHETIC_ACCOUNT_ID is $SYNTHETIC_ACCOUNT_ID" >&2
fi

step "Bringing up local Postgres"
docker compose up -d postgres
# wait for healthcheck
for _ in $(seq 1 30); do
  if docker compose ps postgres | grep -q "healthy"; then
    break
  fi
  sleep 1
done
ok "postgres healthy"

step "Installing Python deps (uv sync)"
uv sync --all-extras
ok "deps installed"

step "Running alembic migrations"
uv run alembic upgrade head
ok "schema up to date"

step "Applying synthetic Terraform env (this may take 5-8 minutes)"
( cd iac/synthetic_env && terraform init -upgrade -input=false )
( cd iac/synthetic_env && terraform apply -auto-approve -input=false )
ok "synthetic env applied"

step "Running Chandra (full observation pipeline)"
CHANDRA_STALE_KEY_DAYS_OVERRIDE=0 uv run chandra run --account "$SYNTHETIC_ACCOUNT_ID"
ok "run complete — artifacts in evals/reports/"

step "Running eval harness"
if CHANDRA_STALE_KEY_DAYS_OVERRIDE=0 uv run chandra eval --account "$SYNTHETIC_ACCOUNT_ID"; then
  ok "eval passed (recall ≥ 80%, per-KRA ≥ 70%)"
else
  echo "Eval thresholds breached — see evals/reports/*.md for the diagnostic." >&2
  exit 1
fi

step "Launching dashboard"
echo "Open another shell and run:  make dashboard"
echo "Then point a browser at http://localhost:8501"

step "Smoke complete."
echo "To tear down the burner: make tf-destroy"
