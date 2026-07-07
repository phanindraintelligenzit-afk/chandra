"""
Standalone MCP probe — deliberately NOT meant to be imported/run inside a
Jupyter kernel on Windows. Run it as a plain script:

    python mcp_probe.py

It connects to both servers, lists their tools, optionally calls one test
tool per server, and writes everything to mcp_probe_result.json in the same
directory. A notebook cell then just shells out to this script and reads
the JSON back — that's the "codes that work in Jupyter" part, since the
actual subprocess creation never happens inside the ipykernel process.

Why this is necessary on Windows: ipykernel's stdout/stderr are wrapped in
OutStream objects that don't implement fileno(). MCP's Windows stdio
transport needs a real OS file handle for the child process's stderr, so it
crashes with `io.UnsupportedOperation: fileno` when run inside a notebook
kernel. Running this file with a normal `python mcp_probe.py` sidesteps the
issue entirely because sys.stderr in a plain script is a real file object.

Terraform MCP server:
    Runs as a local binary (no Docker required) — point TERRAFORM_MCP_BINARY
    at your terraform-mcp-server.exe. The binary takes a `stdio` subcommand
    directly (unlike the Docker image's `run -i --rm ...` wrapper).

AWS API MCP server:
    Runs via `uvx awslabs.aws-api-mcp-server@latest`. Authenticates using
    boto3's standard credential chain — environment variables are checked
    first, so AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (and AWS_SESSION_TOKEN
    if using temporary/STS credentials) set in THIS process's environment
    are inherited by the subprocess and used automatically. No extra config
    needed beyond having them set before running this script.

Logging:
    Uses the stdlib `logging` module instead of print(). Console handler at
    INFO (readable run summary), file handler at DEBUG (everything, including
    full raw payloads and tracebacks) writing to mcp_probe.log next to this
    script. Set MCP_PROBE_LOG_LEVEL=DEBUG to also see full detail on console.
"""

import asyncio
import json
import logging
import os
import re
import sys

from langchain_mcp_adapters.client import MultiServerMCPClient

# ── Logging setup ────────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG_PATH = os.path.join(_SCRIPT_DIR, "mcp_probe.log")

logger = logging.getLogger("mcp_probe")
logger.setLevel(logging.DEBUG)  # capture everything; handlers filter what's shown/written

