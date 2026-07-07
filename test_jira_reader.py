from tools.jira_tools.read_jira_ticket import get_jira_ticket_details
import json
import os

if __name__ == "__main__":
    tests = [
        {"name": "Valid formatting (404 expected)", "url": "https://dummyintelligenzit.atlassian.net/browse/ABC-25"},
        {"name": "Invalid URL format", "url": "https://dummyintelligenzit.atlassian.net/browse/invalid_ticket"},
        {"name": "Empty URL", "url": ""},
        {"name": "Wrong data type", "url": None},
    ]
    
    for t in tests:
        print(f"\n--- Testing: {t['name']} ---")
        print(f"URL: {t['url']}")
        result = get_jira_ticket_details(t['url'])
        print(json.dumps(result, indent=2))
        
    print("\n--- Testing: Missing Env ---")
    original_token = os.environ.get("JIRA_API_TOKEN")
    os.environ["JIRA_API_TOKEN"] = ""
    result = get_jira_ticket_details("https://dummyintelligenzit.atlassian.net/browse/ABC-25")
    print(json.dumps(result, indent=2))
    
    if original_token is not None:
        os.environ["JIRA_API_TOKEN"] = original_token
