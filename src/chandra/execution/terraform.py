"""Terraform validation gate.

Model-generated Terraform is never trusted as-is. Before any executor is
allowed to apply a ``TerraformAction``, its HCL must pass, in order:

    terraform fmt -check   (syntax / formatting)
    terraform init         (provider + module resolution, no backend)
    terraform validate     (semantic validity)
    terraform plan         (only when AWS credentials are available)

Any failing stage rejects the plan. When the ``terraform`` binary is not
installed in the runtime, the result is ``status="unavailable"`` — never
``valid`` — so a missing tool can never be mistaken for a passing check.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from src.chandra.logging import get_logger

logger = get_logger(__name__)

_STAGE_TIMEOUT_S = 120


class TerraformStage(BaseModel):
    name: str
    passed: bool
    output: str = ""


class TerraformValidation(BaseModel):
    status: Literal["valid", "invalid", "unavailable"]
    stages: list[TerraformStage] = []
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "valid"


def terraform_available() -> bool:
    return shutil.which("terraform") is not None


def _run(args: list[str], cwd: Path) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["terraform", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_STAGE_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"terraform {' '.join(args)} failed to run: {exc}"
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, output[:4000]


def validate_terraform(hcl: str, *, run_plan: bool = False, workdir: str | None = None) -> TerraformValidation:
    """Validate a block of Terraform HCL. ``run_plan`` gates the (credential-
    requiring) ``terraform plan`` stage; leave False in unit/offline runs."""
    if not terraform_available():
        logger.warning("terraform.binary_unavailable")
        return TerraformValidation(
            status="unavailable",
            detail="terraform binary not installed; cannot validate HCL before apply",
        )

    stages: list[TerraformStage] = []
    
    import contextlib
    @contextlib.contextmanager
    def _get_workdir():
        if workdir:
            yield Path(workdir)
        else:
            with tempfile.TemporaryDirectory(prefix="chandra-tf-") as tmp:
                wd = Path(tmp)
                (wd / "main.tf").write_text(hcl, encoding="utf-8")
                yield wd

    with _get_workdir() as wd:

        plan_stages = [
            ("fmt", ["fmt", "-check", "-diff"]),
            ("init", ["init", "-backend=false", "-input=false", "-no-color"]),
            ("validate", ["validate", "-no-color"]),
        ]
        if run_plan:
            plan_stages.append(("plan", ["plan", "-input=false", "-no-color", "-lock=false"]))

        for name, args in plan_stages:
            passed, output = _run(args, wd)
            stages.append(TerraformStage(name=name, passed=passed, output=output))
            if not passed:
                logger.warning("terraform.stage_failed", stage=name)
                return TerraformValidation(
                    status="invalid",
                    stages=stages,
                    detail=f"terraform {name} failed",
                )

    logger.info("terraform.validated", stages=len(stages))
    return TerraformValidation(status="valid", stages=stages)
