# Local LLM migration — provider abstraction + safe execution layer

**Status:** machinery landed (M0–M2). Legacy-agent cutover is the reviewed next step (see §6).
**Owner routing:** `src/chandra/llm/**` + `src/chandra/execution/**` (LangGraph team); the
`digitalworker_agents/aws_execution_agent.py` cutover in §6 is Frontend-team CODEOWNERS.

---

## 1. Goal

Make the **reasoning engine** swappable — Claude on Bedrock today, a local
vLLM model tomorrow — **without touching any other subsystem**. Jira, Slack,
Teams, Email, the five KRAs + custom KRA, AWS/Azure/GCP detectors, Terraform,
Kubernetes, LangGraph orchestration, the approval workflow, audit logging,
rollback and memory all keep working exactly as before. Only *which model
proposes the plan* changes; *how the plan is validated and executed* is
identical across providers, and is now strictly typed.

Switching providers is an **environment change, never a code change**:

```bash
# Claude / Bedrock (default, unchanged)
LLM_PROVIDER=bedrock BEDROCK_MODEL_ID=... 

# Local vLLM (OpenAI-compatible)
LLM_PROVIDER=vllm OPENAI_API_BASE=http://vllm:8000/v1 OPENAI_MODEL_NAME=Qwen2.5-32B-Instruct

# Ollama (local)
LLM_PROVIDER=ollama OLLAMA_HOST=http://localhost:11434 OLLAMA_MODEL=qwen2.5:32b
```

We do **not** promise a local model matches Claude. We *benchmark* both and
document the gap (see §5).

---

## 2. Architecture

```
                      ┌──────────────────────────────────────────────┐
   request/intent ───►│  planner.generate_execution_plan()           │
   + context (RAG)    │    ├─ get_provider()  → BaseLLM               │
                      │    │    ClaudeProvider | BedrockProvider      │
                      │    │    VLLMProvider  | OllamaProvider …      │
                      │    │        (text/JSON only — never executes) │
                      │    ├─ validate_plan()      (schema + safety)  │
                      │    ├─ verify_intent_matches_plan()            │
                      │    └─ self-correct ≤ N, else deterministic    │
                      └───────────────────┬──────────────────────────┘
                                          │  validated ExecutionPlan (typed)
                                          ▼
                      ┌──────────────────────────────────────────────┐
                      │  executor.execute_plan()   (DETERMINISTIC)    │
                      │    re-validate → dry_run gate → typed dispatch│
                      │    AwsApiAction → getattr(client, op)(**args) │
                      │    TerraformAction → validate HCL, then apply │
                      │    KubernetesAction → (delegated)             │
                      └──────────────────────────────────────────────┘
```

**The one invariant:** the only thing that crosses from "AI proposes" to
"system executes" is a *validated* `ExecutionPlan`. No executor ever consumes
raw model text — no regex, no free-text parsing, no `eval`, no shell string.

### Provider layer — `src/chandra/llm/`
- `build_chat_model(model, provider, **kwargs)` — the single factory. Chooses
  the backend from `LLM_PROVIDER` (or an explicit `provider=` override). No
  hardcoded model ids; every model comes from env.
- `providers.py` — `BaseLLM` ABC with one uniform surface: `complete()`
  (retries + timeout + generation params), `health_check()`. Concrete
  providers (`VLLMProvider`, `OpenAICompatibleProvider`, `OllamaProvider`,
  `BedrockProvider`, `ClaudeProvider` alias) only name their factory key —
  there is **zero per-provider business logic**. `get_provider()` is the
  business-facing seam.

### Safety layer (M1) — `src/chandra/execution/`
- `schemas.py` — `ExecutionPlan` / typed actions (`aws_api`, `terraform`,
  `kubernetes`, `noop`), `extra="forbid"`. A model that invents fields or a
  CLI string is rejected at the door.
- `validator.py` — `validate_plan()`: tolerant JSON parse → Pydantic schema →
  deterministic safety checks (AWS service allow-list, shell-metacharacter
  scan, actionable-plan check).
- `terraform.py` — `validate_terraform()`: `fmt → init → validate → plan`;
  degrades to `unavailable` (never `valid`) when the binary is missing.
- `verification.py` — `verify_intent_matches_plan()`: rule-based (no LLM)
  check that the plan addresses the request and does not silently exceed it
  (out-of-scope resource + unrequested-destructive-op detection).

