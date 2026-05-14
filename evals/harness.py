"""Eval harness — terraform apply -> chandra run -> recall/precision report.

Flow:

1. (Optional) ``terraform apply`` on ``iac/synthetic_env``.
2. ``terraform output -json seeds`` → ``{detector_id: expected_arn}``.
3. ``chandra run`` invokes the LangGraph pipeline end-to-end against the burner.
4. Findings are pulled from Postgres and joined against the manifest on
   ``(detector_id, resource_arn)``.
5. Recall, precision, FP list, per-KRA breakdown computed and written to
   ``evals/reports/{run_id}.json`` + ``{run_id}.md``.
6. Returns non-zero exit if recall_overall < 0.80 OR any per-KRA recall < 0.70.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from chandra.briefing.schemas import KRAS
from chandra.db.models import EvalRun, Finding as FindingRow
from chandra.db.session import session_scope
from chandra.graphs.chandra_graph import build_graph
from chandra.logging import get_logger

logger = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
TF_DIR = REPO_ROOT / "iac" / "synthetic_env"


# ---------------------------------------------------------------------------
# Terraform helpers
# ---------------------------------------------------------------------------


def _terraform(*args: str, cwd: Path = TF_DIR, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = ["terraform", *args]
    logger.info("eval.terraform", cmd=" ".join(cmd))
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
    )


def terraform_apply() -> None:
    _terraform("init", "-upgrade", "-input=false")
    _terraform("apply", "-auto-approve", "-input=false")


def terraform_seeds() -> dict[str, str]:
    """Return ``{detector_id: expected_arn}`` from ``terraform output``."""
    proc = _terraform("output", "-json", "seeds", check=True)
    data = json.loads(proc.stdout)
    if isinstance(data, dict):
        return {k: str(v) for k, v in data.items()}
    return {}


# ---------------------------------------------------------------------------
# Chandra run
# ---------------------------------------------------------------------------


def run_chandra(account_id: str) -> str:
    """Execute the LangGraph pipeline end-to-end and return the run_id."""
    run_id = str(uuid4())
    graph = build_graph()
    graph.invoke(
        {
            "run_id": run_id,
            "account_id": account_id,
            "regions": [],
            "raw_findings": {},
            "errors": [],
        },
        config={"configurable": {"thread_id": run_id}},
    )
    return run_id


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def load_manifest(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def fetch_findings(run_id: str) -> list[FindingRow]:
    with session_scope() as sess:
        rows = (
            sess.query(FindingRow).filter(FindingRow.run_id == run_id).all()
        )
        # Detach for use after session close.
        for r in rows:
            sess.expunge(r)
        return rows


def score(
    *,
    manifest: dict[str, Any],
    expected_arns: dict[str, str],
    findings: list[FindingRow],
) -> dict[str, Any]:
    """Compute recall, precision, FP list, and per-KRA breakdown.

    Match rule: a seed (detector_id D, arn A) is recalled if **any** finding
    has detector_id == D AND resource_arn == A. False positives are findings
    whose detector_id appears in the manifest but whose ARN doesn't match the
    expected ARN for that detector (i.e. unexpected resource flagged).
    """
    seeds: list[dict[str, Any]] = list(manifest.get("seeds", []))
    seed_by_id: dict[str, dict[str, Any]] = {s["detector_id"]: s for s in seeds}

    findings_by_detector: dict[str, list[FindingRow]] = {}
    for f in findings:
        findings_by_detector.setdefault(f.detector_id, []).append(f)

    recalled: list[str] = []
    missed: list[str] = []
    for detector_id, expected_arn in expected_arns.items():
        matches = findings_by_detector.get(detector_id, [])
        hit = any(f.resource_arn == expected_arn for f in matches)
        # For account-scoped detectors (e.g. COMP-002) the ARN won't match
        # exactly; accept "detector fired at all" as a recall.
        if not hit and detector_id in {"COMP-002-no-cloudtrail", "SEC-004-root-mfa"}:
            hit = bool(matches)
        if hit:
            recalled.append(detector_id)
        else:
            missed.append(detector_id)

    # Per-KRA recall.
    recall_per_kra: dict[str, float] = {}
    for kra in KRAS:
        ids_for_kra = [s["detector_id"] for s in seeds if s.get("kra") == kra]
        if not ids_for_kra:
            recall_per_kra[kra] = 1.0
            continue
        hits = sum(1 for d in ids_for_kra if d in recalled)
        recall_per_kra[kra] = round(hits / len(ids_for_kra), 4)

    recall_overall = (
        round(len(recalled) / max(1, len(expected_arns)), 4) if expected_arns else 0.0
    )

    # False positives: a finding whose detector_id IS expected, but whose ARN
    # does not match the manifest's expected ARN. This is the strict
    # interpretation — i.e. the detector fired on the wrong resource.
    false_positives: list[dict[str, Any]] = []
    for detector_id, expected_arn in expected_arns.items():
        for f in findings_by_detector.get(detector_id, []):
            if f.resource_arn == expected_arn:
                continue
            if detector_id in {"COMP-002-no-cloudtrail", "SEC-004-root-mfa"}:
                continue
            false_positives.append(
                {
                    "detector_id": detector_id,
                    "resource_arn": f.resource_arn,
                    "expected_arn": expected_arn,
                    "title": f.title,
                }
            )

    precision_overall = (
        round(
            len(recalled) / max(1, len(recalled) + len(false_positives)),
            4,
        )
        if (recalled or false_positives)
        else 1.0
    )

    thresholds = manifest.get("thresholds", {}) or {}
    failed_thresholds: list[str] = []
    if recall_overall < float(thresholds.get("recall_overall", 0.8)):
        failed_thresholds.append(
            f"recall_overall {recall_overall} < {thresholds.get('recall_overall', 0.8)}"
        )
    per_kra_threshold = float(thresholds.get("recall_per_kra", 0.7))
    for kra, value in recall_per_kra.items():
        if value < per_kra_threshold:
            failed_thresholds.append(f"recall[{kra}] {value} < {per_kra_threshold}")

    return {
        "recall_overall": recall_overall,
        "recall_per_kra": recall_per_kra,
        "precision_overall": precision_overall,
        "recalled": recalled,
        "missed": missed,
        "false_positives": false_positives,
        "failed_thresholds": failed_thresholds,
        "seed_count": len(expected_arns),
        "finding_count": len(findings),
        "seed_metadata": seed_by_id,
    }


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def render_report(run_id: str, account_id: str, result: dict[str, Any]) -> str:
    now = datetime.now(timezone.utc).isoformat()
    lines: list[str] = []
    lines.append(f"# Chandra Eval Report — {now}")
    lines.append("")
    lines.append(f"- run_id: `{run_id}`")
    lines.append(f"- account_id: `{account_id}`")
    lines.append(f"- seeds: {result['seed_count']}")
    lines.append(f"- findings: {result['finding_count']}")
    lines.append(f"- recall_overall: **{result['recall_overall']}**")
    lines.append(f"- precision_overall: **{result['precision_overall']}**")
    lines.append("")
    lines.append("## Per-KRA recall")
    lines.append("| KRA | Recall |")
    lines.append("| --- | ------ |")
    for kra, value in result["recall_per_kra"].items():
        lines.append(f"| {kra} | {value} |")
    lines.append("")
    lines.append("## Recalled")
    for d in result["recalled"]:
        lines.append(f"- ✅ {d}")
    if result["missed"]:
        lines.append("")
        lines.append("## Missed")
        for d in result["missed"]:
            lines.append(f"- ❌ {d}")
    if result["false_positives"]:
        lines.append("")
        lines.append("## False positives")
        for fp in result["false_positives"]:
            lines.append(
                f"- ⚠ {fp['detector_id']} fired on `{fp['resource_arn']}` "
                f"(expected `{fp['expected_arn']}`)"
            )
    if result["failed_thresholds"]:
        lines.append("")
        lines.append("## Failed thresholds")
        for t in result["failed_thresholds"]:
            lines.append(f"- 🚨 {t}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def persist_eval(run_id: str, result: dict[str, Any]) -> None:
    with session_scope() as sess:
        existing = (
            sess.query(EvalRun).filter(EvalRun.run_id == run_id).one_or_none()
        )
        if existing is None:
            sess.add(
                EvalRun(
                    run_id=run_id,
                    recall_overall=result["recall_overall"],
                    recall_per_kra_jsonb=result["recall_per_kra"],
                    precision_overall=result["precision_overall"],
                    fp_count=len(result["false_positives"]),
                )
            )
        else:
            existing.recall_overall = result["recall_overall"]
            existing.recall_per_kra_jsonb = result["recall_per_kra"]
            existing.precision_overall = result["precision_overall"]
            existing.fp_count = len(result["false_positives"])


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def run_eval(
    *,
    account_id: str,
    manifest_path: Path,
    apply_terraform: bool = False,
    report_dir: Path = Path("evals/reports"),
) -> int:
    """End-to-end eval. Returns the exit code (0 pass, 1 fail)."""
    if apply_terraform:
        terraform_apply()

    # Ensure SEC-003 surfaces on a fresh burner.
    os.environ.setdefault("CHANDRA_STALE_KEY_DAYS_OVERRIDE", "0")

    expected_arns = terraform_seeds()
    manifest = load_manifest(manifest_path)

    run_id = run_chandra(account_id)
    findings = fetch_findings(run_id)
    result = score(
        manifest=manifest,
        expected_arns=expected_arns,
        findings=findings,
    )
    persist_eval(run_id, result)

    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"{run_id}.json"
    md_path = report_dir / f"{run_id}.md"
    json_path.write_text(
        json.dumps({"run_id": run_id, "account_id": account_id, **result}, indent=2, default=str),
        encoding="utf-8",
    )
    md_path.write_text(render_report(run_id, account_id, result), encoding="utf-8")

    if result["failed_thresholds"]:
        logger.error("eval.failed", thresholds=result["failed_thresholds"])
        return 1
    logger.info(
        "eval.passed",
        recall_overall=result["recall_overall"],
        precision_overall=result["precision_overall"],
    )
    return 0


if __name__ == "__main__":
    account = os.environ.get("SYNTHETIC_ACCOUNT_ID")
    if not account:
        print("Set SYNTHETIC_ACCOUNT_ID first.", file=sys.stderr)
        sys.exit(2)
    sys.exit(
        run_eval(
            account_id=account,
            manifest_path=REPO_ROOT / "evals" / "seed_manifest.yaml",
            apply_terraform=False,
        )
    )
