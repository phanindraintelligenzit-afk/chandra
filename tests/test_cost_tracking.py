import json
from unittest.mock import patch, MagicMock
import pytest
import src.chandra.observability.pricing as pricing
from src.chandra.observability.pricing import (
    calculate_cost,
    get_pricing,
    get_model_display_name,
)

@pytest.fixture(autouse=True)
def clear_pricing_cache():
    """Reset the global cache variables in pricing module before each test."""
    pricing._price_cache = {}
    pricing._cache_loaded_at = 0.0
    yield

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

@patch("boto3.client")
def test_pricing_api_fetch_success(mock_boto_client):
    """Test that live prices from AWS Pricing API are fetched and merged."""
    # Mock data from AWS Pricing API (Service: AmazonBedrock, productFamily: AI Model)
    mock_price_list = [
        json.dumps({
            "product": {
                "attributes": {
                    "modelId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                    "usagetype": "USW2-Bedrock:InputTokens"
                }
            },
            "terms": {
                "OnDemand": {
                    "some_term_id": {
                        "priceDimensions": {
                            "some_dimension_id": {
                                "pricePerUnit": {"USD": "0.0035000000"}  # $3.50 per 1K -> $3.50 per 1M (Wait, 0.0035 * 1000 = 3.50)
                            }
                        }
                    }
                }
            }
        }),
        json.dumps({
            "product": {
                "attributes": {
                    "modelId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                    "usagetype": "USW2-Bedrock:OutputTokens"
                }
            },
            "terms": {
                "OnDemand": {
                    "some_term_id": {
                        "priceDimensions": {
                            "some_dimension_id": {
                                "pricePerUnit": {"USD": "0.0165000000"}  # $16.50 per 1K -> $16.50 per 1M
                            }
                        }
                    }
                }
            }
        })
    ]

    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [{"PriceList": mock_price_list}]
    
    mock_client = MagicMock()
    mock_client.get_paginator.return_value = mock_paginator
    mock_boto_client.return_value = mock_client

    # Get pricing - should trigger the mocked boto3 fetch
    pricing_data = get_pricing()

    # Check that live values fetched overrides fallback values
    model_pricing = pricing_data.get("us.anthropic.claude-sonnet-4-5-20250929-v1:0")
    assert model_pricing is not None
    assert model_pricing["input_per_1m"] == 3.50
    assert model_pricing["output_per_1m"] == 16.50

    # Test calculate_cost uses the live API values
    cost = calculate_cost(
        model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        input_tokens=1_000_000,
        output_tokens=1_000_000
    )
    assert cost == 20.0  # 3.5 + 16.5

@patch("boto3.client")
def test_pricing_api_fetch_failure_fallback(mock_boto_client):
    """Test that when AWS API fails, it gracefully falls back to local JSON data."""
    mock_boto_client.side_effect = Exception("AWS connection timeout")

    # Get pricing - should fallback
    pricing_data = get_pricing()

    # Claude 4.5 fallback pricing is loaded from json (3.0, 15.0)
    model_pricing = pricing_data.get("us.anthropic.claude-sonnet-4-5-20250929-v1:0")
    assert model_pricing is not None
    assert model_pricing["input_per_1m"] == 3.0
    assert model_pricing["output_per_1m"] == 15.0

    cost = calculate_cost(
        model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        input_tokens=1_000_000,
        output_tokens=1_000_000
    )
    assert cost == 18.0

def test_unknown_model_returns_zero():
    """Test that requesting pricing for an unknown model returns 0.0 instead of crashing."""
    cost = calculate_cost(
        model_id="unknown.model-id-here-v1:0",
        input_tokens=1_000_000,
        output_tokens=1_000_000
    )
    assert cost == 0.0

def test_model_display_names():
    """Test displaying clean model names."""
    name = get_model_display_name("us.anthropic.claude-sonnet-4-5-20250929-v1:0")
    assert name == "Claude Sonnet 4.5"

    unknown_name = get_model_display_name("some-crazy-unknown-model")
    assert unknown_name == "some-crazy-unknown-model"

