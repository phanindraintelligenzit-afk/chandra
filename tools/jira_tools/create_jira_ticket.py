# ruff: noqa: N806, PLR0911, E501
import os

import requests
from dotenv import load_dotenv
from jira import JIRA
from jira.exceptions import JIRAError
from requests.auth import HTTPBasicAuth

# Load variables from your .env file
load_dotenv() 

def create_jira_ticket(project_key: str, summary: str, description: str = "", issuetype: str = "Task", priority: str = None, labels: list = None):
    """
    Checks if a Jira project exists, creates it if necessary, 
    and creates a ticket using the provided string arguments including priority.
    """
    JIRA_SERVER = os.getenv("JIRA_SERVER")
    JIRA_EMAIL = os.getenv("JIRA_EMAIL")
    JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

    if not project_key or not summary:
        return {"status": "error", "message": "Both project_key and summary are required."}

    # 1. Build the fields dictionary from your function arguments
    fields = {
        'project': {'key': project_key},       
        'summary': summary,
        'description': description,
        'issuetype': {'name': issuetype}    
    }
    
    if priority:
        fields['priority'] = {'name': priority}

    if labels:
        fields['labels'] = labels

    try:
        # 2. Authenticate with the jira library
        jira = JIRA(
            server=JIRA_SERVER, 
            basic_auth=(JIRA_EMAIL, JIRA_API_TOKEN)
        )

        # 3. Check if the project already exists
        try:
            jira.project(project_key)
            print(f"Project '{project_key}' already exists. Skipping creation.")

        except JIRAError as e:
            # If the project is not found (404 Error), create it
            if e.status_code == 404:
                print(f"Project '{project_key}' not found. Creating it now...")
                
                # Setup requests auth for the REST API calls
                auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)
                headers = {"Accept": "application/json", "Content-Type": "application/json"}
                
                # Get your Account ID (Required to make you the project owner)
                user_response = requests.get(f"{JIRA_SERVER}/rest/api/3/myself", auth=auth, headers=headers)
                user_response.raise_for_status()
                account_id = user_response.json().get("accountId")
                
                # Create the project using the REST API
                project_payload = {
                    "key": project_key,
                    "name": f"{project_key} Automated Project",
                    "projectTypeKey": "software",
                    "projectTemplateKey": "com.pyxis.greenhopper.jira:gh-simplified-kanban-classic",
                    "description": "Created automatically via Python script.",
                    "leadAccountId": account_id
                }
                
                project_response = requests.post(f"{JIRA_SERVER}/rest/api/3/project", auth=auth, headers=headers, json=project_payload)
                project_response.raise_for_status()
                print(f"Successfully created new project: '{project_key}'!")
                
            else:
                # If it's a different error (like bad token), raise it
                raise e

        # 4. Create the ticket using the constructed dictionary
        new_issue = jira.create_issue(fields=fields)
        issue_url = f"{JIRA_SERVER}/browse/{new_issue.key}"

        print(f"Success! Ticket {new_issue.key} created.")

        return {
            "status": "success",
            "issue_key": new_issue.key,
            "url": issue_url
        }

    except Exception as e:
        print(f"An error occurred: {e}")
        return {"status": "error", "message": str(e)}


def add_comment_to_ticket(issue_key: str, steps: list) -> dict:
    """Add implementation steps as a comment on an existing Jira ticket."""
    JIRA_SERVER = os.getenv("JIRA_SERVER")
    JIRA_EMAIL = os.getenv("JIRA_EMAIL")
    JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

    if not steps:
        return {"status": "skipped", "message": "No steps provided"}

    comment_body = "*Steps to implement:*\n" + "\n".join(
        f"{i + 1}. {step}" for i, step in enumerate(steps)
    )

    try:
        jira = JIRA(server=JIRA_SERVER, basic_auth=(JIRA_EMAIL, JIRA_API_TOKEN))
        jira.add_comment(issue_key, comment_body)
        print(f"Steps comment added to {issue_key}")
        return {"status": "success"}
    except Exception as e:
        print(f"Failed to add comment to {issue_key}: {e}")
        return {"status": "error", "message": str(e)}


