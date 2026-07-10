# Email Webhook Integration Guide

This guide explains how to automatically forward incoming emails to the Chandra Digital Worker using Microsoft Power Automate.

## Prerequisites
- A Microsoft 365 / Outlook account.
- Your Chandra backend must be running and exposed to the internet (e.g., using `ngrok`).

## Step-by-Step Setup

### Step 1: Create the Trigger
1. Log in to [Microsoft Power Automate](https://make.powerautomate.com/).
2. Create an **Automated cloud flow**.
3. Select the trigger: **When a new email arrives (V3)** (Office 365 Outlook).
4. Configure any specific folders (like "Inbox") or filters (like specific senders or subjects) if you don't want every email to trigger Chandra.

### Step 2: Add the HTTP Action
1. Click **New step** and search for **HTTP**.
2. Select the standard **HTTP** action (the one with the green icon, noting that it is a premium connector).
3. Configure the HTTP action as follows:
   - **Method**: Select `POST`.
   - **URI**: Enter your Chandra webhook URL. If using ngrok, it should look like:
     `https://<your-ngrok-id>.ngrok-free.app/webhooks/email`
   - **Headers**:
     - Key: `Content-Type` | Value: `application/json`
     - *(Optional: If you set `CHANDRA_WEBHOOK_TOKEN` in your `.env` file, add a second header: Key: `X-Chandra-Webhook-Token` | Value: `<your-secret-token>`)*
   - **Body**: Click inside the box and use the "Dynamic content" popup to map the email fields. For example:
     ```json
     {
       "subject": "@{triggerOutputs()?['body/subject']}",
       "message": "@{triggerOutputs()?['body/bodyPreview']}",
       "from": "@{triggerOutputs()?['body/from']}"
     }
     ```

### Step 3: Test It
1. Save the flow.
2. Click the **Test** button in the top right corner, select **Manually**, and click **Test**.
3. Send an email to the inbox being monitored (e.g., send an email to yourself).
   - **Subject**: `Please provision a new S3 bucket for logs`
   - **Body**: `We need a new S3 bucket created for the upcoming log review.`
4. Check your Chandra Human Approval Center dashboard. A new Action Card should appear instantly, populated with the email content!
