# ruff: noqa: N806, PLR0911, E501
import logging

# ruff: noqa: N806, PLR0911, E501, N803
import os
import re

import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

def adf_to_text(node):  # type: ignore
    """
    Recursively parse Jira Atlassian Document Format (ADF) into a plain readable string.
    """
    if not isinstance(node, dict):
        return str(node) if node else ""
        
    parts = []
    
    # If it's a text node, grab the text
    if node.get("type") == "text" and "text" in node:
        parts.append(node["text"])
        
    # Recurse over children
    if "content" in node and isinstance(node["content"], list):
        for child in node["content"]:
            parts.append(adf_to_text(child))  # type: ignore
            
    # Add spacing for blocks
    if node.get("type") in ["paragraph", "heading", "listItem"]:
        parts.append("\n")
        
    return "".join(parts).strip()

def get_jira_ticket_details(jiraUrl: str) -> dict:  # type: ignore
    """
    Given a Jira URL, extract the issue key and retrieve ticket details
    (summary, description, priority, comments) via Jira REST API.
    
    Args:
        jiraUrl (str): The full URL to the Jira ticket.
        
    Returns:
        dict: A dictionary containing ticketName, description, priorityLevel, 
              comments, or an error message.
    """
    load_dotenv()
    
    JIRA_SERVER = os.getenv("JIRA_SERVER")
    JIRA_EMAIL = os.getenv("JIRA_EMAIL")
    JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

    if not JIRA_SERVER or not JIRA_EMAIL or not JIRA_API_TOKEN:
        logger.error("Missing Jira credentials in environment variables.")
        return {"error": "Missing Jira credentials in environment variables."}

    if not jiraUrl or not isinstance(jiraUrl, str):
        logger.error("Invalid Jira URL provided.")
        return {"error": "Invalid Jira URL provided."}

    # Extract issue key robustly using regex
    try:
        match = re.search(r'([A-Z]+-[0-9]+)', jiraUrl, re.IGNORECASE)
        if not match:
            logger.error("Could not extract issue key from URL.")
            return {"error": "Could not extract issue key from URL. Ensure it contains a valid key (e.g. PROJ-123)."}
        issue_key = match.group(1).upper()
    except Exception as e:
        logger.error(f"Invalid URL format: {e!s}")
        return {"error": f"Invalid URL format: {e!s}"}

    url = f"{JIRA_SERVER}/rest/api/3/issue/{issue_key}"
    auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)
    headers = {"Accept": "application/json"}

    try:
        logger.info(f"Fetching Jira ticket details for {issue_key}")
        response = requests.get(url, auth=auth, headers=headers, timeout=10)
        
        if response.status_code == 404:
            logger.warning(f"Jira ticket {issue_key} not found (404).")
            return {"error": f"Jira ticket {issue_key} not found (404)."}
        if response.status_code == 403:
            logger.warning(f"Access denied to Jira ticket {issue_key} (403).")
            return {"error": f"Access denied to Jira ticket {issue_key} (403)."}
        if response.status_code == 401:
            logger.warning("Authentication failed (401). Check API token.")
            return {"error": "Authentication failed (401). Check API token."}
        
        response.raise_for_status()
        data = response.json()
        
        fields = data.get("fields", {})
        
        # Extract summary
        summary = fields.get("summary", "")
        
        # Extract and parse description (ADF format)
        raw_description = fields.get("description", "")
        description = adf_to_text(raw_description) if isinstance(raw_description, dict) else str(raw_description)  # type: ignore
        
        # Extract priority
        priority_obj = fields.get("priority", {})
        priority = priority_obj.get("name", "") if priority_obj else ""
        
        # Extract and parse comments
        comment_obj = fields.get("comment", {})
        comments_list = comment_obj.get("comments", [])
        
        extracted_comments = []
        for c in comments_list:
            body = c.get("body", "")
            parsed_body = adf_to_text(body) if isinstance(body, dict) else str(body)  # type: ignore
            if parsed_body:
                extracted_comments.append(parsed_body)

        return {
            "ticketName": summary,
            "description": description,
            "priorityLevel": priority,
            "comments": extracted_comments
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"Network or API error while fetching Jira ticket: {e!s}")
        return {"error": f"Network or API error: {e!s}"}
    except ValueError:
        logger.error("Invalid JSON response from Jira.")
        return {"error": "Invalid JSON response from Jira."}
    except Exception as e:
        logger.error(f"Unexpected error while fetching Jira ticket: {e!s}")
        return {"error": f"Unexpected error: {e!s}"}
