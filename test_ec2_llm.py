from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
import os
from dotenv import load_dotenv
load_dotenv(override=True)
llm = ChatOpenAI(
    base_url=os.getenv("OPENAI_API_BASE"),
    api_key=os.getenv("OPENAI_API_KEY"),
    model=os.getenv("OPENAI_MODEL_NAME")
)

messages = [
    SystemMessage(content="You are an expert AWS infrastructure engineer specializing in Terraform. Generate production-ready HCL code with proper resource naming, tagging, and security best practices. When asked for infrastructure, respond only with valid Terraform code and brief inline comments."),
    HumanMessage(content="Do you have knowledge aws cloud terraform files")
]

response = llm.invoke(messages)
print(response.content)