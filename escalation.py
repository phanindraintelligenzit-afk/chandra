"""
escalation.py
─────────────────────────────────────────────────────────────────
Flow: agent creates payload → SNS publish → Slack + Email

IAM context
  bedrock_policy_arn : arn:aws:iam::827295473120:policy/chandra-bedrock-sonnet
  role_arn           : arn:aws:iam::827295473120:role/chandra-runtime-prod
  role_name          : Chandra-runtime-prod
─────────────────────────────────────────────────────────────────
"""

import boto3
import json
import logging
import sys
import argparse
from datetime import datetime, timezone
from botocore.exceptions import ClientError

# ── Logging ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Config ───────────────────────────────────────────────────────
ROLE_ARN          = "arn:aws:iam::827295473120:role/chandra-runtime-prod"
BEDROCK_REGION    = "us-east-1"          # change if your Bedrock endpoint differs
SNS_REGION        = "us-east-1"          # change to match your SNS topic region
BEDROCK_MODEL_ID  = "anthropic.claude-3-sonnet-20240229-v1:0"

# ── Paste your SNS ARN(s) from `terraform output` here ───────────
SNS_TOPIC_ARN     = "arn:aws:sns:us-east-1:827295473120:chandra-escalation"   # e.g. arn:aws:sns:us-east-1:827295473120:chandra-escalation


# ── Step 1 – Assume the runtime role ─────────────────────────────
def assume_role(role_arn: str) -> dict:
    """Return temporary STS credentials for the given role."""
    log.info("Assuming role: %s", role_arn)
    sts = boto3.client("sts")
    resp = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName="chandra-escalation-session",
        DurationSeconds=900,      # 15 min – enough for one run
    )
    creds = resp["Credentials"]
    log.info("Role assumed successfully. Session expires: %s", creds["Expiration"])
    return {
        "aws_access_key_id":     creds["AccessKeyId"],
        "aws_secret_access_key": creds["SecretAccessKey"],
        "aws_session_token":     creds["SessionToken"],
    }


# ── Step 2 – Call Bedrock to generate the escalation payload ─────
def generate_escalation_payload(creds: dict, incident: dict) -> dict:
    """
    Ask Claude (via Bedrock) to produce a structured escalation payload.
    Returns a dict with keys: summary, severity, recommended_action, escalation_path.
    """
    bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION, **creds)

    system_prompt = (
        "You are an incident-management assistant. "
        "Given incident details, respond ONLY with a valid JSON object "
        "containing exactly these keys: "
        "summary (str), severity (str: low|medium|high|critical), "
        "recommended_action (str), escalation_path (list of str). "
        "No markdown, no extra keys."
    )

    user_message = f"Incident details:\n{json.dumps(incident, indent=2)}"

    log.info("Calling Bedrock model: %s", BEDROCK_MODEL_ID)
    response = bedrock.invoke_model(
        modelId=BEDROCK_MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 512,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_message}],
        }),
    )

    raw = json.loads(response["body"].read())
    text = raw["content"][0]["text"].strip()
    log.info("Bedrock raw response: %s", text)

    payload = json.loads(text)          # Claude was told to return pure JSON
    payload["incident_id"]  = incident.get("incident_id", "N/A")
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    return payload


# ── Step 3 – Publish to SNS (→ Slack + Email) ────────────────────
def publish_to_sns(creds: dict, topic_arn: str, payload: dict) -> str:
    """
    Publish the escalation payload to SNS.
    SNS will fan out to all subscriptions (email endpoint + Slack Lambda/webhook).
    Returns the SNS MessageId.
    """
    sns = boto3.client("sns", region_name=SNS_REGION, **creds)

    severity   = payload.get("severity", "unknown").upper()
    subject    = f"[{severity}] Incident Escalation – {payload.get('incident_id', 'N/A')}"
    message    = json.dumps(payload, indent=2)

    log.info("Publishing to SNS topic: %s", topic_arn)
    resp = sns.publish(
        TopicArn=topic_arn,
        Subject=subject,
        Message=message,
        MessageAttributes={
            "severity": {
                "DataType": "String",
                "StringValue": payload.get("severity", "unknown"),
            },
        },
    )
    message_id = resp["MessageId"]
    log.info("SNS publish successful. MessageId: %s", message_id)
    return message_id


# ── Orchestrator ─────────────────────────────────────────────────
def escalate(incident: dict, sns_topic_arn: str = SNS_TOPIC_ARN) -> dict:
    """
    Full escalation flow:
      assume role → bedrock payload → SNS publish
    Returns a result summary dict.
    """
    if sns_topic_arn == "PASTE_SNS_TOPIC_ARN_HERE":
        raise ValueError(
            "Set SNS_TOPIC_ARN at the top of this file (from `terraform output`)."
        )

    # 1. Credentials
    creds = assume_role(ROLE_ARN)

    # 2. Agent generates payload
    payload = generate_escalation_payload(creds, incident)
    log.info("Escalation payload:\n%s", json.dumps(payload, indent=2))

    # 3. Publish
    message_id = publish_to_sns(creds, sns_topic_arn, payload)

    result = {
        "status":     "published",
        "message_id": message_id,
        "payload":    payload,
    }
    return result


# ── CLI entry point ──────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="Chandra escalation runner")
    p.add_argument("--incident-id",  default="INC-0001",      help="Incident identifier")
    p.add_argument("--title",        default="Service degradation detected")
    p.add_argument("--description",  default="High error rate on payment service (>5%% for 10 min)")
    p.add_argument("--service",      default="payment-service")
    p.add_argument("--environment",  default="production")
    p.add_argument("--reporter",     default="cloudwatch-alarm")
    p.add_argument("--sns-arn",      default=SNS_TOPIC_ARN,    help="Override SNS topic ARN")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    incident_details = {
        "incident_id":  args.incident_id,
        "title":        args.title,
        "description":  args.description,
        "service":      args.service,
        "environment":  args.environment,
        "reporter":     args.reporter,
        "timestamp":    datetime.now(timezone.utc).isoformat(),
    }

    try:
        result = escalate(incident_details, sns_topic_arn=args.sns_arn)
        print("\n✅  Escalation published successfully")
        print(json.dumps(result, indent=2, default=str))
        sys.exit(0)
    except (ClientError, ValueError) as exc:
        log.error("Escalation failed: %s", exc)
        sys.exit(1)