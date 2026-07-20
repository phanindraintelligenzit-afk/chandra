#!/usr/bin/env python
"""Benchmark a reasoning provider on Chandra planning tickets.

Replays a JSONL fixture of cloud-operations tickets through the structured
planner (:func:`generate_execution_plan`) against whichever provider
``LLM_PROVIDER`` (or ``--provider``) selects — Claude on Bedrock today, a
local vLLM model tomorrow — and scores the *measurable* dimensions of the
migration acceptance criteria:

* **Intent accuracy**   — plan validated AND intent-verification passed.
* **KRA accuracy**      — plan's ``kra_code`` matches the expected KRA.
* **Terraform accuracy** — Terraform steps that passed HCL validation.
* **Execution success** — the validated plan runs clean in dry-run.
* **Hallucination rate** — tickets that fell back to the deterministic
  no-op plan, or whose plan referenced resources nobody asked about.
* **Latency**           — wall-clock per ticket (mean / p50 / p95).
* **Self-correction**   — how many attempts the planner needed.

Token usage is provider-reported and only recorded when the backend
returns it; this harness does not estimate it (honest-by-default).

This is the tool the 1000-ticket comparison is run with (Claude vs vLLM);
the seed fixture (``evals/fixtures/llm_benchmark_seed.jsonl``) is a small
smoke set. Running against a real provider needs a reachable endpoint;
without one, every ticket fails closed to the deterministic fallback,
which the report shows as a 100% hallucination rate rather than crashing.

Usage
-----
    uv run python scripts/benchmark_llm.py \
        --fixture evals/fixtures/llm_benchmark_seed.jsonl \
        --provider vllm --limit 1000 --out evals/reports/
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Run as a plain script (`python scripts/benchmark_llm.py`): the script's own
# directory — not the repo root — is on sys.path, so make ``src.chandra``
# resolvable the same way the app does when launched from the repo root.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from src.chandra.execution.executor import execute_plan  # noqa: E402  # after sys.path shim
from src.chandra.execution.planner import generate_execution_plan  # noqa: E402
from src.chandra.execution.schemas import ActionKind  # noqa: E402
from src.chandra.llm.providers import BaseLLM, get_provider  # noqa: E402
from src.chandra.logging import get_logger  # noqa: E402

logger = get_logger(__name__)


@dataclass
class TicketOutcome:
    ticket_id: str
    intent_ok: bool
    kra_ok: bool | None
    kind_ok: bool | None
    terraform_ok: bool | None
    execution_ok: bool
    hallucinated: bool
    attempts: int
    latency_s: float
    generated_by: str


@dataclass
class BenchmarkReport:
    provider: str
    total: int
    outcomes: list[TicketOutcome] = field(default_factory=list)

    def _rate(self, predicate: Any) -> float:
        vals = [predicate(o) for o in self.outcomes]
        vals = [v for v in vals if v is not None]
        return round(sum(1 for v in vals if v) / len(vals), 4) if vals else 0.0

    def summary(self) -> dict[str, Any]:
        latencies = sorted(o.latency_s for o in self.outcomes)
        attempts = [o.attempts for o in self.outcomes]
        return {
            "provider": self.provider,
            "total_tickets": self.total,
            "intent_accuracy": self._rate(lambda o: o.intent_ok),
            "kra_accuracy": self._rate(lambda o: o.kra_ok),
            "action_kind_accuracy": self._rate(lambda o: o.kind_ok),
            "terraform_accuracy": self._rate(lambda o: o.terraform_ok),
            "execution_success_rate": self._rate(lambda o: o.execution_ok),
            "hallucination_rate": self._rate(lambda o: o.hallucinated),
            "avg_attempts": round(statistics.mean(attempts), 3) if attempts else 0.0,
            "latency_s": {
                "mean": round(statistics.mean(latencies), 3) if latencies else 0.0,
                "p50": round(_pct(latencies, 50), 3),
                "p95": round(_pct(latencies, 95), 3),
                "max": round(max(latencies), 3) if latencies else 0.0,
            },
            "token_usage": "unavailable (provider does not report usage to this harness)",
        }


def _pct(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _run_ticket(ticket: dict[str, Any], llm: BaseLLM) -> TicketOutcome:
    intent = str(ticket["intent"])
    context = str(ticket.get("context", ""))
    expected_kra = ticket.get("expected_kra")
    expected_kind = ticket.get("expected_kind")

    start = time.perf_counter()
    result = generate_execution_plan(intent, context, llm=llm, kra_code=None)
    latency = time.perf_counter() - start

    plan = result.plan
    first_kind = plan.steps[0].action.kind if plan.steps else ActionKind.NOOP

    # Dry-run execution never touches the cloud; it proves the validated
    # plan is dispatchable end-to-end.
    exec_result = execute_plan(plan)

    hallucinated = (not result.valid) or bool(
        result.verification and result.verification.scope_warnings
    )
    kra_ok = (plan.kra_code == expected_kra) if expected_kra is not None else None
    kind_ok = (first_kind.value == expected_kind) if expected_kind is not None else None
    terraform_ok = None
    if expected_kind == "terraform":
        terraform_ok = any(
            s.status in ("dry_run", "executed") and s.kind is ActionKind.TERRAFORM
            for s in exec_result.steps
        )

    return TicketOutcome(
        ticket_id=str(ticket.get("id", "?")),
        intent_ok=bool(result.valid and result.verification and result.verification.passed),
        kra_ok=kra_ok,
        kind_ok=kind_ok,
        terraform_ok=terraform_ok,
        execution_ok=exec_result.ok,
        hallucinated=hallucinated,
        attempts=result.attempts,
        latency_s=latency,
        generated_by=result.generated_by,
    )


def load_fixture(path: Path, limit: int | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
            if limit is not None and len(rows) >= limit:
                break
    return rows


def render_markdown(summary: dict[str, Any], outcomes: list[TicketOutcome]) -> str:
    lat = summary["latency_s"]
    lines = [
        f"# LLM benchmark — provider `{summary['provider']}`",
        "",
        f"- Tickets: **{summary['total_tickets']}**",
        f"- Intent accuracy: **{summary['intent_accuracy']:.1%}**",
        f"- KRA accuracy: **{summary['kra_accuracy']:.1%}**",
        f"- Action-kind accuracy: **{summary['action_kind_accuracy']:.1%}**",
        f"- Terraform accuracy: **{summary['terraform_accuracy']:.1%}**",
        f"- Execution success (dry-run): **{summary['execution_success_rate']:.1%}**",
        f"- Hallucination rate: **{summary['hallucination_rate']:.1%}**",
        f"- Avg self-correction attempts: **{summary['avg_attempts']}**",
        f"- Latency (s): mean {lat['mean']} · p50 {lat['p50']} · p95 {lat['p95']}"
        f" · max {lat['max']}",
        f"- Token usage: {summary['token_usage']}",
        "",
        "| Ticket | intent | kra | kind | exec | hallucinated | attempts | latency_s | by |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for o in outcomes:
        lines.append(
            f"| {o.ticket_id} | {_mark(o.intent_ok)} | {_mark(o.kra_ok)} | {_mark(o.kind_ok)} "
            f"| {_mark(o.execution_ok)} | {_mark(o.hallucinated)} | {o.attempts} "
            f"| {o.latency_s:.3f} | {o.generated_by} |"
        )
    return "\n".join(lines) + "\n"


def _mark(v: bool | None) -> str:
    return "—" if v is None else ("✅" if v else "❌")


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark a reasoning provider on tickets")
    parser.add_argument("--fixture", default="evals/fixtures/llm_benchmark_seed.jsonl", type=Path)
    parser.add_argument("--provider", default=None, help="LLM provider (default: LLM_PROVIDER env)")
    parser.add_argument("--limit", default=None, type=int, help="Max tickets to run")
    parser.add_argument("--out", default="evals/reports", type=Path, help="Report output dir")
    args = parser.parse_args()

    tickets = load_fixture(args.fixture, args.limit)
    llm = get_provider(args.provider)
    logger.info("benchmark.start", provider=llm.provider, tickets=len(tickets))

    report = BenchmarkReport(provider=llm.provider, total=len(tickets))
    for ticket in tickets:
        outcome = _run_ticket(ticket, llm)
        report.outcomes.append(outcome)
        logger.info(
            "benchmark.ticket",
            id=outcome.ticket_id,
            intent_ok=outcome.intent_ok,
            attempts=outcome.attempts,
            latency_s=round(outcome.latency_s, 3),
        )

    summary = report.summary()
    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = args.out / f"llm_benchmark_{llm.provider}_{stamp}"
    (base.with_suffix(".json")).write_text(
        json.dumps({"summary": summary, "outcomes": [vars(o) for o in report.outcomes]}, indent=2),
        encoding="utf-8",
    )
    md = render_markdown(summary, report.outcomes)
    (base.with_suffix(".md")).write_text(md, encoding="utf-8")

    print(md)
    print(f"\nReports written to {base}.json / {base}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
