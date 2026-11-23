---
title: First run
parent: Getting started
nav_order: 4
---

# First run

1. Open `https://<host>:8991`. Your browser warns about Backuparr's
   self-signed certificate the first time; see [HTTPS](../security.md#https).
   Plain HTTP on `http://<host>:8990` works too.
2. Create the admin account on the setup screen. Rebuilding a server? Choose
   to [restore Backuparr](../restoring.md#restoring-backuparr-itself) there
   instead.
3. Backuparr shows your [encryption key](../security.md#your-encryption-key).
   Save it in a password manager before you continue: you need it to restore
   Backuparr.
4. On the **Settings** tab, switch on each app to back up, enter its URL
   (e.g. `http://radarr:7878` on the same Compose network, or `IP:port` for
   apps on host networking) and its API key, and click **Test connection**.
   Using Prowlarr? Set it up first and click **Discover services** - see
   [Prowlarr discovery](../settings.md#prowlarr-discovery).
5. Enable at least one [destination](../destinations.md).
6. Set retention and a schedule under **Schedule & Notifications**.
7. Click **Save settings**.

![Settings tab: apps grouped by category on the left, each category folding away under a header with its enabled count, Server open on Backuparr's own card, and destinations on the right, Local Storage's retention saying how many backups of each app it keeps and their size]({{ '/screenshots/settings.png' | relative_url }})

Then click **Run backup now** on **Run & Status** to make your first backup.
See [Backups and history](../backups.md) for the schedule, retention, logs and
the History tab, and [Restoring](../restoring.md) for restores.
