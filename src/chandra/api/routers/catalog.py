"""Digital Worker configuration catalogue (PRD L2 §§6-8, §26.8).

Approved tasks, permission sets, custom KRAs and worker settings. Second router
out of ``fastapi_app``: like governance it touches none of the shared job state,
so it moves without the risk the orchestration endpoints carry.

Reads are open; writes require CONFIGURE_WORKER. The asymmetry is deliberate -
these rows decide what a Digital Worker may ever do, so editing them is
privileged while the console needs to display them to everyone.

Postgres is the system of record (§26.8); the JSON files these endpoints used to
read and write are gone, and ConfigRepository is the only writer.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from src.chandra.api import deps
from src.chandra.catalog import DIGITAL_WORKER_SETTINGS_KEY
from src.chandra.governance import Capability
from src.chandra.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["catalog"])


class CustomKrasPayload(BaseModel):
    kras: list[dict[str, Any]] = Field(
        description="Full list of custom KRAs to persist. Replaces the tenant's set."
    )


class AwsTasksPayload(BaseModel):
    tasks: list[dict[str, Any]] = Field(description="List of AWS Tasks to persist.")


class PermissionSetsPayload(BaseModel):
    permissions: list[dict[str, Any]] = Field(description="List of permission sets to persist.")


class RecommendPermissionSetsPayload(BaseModel):
    required_permissions: list[dict[str, Any]]


class DigitalWorkerSettings(BaseModel):
    max_iterations: int = Field(default=5, description="Maximum agent loop iterations.")
    command_timeout: int = Field(default=300, description="Timeout for shell commands.")


AWS_ACTION_CATALOG = {
    "ec2": [
        "ec2:RunInstances",
        "ec2:StopInstances",
        "ec2:StartInstances",
        "ec2:TerminateInstances",
        "ec2:DescribeInstances",
        "ec2:DescribeSecurityGroups",
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:CreateTags",
        "ec2:CreateVolume",
        "ec2:AttachVolume",
    ],
    "s3": [
        "s3:CreateBucket",
        "s3:DeleteBucket",
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject",
        "s3:ListBucket",
        "s3:PutBucketPolicy",
        "s3:PutEncryptionConfiguration",
    ],
    "iam": [
        "iam:CreateUser",
        "iam:CreateRole",
        "iam:AttachUserPolicy",
        "iam:AttachRolePolicy",
        "iam:PutUserPolicy",
        "iam:GetUser",
        "iam:ListAttachedUserPolicies",
        "iam:PassRole",
    ],
    "lambda": [
        "lambda:CreateFunction",
        "lambda:UpdateFunctionCode",
        "lambda:UpdateFunctionConfiguration",
        "lambda:DeleteFunction",
        "lambda:GetFunction",
        "lambda:InvokeFunction",
        "lambda:CreateEventSourceMapping",
        "lambda:DeleteEventSourceMapping",
    ],
    "dynamodb": [
        "dynamodb:PutItem",
        "dynamodb:GetItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:Scan",
        "dynamodb:Query",
        "dynamodb:CreateTable",
    ],
    "cloudwatch": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "cloudwatch:PutMetricData",
    ],
    "vpc": [
        "ec2:CreateVpc",
        "ec2:CreateSubnet",
        "ec2:CreateRouteTable",
        "ec2:CreateInternetGateway",
        "ec2:DescribeVpcs",
        "ec2:DescribeSubnets",
        "ec2:DeleteVpc",
        "ec2:DeleteSubnet",
    ],
    "rds": [
        "rds:CreateDBInstance",
        "rds:DeleteDBInstance",
        "rds:ModifyDBInstance",
        "rds:DescribeDBInstances",
        "rds:CreateDBCluster",
        "rds:CreateDBSnapshot",
    ],
    "sqs": [
        "sqs:CreateQueue",
        "sqs:DeleteQueue",
        "sqs:SendMessage",
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes",
        "sqs:ListQueues",
    ],
    "sns": [
        "sns:CreateTopic",
        "sns:DeleteTopic",
        "sns:Publish",
        "sns:Subscribe",
        "sns:Unsubscribe",
        "sns:ListTopics",
        "sns:ListSubscriptions",
    ],
    "ecs": [
        "ecs:CreateCluster",
        "ecs:DeleteCluster",
        "ecs:RegisterTaskDefinition",
        "ecs:RunTask",
        "ecs:StartTask",
        "ecs:StopTask",
        "ecs:DescribeClusters",
    ],
    "elb": [
        "elasticloadbalancing:CreateLoadBalancer",
        "elasticloadbalancing:DeleteLoadBalancer",
        "elasticloadbalancing:RegisterTargets",
        "elasticloadbalancing:DescribeLoadBalancers",
    ],
    "cloudfront": [
        "cloudfront:CreateDistribution",
        "cloudfront:UpdateDistribution",
        "cloudfront:DeleteDistribution",
        "cloudfront:GetDistribution",
        "cloudfront:CreateInvalidation",
    ],
    "elasticache": [
        "elasticache:CreateCacheCluster",
        "elasticache:DeleteCacheCluster",
        "elasticache:DescribeCacheClusters",
        "elasticache:CreateReplicationGroup",
    ],
    "apigateway": [
        "apigateway:POST",
        "apigateway:GET",
        "apigateway:PUT",
        "apigateway:DELETE",
        "apigateway:PATCH",
    ],
    "kms": [
        "kms:CreateKey",
        "kms:Encrypt",
        "kms:Decrypt",
        "kms:GenerateDataKey",
        "kms:DescribeKey",
        "kms:ScheduleKeyDeletion",
    ],
    "secretsmanager": [
        "secretsmanager:CreateSecret",
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue",
        "secretsmanager:DeleteSecret",
    ],
    "route53": [
        "route53:CreateHostedZone",
        "route53:ChangeResourceRecordSets",
        "route53:ListHostedZones",
        "route53:ListResourceRecordSets",
    ],
    "stepfunctions": [
        "states:CreateStateMachine",
        "states:UpdateStateMachine",
        "states:DeleteStateMachine",
        "states:StartExecution",
        "states:DescribeExecution",
    ],
    "athena": [
        "athena:StartQueryExecution",
        "athena:GetQueryExecution",
        "athena:GetQueryResults",
        "athena:CreateWorkGroup",
    ],
}


COMMON_RESOURCE_ARNS = [
    "arn:aws:s3:::*",
    "arn:aws:ec2:*:*:instance/*",
    "arn:aws:ec2:*:*:security-group/*",
    "arn:aws:iam::*:user/*",
    "arn:aws:iam::*:role/*",
]

# ── Custom KRAs ──────────────────────────────────────────────────────────────


@router.get("/customKras")
def get_custom_kras() -> JSONResponse:
    """Read all custom KRAs for the tenant."""
    entries = deps.config_repo().list_custom_kras()
    return JSONResponse(
        status_code=200,
        content={"status": "success", "count": len(entries), "kras": entries},
    )


@router.put("/customKras")
def put_custom_kras(payload: CustomKrasPayload, request: Request) -> JSONResponse:
    """Replace the tenant's custom KRAs with the supplied list."""
    deps.require_capability(request, Capability.CONFIGURE_WORKER)
    try:
        written = deps.config_repo().replace_custom_kras(payload.kras)
    except Exception as exc:
        logger.exception("Failed to write custom KRAs: %s", exc)
        return JSONResponse(status_code=500, content={"status": "error", "exception": str(exc)})
    return JSONResponse(
        status_code=200,
        content={"status": "success", "count": written, "message": f"Saved {written} custom KRAs"},
    )


