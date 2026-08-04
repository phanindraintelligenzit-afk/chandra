import os
import logging
from src.chandra.llm import build_chat_model

logging.basicConfig(level=logging.INFO)

def test():
    print("Testing LLM Invocation...")
    llm = build_chat_model()
    
    print(f"\n1. Resolved LLM Class: {llm.__class__.__name__}")
    
    if hasattr(llm, "model_id"):
        print(f"2. Configured Model: {llm.model_id}")
    elif hasattr(llm, "model_name"):
        print(f"2. Configured Model: {llm.model_name}")
        
    print("\nInvoking model with: 'Say hello in one word'")
    try:
        response = llm.invoke("Say hello in one word")
        print(f"\n3. Response: {response.content}")
        print("\nSUCCESS: Real invocation completed.")
    except Exception as e:
        print(f"\nERROR: {e}")

if __name__ == "__main__":
    test()
