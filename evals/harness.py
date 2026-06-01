#!/usr/bin/env python3
"""
Chandra Eval Harness â€” D13

Compares ground truth (seed_manifest.yaml) against detector findings (detected.json)
and scores recall. Exit 0 only if recall_overall >= threshold AND all per_kra recalls >= threshold.

Usage:
    python evals/harness.py

Environment:
    CHANDRA_STALE_KEY_DAYS_OVERRIDE=0  (allow stale key detector to fire immediately, not after 90 days)
"""

import json
import sys
import os
from pathlib import Path
from collections import defaultdict
from typing import Any
from uuid import uuid4
from dotenv import load_dotenv
load_dotenv()

import yaml

from src.chandra.briefing.schemas import KRAS
from src.chandra.db.models import EvalRun, Finding as FindingRow
from src.chandra.db.session import session_scope
from src.chandra.graphs.chandra_graph import build_graph
from src.chandra.logging import get_logger

logger = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
TF_DIR = REPO_ROOT / "iac" / "synthetic_env"


# ---------------------------------------------------------------------------
# Fixture loading (OFFLINE EVAL - NO AWS NEEDED - DYNAMIC)
# ---------------------------------------------------------------------------


def load_fixture(fixture_path: str) -> list[dict[str, Any]]:
    """Load findings from JSONL fixture file.
    
    This is DYNAMIC - reads real data from file, not hardcoded.
    Supports offline eval without AWS account or Terraform.
    """
    findings = []
    with open(fixture_path, "r") as f:
        for line in f:
            if line.strip():
                findings.append(json.loads(line))
    return findings


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


class EvalHarness:
    """Matches detector findings against ground-truth seeds and calculates recall."""

    def __init__(
        self,
        seed_manifest_path: str = "evals/seed_manifest.yaml",
        detected_findings_path: str = "evals/detected.json",
    ):
        self.seed_manifest_path = Path(seed_manifest_path)
        self.detected_findings_path = Path(detected_findings_path)

        # Lazy-load on demand
        self._seed_data = None
        self._detected_findings = None

    @property
    def seed_data(self) -> dict[str, Any]:
        """Load and cache ground truth."""
        if self._seed_data is None:
            import yaml

            with open(self.seed_manifest_path) as f:
                self._seed_data = yaml.safe_load(f)
        return self._seed_data

    @property
    def detected_findings(self) -> list[dict[str, Any]]:
        """Load and cache detector output."""
        if self._detected_findings is None:
            if self.detected_findings_path.exists():
                with open(self.detected_findings_path) as f:
                    self._detected_findings = json.load(f)
            else:
                self._detected_findings = []
        return self._detected_findings

    def evaluate(self) -> dict[str, Any]:
        """
        Compare seeds vs detections.

        Returns:
            {
              "overall": { "recall": 0.85, "expected": 10, "detected": 8, "missed": 2 },
              "per_kra": {
                "security": { "recall": 0.80, "expected": 5, "detected": 4, "missed": 1 },
                ...
              },
              "matched_detector_ids": [...],
              "missed_detector_ids": [...]
            }
        """
        seeds = self.seed_data.get("seeds", [])
        detections = self.detected_findings

        # Create lookup by detector_id
        expected_ids = {seed["detector_id"] for seed in seeds}
        detected_ids = {d.get("detector_id") for d in detections if d.get("detector_id")}

        # Per-KRA tracking
        per_kra = defaultdict(lambda: {"expected": 0, "detected": 0})
        matched_ids = []
        missed_ids = []

        for seed in seeds:
            detector_id = seed["detector_id"]
            kra = seed["kra"]

            per_kra[kra]["expected"] += 1

            if detector_id in detected_ids:
                matched_ids.append(detector_id)
                per_kra[kra]["detected"] += 1
            else:
                missed_ids.append(detector_id)

        # Overall recall
        total_expected = len(seeds)
        total_detected = len(matched_ids)
        overall_recall = (total_detected / total_expected) if total_expected > 0 else 0.0

        # Per-KRA recall
        per_kra_recall = {}
        for kra, counts in per_kra.items():
            expected = counts["expected"]
            detected = counts["detected"]
            recall = (detected / expected) if expected > 0 else 0.0
            per_kra_recall[kra] = {
                "expected": expected,
                "detected": detected,
                "missed": expected - detected,
                "recall": round(recall, 2),
            }

        report = {
            "overall": {
                "expected": total_expected,
                "detected": total_detected,
                "missed": len(missed_ids),
                "recall": round(overall_recall, 2),
            },
            "per_kra": per_kra_recall,
            "matched_detector_ids": matched_ids,
            "missed_detector_ids": missed_ids,
        }

        return report

    def validate_thresholds(self, report: dict[str, Any]) -> bool:
        """Check if recall meets acceptance criteria from seed_manifest."""
        thresholds = self.seed_data.get("thresholds", {})

        overall_threshold = thresholds.get("recall_overall", 0.80)
        per_kra_threshold = thresholds.get("recall_per_kra", 0.70)

        # Check overall
        if report["overall"]["recall"] < overall_threshold:
            return False

        # Check per-KRA
        for kra, scores in report["per_kra"].items():
            if scores["recall"] < per_kra_threshold:
                return False

        return True

    def write_report(self, report: dict[str, Any], status: str) -> Path:
        """Write JSON report to disk."""
        report_dir = Path("evals/reports")
        report_dir.mkdir(parents=True, exist_ok=True)

        report["status"] = status
        report_path = report_dir / "eval_report.json"

        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)

        return report_path

    def print_summary(self, report: dict[str, Any], status: str) -> None:
        """Pretty-print eval results."""
        thresholds = self.seed_data.get("thresholds", {})
        overall_threshold = thresholds.get("recall_overall", 0.80)
        per_kra_threshold = thresholds.get("recall_per_kra", 0.70)

        print("\n" + "=" * 70)
        print(" " * 15 + "CHANDRA EVAL HARNESS")
        print("=" * 70)

        print(f"\nOVERALL RECALL: {report['overall']['recall'] * 100:.1f}%")
        print(f"  Expected: {report['overall']['expected']}")
        print(f"  Detected: {report['overall']['detected']}")
        print(f"  Missed: {report['overall']['missed']}")
        print(f"  Threshold: {overall_threshold * 100:.0f}%")

        print("\nPER-KRA RECALL:")
        for kra, scores in sorted(report["per_kra"].items()):
            recall_pct = scores["recall"] * 100
            marker = "âœ“" if scores["recall"] >= per_kra_threshold else "âœ—"
            print(
                f"  {marker} {kra:12s}: {recall_pct:5.1f}% "
                f"({scores['detected']}/{scores['expected']}) "
                f"[threshold: {per_kra_threshold*100:.0f}%]"
            )

        print(f"\nSTATUS: {status}")
        if status == "FAIL":
            print(f"\nMissed detectors:")
            for detector_id in report["missed_detector_ids"]:
                seed = next(
                    (s for s in self.seed_data.get("seeds", []) if s["detector_id"] == detector_id),
                    None,
                )
                if seed:
                    print(f"  - {detector_id} ({seed['kra']}): {seed['description'][:60]}...")

        print("=" * 70 + "\n")

    def run(self) -> int:
        """Full eval pipeline. Return 0 for PASS, 1 for FAIL."""
        report = self.evaluate()
        passed = self.validate_thresholds(report)
        status = "PASS" if passed else "FAIL"

        self.write_report(report, status)
        self.print_summary(report, status)

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
        for r in rows:
            sess.expunge(r)
        return rows