# ── Approved task catalogue ──────────────────────────────────────────────────


@router.get("/api/aws-tasks")
@router.get("/aws-tasks")
def get_aws_tasks() -> JSONResponse:
    tasks = deps.config_repo().list_aws_tasks()
    return JSONResponse(
        status_code=200, content={"status": "success", "count": len(tasks), "tasks": tasks}
    )


@router.put("/api/aws-tasks")
@router.put("/aws-tasks")
def put_aws_tasks(payload: AwsTasksPayload, request: Request) -> JSONResponse:
    deps.require_capability(request, Capability.CONFIGURE_WORKER)
    try:
        written = deps.config_repo().replace_aws_tasks(payload.tasks)
    except Exception as exc:
        logger.exception("Failed to write AWS tasks: %s", exc)
        return JSONResponse(status_code=500, content={"status": "error", "exception": str(exc)})
    return JSONResponse(
        status_code=200,
        content={"status": "success", "count": written, "message": f"Saved {written} AWS tasks"},
    )


# ── Permission sets ──────────────────────────────────────────────────────────


@router.get("/api/permission-sets")
def get_permission_sets() -> JSONResponse:
    perms = deps.config_repo().list_permission_sets()
    return JSONResponse(
        status_code=200,
        content={"status": "success", "count": len(perms), "permissions": perms},
    )


