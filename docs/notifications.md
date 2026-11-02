---
title: Notifications
nav_order: 8
---

# Notifications

Set **Notify URL** on the Settings tab to get a summary after every run: one
✅/❌ line per app. Backuparr recognises the services below from the URL and
formats the message for them.

## Discord

In the channel: **Edit Channel > Integrations > Webhooks > New Webhook > Copy
Webhook URL**, and paste it into **Notify URL**.

## Slack

Create an [incoming webhook](https://api.slack.com/messaging/webhooks) and
paste its `https://hooks.slack.com/services/...` URL into **Notify URL**.

## Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Message the bot once, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `chat.id` (for a
   group, add the bot first and use the group's negative ID).
3. Set **Notify URL** to
   `https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>`.

## Gotify

Create an application in Gotify and set **Notify URL** to
`https://<your-gotify-host>/message?token=<APP_TOKEN>`.

## ntfy.sh, Healthchecks.io, or anything else

Any other URL gets the message as a plain-text `POST`, which is
[ntfy.sh](https://ntfy.sh)'s format (e.g. `https://ntfy.sh/your-topic`). This
also works for [healthchecks.io](https://healthchecks.io) ping URLs, Uptime
Kuma push URLs, and other webhooks that accept raw text.