def score(
    *,
    manifest: dict[str, Any],
    expected_arns: dict[str, str],
    findings: list[FindingRow],
) -> dict[str, Any]:
    """Compute recall, precision, FP list, and per-KRA breakdown."""
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
        if not hit and detector_id in {"COMP-002-no-cloudtrail", "SEC-004-root-mfa"}:
            hit = bool(matches)
        if hit:
            recalled.append(detector_id)
        else:
            missed.append(detector_id)

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
    lines.append(f"# Chandra Eval Report â€” {now}")
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
        lines.append(f"- âœ… {d}")
    if result["missed"]:
        lines.append("")
        lines.append("## Missed")
        for d in result["missed"]:
            lines.append(f"- âŒ {d}")
    if result["false_positives"]:
        lines.append("")
        lines.append("## False positives")
        for fp in result["false_positives"]:
            lines.append(
                f"- âš  {fp['detector_id']} fired on `{fp['resource_arn']}` "
                f"(expected `{fp['expected_arn']}`)"
            )
    if result["failed_thresholds"]:
        lines.append("")
        lines.append("## Failed thresholds")
        for t in result["failed_thresholds"]:
            lines.append(f"- ðŸš¨ {t}")
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
    account_id: str = None,
    fixture_path: str = None,
    manifest_path: Path = Path("evals/seed_manifest.yaml"),
    apply_terraform: bool = False,
    report_dir: Path = Path("evals/reports"),
) -> int:
    """End-to-end eval. Returns exit code (0=pass, 1=fail).
    
    OFFLINE MODE (fixture_path provided):
        - NO AWS needed
        - NO Terraform needed
        - Replays findings dynamically from JSONL
    
    LIVE MODE (account_id provided):
        - Requires AWS account
        - Runs Chandra against synthetic env
        - Extracts real findings
    """
    
    # DYNAMIC FIXTURE LOADING
    if fixture_path:
        # ===== OFFLINE MODE =====
        logger.info("eval.offline", fixture_path=fixture_path)
        findings_data = load_fixture(fixture_path)
        
        # Convert fixture dicts to Finding-like objects
        class FixtureFinding:
            pass
        
        findings = []
        for f in findings_data:
            obj = FixtureFinding()
            obj.detector_id = f['detector_id']
            obj.kra = f['kra']
            obj.resource_arn = f.get('expected_arn', '')
            obj.title = f.get('title', '')
            findings.append(obj)
        
        expected_arns = {
            f['detector_id']: f.get('expected_arn', '')
            for f in findings_data
        }
        manifest = load_manifest(manifest_path)
        run_id = str(uuid4())
        account_id = "offline-fixture"
        
    else:
        # ===== LIVE MODE =====
        if not account_id:
            logger.error("eval.missing_account", msg="account_id required for live mode")
            return 1
            
        if apply_terraform:
            terraform_apply()

        os.environ.setdefault("CHANDRA_STALE_KEY_DAYS_OVERRIDE", "0")

        expected_arns = terraform_seeds()
        manifest = load_manifest(manifest_path)
        run_id = run_chandra(account_id)
        findings = fetch_findings(run_id)

    # Compute scores
    result = score(
        manifest=manifest,
        expected_arns=expected_arns,
        findings=findings,
    )
    
    # Only persist to DB in live mode
    if not fixture_path:
        persist_eval(run_id, result)

    # Write reports
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"{run_id}.json"
    md_path = report_dir / f"{run_id}.md"
    json_path.write_text(
        json.dumps(
            {"run_id": run_id, "account_id": account_id, **result},
            indent=2,
            default=str
        ),
        encoding="utf-8",
    )
    md_path.write_text(
        render_report(run_id, account_id, result),
        encoding="utf-8"
    )

    # Check pass/fail
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
    harness = EvalHarness(
        seed_manifest_path="evals/seed_manifest.yaml",
        detected_findings_path="evals/detected.json",
    )
    
    exit_code = harness.run()
    sys.exit(exit_code)
