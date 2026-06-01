import json
from src.chandra.observability.pricing import calculate_cost
from src.chandra.observability.callbacks import UsageCapture

print("=" * 70)
print("SIMULATING BEDROCK LLM CALL WITH TOKEN USAGE")
print("=" * 70)
print()

# Simulate LLM call by creating a mock response
print("Simulating Claude response via Bedrock...")
print("-" * 70)
print()

# Create callback
cb = UsageCapture()

# Simulate token usage (mock data - like real Bedrock would return)
cb.input_tokens = 1250
cb.output_tokens = 487

# Calculate cost
cb.total_cost = calculate_cost(
    model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    input_tokens=cb.input_tokens,
    output_tokens=cb.output_tokens,
)

# Simulate LLM response
response_text = "The capital of France is Paris, the largest city in the country. It is known as the City of Light and serves as the cultural and political center of France."

print("LLM RESPONSE:")
print("-" * 70)
print(response_text)
print()

print("TOKEN USAGE:")
print("-" * 70)
print(f"Input Tokens:  {cb.input_tokens:,}")
print(f"Output Tokens: {cb.output_tokens:,}")
print(f"Total Tokens:  {cb.input_tokens + cb.output_tokens:,}")
print()

print("COST CALCULATION:")
print("-" * 70)
input_cost = (cb.input_tokens / 1_000_000) * 3.0
output_cost = (cb.output_tokens / 1_000_000) * 15.0
print(f"Input Cost:    ${input_cost:.6f}  (1,250 tokens × $3.00/1M)")
print(f"Output Cost:   ${output_cost:.6f}  (487 tokens × $15.00/1M)")
print(f"Total Cost:    ${cb.total_cost:.6f}")
print()

print("=" * 70)
print("PRICING TABLE")
print("=" * 70)
print("Model: Claude Sonnet 4.5")
print("Input:  $3.00 per 1,000,000 tokens")
print("Output: $15.00 per 1,000,000 tokens")
print("=" * 70)
print()

print("JSON OUTPUT:")
print("-" * 70)
json_output = {
    "response": response_text,
    "tokens": {
        "input_tokens": cb.input_tokens,
        "output_tokens": cb.output_tokens,
        "total_tokens": cb.input_tokens + cb.output_tokens,
    },
    "cost": {
        "input_cost_usd": round(input_cost, 6),
        "output_cost_usd": round(output_cost, 6),
        "total_cost_usd": round(cb.total_cost, 6),
        "currency": "USD"
    }
}
print(json.dumps(json_output, indent=2))
print()

print("=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"✓ Input tokens captured:   {cb.input_tokens:,}")
print(f"✓ Output tokens captured:  {cb.output_tokens:,}")
print(f"✓ Total cost calculated:   ${cb.total_cost:.6f}")
print("=" * 70)