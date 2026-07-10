# Microsoft Teams Webhook Integration Guide

This guide explains how to automatically forward Microsoft Teams messages (e.g., when a channel receives a new message) to the Chandra Digital Worker using Microsoft Power Automate.

## Prerequisites
- A Microsoft 365 / Teams account.
- Your Chandra backend must be running and exposed to the internet (e.g., using `ngrok`).

## Step-by-Step Setup

### Step 1: Create the Trigger
1. Log in to [Microsoft Power Automate](https://make.powerautomate.com/).
2. Create an **Automated cloud flow**.
3. Select a Teams trigger, such as:
   - **When a new channel message is added**
   - **When I am mentioned in a channel message**
4. Configure the trigger by selecting the appropriate **Team** and **Channel**.

### Step 2: Add the HTTP Action
1. Click **New step** and search for **HTTP**.
2. Select the standard **HTTP** action (the one with the green icon, noting that it is a premium connector).
3. Configure the HTTP action as follows:
   - **Method**: Select `POST`.
   - **URI**: Enter your Chandra webhook URL. If using ngrok, it should look like:
     `https://<your-ngrok-id>.ngrok-free.app/webhooks/teams`
   - **Headers**:
     - Key: `Content-Type` | Value: `application/json`
     - *(Optional: If you set `CHANDRA_WEBHOOK_TOKEN` in your `.env` file, add a second header: Key: `X-Chandra-Webhook-Token` | Value: `<your-secret-token>`)*
   - **Body**: Click inside the box and use the "Dynamic content" popup to map the Teams message fields. For example:
     ```json
     {
       "title": "Teams Request from @{triggerOutputs()?['body/from/user/displayName']}",
       "text": "@{triggerOutputs()?['body/body/content']}",
       "channel": "@{triggerOutputs()?['body/channelIdentity/channelId']}"
     }
     ```

### Step 3: Test It
1. Save the flow.
2. Click the **Test** button in the top right corner, select **Manually**, and click **Test**.
3. Go to Microsoft Teams and send a message in the monitored channel (or mention the bot, depending on your trigger).
4. Check your Chandra Human Approval Center dashboard. The flow will trigger and a new Action Card will appear instantly with the Teams message content!
