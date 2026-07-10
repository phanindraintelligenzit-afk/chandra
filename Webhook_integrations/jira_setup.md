# Jira Webhook Integration Guide

This guide explains how to connect your Jira workspace to the Chandra Digital Worker via webhooks. When a user creates a new Jira ticket, it will automatically be forwarded to the backend and appear in your Human Approval Center dashboard.

## Prerequisites
- Administrator access to your Jira workspace.
- Your backend must be running and exposed to the internet (e.g., using `ngrok`).

## Step-by-Step Setup

1. **Access Webhook Settings**
   - Log into your Jira Workspace.
   - Click the gear icon (⚙️) in the top right to open **Settings**, then select **System**.
   - Scroll down the left sidebar under the "Advanced" section and click on **WebHooks**.

2. **Create a New Webhook**
   - Click the **Create a WebHook** button at the top right of the screen.
   - Fill in the basic details:
     - **Name:** `Chandra AI Digital Worker` (or any name you prefer)
     - **Status:** Ensure this is set to `Enabled`.
     - **URL:** Enter your webhook endpoint URL. If you are using ngrok, it should look like:
       `https://<your-ngrok-url>/webhooks/jira`
       *(Example: `https://4bec-196-12-41-154.ngrok-free.app/webhooks/jira`)*

3. **Configure Events**
   - Scroll down to the **Events** section.
   - Under the **Issue related events** category, find the **Issue** row.
   - Check the box for **created**. 
   - *(Optional)* If you want updates to be synced as well, you can check **updated**.

4. **Save and Test**
   - Scroll to the bottom and click **Create** (or **Save**).
   - To test the integration, create a new issue in your Jira project. 
   - Check your Chandra Human Approval Center dashboard; the new Jira ticket should appear immediately as a pending request!