@router.put("/api/permission-sets")
def put_permission_sets(payload: PermissionSetsPayload, request: Request) -> JSONResponse:
    deps.require_capability(request, Capability.CONFIGURE_WORKER)
    try:
        written = deps.config_repo().replace_permission_sets(payload.permissions)
    except Exception as exc:
        logger.exception("Failed to write permission sets: %s", exc)
        return JSONResponse(status_code=500, content={"status": "error", "exception": str(exc)})
    return JSONResponse(
        status_code=200,
        content={
            "status": "success",
            "count": written,
            "message": f"Saved {written} permission sets",
        },
    )


@router.post("/api/permission-sets/recommend")
def recommend_permission_sets(payload: RecommendPermissionSetsPayload) -> JSONResponse:
    """Suggest an existing permission set for a set of required actions.

    Advisory only. The recommendation is a starting point for a human choosing a
    permission set; Gate 1 still verifies whatever is actually attached against
    the catalogue, so an LLM suggestion here can never widen what a run may do.
    """
    try:
        import json

        import json_repair
        from src.chandra.llm import get_llm

        perms = deps.config_repo().list_permission_sets()
        llm = get_llm()
        prompt = (
            "You are an AWS IAM expert. Given a list of required permissions and a list of "
            "existing permission sets, recommend the BEST existing permission set that covers "
            "all required permissions using wildcard matching. If no existing permission set "
            "is adequate, suggest creating a new one. Return a JSON object with: "
            "1. 'recommendation_type': 'existing' or 'new' "
            "2. 'permission_set_id': The ID of the existing set if 'existing', else null "
            "3. 'reason': Why this set is recommended, or why a new one is needed "
            "4. 'suggested_new_set': If 'new', provide a suggested name and actions array."
        )
        response = llm.invoke(
            [
                ("system", prompt),
                (
                    "user",
                    json.dumps(
                        {
                            "required_permissions": payload.required_permissions,
                            "existing_sets": perms,
                        }
                    ),
                ),
            ]
        )
        text = response.content if isinstance(response.content, str) else str(response.content)
        parsed = json_repair.loads(text)
        return JSONResponse(
            status_code=200, content={"status": "success", "recommendation": parsed}
        )
    except Exception as exc:
        logger.exception("Failed to recommend permission sets: %s", exc)
        return JSONResponse(status_code=500, content={"status": "error", "exception": str(exc)})


@router.get("/api/permission-sets/actions")
def get_permission_actions(aws_service: str | None = Query(default=None)) -> JSONResponse:
    if aws_service:
        service_key = aws_service.lower().strip()
        return JSONResponse(
            status_code=200,
            content={"service": service_key, "actions": AWS_ACTION_CATALOG.get(service_key, [])},
        )
    all_actions = [act for actions in AWS_ACTION_CATALOG.values() for act in actions]
    return JSONResponse(status_code=200, content={"actions": all_actions})


@router.get("/api/permission-sets/resource-arns")
def get_resource_arns() -> JSONResponse:
    return JSONResponse(status_code=200, content={"resource_arns": COMMON_RESOURCE_ARNS})


# ── Worker settings ──────────────────────────────────────────────────────────


@router.get("/settings/digital-worker", response_model=DigitalWorkerSettings)
def get_digital_worker_settings() -> DigitalWorkerSettings:
    """Read the tenant's digital worker settings (Postgres tenant_settings)."""
    try:
        data = deps.config_repo().get_setting(DIGITAL_WORKER_SETTINGS_KEY)
        if data:
            return DigitalWorkerSettings(**data)
    except Exception as exc:
        logger.warning("Failed to load digital worker settings: %s", exc)
    return DigitalWorkerSettings()


@router.post("/settings/digital-worker")
def update_digital_worker_settings(
    settings_payload: DigitalWorkerSettings, request: Request
) -> Any:
    """Update the tenant's digital worker settings."""
    deps.require_capability(request, Capability.CONFIGURE_WORKER)
    try:
        deps.config_repo().put_setting(DIGITAL_WORKER_SETTINGS_KEY, settings_payload.model_dump())
        return {"status": "success"}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})
