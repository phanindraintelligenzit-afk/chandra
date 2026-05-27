BEDROCK_PRICING = {
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": {
        "input_per_1m": 3.0,
        "output_per_1m": 15.0,
    }
}

def calculate_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    pricing = BEDROCK_PRICING.get(model_id)
    if not pricing:
        return 0.0
    input_cost = (input_tokens / 1_000_000) * pricing["input_per_1m"]
    output_cost = (output_tokens / 1_000_000) * pricing["output_per_1m"]
    return round(input_cost + output_cost, 6)
