"""Execution Verification Engine.

Generates and executes a verification script to prove that an automation
successfully achieved the desired cloud state.
"""

from __future__ import annotations

import builtins
import contextlib
import io
import json

import ast
from src.chandra.aws.client_factory import get_default_factory
from src.chandra.config import settings

FORBIDDEN_MODULES = {"os", "sys", "subprocess", "shlex", "pty", "socket"}
FORBIDDEN_FUNCTIONS = {"exec", "eval", "open", "compile"}

class SecurityError(Exception):
    pass

class SecurityVisitor(ast.NodeVisitor):
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.split(".")[0] in FORBIDDEN_MODULES:
                raise SecurityError(f"Import of {alias.name} is forbidden by policy.")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and node.module.split(".")[0] in FORBIDDEN_MODULES:
            raise SecurityError(f"Import from {node.module} is forbidden by policy.")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_FUNCTIONS:
                raise SecurityError(f"Function {node.func.id}() is forbidden by policy.")
        self.generic_visit(node)

def _validate_ast(code: str) -> None:
    tree = ast.parse(code)
    visitor = SecurityVisitor()
    visitor.visit(tree)
from src.chandra.digital_worker.schemas import (
    CloudRequest,
    ExecutionOutcome,
    RequestClassification,
    ResolutionPlan,
    ValidationCheck,
    ValidationResult,
)
from src.chandra.logging import get_logger
from src.chandra.observability.callbacks import UsageCapture

logger = get_logger(__name__)

VERIFICATION_PROMPT = """You are an Enterprise Cloud QA Engineer.
An automation script was just executed. Your job is to write a verification script to prove it worked.

Inputs provided in JSON:
- request: The original request.
- plan: Step-by-step resolution plan.
- execution_logs: The stdout/stderr from the automation script.

Execution Context:
- A configured `boto3` session is ALREADY INJECTED into the execution environment as `aws_session`.
- DO NOT configure credentials or region yourself.
- DO NOT use `boto3.client(...)`. INSTEAD, use `aws_session.client(...)`.
- The script should raise an `AssertionError` (e.g., `assert ...`) if the resource is missing or incorrect.
- If the assertion passes, print "Verification Passed".
- DO NOT import `os`, `sys`, or `subprocess`. They are blocked for security.

Output ONLY valid Python code inside a ```python block. No markdown around it.
"""


def _generate_verification_code(
    request: CloudRequest, plan: ResolutionPlan, execution_logs: str
) -> str:
    """Ask Bedrock to write the Python verification script."""
    try:
        from src.chandra.llm import build_chat_model
        import os
        llm = build_chat_model()
        
        payload = {
            "request": request.model_dump(mode="json", exclude={"raw_payload"}),
            "plan": plan.model_dump(mode="json"),
            "execution_logs": execution_logs,
        }
        
        cb = UsageCapture()
        response = llm.invoke(
            [
                ("system", VERIFICATION_PROMPT),
                ("user", json.dumps(payload, default=str)),
            ],
            config={"callbacks": [cb]},
        )
        
        code = response.content if isinstance(response.content, str) else str(response.content)
        
        if "```python" in code:
            code = code.split("```python")[1].split("```")[0].strip()
        elif "```" in code:
            code = code.split("```")[1].split("```")[0].strip()
            
        return code
    except Exception as exc:
        logger.error("llm.verification_code_generation_failed", error=str(exc))
        return ""


def verify_execution(
    request: CloudRequest,
    classification: RequestClassification,
    plan: ResolutionPlan,
    execution: ExecutionOutcome,
) -> ValidationResult:
    """Generate and run the verification script."""
    if execution.status == "skipped" or execution.dry_run:
        return ValidationResult(
            passed=True,
            checks=[ValidationCheck(name="dry_run_or_skipped", passed=True, detail="No verification needed.")],
        )

    if execution.status == "failed":
        return ValidationResult(
            passed=False,
            checks=[ValidationCheck(name="execution_failed", passed=False, detail="Automation failed before verification.")],
        )

    logger.info("verifier.generating_code", request_id=request.request_id)
    code = _generate_verification_code(request, plan, execution.execution_logs)
    
    if not code:
        return ValidationResult(
            passed=False,
            checks=[ValidationCheck(name="code_generation", passed=False, detail="Failed to generate verification script.")],
        )

    logger.info("verifier.validating_ast", request_id=request.request_id)
    try:
        _validate_ast(code)
    except Exception as exc:
        logger.error("verifier.security_validation_failed", error=str(exc))
        return ValidationResult(
            passed=False,
            checks=[ValidationCheck(name="security_validation", passed=False, detail=str(exc))],
            verification_code=code,
        )

    logger.info("verifier.running_code", request_id=request.request_id)
    
    safe_builtins = {k: v for k, v in builtins.__dict__.items() if k not in FORBIDDEN_FUNCTIONS}
    factory = get_default_factory()
    exec_globals = {
        "__builtins__": safe_builtins,
        "aws_session": factory.session,
    }
    
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    passed = True
    error_detail = ""

    try:
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            print("--- Starting Verification ---")
            exec(code, exec_globals)
            print("--- Verification Complete ---")
    except Exception as exc:
        passed = False
        error_detail = f"Assertion or runtime error: {exc}"
        print(f"VERIFICATION ERROR: {exc}", file=stderr_buf)
        logger.error("verifier.runtime_error", error=str(exc))

    logs = stdout_buf.getvalue() + "\n" + stderr_buf.getvalue()
    
    for line in logs.splitlines():
        if line.strip():
            logger.info(line.strip())

    checks = [
        ValidationCheck(
            name="cloud_state_assertion",
            passed=passed,
            detail=error_detail or "Verified successfully.",
        )
    ]

    return ValidationResult(
        passed=passed,
        checks=checks,
        verification_code=code,
        verification_logs=logs,
    )
