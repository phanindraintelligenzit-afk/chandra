from src.chandra.observability.pricing import calculate_cost


def test_cost_calculation_input_only():
    cost = calculate_cost(
        model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    assert cost == 3.0


def test_cost_calculation_output_only():
    cost = calculate_cost(
        model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        input_tokens=0,
        output_tokens=1_000_000,
    )
    assert cost == 15.0


def test_cost_calculation_combined():
    cost = calculate_cost(
        model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert cost == 18.0


def test_cost_calculation_partial():
    cost = calculate_cost(
        model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        input_tokens=500_000,
        output_tokens=500_000,
    )
    assert cost == 9.0