def add_approval_comment(issue_key: str, human_review_needed: bool) -> dict:
    """Add an approval-status comment indicating whether the action is auto-approved or awaiting human review."""
    JIRA_SERVER = os.getenv("JIRA_SERVER")
    JIRA_EMAIL = os.getenv("JIRA_EMAIL")
    JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

    if human_review_needed:
        body = (
            "⏳ *Pending human approval.* "
            "This action requires manual review and sign-off before execution can proceed. "
            "Please review the steps above and approve or reject accordingly."
        )
    else:
        body = (
            "✅ *Agent auto-approved this action.* "
            "No human review is required. "
            "Automated remediation will proceed as outlined in the steps above."
        )

    try:
        jira = JIRA(server=JIRA_SERVER, basic_auth=(JIRA_EMAIL, JIRA_API_TOKEN))
        jira.add_comment(issue_key, body)
        print(f"Approval comment added to {issue_key}")
        return {"status": "success"}
    except Exception as e:
        print(f"Failed to add approval comment to {issue_key}: {e}")
        return {"status": "error", "message": str(e)}


def add_summary_comment(issue_key: str, summary: str) -> dict:
    """Add a final summary comment on an existing Jira ticket."""
    JIRA_SERVER = os.getenv("JIRA_SERVER")
    JIRA_EMAIL = os.getenv("JIRA_EMAIL")
    JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

    if not summary:
        return {"status": "skipped", "message": "No summary provided"}

    try:
        jira = JIRA(server=JIRA_SERVER, basic_auth=(JIRA_EMAIL, JIRA_API_TOKEN))
        jira.add_comment(issue_key, summary)
        print(f"Summary comment added to {issue_key}")
        return {"status": "success"}
    except Exception as e:
        print(f"Failed to add summary comment to {issue_key}: {e}")
        return {"status": "error", "message": str(e)}


def update_ticket_status(issue_key: str, status_name: str) -> dict:
    """Transition a Jira ticket to a new status (e.g., 'Done', 'Backlog')."""
    JIRA_SERVER = os.getenv("JIRA_SERVER")
    JIRA_EMAIL = os.getenv("JIRA_EMAIL")
    JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

    try:
        jira = JIRA(server=JIRA_SERVER, basic_auth=(JIRA_EMAIL, JIRA_API_TOKEN))
        transitions = jira.transitions(issue_key)
        
        transition_id = None
        for t in transitions:
            if t['name'].lower() == status_name.lower() or t['to']['name'].lower() == status_name.lower():
                transition_id = t['id']
                break
        
        if transition_id:
            jira.transition_issue(issue_key, transition_id)
            print(f"Successfully transitioned {issue_key} to '{status_name}'")
            return {"status": "success"}
        else:
            print(f"Transition to '{status_name}' not found for {issue_key}")
            return {"status": "error", "message": f"Transition '{status_name}' not found"}
    except Exception as e:
        print(f"Failed to transition {issue_key}: {e}")
        return {"status": "error", "message": str(e)}

def add_label_to_ticket(issue_key: str, label: str) -> dict:
    """Add a label to an existing Jira ticket."""
    JIRA_SERVER = os.getenv("JIRA_SERVER")
    JIRA_EMAIL = os.getenv("JIRA_EMAIL")
    JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

    try:
        jira = JIRA(server=JIRA_SERVER, basic_auth=(JIRA_EMAIL, JIRA_API_TOKEN))
        issue = jira.issue(issue_key)
        # Ensure we don't duplicate the label
        if label not in issue.fields.labels:
            issue.fields.labels.append(label)
            issue.update(fields={"labels": issue.fields.labels})
            print(f"Successfully added label '{label}' to {issue_key}")
        else:
            print(f"Label '{label}' already exists on {issue_key}")
        return {"status": "success"}
    except Exception as e:
        print(f"Failed to add label to {issue_key}: {e}")
        return {"status": "error", "message": str(e)}

# ==========================================
# HOW TO USE THE FUNCTION:
# ==========================================
# if __name__ == "__main__":
    
#     # Now you can call the function passing the priority!
#     # Valid Jira priorities usually include: "Highest", "High", "Medium", "Low", "Lowest"
#     result = create_jira_ticket(
#         project_key="DEV",
#         summary="Fix login button styling",
#         description="The login button is overflowing its container on mobile devices.",
#         issuetype="Bug",
#         priority="High"  # Added priority here
#     )
    
#     if result["status"] == "success":
#         print(f"Ticket URL: {result['url']}")