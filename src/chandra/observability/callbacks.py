from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from src.chandra.observability.pricing import calculate_cost


class UsageCapture(BaseCallbackHandler):
    def __init__(self):
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_cost = 0.0

    def on_llm_end(self, response: LLMResult, **kwargs) -> None:
        if response.llm_output is None:
            return
        usage = response.llm_output.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        model_id = response.llm_output.get(
            "model_id", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
        )
        cost = calculate_cost(model_id, input_tokens, output_tokens)
        self.total_cost += cost
