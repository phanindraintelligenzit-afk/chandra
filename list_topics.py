import json
from src.chandra.aws.client_factory import get_default_factory
from dotenv import load_dotenv
load_dotenv(override=True)

try:
    print("Fetching SNS topics using credentials from .env...")
    factory = get_default_factory()
    sns = factory.client("sns")
    response = sns.list_topics()
    
    topics = response.get("Topics", [])
    if not topics:
        print("No SNS topics found in this region.")
    else:
        print("\nAvailable SNS Topics:")
        for topic in topics:
            print(f"- {topic['TopicArn']}")
            
except Exception as e:
    print(f"Error fetching topics: {e}")
