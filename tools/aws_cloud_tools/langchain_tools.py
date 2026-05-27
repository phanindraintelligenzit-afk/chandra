"""LangChain @tool wrappers around async fetchers in the tools package."""

from __future__ import annotations

import asyncio
from typing import Any, Callable
import os
from langchain_core.tools import tool

from .tool_findings import run_all_detectors
from .account_audit import AWSOptimizationAndSecurityFetcher
from .aws_config import AWSConfigHistoryFetcher
from .budgets_fetcher import AWSBudgetsFetcher
from .cloud_trail_fetcher import AWSCloudTrailFetcher
from .cloudwatch_alarms_fetcher import AWSCloudWatchAlarmsFetcher
from .cost_explorer import AWSCostExplorerFetcher
from .guardduty_fetcher import AWSGuardDutyFetcher
from .health_events_fetcher import AWSHealthEventsFetcher
from .logs_fetcher import AWSCloudWatchLogsFetcher
from .metrics_fetcher import CloudWatchMetricsFetcher
from .xray_tracer import AWSXRayFetcher
from dotenv import load_dotenv
load_dotenv()

_metrics = CloudWatchMetricsFetcher()
_alarms = AWSCloudWatchAlarmsFetcher()
_health = AWSHealthEventsFetcher()
_budgets = AWSBudgetsFetcher()
_cloudtrail = AWSCloudTrailFetcher()
_cost = AWSCostExplorerFetcher()
_guardduty = AWSGuardDutyFetcher()
_logs = AWSCloudWatchLogsFetcher()
_account_audit = AWSOptimizationAndSecurityFetcher()
_config = AWSConfigHistoryFetcher()
_xray = AWSXRayFetcher()


def _run(coro):
    return asyncio.run(coro)


def _with_tool_name(result: dict[str, Any], name: str) -> dict[str, Any]:
    out = dict(result) if isinstance(result, dict) else {"data": result}
    out["Tool"] = name
    return out


@tool
def fetch_metrics_summary(
    region: str,
    last_hours: int = 6,
    period: int = 1800,
    namespace: str | None = None,
) -> dict:
    """Compact CloudWatch metrics summary for a region (optionally one namespace)."""
    result = _run(
        _metrics.fetch_metrics_summary(
            region=region,
            last_hours=last_hours,
            period=period,
            namespace=namespace,
        )
    )
    return _with_tool_name(result, "fetch_metrics_summary")


@tool
def fetch_alarms_summary(
    state_filter: str | None = None,
    max_total_alarms: int = 40,
) -> dict:
    """CloudWatch alarms summary across all regions."""
    result = _run(
        _alarms.fetch_alarms_summary(
            state_filter=state_filter,
            max_total_alarms=max_total_alarms,
        )
    )
    return _with_tool_name(result, "fetch_alarms_summary")


@tool
def fetch_recent_events(max_results: int = 20) -> dict:
    """AWS Health open issues and scheduled changes."""
    result = _run(_health.fetch_recent_events(max_results=max_results))
    return _with_tool_name(result, "fetch_recent_events")


@tool
def fetch_budget_status() -> dict:
    """AWS Budgets actual spend vs limits."""
    result = _run(_budgets.fetch_budget_status())
    return _with_tool_name(result, "fetch_budget_status")


@tool
def fetch_events_summary(
    last_hours: int = 2,
    event_name: str | None = None,
    max_total_events: int = 30,
) -> dict:
    """CloudTrail API events summary across regions."""
    result = _run(
        _cloudtrail.fetch_events_summary(
            last_hours=last_hours,
            event_name=event_name,
            max_total_events=max_total_events,
        )
    )
    return _with_tool_name(result, "fetch_events_summary")


@tool
def fetch_costs_summary(days_lookback: int = 7) -> dict:
    """AWS Cost Explorer daily spend by region and service."""
    result = _run(_cost.fetch_costs_summary(days_lookback=days_lookback))
    return _with_tool_name(result, "fetch_costs_summary")


@tool
def fetch_active_threats() -> dict:
    """GuardDuty active threats summary across regions."""
    result = _run(_guardduty.fetch_active_threats())
    return _with_tool_name(result, "fetch_active_threats")


