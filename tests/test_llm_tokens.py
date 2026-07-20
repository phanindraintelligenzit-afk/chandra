import json

from src.chandra.llm import build_chat_model
from src.chandra.config import settings
from src.chandra.observability.callbacks import UsageCapture

print("=" * 70)
print("CAPTURING TOKENS AND COST FROM REAL BEDROCK LLM CALL")
print("=" * 70)
print()

# Create callback to capture usage
cb = UsageCapture()

# Create LLM instance
llm = build_chat_model(temperature=0.0, max_tokens=512)

print("Calling Claude via Bedrock...")
print("-" * 70)

try:
    # Call LLM with callback
    response = llm.invoke(
        [
            ("system", "You are a helpful assistant."),
            ("user", "What is the capital of France? Answer in one sentence."),
        ],
        config={"callbacks": [cb]},
    )

    print()
    print("LLM RESPONSE:")
    print("-" * 70)
    print(response.content)
    print()

    print("TOKEN USAGE:")
    print("-" * 70)
    print(f"Input Tokens:  {cb.input_tokens:,}")
    print(f"Output Tokens: {cb.output_tokens:,}")
    print(f"Total Tokens:  {cb.input_tokens + cb.output_tokens:,}")
    print()

    print("COST CALCULATION:")
    print("-" * 70)
    print(f"Input Cost:  ${(cb.input_tokens / 1_000_000) * 3.0:.6f}")
    print(f"Output Cost: ${(cb.output_tokens / 1_000_000) * 15.0:.6f}")
    print(f"Total Cost:  ${cb.total_cost:.6f}")
    print()

    print("=" * 70)
    print("PRICING TABLE")
    print("=" * 70)
    print("Model: Claude Sonnet 4.5")
    print("Input:  $3.00 per 1M tokens")
    print("Output: $15.00 per 1M tokens")
    print("=" * 70)
    print()

    # Show in JSON format
    print("JSON OUTPUT:")
    print("-" * 70)
    json_output = {
        "response": response.content,
        "tokens": {
            "input": cb.input_tokens,
            "output": cb.output_tokens,
            "total": cb.input_tokens + cb.output_tokens,
        },
        "cost": {
            "input_cost": round((cb.input_tokens / 1_000_000) * 3.0, 6),
            "output_cost": round((cb.output_tokens / 1_000_000) * 15.0, 6),
            "total_cost": round(cb.total_cost, 6),
            "currency": "USD",
        },
    }
    print(json.dumps(json_output, indent=2))

except Exception as e:
    print(f"Error: {e}")
    print()
    print("Note: This requires AWS credentials and Bedrock access.")
    print("Make sure your .env file has AWS_PROFILE and AWS_DEFAULT_REGION set.")