_console_level = os.getenv("MCP_PROBE_LOG_LEVEL", "INFO").upper()
_formatter = logging.Formatter(
    fmt="%(asctime)s [%(levelname)-7s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setLevel(getattr(logging, _console_level, logging.INFO))
_console_handler.setFormatter(_formatter)
logger.addHandler(_console_handler)

# File handler: always DEBUG, overwritten fresh each run so it reflects only
# the latest attempt (mode="w"). Switch to mode="a" if you want history.
_file_handler = logging.FileHandler(_LOG_PATH, mode="w", encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(_formatter)
logger.addHandler(_file_handler)

logger.propagate = False

logger.info("Logging to console at %s, full DEBUG log written to %s", _console_level, _LOG_PATH)

# ── AWS region/credentials ──────────────────────────────────────────────
# AWS_REGION / AWS_DEFAULT_REGION: set a default if not already present in
# the environment. Do NOT hardcode AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
# here — set them in your actual environment (PowerShell/.env/CI secrets)
# and this script will pick them up via os.getenv / subprocess inheritance.
os.environ["AWS_REGION"] = os.getenv("AWS_REGION", "us-east-1")
os.environ["AWS_DEFAULT_REGION"] = os.getenv("AWS_DEFAULT_REGION", os.environ["AWS_REGION"])
logger.debug("AWS_REGION=%s AWS_DEFAULT_REGION=%s", os.environ["AWS_REGION"], os.environ["AWS_DEFAULT_REGION"])

for _required in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
    if not os.getenv(_required):
        logger.warning(
            "%s is not set in the environment. The aws_api MCP server will fall back to "
            "~/.aws/credentials, an AWS_PROFILE, or an instance/task role if one is available. "
            "If none of those apply, AWS calls will fail.",
            _required,
        )
    else:
        logger.debug("%s is set (value not logged)", _required)

# ── Terraform MCP binary path ───────────────────────────────────────────
# Resolve relative to this script's directory so it works regardless of the
# current working directory you run `python mcp_probe.py` from. Adjust the
# subpath if your binary lives somewhere else.
TERRAFORM_MCP_BINARY = os.getenv(
    "TERRAFORM_MCP_BINARY",
    os.path.join(_SCRIPT_DIR, "terraform", "terraform-mcp-server.exe"),
)

if not os.path.isfile(TERRAFORM_MCP_BINARY):
    logger.warning(
        "terraform-mcp-server binary not found at '%s'. "
        "Set TERRAFORM_MCP_BINARY env var or fix the path.",
        TERRAFORM_MCP_BINARY,
    )
else:
    logger.debug("terraform-mcp-server binary found at '%s'", TERRAFORM_MCP_BINARY)

SERVER_CONFIG = {
    "terraform": {
        "command": TERRAFORM_MCP_BINARY,
        "args": ["stdio"],
        "transport": "stdio",
    },
    "aws_api": {
        "command": "uvx",
        "args": ["awslabs.aws-api-mcp-server@latest"],
        # Explicitly pass through AWS env vars to the subprocess. uvx/boto3
        # would inherit the parent environment anyway on most setups, but
        # being explicit here makes the credential source unambiguous and
        # avoids surprises if MultiServerMCPClient ever sandboxes env vars.
        "env": {
            "AWS_REGION": os.getenv("AWS_REGION", "us-east-1"),
            "AWS_DEFAULT_REGION": os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
            **({"AWS_ACCESS_KEY_ID": os.getenv("AWS_ACCESS_KEY_ID")}
               if os.getenv("AWS_ACCESS_KEY_ID") else {}),
            **({"AWS_SECRET_ACCESS_KEY": os.getenv("AWS_SECRET_ACCESS_KEY")}
               if os.getenv("AWS_SECRET_ACCESS_KEY") else {}),
            **({"AWS_SESSION_TOKEN": os.getenv("AWS_SESSION_TOKEN")}
               if os.getenv("AWS_SESSION_TOKEN") else {}),
            **({"AWS_PROFILE": os.getenv("AWS_PROFILE")}
               if os.getenv("AWS_PROFILE") else {}),
        },
        "transport": "stdio",
    },
}

logger.debug("SERVER_CONFIG (command/args/transport only, env omitted): %s", {
    name: {k: v for k, v in cfg.items() if k != "env"}
    for name, cfg in SERVER_CONFIG.items()
})

# Terraform MCP server's docs flow is a two-step lookup:
#   1. search_providers(provider_name, provider_namespace, service_slug, provider_document_type)
#      -> numeric provider_doc_id
#   2. get_provider_details(provider_doc_id) -> actual documentation
# (Note: params are snake_case; camelCase is silently ignored by the server.)
QUOTAS_TOOL_NAME = "call_aws"
SEARCH_PROVIDERS_TOOL_NAME = "search_providers"
GET_PROVIDER_DETAILS_TOOL_NAME = "get_provider_details"


async def probe() -> dict:
    client = MultiServerMCPClient(SERVER_CONFIG)
    result: dict = {"servers": {}, "quotas_test": None, "docs_test": None, "errors": []}

    all_tools = {}
    for server_name in SERVER_CONFIG:
        logger.info("[%s] connecting and listing tools...", server_name)
        try:
            tools = await client.get_tools(server_name=server_name)
            all_tools[server_name] = {t.name: t for t in tools}
            result["servers"][server_name] = [
                {"name": t.name, "description": (t.description or "").strip()}
                for t in tools
            ]
            logger.info("[%s] OK — %d tool(s) found: %s", server_name, len(tools), [t.name for t in tools])
            for t in tools:
                logger.debug("[%s] tool '%s' description: %s", server_name, t.name, (t.description or "").strip())
                if server_name == "terraform" and t.name in (
                    SEARCH_PROVIDERS_TOOL_NAME, GET_PROVIDER_DETAILS_TOOL_NAME,
                ):
                    try:
                        logger.debug("[%s] tool '%s' schema: %s", server_name, t.name, json.dumps(t.args, indent=2))
                    except Exception:
                        logger.debug("[%s] could not read schema for '%s'", server_name, t.name, exc_info=True)
        except Exception as exc:
            msg = f"{server_name}.get_tools failed: {exc}"
            result["errors"].append(msg)
            logger.error("[%s] FAILED to connect/list tools", server_name, exc_info=True)

    # Optional: exercise one tool per server so you know calls work, not just listing.
    logger.info("[aws_api] testing tool '%s'...", QUOTAS_TOOL_NAME)
    aws_tool = all_tools.get("aws_api", {}).get(QUOTAS_TOOL_NAME)
    if aws_tool is not None:
        try:
            region = os.getenv("AWS_REGION", "us-east-1")
            cli_command = f"aws service-quotas list-service-quotas --service-code lambda --max-results 5 --region {region}"
            logger.debug("[aws_api] invoking call_aws with cli_command=%r", cli_command)
            raw = await aws_tool.ainvoke({"cli_command": cli_command})
            result["quotas_test"] = raw if isinstance(raw, str) else json.dumps(raw, default=str)
            logger.info("[aws_api] OK — got %d chars back", len(result["quotas_test"]))
            logger.debug("[aws_api] full response: %s", result["quotas_test"])
        except Exception as exc:
            msg = f"quotas call failed: {exc}"
            result["errors"].append(msg)
            logger.error("[aws_api] call_aws FAILED", exc_info=True)
    elif "aws_api" in all_tools:
        msg = f"tool '{QUOTAS_TOOL_NAME}' not found on aws_api server"
        result["errors"].append(msg)
        logger.warning("[aws_api] SKIPPED: %s", msg)
    else:
        logger.warning("[aws_api] SKIPPED: server itself unavailable (see connection error above)")

    logger.info("[terraform] testing '%s' -> '%s'...", SEARCH_PROVIDERS_TOOL_NAME, GET_PROVIDER_DETAILS_TOOL_NAME)
    search_tool = all_tools.get("terraform", {}).get(SEARCH_PROVIDERS_TOOL_NAME)
    details_tool = all_tools.get("terraform", {}).get(GET_PROVIDER_DETAILS_TOOL_NAME)

    if search_tool is None or details_tool is None:
        if "terraform" in all_tools:
            missing = []
            if search_tool is None:
                missing.append(SEARCH_PROVIDERS_TOOL_NAME)
            if details_tool is None:
                missing.append(GET_PROVIDER_DETAILS_TOOL_NAME)
            msg = f"tool(s) not found on terraform server: {', '.join(missing)}"
            result["errors"].append(msg)
            logger.warning("[terraform] SKIPPED: %s", msg)
            logger.warning("[terraform] Actual tools available: %s", list(all_tools.get("terraform", {}).keys()))
        else:
            logger.warning("[terraform] SKIPPED: server itself unavailable (see connection error above)")
    else:
        try:
            search_args = {
                "provider_name": "aws",
                "provider_namespace": "hashicorp",
                "service_slug": "aws_lambda_function",
                "provider_document_type": "resources",
            }
            logger.info("[terraform] step 1: search_providers(%s)...", search_args)
            search_raw = await search_tool.ainvoke(search_args)
            search_text = search_raw if isinstance(search_raw, str) else json.dumps(search_raw, default=str)
            logger.info("[terraform] step 1 OK — %d chars back", len(search_text))
            logger.debug("[terraform] step 1 full raw response: %s", search_text)

            # The response is an MCP "text content" envelope:
            #   [{"type": "text", "text": "<plain text body>", "id": "lc_..."}]
            # The real IDs live INSIDE that plain-text body, formatted as
            # "- providerDocID: 12704959" lines — not as JSON object fields.
            # (Confirmed from actual server output; the earlier JSON-dict-key
            # extraction attempt was wrong because this body is prose, not JSON.)
            provider_doc_id = None
            inner_text = search_text
            try:
                parsed = json.loads(search_text)
                if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) and "text" in parsed[0]:
                    inner_text = parsed[0]["text"]
                    logger.debug("[terraform] unwrapped MCP text envelope, inner text length=%d", len(inner_text))
                elif isinstance(parsed, dict) and "text" in parsed:
                    inner_text = parsed["text"]
            except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
                logger.debug("[terraform] step 1 response was not a JSON envelope — treating as raw text", exc_info=True)

            match = re.search(r"providerDocID:\s*(\d+)", inner_text)
            if match:
                provider_doc_id = match.group(1)
                logger.debug("[terraform] extracted provider_doc_id=%s via regex from inner text", provider_doc_id)
            else:
                logger.debug("[terraform] no 'providerDocID: <digits>' pattern found in inner text")

            if not provider_doc_id:
                msg = (
                    "search_providers ran but no numeric provider_doc_id was found in "
                    "its response — check the DEBUG log for the full step 1 raw output "
                    "and adjust extraction accordingly"
                )
                result["errors"].append(msg)
                logger.warning("[terraform] step 2 SKIPPED: %s", msg)
                result["docs_test"] = search_text
            else:
                logger.info("[terraform] step 2: get_provider_details(provider_doc_id=%s)...", provider_doc_id)
                details_raw = await details_tool.ainvoke({"provider_doc_id": provider_doc_id})
                result["docs_test"] = details_raw if isinstance(details_raw, str) else json.dumps(details_raw, default=str)
                logger.info("[terraform] step 2 OK — %d chars back", len(result["docs_test"]))
                logger.debug("[terraform] step 2 full raw response: %s", result["docs_test"])
        except Exception as exc:
            msg = f"docs call failed: {exc}"
            result["errors"].append(msg)
            logger.error("[terraform] docs flow FAILED", exc_info=True)

    return result


def main() -> None:
    try:
        result = asyncio.run(probe())
    except Exception:
        logger.critical("probe() raised an unhandled exception — aborting", exc_info=True)
        raise

    out_path = os.path.join(_SCRIPT_DIR, "mcp_probe_result.json")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        logger.info("Wrote %s", out_path)
    except Exception:
        logger.error("Failed to write %s", out_path, exc_info=True)

    if result["errors"]:
        logger.warning("=== SUMMARY: %d error(s) encountered ===", len(result["errors"]))
        for e in result["errors"]:
            logger.warning("  - %s", e)
    else:
        logger.info("=== SUMMARY: no errors ===")

    logger.info("=== SERVERS (tool names + descriptions) ===")
    logger.info("%s", json.dumps(result["servers"], indent=2))

    logger.info("=== docs_test (full) ===")
    logger.info("%s", result["docs_test"] or "(empty)")

    logger.info("=== quotas_test (full) ===")
    logger.info("%s", result["quotas_test"] or "(empty)")

    logger.info("Full run detail (including all DEBUG-level payloads) is in %s", _LOG_PATH)


if __name__ == "__main__":
    main()