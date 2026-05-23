# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies (uses uv)
uv sync

# Run the FastAPI server locally (port 6001)
uv run uvicorn fastapi_app:app --host 0.0.0.0 --port 6001 --reload

# Run the Gradio chat UI locally (port 7861)
uv run app.py

# Build and run both services via Docker Compose
docker compose up --build

# Lint
uv run ruff check .
uv run ruff format .

# Type check
uv run mypy --strict src
```

## Environment Setup

Copy `.env.example` to `.env` and fill in:

| Variable | Purpose |
|---|---|
| `MODEL_NAME` | Bedrock model ID (e.g. `anthropic.claude-sonnet-4-5-20250929-v1:0`) |
| `AWS_PROFILE` / `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` | AWS credentials |
| `AWS_DEFAULT_REGION` | Default region (default: `us-east-1`) |
| `JIRA_SERVER`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | Required only for `analyzer_agent` |

All config is loaded via `config.py` (`pydantic-settings`). Never read `os.environ` directly outside that module — import `settings` from `config.py`.

## Architecture

The repo contains **two separate LangGraph pipelines** plus a Gradio chat UI, all served by FastAPI.

### 1. Gradio Chat Agent (`graph.py` + `app.py`)

An interactive ReAct-style agent. `graph.py` defines a `StateGraph` with two nodes: `call_llm` (Bedrock via `ChatBedrockConverse`) and `execute_tools`. `app.py` wraps it in a Gradio UI. Tools are defined in `call_tools.py` (CloudWatch metrics, X-Ray, Config resource changes, CloudTrail logs, Cost Explorer) and bound directly to the LLM via `bind_tools`. Conversation memory uses `MemorySaver` keyed by `thread_id`.

### 2. KRA Observability Pipeline (`observation_agent.py` + `fastapi_app.py`)

A non-interactive pipeline that fires all AWS data-collection tools in parallel, normalizes results, then passes raw data to the LLM to generate a structured `ObservabilityReport`. Three-node graph: `trigger_tools → execute_tools (ToolNode) → normalize → summary`. Tools are the richer async fetchers in `tools/aws_cloud_tools/langchain_tools.py` (14 tools including GuardDuty, Budgets, Health Events, structured findings from `tools/observability_tools/`).

### 3. Analyzer Agent (`analyzer_agent.py`)

Two-node graph: `analyze → create_tickets`. Takes a list of remediation `ActionItem`s from the observability report, uses structured LLM output (`with_structured_output`) to assign severity/priority/HumanReviewNeeded, then creates a Jira ticket per action.

### FastAPI (`fastapi_app.py`)

Three endpoints:
- `GET /getCostMetrics` — calls `AWSCostExplorerFetcher` directly
- `POST /getAgentObservations` — instantiates `AwsObservabilityAgent` and runs the KRA pipeline
- `POST /analyzeActions` — instantiates `AnalyzerAgent` and runs ticket creation

### Tools Layer

```
tools/
├── call_tools.py                  # Simple sync boto3 tools for the Gradio agent
├── langchain_tools.py             # Re-export shim → aws_cloud_tools/langchain_tools.py
├── aws_cloud_tools/
│   ├── langchain_tools.py         # 14 @tool wrappers around async fetchers; defines TOOLS_LIST
│   ├── tool_findings.py           # Runs observability_tools detectors via run_all_detectors()
│   ├── cost_explorer.py, metrics_fetcher.py, ...  # Async fetchers (one class per data source)
│   └── ...
├── observability_tools/           # KRA detectors (compliance, security, reliability, performance, cost)
│   ├── base.py                    # DetectorContext dataclass
│   └── *.py                       # Each returns list[Finding]
└── jira_tools/create_jira_ticket.py
```

The `tools/observability_tools/` detectors are the only place that produces `Finding` objects. `tool_findings.run_all_detectors()` fans them out and aggregates results. The `briefing/composer.py` module is the designated place for Bedrock calls for scorecard math and executive summaries (separate from the agent pipelines above which call Bedrock in their `summary` nodes).

### Key constraint

`tools/observability_tools/` detectors must **not** call Bedrock. Only `briefing/composer.py` and the agent `summary`/`analyze` nodes may invoke the LLM.

## Terraform

`terraform_files/` provisions a synthetic AWS environment for eval. Apply with:

```bash
cd terraform_files
terraform init
terraform apply
```
