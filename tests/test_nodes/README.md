# test_nodes -- per-node smoke tests for the Chandra graph

Every script in this folder is a **standalone, runnable test** for a
single node in `src/chandra/graphs/chandra_graph.py`. Each one:

1. Loads the project root `.env` (via python-dotenv) so it picks up
   `SYNTHETIC_ACCOUNT_ID`, `AWS_DEFAULT_REGION`, `BEDROCK_MODEL_ID`,
   `SNS_TOPIC_ARN`, and (optionally) `POSTGRES_URL`.
2. Builds a small `ChandraState` (the typed dict threaded through the graph).
3. Calls the node function directly with that state.
4. Prints the input + output in a readable shape so you can eyeball
   what each node actually does.

## Two modes

### `REAL` mode (default)

The scripts hit real AWS, real Bedrock, and real SNS. The first line of
output tells you which mode you're in, e.g.:

```
[mode: REAL -- account=827295473120 region=us-east-1 bedrock=qwen.qwen3-next-80b-a3b-a3b sns=arn:aws:sns:us-east-1:827295473120:chandra-escalation-critical]
```

Use this when you want to exercise the production path against the
synthetic AWS account (real billing, real Slack notification through
the SNS topic, etc.).

### `MOCK` mode (offline)

Set `CHANDRA_TEST_NODES_MOCK=1` before running a script to make it
self-contained: moto backs AWS, Bedrock is patched, Postgres is
in-memory SQLite. No real cloud calls, no Bedrock bill.

```bash
CHANDRA_TEST_NODES_MOCK=1 uv run python -m tests.test_nodes.onboard_account
```

## Quick start

```bash
# Single node -- real AWS / real Bedrock
uv run python -m tests.test_nodes.onboard_account
uv run python -m tests.test_nodes.ingest_observations
uv run python -m tests.test_nodes.kra_supervisor
uv run python -m tests.test.nodes.observe_cost
uv run python -m tests.test_nodes.observe_security
uv run python -m tests.test_nodes.observe_compliance
uv run python -m tests.test_nodes.observe_performance
uv run python -m tests.test_nodes.observe_reliability
uv run python -m tests.test_nodes.analyze
uv run python -m tests.test_nodes.decision_router
uv run python -m tests.test_nodes.action_executor
uv run python -m tests.test_nodes.escalation
uv run python -m tests.test_nodes.compose_briefing
uv run python -m tests.test_nodes.approval_node
uv run python -m tests.test_nodes.persist

# Or run them all in pipeline order
uv run python -m tests.test_nodes.run_all

# Offline / CI run of the full pipeline
CHANDRA_TEST_NODES_MOCK=1 uv run python -m tests.test_nodes.run_all
```

## Node -> script mapping

| LangGraph node | Test script | Test function |
| --- | --- | --- |
| `onboard_account` | `onboard_account.py` | `test_onboardaccount()` |
| `ingest_observations` | `ingest_observations.py` | `test_ingestobservations()` |
| `kra_supervisor` | `kra_supervisor.py` | `test_krasupervisor()` |
| `_route_kra_workers` | `kra_supervisor.py` | `test_routekraworkers()` |
| `observe_cost` | `observe_cost.py` | `test_observecost()` |
| `observe_security` | `observe_security.py` | `test_observesecurity()` |
| `observe_compliance` | `observe_compliance.py` | `test_observecompliance()` |
| `observe_performance` | `observe_performance.py` | `test_observeperformance()` |
| `observe_reliability` | `observe_reliability.py` | `test_observereliability()` |
| `analyze` | `analyze.py` | `test_analyze()` |
| `decision_router` | `decision_router.py` | `test_decisionrouter()` |
| `action_executor` | `action_executor.py` | `test_actionexecutor()` |
| `escalation` | `escalation.py` | `test_escalation()` |
| `compose_briefing` | `compose_briefing.py` | `test_composebriefing()` |
| `approval_node` | `approval_node.py` | `test_approvalnode()` |
| `persist` | `persist.py` | `test_persist()` |

## Conventions

* Every script imports the shared `_env.py` helper, which:
  * loads the real `.env` at the repo root
  * exposes `aws_scope()` and `bedrock_scope()` context managers that
    hit real services by default and switch to moto / patched Bedrock
    when `CHANDRA_TEST_NODES_MOCK=1` is set
  * exposes `real_account_id()`, `real_region()`, `real_sns_topic_arn()`,
    and `real_postgres_url()` so the scripts read the values that the
    rest of Chandra reads -- no hardcoded credentials.
* The `persist` script uses `POSTGRES_URL` from `.env` when set; in
  MOCK_MODE (or when no URL is set) it falls back to in-memory SQLite
  so the demo still runs.
* Output is intentionally `print`-based -- the point is to see the
  shape of the data, not to assert.
* The folder is *outside* `tests/unit/`, so `make test` is unaffected.

## Safety notes for `REAL` mode

* A few scripts **seed resources** before invoking the node
  (alarms, rules, an EBS volume, a public S3 bucket) so the output
  shows real findings. These resources are real -- you may want to
  clean them up afterwards.
* `escalation` will publish a real message to the SNS topic in your
  `.env`, which will be forwarded to Slack via AWS Chatbot.
* `analyze` and `compose_briefing` will call Bedrock and bill tokens
  against the configured `BEDROCK_MODEL_ID`.
* `persist` will write a real Run/Finding/Briefing row to the
  configured Postgres database. Use a sandbox account or wrap in a
  transaction you can roll back.
