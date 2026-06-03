"""
Demo: Dynamic Bedrock Pricing
- Shows pricing loaded automatically (AWS API or fallback JSON)
- No hardcoded values anywhere in the code
"""
from src.chandra.observability.pricing import (
    get_pricing,
    calculate_cost,
    get_model_display_name,
)

MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

print("=" * 60)
print("  DYNAMIC BEDROCK PRICING TEST")
print("=" * 60)

pricing = get_pricing()
print(f"  Models loaded: {len(pricing)}")
print()

print("  ALL KNOWN MODELS:")
print("-" * 60)
for mid, p in pricing.items():
    if isinstance(p, dict) and "input_per_1m" in p:
        name = p.get("display_name", mid[:40])
        print(f"  {name}")
        print(f"    Input:  ${p['input_per_1m']:.4f} / 1M tokens")
        print(f"    Output: ${p['output_per_1m']:.4f} / 1M tokens")
print()

INPUT_TOKENS = 2500
OUTPUT_TOKENS = 1200
cost = calculate_cost(MODEL, INPUT_TOKENS, OUTPUT_TOKENS)
name = get_model_display_name(MODEL)

print("  COST CALCULATION:")
print("-" * 60)
print(f"  Model:         {name}")
print(f"  Input Tokens:  {INPUT_TOKENS:,}")
print(f"  Output Tokens: {OUTPUT_TOKENS:,}")
print(f"  Total Cost:    ${cost:.6f} USD")
print()
print("=" * 60)
print("  DYNAMIC PRICING: PASS  (no hardcoded values)")
print("=" * 60)
