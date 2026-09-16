"""Request models shared by more than one router.

``ActionInput`` is used by both the analyzer (``/analyzeActions``) and
orchestration (``/orchestrate``), so it cannot live inside either router without
one importing the other. The camelCase field names are the console's existing
wire contract, not new style.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field


class ActionInput(BaseModel):
    actionName: str = Field(description="Short name of the action")
    actionDescription: str = Field(description="Detailed description of what needs to be done")
    service: str | None = Field(default="AWS", description="AWS service this action applies to")
    kraCode: str | None = Field(default=None, description="KRA identifier (e.g. KRA-01)")
    priorityLevel: str | None = Field(default=None, description="Priority level (e.g. P1)")
    steps: list[str] | None = Field(
        default=None, description="Implementation steps to add as a Jira comment"
    )
    detectorId: str | None = Field(
        default=None, description="Detector ID for predefined KRA actions"
    )
    resourceArn: str | None = Field(
        default=None, description="Target resource ARN for predefined KRA actions"
    )
    region: str | None = Field(
        default=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        description="Target region for predefined KRA actions",
    )
    action_type: str | None = Field(
        default="KRA_REMEDIATION",
        description="Execution path discriminator. Either AWS_TASK or KRA_REMEDIATION.",
    )
    permission_set_id: str | None = Field(
        default=None, description="AWS permission set selected during onboarding"
    )