@tool
def fetch_critical_errors(
    log_group_prefix: str | None = None,
    hours_lookback: int = 24,
    max_results: int = 50,
) -> dict:
    """CloudWatch Logs scan for ERROR, Exception, or Critical entries."""
    result = _run(
        _logs.fetch_critical_errors(
            log_group_prefix=log_group_prefix,
            hours_lookback=hours_lookback,
            max_results=max_results,
        )
    )
    return _with_tool_name(result, "fetch_critical_errors")


@tool
def fetch_global_logs(
    log_group_prefix: str | None = None,
    hours_lookback: int = 24,
    max_total_results: int = 20,
) -> dict:
    """Recent CloudWatch Logs across regions."""
    result = _run(
        _logs.fetch_global_logs(
            log_group_prefix=log_group_prefix,
            hours_lookback=hours_lookback,
            max_total_results=max_total_results,
        )
    )
    return _with_tool_name(result, "fetch_global_logs")


@tool
def fetch_account_audit(max_results: int = 20) -> dict:
    """Trusted Advisor-style security and cost optimization findings."""
    result = _run(_account_audit.fetch_account_audit(max_results=max_results))
    return _with_tool_name(result, "fetch_account_audit")


@tool
def check_recorder_status() -> dict:
    """AWS Config recorder on/off status per region."""
    result = _run(_config.check_recorder_status())
    return _with_tool_name(result, "check_recorder_status")


@tool
def fetch_recent_changes(
    hours_lookback: int = 24,
    max_total_results: int = 15,
) -> dict:
    """AWS Config resource configuration changes across regions."""
    result = _run(
        _config.fetch_recent_changes(
            hours_lookback=hours_lookback,
            max_total_results=max_total_results,
        )
    )
    return _with_tool_name(result, "fetch_recent_changes")


@tool
def fetch_global_xray_summary(
    hours_lookback: int = 8,
    only_errors: bool = False,
    max_traces_total: int = 50,
) -> dict:
    """X-Ray trace summary across all regions."""
    result = _run(
        _xray.fetch_global_xray_summary(
            hours_lookback=hours_lookback,
            only_errors=only_errors,
            max_traces_total=max_traces_total,
        )
    )
    return _with_tool_name(result, "fetch_global_xray_summary")


@tool
def fetch_all_findings() -> dict:
    """Compliance, security, reliability, performance and cost findings across all enabled AWS regions."""
    findings = _run(run_all_detectors())
    serialized = [f.model_dump() if hasattr(f, "model_dump") else f for f in findings]
    return _with_tool_name({"findings": serialized, "total": len(serialized)}, "fetch_all_findings")


TOOLS_LIST: list[Callable[..., Any]] = [
    # fetch_metrics_summary,
    fetch_alarms_summary,
    fetch_recent_events,
    fetch_budget_status,
    fetch_events_summary,
    fetch_costs_summary,
    fetch_active_threats,
    fetch_critical_errors,
    # fetch_global_logs,
    fetch_account_audit,
    check_recorder_status,
    fetch_recent_changes,
    fetch_global_xray_summary,
    # fetch_all_findings,
]

DEFAULT_REGION = os.getenv("AWS_DEFAULT_REGION")


def default_tool_args(tool_name: str, region: str = DEFAULT_REGION) -> dict:
    """Default kwargs for parallel tool invocation in the observability pipeline."""
    return {
        # "fetch_metrics_summary": {"region": region, "last_hours": 1},
        "fetch_alarms_summary": {"max_total_alarms": 20},
        "fetch_recent_events": {"max_results": 20},
        "fetch_budget_status": {},
        "fetch_events_summary": {"last_hours": 2, "max_total_events": 15},
        "fetch_costs_summary": {"days_lookback": 7},
        "fetch_active_threats": {},
        "fetch_critical_errors": {"hours_lookback": 12, "max_results": 20},
        # "fetch_global_logs": {"hours_lookback": 4, "max_total_results": 15},
        "fetch_account_audit": {"max_results": 20},
        "check_recorder_status": {},
        "fetch_recent_changes": {"hours_lookback": 12, "max_total_results": 15},
        "fetch_global_xray_summary": {"hours_lookback": 8, "max_traces_total": 20},
        # "fetch_all_findings": {},
    }.get(tool_name, {})
