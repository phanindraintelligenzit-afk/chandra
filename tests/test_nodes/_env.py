"""Shared environment setup for the per-node test scripts.

By default the scripts in this folder use the credentials from the
project's real ``.env`` file (the same one ``chandra`` itself reads
via pydantic-settings) and hit actual AWS / Bedrock / SNS endpoints.

To run them offline (mocked moto + patched Bedrock + in-memory SQLite
+ SNS that's not published), set ``CHANDRA_TEST_NODES_MOCK=1`` in the
environment before invoking the script:

    CHANDRA_TEST_NODES_MOCK=1 uv run python -m tests.test_nodes.onboard_account

In real-env mode the scripts will:
* Use the ``SYNTHETIC_ACCOUNT_ID`` and ``AWS_DEFAULT_REGION`` from .env
* Hit real CloudWatch, EC2, S3, IAM, etc. via the boto3 default chain
* Call real Bedrock with ``BEDROCK_MODEL_ID`` (will bill tokens)
* Publish to the real ``SNS_TOPIC_ARN`` from .env
* Write to the real ``POSTGRES_URL`` from .env (if uncommented)

Credentials are never hardcoded in the test scripts -- they are
loaded from .env by python-dotenv and read back via os.environ.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

# --- 1. Load the real .env into os.environ ---------------------------
# We try the repo root first (../../.env from this file's perspective).
REPO_ROOT = Path(__file__).resolve().parents[2]
DOTENV_PATH = REPO_ROOT / ".env"

try:
    from dotenv import load_dotenv  # type: ignore

    if DOTENV_PATH.exists():
        load_dotenv(DOTENV_PATH, override=False)
except ImportError:
    # python-dotenv is a project dep; this branch is defensive only.
    pass

# --- 2. Determine whether to mock or hit real services ---------------
MOCK_MODE = os.environ.get("CHANDRA_TEST_NODES_MOCK", "").lower() in {
    "1",
    "true",
    "yes",
}


# --- 3. Helpers for the real-env values ------------------------------


def real_account_id() -> str:
    """Return the synthetic AWS account id from .env, or a sane default."""
    return os.environ.get("SYNTHETIC_ACCOUNT_ID", "123456789012")


def real_region() -> str:
    """Return the AWS region from .env, defaulting to us-east-1."""
    return os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def real_sns_topic_arn() -> str:
    """Return the SNS topic ARN from .env (for the escalation node)."""
    return os.environ.get(
        "SNS_TOPIC_ARN",
        "arn:aws:sns:us-east-1:123456789012:chandra-escalations",
    )


def real_postgres_url() -> str | None:
    """Return the Postgres URL from .env, or None if not configured."""
    return os.environ.get("POSTGRES_URL")


# --- 4. Offline-mock context managers (only used in MOCK_MODE) -------


@contextmanager
def mock_aws() -> Iterator[None]:
    """Activate moto's ``mock_aws`` for the lifetime of the context."""
    from moto import mock_aws as _mock_aws

    with _mock_aws():
        yield


@contextmanager
def mock_bedrock(ranked_payload: str | None = None) -> Iterator[MagicMock]:
    """Patch ``langchain_aws.ChatBedrockConverse`` so no real LLM call happens."""
    if ranked_payload is None:
        ranked_payload = (
            '{"ranked": [], "executive_summary": "Synthetic briefing."}'
        )

    mock_response = MagicMock()
    mock_response.content = ranked_payload

    with patch("langchain_aws.ChatBedrockConverse") as mock_bedrock:
        instance = MagicMock()
        instance.invoke.return_value = mock_response
        mock_bedrock.return_value = instance
        yield mock_bedrock


@contextmanager
def mock_sns() -> Iterator[None]:
    """Activate moto (covers SNS) for the lifetime of the context."""
    from moto import mock_aws as _mock_aws

    with _mock_aws():
        yield


# --- 5. Public API used by the per-node scripts ----------------------


def make_state(**overrides: Any) -> dict[str, Any]:
    """Build a minimal ``ChandraState`` for a node test.

    Defaults are pulled from the real ``.env`` so the scripts exercise
    the same account/region the production run does.
    """
    state: dict[str, Any] = {
        "run_id": "demo-run-001",
        "account_id": real_account_id(),
        "regions": [real_region()],
    }
    state.update(overrides)
    return state


@contextmanager
def aws_scope() -> Iterator[None]:
    """Enter the AWS-touching scope: real AWS by default, moto when MOCK_MODE.

    Use this in every script that calls boto3 (directly or via a node).
    """
    if MOCK_MODE:
        with mock_aws():
            yield
    else:
        yield


@contextmanager
def bedrock_scope(payload: str | None = None) -> Iterator[MagicMock | None]:
    """Enter the Bedrock-touching scope: real Bedrock by default, patched when MOCK_MODE.

    Returns the patched class in mock mode (or ``None`` in real mode).
    """
    if MOCK_MODE:
        with mock_bedrock(payload) as patched:
            yield patched
    else:
        yield None


def banner(title: str) -> None:
    """Print a section banner so the output is scannable."""
    print()
    print("=" * 78)
    print("  " + title)
    print("=" * 78)


def mode_banner() -> None:
    """Print a one-liner telling the user which mode the script is in."""
    if MOCK_MODE:
        print(
            "[mode: MOCK -- moto + patched Bedrock + in-memory SQLite; "
            "no real AWS calls will be made]"
        )
    else:
        print(
            f"[mode: REAL -- account={real_account_id()} "
            f"region={real_region()} "
            f"bedrock={os.environ.get('BEDROCK_MODEL_ID', 'default')} "
            f"sns={real_sns_topic_arn()}]"
        )
