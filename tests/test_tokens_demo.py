from chandra.observability.pricing import calculate_cost

print("=" * 60)
print("BEDROCK TOKEN USAGE AND COST TRACKING")
print("=" * 60)
print()

# Test 1: Input tokens only
print("TEST 1: Input tokens only")
print("-" * 60)
input_tokens = 1_000_000
output_tokens = 0
cost = calculate_cost(
    model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    input_tokens=input_tokens,
    output_tokens=output_tokens,
)
print(f"Input Tokens:  {input_tokens:,}")
print(f"Output Tokens: {output_tokens:,}")
print(f"Total Tokens:  {input_tokens + output_tokens:,}")
print(f"Cost (USD):    ${cost}")
print()

# Test 2: Output tokens only
print("TEST 2: Output tokens only")
print("-" * 60)
input_tokens = 0
output_tokens = 1_000_000
cost = calculate_cost(
    model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    input_tokens=input_tokens,
    output_tokens=output_tokens,
)
print(f"Input Tokens:  {input_tokens:,}")
print(f"Output Tokens: {output_tokens:,}")
print(f"Total Tokens:  {input_tokens + output_tokens:,}")
print(f"Cost (USD):    ${cost}")
print()

# Test 3: Combined (realistic scenario)
print("TEST 3: Combined (realistic scenario)")
print("-" * 60)
input_tokens = 2_500
output_tokens = 1_200
cost = calculate_cost(
    model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    input_tokens=input_tokens,
    output_tokens=output_tokens,
)
print(f"Input Tokens:  {input_tokens:,}")
print(f"Output Tokens: {output_tokens:,}")
print(f"Total Tokens:  {input_tokens + output_tokens:,}")
print(f"Cost (USD):    ${cost}")
print()

# Test 4: Full run scenario
print("TEST 4: Full Chandra run scenario")
print("-" * 60)
input_tokens = 5_000
output_tokens = 3_000
cost = calculate_cost(
    model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    input_tokens=input_tokens,
    output_tokens=output_tokens,
)
print(f"Input Tokens:  {input_tokens:,}")
print(f"Output Tokens: {output_tokens:,}")
print(f"Total Tokens:  {input_tokens + output_tokens:,}")
print(f"Cost (USD):    ${cost}")
print()

print("=" * 60)
print("PRICING TABLE")
print("=" * 60)
print("Model: Claude Sonnet 4.5")
print("Input:  $3.00 per 1M tokens")
print("Output: $15.00 per 1M tokens")
print("=" * 60)