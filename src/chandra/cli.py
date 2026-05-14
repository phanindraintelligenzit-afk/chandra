"""Chandra command-line entrypoint.

Subcommands:

* ``chandra run``    — execute the LangGraph pipeline end-to-end against an account.
* ``chandra eval``   — run the eval harness against the synthetic env.
* ``chandra render`` — re-render the most recent briefing for an account from Postgres.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import uuid4

import typer
from rich.console import Console
from rich.table import Table

from chandra.config import settings
from chandra.db.models import Briefing, Run
from chandra.db.session import session_scope
from chandra.graphs.chandra_graph import build_graph
from chandra.logging import get_logger

app = typer.Typer(
    name="chandra",
    help="Chandra — Digital Cloud Engineer agent.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
logger = get_logger(__name__)


@app.command("run")
def run(
    account: str = typer.Option(
        ...,
        "--account",
        "-a",
        help="AWS account id to observe.",
        envvar="SYNTHETIC_ACCOUNT_ID",
    ),
    regions: list[str] = typer.Option(
        None,
        "--region",
        "-r",
        help="Override discovered region list (repeatable).",
    ),
    output_dir: Path = typer.Option(
        Path("evals/reports"),
        "--output-dir",
        "-o",
        help="Where to write the briefing markdown + JSON artifacts.",
    ),
) -> None:
    """Execute one full Chandra observation run."""
    run_id = str(uuid4())
    console.print(f"[bold cyan]chandra run[/] account={account} run_id={run_id}")

    graph = build_graph()
    initial = {
        "run_id": run_id,
        "account_id": account,
        "regions": regions or [],
        "raw_findings": {},
        "errors": [],
    }
    final_state = graph.invoke(
        initial,
        config={"configurable": {"thread_id": run_id}},
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / f"briefing-{run_id}.md"
    json_path = output_dir / f"briefing-{run_id}.json"
    md_path.write_text(final_state.get("briefing_md", ""), encoding="utf-8")
    json_path.write_text(
        json.dumps(final_state.get("briefing_json", {}), indent=2, default=str),
        encoding="utf-8",
    )

    scorecard = final_state.get("scorecard", {})
    _print_scorecard(scorecard)

    findings_count = sum(
        len(v) for v in (final_state.get("raw_findings", {}) or {}).values()
    )
    console.print(f"[green]Wrote[/] {md_path}")
    console.print(f"[green]Wrote[/] {json_path}")
    console.print(
        f"[bold]Done.[/] findings={findings_count} "
        f"errors={len(final_state.get('errors', []))}"
    )


@app.command("eval")
def eval_cmd(
    account: str = typer.Option(
        ...,
        "--account",
        "-a",
        help="AWS account id of the synthetic env.",
        envvar="SYNTHETIC_ACCOUNT_ID",
    ),
    manifest: Path = typer.Option(
        Path("evals/seed_manifest.yaml"),
        "--manifest",
        "-m",
        help="Path to seed_manifest.yaml.",
    ),
    apply_terraform: bool = typer.Option(
        False,
        "--apply",
        help="Run `terraform apply` on iac/synthetic_env before evaluating.",
    ),
    report_dir: Path = typer.Option(
        Path("evals/reports"),
        "--report-dir",
        help="Where to write the eval report.",
    ),
) -> None:
    """Run the eval harness against the synthetic env."""
    from evals.harness import run_eval  # local import: harness pulls in heavy deps

    exit_code = run_eval(
        account_id=account,
        manifest_path=manifest,
        apply_terraform=apply_terraform,
        report_dir=report_dir,
    )
    raise typer.Exit(code=exit_code)


@app.command("render")
def render_latest(
    account: str = typer.Option(..., "--account", "-a"),
    output: Path = typer.Option(
        Path("latest-briefing.md"),
        "--output",
        "-o",
    ),
) -> None:
    """Re-render the latest persisted briefing for an account."""
    with session_scope() as sess:
        run = (
            sess.query(Run)
            .filter(Run.account_id == account)
            .order_by(Run.started_at.desc())
            .first()
        )
        if run is None:
            console.print(f"[red]No runs found for account {account}.[/]")
            raise typer.Exit(code=1)
        briefing = (
            sess.query(Briefing).filter(Briefing.run_id == run.id).one_or_none()
        )
        if briefing is None:
            console.print(f"[red]Run {run.id} has no briefing row.[/]")
            raise typer.Exit(code=1)
        output.write_text(briefing.markdown_text, encoding="utf-8")
        console.print(f"[green]Wrote[/] {output} (run_id={run.id})")


def _print_scorecard(scorecard: dict[str, int]) -> None:
    if not scorecard:
        return
    table = Table(title="KRA Scorecard", show_header=True, header_style="bold")
    table.add_column("KRA")
    table.add_column("Score", justify="right")
    for kra in ("cost", "security", "compliance", "performance", "reliability"):
        table.add_row(kra, str(scorecard.get(kra, 0)))
    table.add_row("[bold]overall[/]", f"[bold]{scorecard.get('overall', 0)}[/]")
    console.print(table)


def main() -> None:
    """Console-script entrypoint."""
    if settings.log_level == "DEBUG":
        logger.debug("cli.start", argv=sys.argv)
    app()


if __name__ == "__main__":
    main()
