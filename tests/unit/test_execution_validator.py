"""Structured execution-plan safety layer: schema, safety, verification, terraform."""

from __future__ import annotations

from unittest.mock import patch

from src.chandra.execution import (
    ExecutionPlan,
    validate_plan,
    validate_terraform,
    verify_intent_matches_plan,
)

# ---------------------------------------------------------------------------
# validate_plan — parsing + schema
# ---------------------------------------------------------------------------


def _good_plan() -> dict:
    return {
        "intent": "Block public access on S3 bucket acme-logs",
        "kra_code": "security",
        "dry_run": True,
        "steps": [
            {
                "order": 1,
                "requires_approval": True,
                "rationale": "close public exposure",
                "action": {
                    "kind": "aws_api",
                    "service": "s3",
                    "operation": "put_public_access_block",
                    "resource_id": "acme-logs",
                    "parameters": {"Bucket": "acme-logs"},
                },
            }
        ],
    }


class TestSchema:
    def test_valid_plan_parses(self) -> None:
        result = validate_plan(_good_plan())
        assert result.valid
        assert result.plan is not None
        assert result.plan.steps[0].action.service == "s3"

    def test_json_string_input(self) -> None:
        import json

        result = validate_plan(json.dumps(_good_plan()))
        assert result.valid

    def test_non_json_string_rejected(self) -> None:
        result = validate_plan("Sure! Here is the plan: run `aws s3 rm ...`")
        assert not result.valid
        assert any("parseable" in e for e in result.errors)

    def test_unknown_field_rejected(self) -> None:
        plan = _good_plan()
        plan["exploit"] = "extra"
        result = validate_plan(plan)
        assert not result.valid

    def test_unknown_action_kind_rejected(self) -> None:
        plan = _good_plan()
        plan["steps"][0]["action"]["kind"] = "shell"
        result = validate_plan(plan)
        assert not result.valid

    def test_plan_object_passthrough(self) -> None:
        model = ExecutionPlan.model_validate(_good_plan())
        result = validate_plan(model)
        assert result.valid


# ---------------------------------------------------------------------------
# validate_plan — safety / hallucination
# ---------------------------------------------------------------------------


class TestSafety:
    def test_disallowed_service_rejected(self) -> None:
        plan = _good_plan()
        plan["steps"][0]["action"]["service"] = "quantumcompute"
        result = validate_plan(plan)
        assert not result.valid
        assert any("allow-list" in e for e in result.errors)

    def test_shell_injection_in_parameter_rejected(self) -> None:
        plan = _good_plan()
        plan["steps"][0]["action"]["parameters"] = {"Bucket": "acme; rm -rf /"}
        result = validate_plan(plan)
        assert not result.valid
        assert any("shell metacharacters" in e for e in result.errors)

    def test_guidance_only_plan_is_valid_with_warning(self) -> None:
        plan = {
            "intent": "advise on cost",
            "steps": [{"order": 1, "action": {"kind": "noop", "note": "recommend reserved"}}],
        }
        result = validate_plan(plan)
        assert result.valid
        assert any("no actionable" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# verify_intent_matches_plan
# ---------------------------------------------------------------------------


class TestIntentVerification:
    def test_matching_plan_passes(self) -> None:
        result = validate_plan(_good_plan())
        assert result.plan is not None
        verdict = verify_intent_matches_plan(
            "Block public access on S3 bucket acme-logs", result.plan
        )
        assert verdict.passed

    def test_unrequested_destructive_op_flagged(self) -> None:
        plan = _good_plan()
        plan["intent"] = "tag the dev instances"
        plan["steps"][0]["action"] = {
            "kind": "aws_api",
            "service": "rds",
            "operation": "delete_db_instance",
            "resource_id": "prod-db",
            "parameters": {"DBInstanceIdentifier": "prod-db"},
        }
        result = validate_plan(plan)
        assert result.plan is not None
        verdict = verify_intent_matches_plan("tag the dev instances", result.plan)
        assert not verdict.passed
        assert any("destructive" in r for r in verdict.reasons)

    def test_offscope_resource_warns(self) -> None:
        result = validate_plan(_good_plan())
        assert result.plan is not None
        verdict = verify_intent_matches_plan("do something about billing", result.plan)
        # Not a hard fail, but the off-intent resource is surfaced.
        assert verdict.scope_warnings


# ---------------------------------------------------------------------------
# validate_terraform — degrades gracefully without the binary
# ---------------------------------------------------------------------------


class TestTerraform:
    def test_unavailable_when_binary_missing(self) -> None:
        with patch("src.chandra.execution.terraform.terraform_available", return_value=False):
            result = validate_terraform('resource "aws_s3_bucket" "b" {}')
        assert result.status == "unavailable"
        assert not result.ok  # unavailable is NEVER treated as valid

    def test_invalid_hcl_when_binary_present(self) -> None:
        # Simulate terraform present; fmt fails on malformed HCL.
        with (
            patch("src.chandra.execution.terraform.terraform_available", return_value=True),
            patch(
                "src.chandra.execution.terraform._run",
                return_value=(False, "Error: Invalid block definition"),
            ),
        ):
            result = validate_terraform("this is not valid hcl {{{")
        assert result.status == "invalid"
        assert not result.ok

    def test_valid_hcl_when_all_stages_pass(self) -> None:
        with (
            patch("src.chandra.execution.terraform.terraform_available", return_value=True),
            patch("src.chandra.execution.terraform._run", return_value=(True, "Success")),
        ):
            result = validate_terraform('resource "aws_s3_bucket" "b" { bucket = "x" }')
        assert result.status == "valid"
        assert result.ok
