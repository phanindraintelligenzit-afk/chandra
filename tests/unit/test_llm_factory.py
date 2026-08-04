import pytest
from src.chandra.config import settings
from src.chandra.llm import build_chat_model
import os

def test_bedrock_selects_bedrock_with_default_model(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "bedrock")
    monkeypatch.setattr(settings, "bedrock_model_id", "moonshotai.kimi-k2.5")
    monkeypatch.setattr(settings, "aws_default_region", "us-east-1")
    
    # Should not throw
    llm = build_chat_model()
    # It returns a ChatBedrockConverse instance
    assert llm.__class__.__name__ == "ChatBedrockConverse"
    assert llm.model_id == "moonshotai.kimi-k2.5"
    assert llm.region_name == "us-east-1"


def test_vllm_selects_openai_with_default_model(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "vllm")
    monkeypatch.setattr(settings, "vllm_model", "google/gemma-4-12B-it-qat-w4a16-ct")
    monkeypatch.setattr(settings, "vllm_api_base", "http://localhost:8000/v1")
    
    llm = build_chat_model()
    assert llm.__class__.__name__ == "ChatOpenAI"
    assert llm.model_name == "google/gemma-4-12B-it-qat-w4a16-ct"
    # Ensure it didn't leak Bedrock's model
    assert getattr(settings, "bedrock_model_id", None) != llm.model_name


def test_invalid_provider_fails_clearly():
    with pytest.raises(ValueError, match="Unsupported LLM_PROVIDER 'invalid'"):
        build_chat_model(provider="invalid")


def test_missing_bedrock_model_fails_clearly(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "bedrock")
    monkeypatch.setattr(settings, "bedrock_model_id", None)
    
    with pytest.raises(ValueError, match="LLM_PROVIDER=bedrock requires BEDROCK_MODEL_ID configuration"):
        build_chat_model()


def test_missing_vllm_config_fails_clearly(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "vllm")
    monkeypatch.setattr(settings, "vllm_model", None)
    monkeypatch.setattr(settings, "openai_model_name", None)
    monkeypatch.setattr(settings, "vllm_api_base", "http://fake")
    
    with pytest.raises(ValueError, match="LLM_PROVIDER=vllm requires VLLM_MODEL"):
        build_chat_model()

    monkeypatch.setattr(settings, "vllm_api_base", None)
    monkeypatch.setattr(settings, "openai_api_base", None)
    
    with pytest.raises(ValueError, match="LLM_PROVIDER=vllm requires VLLM_API_BASE"):
        build_chat_model()
