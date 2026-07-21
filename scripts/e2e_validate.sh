#!/usr/bin/env bash
# End-to-end live validation harness for the Local-LLM migration.
#
# Run this IN THE ENVIRONMENT THAT HAS the GPU + vLLM server + real AWS/Jira
# (not in CI, which has none of those). It performs tasks 2-8 of the runtime
# validation plan and writes a combined report — it does not fabricate
# anything: every number comes from a live probe.
#
#   Tasks covered:
#     2  verify vLLM server up, /health, configured model
#     3  confirm provider config resolves to vLLM
#     6  run the 1000-ticket benchmark (pass --fixture)
#     7  run the same fixture against Bedrock/Claude for comparison
#     8  capture token throughput (vLLM /metrics) + GPU/mem (nvidia-smi)
#
# Usage:
#   VLLM_API_BASE=http://localhost:8000/v1 VLLM_MODEL=<approved-model> \
#     scripts/e2e_validate.sh --fixture evals/fixtures/your_1000.jsonl
#
# Requires: LLM_PROVIDER handling in the app (already present), curl, jq
# (optional, for pretty metrics), and — for the Bedrock leg — working AWS
# credentials with Bedrock access.
set -euo pipefail

FIXTURE="evals/fixtures/llm_benchmark_seed.jsonl"
LIMIT=1000
OUT="evals/reports"
while [ $# -gt 0 ]; do
  case "$1" in
    --fixture) FIXTURE="$2"; shift 2 ;;
    --limit)   LIMIT="$2"; shift 2 ;;
    --out)     OUT="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

: "${VLLM_API_BASE:?set VLLM_API_BASE (e.g. http://localhost:8000/v1)}"
: "${VLLM_MODEL:?set VLLM_MODEL to the approved model name}"
BASE_HOST="${VLLM_API_BASE%/v1}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT"
REPORT="$OUT/e2e_validation_${STAMP}.md"

log() { echo "[$(date -u +%H:%M:%S)] $*"; }
section() { printf '\n## %s\n\n' "$1" >>"$REPORT"; }

echo "# E2E validation — $STAMP" >"$REPORT"

# ── Task 2: vLLM server up + health + model ────────────────────────────────
log "Task 2: probing vLLM health + model"
section "Task 2 — vLLM server"
if ! curl -fsS -m 10 "${BASE_HOST}/health" >/dev/null; then
  echo "FAIL: ${BASE_HOST}/health not reachable" | tee -a "$REPORT" >&2
  exit 1
fi
echo "- health: OK (${BASE_HOST}/health)" >>"$REPORT"
MODELS="$(curl -fsS -m 10 "${VLLM_API_BASE}/models" || true)"
echo "- models served: \`${MODELS}\`" >>"$REPORT"
case "$MODELS" in
  *"$VLLM_MODEL"*) echo "- configured model present: $VLLM_MODEL ✅" >>"$REPORT" ;;
  *) echo "- WARNING: VLLM_MODEL=$VLLM_MODEL not found in /models" >>"$REPORT" ;;
esac

# ── Task 3: provider config resolves to vLLM ───────────────────────────────
log "Task 3: verifying provider resolution + health_check()"
section "Task 3 — provider resolution"
LLM_PROVIDER=vllm uv run python - <<'PY' >>"$REPORT" 2>&1 || true
from src.chandra.llm.providers import get_provider
p = get_provider()
print(f"- provider = `{p.provider}`")
print(f"- health_check() = {p.health_check()}")
PY

# ── Task 8 (pre): GPU/mem snapshot ─────────────────────────────────────────
section "Task 8 — GPU / memory (nvidia-smi)"
if command -v nvidia-smi >/dev/null; then
  nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total \
    --format=csv,noheader >>"$REPORT" 2>&1 || echo "- nvidia-smi query failed" >>"$REPORT"
else
  echo "- nvidia-smi not available on this host" >>"$REPORT"
fi

# ── Task 8 (throughput): vLLM Prometheus metrics ───────────────────────────
section "Task 8 — vLLM token metrics (/metrics)"
if curl -fsS -m 10 "${BASE_HOST}/metrics" >/tmp/vllm_metrics.txt 2>/dev/null; then
  grep -E "vllm:(prompt|generation)_tokens_total|vllm:.*throughput" /tmp/vllm_metrics.txt \
    >>"$REPORT" 2>&1 || echo "- no token counters exposed yet" >>"$REPORT"
else
  echo "- ${BASE_HOST}/metrics not exposed (start vLLM with --disable-log-stats off)" >>"$REPORT"
fi

# ── Tasks 6-7: benchmark both providers on the same fixture ────────────────
log "Task 6: benchmarking vLLM on $FIXTURE (limit=$LIMIT)"
section "Task 6 — vLLM benchmark"
LLM_PROVIDER=vllm uv run python scripts/benchmark_llm.py \
  --fixture "$FIXTURE" --provider vllm --limit "$LIMIT" --out "$OUT" | tee -a "$REPORT"

log "Task 7: benchmarking Bedrock/Claude on the same fixture"
section "Task 7 — Bedrock/Claude benchmark"
if LLM_PROVIDER=bedrock uv run python scripts/benchmark_llm.py \
     --fixture "$FIXTURE" --provider bedrock --limit "$LIMIT" --out "$OUT" | tee -a "$REPORT"; then
  :
else
  echo "- Bedrock leg failed (check AWS credentials / Bedrock access)" >>"$REPORT"
fi

# ── Task 8 (post): GPU/mem snapshot after load ─────────────────────────────
section "Task 8 — GPU / memory after load"
if command -v nvidia-smi >/dev/null; then
  nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total \
    --format=csv,noheader >>"$REPORT" 2>&1 || true
fi

log "DONE — report at $REPORT"
echo -e "\nThe two per-provider reports in $OUT are the Claude-vs-vLLM comparison." >>"$REPORT"
echo "$REPORT"
