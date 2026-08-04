from src.chandra.llm import build_chat_model
from langchain_core.messages import SystemMessage, HumanMessage

print("Testing Bedrock Initialization...")
llm = build_chat_model()
print(f"Provider: {llm.__class__.__name__}")
print(f"Model ID: {getattr(llm, 'model_id', getattr(llm, 'model_name', 'unknown'))}")

print("\nInvoking Model...")
messages = [
    SystemMessage(content="You are a helpful assistant."),
    HumanMessage(content="Say 'Bedrock is working!' and nothing else.")
]

try:
    response = llm.invoke(messages)
    print("Response:", response.content)
except Exception as e:
    print("Error:", e)