### Reasoning + execution (M2)
- `planner.py` — `generate_execution_plan()`: JSON-only prompt →
  `validate_plan` → `verify_intent_matches_plan` → **self-correct up to
  `max_attempts`** by feeding rejection reasons back → deterministic no-op
  fallback if the model never produces valid JSON.
- `executor.py` — `execute_plan()`: deterministic. Re-validates (defence in
  depth), `dry_run` by default (a real mutation needs `plan.dry_run=False`
  **and** `force_execute=True` — fail-safe, not fail-open), dispatches each
  typed action via `AwsClientFactory`. Never `boto3.client(...)` directly.

---

## 3. Workflow / sequence

1. Intake normalizes a request → intent + context (RAG sources: prior Jira,
   runbooks, AWS/Terraform docs, internal KB, CloudWatch — collected by the
   existing `digital_worker.context` collectors).
2. `generate_execution_plan(intent, context)` asks the configured provider
   for JSON only.
3. `validate_plan` rejects malformed/unsafe output; `verify_intent_matches_plan`
   rejects off-intent output; both feed the self-correction loop.
4. A **validated** `ExecutionPlan` reaches the approval gate
   (`interrupt_before`) — unchanged human `Approve / Reject / Escalate`.
5. On approval, `execute_plan` runs it deterministically (dry-run by default);
   notifications / Jira / audit / memory fire exactly as today.

---

## 4. Files added / changed and why

| File | Change | Why |
|---|---|---|
| `src/chandra/llm/__init__.py` | `build_chat_model` gains a `provider=` override | A provider class can pick its backend regardless of ambient `LLM_PROVIDER`. |
| `src/chandra/llm/providers.py` | **new** — `BaseLLM` + providers + `get_provider` | Uniform reasoning surface so business logic depends on `BaseLLM`, never on Bedrock/vLLM/Ollama. |
| `src/chandra/execution/schemas.py` | **new (M1)** — typed `ExecutionPlan` | Structured-only contract; the anti-hallucination foundation. |
| `src/chandra/execution/validator.py` | **new (M1)** — `validate_plan` | Schema + deterministic safety gate. |
| `src/chandra/execution/terraform.py` | **new (M1)** — `validate_terraform` | Prove generated HCL before any apply. |
| `src/chandra/execution/verification.py` | **new (M1)** — intent check | Catch schema-valid-but-wrong plans without an LLM. |
| `src/chandra/execution/planner.py` | **new** — `generate_execution_plan` | JSON-only planning + self-correction + safe fallback. |
| `src/chandra/execution/executor.py` | **new** — `execute_plan` | Deterministic typed executor; the "never execute raw text" landing spot. |
| `src/chandra/execution/bridge.py` | **new** — `plan_and_execute` | The one call the agent / DW graph use: provider → planner → validate → verify → executor, with the approval + dry-run contract. |
| `src/chandra/config.py` | `VLLM_API_BASE` / `VLLM_MODEL` / `VLLM_API_KEY` + `CHANDRA_TYPED_EXECUTION` | First-class vLLM config; the flag that enforces the typed pipeline. |
| `src/chandra/llm/__init__.py` | vLLM branch prefers `VLLM_*`, falls back to `OPENAI_*` | Natural local-vLLM config while any OpenAI-compatible server keeps working. |
| `digitalworker_agents/aws_execution_agent.py` | `_typed_execution_gate` in `_execute_node` | With `CHANDRA_TYPED_EXECUTION=true`, remediation runs only through a validated `ExecutionPlan` + deterministic executor — never generated shell/python via subprocess. Default off preserves the legacy engine. |
| `copilot_agents/call_tools.py` | removed dead `ChatOpenAI` import | The codebase now constructs no chat model outside the factory. |
| `.env.example` | provider blocks + typed-execution flag; removed hardcoded model ids / password / IP | Documents the env-only provider swap; drops leaked-looking values. |
| `scripts/benchmark_llm.py` + `evals/fixtures/llm_benchmark_seed.jsonl` | **new** | Replay tickets and score any provider (Claude vs vLLM). |
| `tests/unit/test_llm_providers.py`, `test_execution_planner.py`, `test_execution_executor.py`, `test_execution_bridge.py`, `test_execution_validator.py` | **new** | Cover retry/health/selection, self-correct/fallback, dry-run gate + typed dispatch, bridge approval/dry-run contract, schema/safety/verify. |

