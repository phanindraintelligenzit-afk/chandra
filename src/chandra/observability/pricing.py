"""
Dynamic Bedrock pricing loader.

Priority:
  1. AWS Pricing API (live via boto3) — refreshed every hour
  2. Local pricing_fallback.json     — works offline, no AWS creds needed

The AWS Pricing API only works in us-east-1.
Results are cached in-process for 1 hour to avoid repeated API calls.
"""

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Cache ─────────────────────────────────────────────────────────────────────
_CACHE_TTL = 3600          # seconds (1 hour)
_price_cache: dict = {}
_cache_loaded_at: float = 0.0

# ── Fallback file ─────────────────────────────────────────────────────────────
_FALLBACK_PATH = Path(__file__).parent / "pricing_fallback.json"


def _load_fallback() -> dict:
    """Load pricing from local JSON fallback. Always works — no network needed."""
    try:
        with open(_FALLBACK_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception as exc:
        logger.error("Failed to load fallback pricing file: %s", exc)
        return {}


def _fetch_aws_live_pricing() -> dict:
    """
    Try to fetch live Bedrock pricing from AWS Pricing API (us-east-1).
    Returns {} if API is unreachable or Bedrock not found in response.
    """
    try:
        import boto3

        client = boto3.client("pricing", region_name="us-east-1")
        pricing_map: dict = {}

        paginator = client.get_paginator("get_products")
        pages = paginator.paginate(
            ServiceCode="AmazonBedrock",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "AI Model"},
            ],
        )

        for page in pages:
            for raw in page.get("PriceList", []):
                try:
                    item = json.loads(raw)
                    attrs = item.get("product", {}).get("attributes", {})
                    model_id = attrs.get("modelId", "")
                    usage_type = attrs.get("usagetype", "").lower()

                    terms = item.get("terms", {}).get("OnDemand", {})
                    for term in terms.values():
                        for pd in term.get("priceDimensions", {}).values():
                            price_per_unit = float(
                                pd.get("pricePerUnit", {}).get("USD", 0)
                            )
                            # AWS quotes price per 1K tokens → convert to per 1M
                            price_per_1m = price_per_unit * 1000

                            if not model_id or price_per_1m == 0:
                                continue

                            if model_id not in pricing_map:
                                pricing_map[model_id] = {}

                            if "input" in usage_type:
                                pricing_map[model_id]["input_per_1m"] = round(price_per_1m, 4)
                            elif "output" in usage_type:
                                pricing_map[model_id]["output_per_1m"] = round(price_per_1m, 4)
                except Exception:
                    continue

        if pricing_map:
            logger.info(
                "Live Bedrock pricing fetched from AWS for %d models", len(pricing_map)
            )
        return pricing_map

    except Exception as exc:
        logger.warning(
            "AWS Pricing API unavailable (%s) — falling back to local JSON.", exc
        )
        return {}


def get_pricing() -> dict:
    """
    Return merged pricing dict (live + fallback).
    Uses in-process cache valid for 1 hour.
    """
    global _price_cache, _cache_loaded_at

    now = time.time()
    if _price_cache and (now - _cache_loaded_at) < _CACHE_TTL:
        return _price_cache

    fallback = _load_fallback()
    live = _fetch_aws_live_pricing()

    # Live data overrides fallback; fallback fills missing models
    merged = {**fallback, **live}
    _price_cache = merged
    _cache_loaded_at = now

    source = "AWS Pricing API" if live else "local fallback JSON"
    logger.info("Bedrock pricing loaded from %s (%d models)", source, len(merged))
    return _price_cache


def calculate_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """
    Calculate USD cost for a Bedrock LLM call.

    Pricing is fetched dynamically — never hardcoded.

    Args:
        model_id:      Bedrock model ID
        input_tokens:  Number of prompt tokens
        output_tokens: Number of completion tokens

    Returns:
        Total cost in USD (rounded to 6 decimal places). 0.0 if model unknown.
    """
    pricing = get_pricing()

    model_pricing = pricing.get(model_id)

    # Also try without "us." regional prefix
    if not model_pricing and model_id.startswith("us."):
        model_pricing = pricing.get(model_id[3:])

    if not model_pricing:
        logger.warning(
            "No pricing found for model '%s'. Add it to pricing_fallback.json.", model_id
        )
        return 0.0

    input_cost = (input_tokens / 1_000_000) * model_pricing.get("input_per_1m", 0)
    output_cost = (output_tokens / 1_000_000) * model_pricing.get("output_per_1m", 0)
    return round(input_cost + output_cost, 6)


def get_model_display_name(model_id: str) -> str:
    """Return human-readable name for a model ID."""
    pricing = get_pricing()
    data = pricing.get(model_id) or pricing.get(model_id[3:] if model_id.startswith("us.") else model_id, {})
    return data.get("display_name", model_id)
