#!/usr/bin/env python3
"""
Chandra Eval Harness — D13

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
            marker = "✓" if scores["recall"] >= per_kra_threshold else "✗"
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

        return 0 if passed else 1


def main():
    """CLI entry point."""
    harness = EvalHarness(
        seed_manifest_path="evals/seed_manifest.yaml",
        detected_findings_path="evals/detected.json",
    )
    return harness.run()


if __name__ == "__main__":
    sys.exit(main())