Nothing was removed. The existing FastAPI service, notification agents, Jira
tracker, KRA engine, LangGraph orchestration, approval workflow, audit,
rollback and memory are untouched.

---

## 5. Benchmark

`scripts/benchmark_llm.py` replays a JSONL fixture through the planner against
a chosen provider and scores the **measurable** acceptance dimensions:

- Intent accuracy (validated **and** intent-verified)
- KRA accuracy (`kra_code` vs expected)
- Action-kind accuracy, Terraform accuracy
- Execution success (dry-run dispatch clean)
- Hallucination rate (fell back to deterministic **or** out-of-scope resource)
- Latency (mean / p50 / p95 / max) and self-correction attempts
- Token usage — recorded **only** when the backend reports it (no estimation)

```bash
uv run python scripts/benchmark_llm.py \
  --fixture evals/fixtures/llm_benchmark_seed.jsonl \
  --provider vllm --limit 1000 --out evals/reports/
```

The 1000-ticket Claude-vs-vLLM comparison is run with this tool once a vLLM
endpoint is reachable; the seed fixture is a smoke set. With no reachable
model the harness degrades honestly (every ticket → deterministic fallback →
100% hallucination rate) instead of crashing.

---

## 6. Legacy-agent cutover — wired behind a flag

`digitalworker_agents/aws_execution_agent.py` (~3.6k lines) generates
Python/Terraform/shell as **text** (`ExecutionPlan.execution_type ∈
{python, terraform, shell, mixed}`) and runs it via `subprocess`. That is the
permissive path this migration replaces.

**Landed (this PR):** a `_typed_execution_gate` at the top of `_execute_node`.
When `CHANDRA_TYPED_EXECUTION=true`, the node routes the request through
`plan_and_execute` (provider → planner → validate → verify → deterministic
executor) and **never** runs generated code via subprocess; the model only
proposes a typed `ExecutionPlan`. The gate maps the executor's step results
back into the agent's `execution_results` shape, so the downstream report /
notify / Jira nodes are unchanged.

**Default off, on purpose.** With the flag unset the legacy code-gen engine
runs exactly as before — existing functionality is preserved. Flipping the
flag to `true` requires an **end-to-end validation pass** in an environment
that has the MCP servers + live AWS the agent needs (which CI here does not),
because the typed executor's boto3 coverage must be confirmed against the real
remediation set before it becomes the only path. That E2E pass is the
remaining gate before removing the legacy subprocess engine entirely.

**Recommended cutover sequence (operator):**
1. `CHANDRA_TYPED_EXECUTION=true` + `dry_run` on a staging account → confirm
   the typed plans match intent for the real ticket mix.
2. Widen `ALLOWED_AWS_SERVICES` / add typed handlers for any remediation the
   plans can't yet express (the validator will reject them loudly — no silent
   gaps).
3. Real (non-dry-run) execution on staging → prod.
4. Once parity is proven, delete the legacy `_plan_node` code-gen +
   `_run_commands` subprocess path (separate Frontend-CODEOWNERS PR).

---

## 7. Deployment / rollback

- **Deploy vLLM:** run an OpenAI-compatible server (vLLM/TGI) serving the
  benchmarked model; set `LLM_PROVIDER=vllm`, `OPENAI_API_BASE`,
  `OPENAI_MODEL_NAME`. `BaseLLM.health_check()` gates readiness.
- **Rollback:** set `LLM_PROVIDER=bedrock`. No redeploy of app code needed —
  provider selection is pure env. The deterministic fallback in the planner
  and the dry-run-default executor mean a bad model degrades to guidance-only,
  never to unsafe execution.
- **Production readiness:** provider `health_check` wired to `/health/ready`;
  dry-run default on; Terraform gated by `validate_terraform`; AWS calls
  allow-listed; audit + approval unchanged.

---

## 8. Roadmap

- [ ] vLLM endpoint + real 1000-ticket Claude-vs-vLLM benchmark run.
- [ ] Legacy `aws_execution_agent.py` cutover (§6) — Frontend PR.
- [ ] RAG context assembly wired into `generate_execution_plan(context=...)`
      from the existing `digital_worker.context` collectors.
- [ ] Token-usage capture from provider response metadata.
- [ ] Architecture diagram refresh (`docs/architecture-diagram.html`).
