from src.chandra.llm import build_chat_model
from langchain_core.messages import SystemMessage, HumanMessage
import os
from dotenv import load_dotenv
load_dotenv(override=True)
llm = build_chat_model()

messages = [
    SystemMessage(content="You are an expert AWS infrastructure engineer specializing in Terraform. Generate production-ready HCL code with proper resource naming, tagging, and security best practices. When asked for infrastructure, respond only with valid Terraform code and brief inline comments."),
    HumanMessage(content="Do you have knowledge aws cloud terraform files")
]

response = llm.invoke(messages)
print(response.content)