# Slack Webhook Integration Guide

This guide explains how to connect your Slack workspace to the Chandra Digital Worker via the Slack Events API. When a user mentions the bot or sends it a direct message, it will automatically be forwarded to the backend and appear in your Human Approval Center dashboard.

## Prerequisites
- A Slack Workspace where you have permission to install apps.
- Your backend must be running and exposed to the internet (e.g., using `ngrok`).

## Step-by-Step Setup

1. **Create the Slack App**
   - Go to [api.slack.com/apps](https://api.slack.com/apps) and click **Create New App**.
   - Select **From scratch**.
   - Give your app a name (e.g., "AWS Digital Worker") and pick the workspace you want to install it in.

2. **Enable Event Subscriptions**
   - On the left sidebar of your app configuration page, click on **Event Subscriptions**.
   - Toggle **Enable Events** to **On**.
   - Under **Request URL**, enter your webhook endpoint. If using ngrok, it should look like:
     `https://<your-ngrok-url>/webhooks/slack`
     *(Example: `https://4bec-196-12-41-154.ngrok-free.app/webhooks/slack`)*
   - *Note: Slack will instantly send a challenge to this URL to verify it. Your backend must be running. If it works, you will see a green "Verified" checkmark.*

3. **Subscribe to Bot Events**
   - Scroll down to the **Subscribe to bot events** section.
   - Click the **Add Bot User Event** button.
   - Search for and add the following two events:
     - `app_mention`: Allows the bot to read messages where it is explicitly @mentioned in a channel.
     - `message.im`: Allows the bot to read direct messages sent to it privately.
   - Keep "Delayed Events" toggled **Off** for local testing, but you may turn it **On** for production.
   - Click **Save Changes** at the bottom right.

4. **Enable Messaging (App Home)**
   - On the left sidebar, click on **App Home** (under the Features section).
   - Scroll down to the **Show Tabs** section.
   - Check the box that says: **Allow users to send Slash commands and messages from the messages tab**. This unlocks the chat box so users can actually message the bot.

5. **Install the App to Your Workspace**
   - On the left sidebar, click on **Install App**.
   - Click the green **Install to Workspace** (or **Reinstall to Workspace**) button.
   - Review the permissions requested and click **Allow**.

6. **Test the Integration**
   - Open your Slack application or workspace in the browser.
   - Under the "Apps" section in the sidebar, find your bot and open a Direct Message with it.
   - Send it a message (e.g., *"Please check if the acme-logs S3 bucket is public"*).
   - Check your Chandra Human Approval Center dashboard; the new Slack request should appear immediately!
