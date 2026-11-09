---
title: First run
parent: Getting started
nav_order: 4
---

# First run

Open `http://<host>:8990` and create the admin account on the setup screen
(rebuilding a server? choose to [restore Backuparr](../restoring.md#restoring-backuparr-itself)
there instead).
Backuparr then shows your [encryption key](../security.md#your-encryption-key):
save it in a password manager before you continue - you need it to restore
Backuparr. Then, on the **Settings** tab:

1. Switch on each app to back up, enter its URL (e.g. `http://radarr:7878` on
   the same Compose network, or `IP:port` for apps on host networking) and its
   API key, and click **Test connection**. Using Prowlarr? Set it up first and
   click **Discover services** - see [Prowlarr discovery](../settings.md#prowlarr-discovery).
2. Enable at least one [destination](../destinations.md).
3. Set retention and a schedule under **Schedule & Notifications**.
4. Click **Save settings**.

![Settings tab: apps grouped by category on the left, each category folding away under a header with its enabled count, and destinations on the right]({{ '/screenshots/settings.png' | relative_url }})

Then click **Run backup now** on **Run & Status** to make your first backup.
See [Backups and history](../backups.md) for the schedule, retention, logs and
the History tab, and [Restoring](../restoring.md) for restores.
