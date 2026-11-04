---
title: First run
parent: Getting started
nav_order: 4
---

# First run

Open `http://<host>:8990` and create the admin account on the setup screen.
Then, on the **Settings** tab:

1. Switch on each app to back up, enter its URL (e.g. `http://radarr:7878` on
   the same Compose network, or `IP:port` for apps on host networking) and its
   API key, and click **Test connection**. Using Prowlarr? Set it up first and
   click **Discover services** - see [Prowlarr discovery](../settings.md#prowlarr-discovery).
2. Enable at least one [destination](../destinations.md).
3. Set retention and a schedule. Retention deletes each app's backups older
   than the number of days you set, but always keeps its newest one.
4. Click **Save settings**. Schedule changes apply without a restart.

**Run & Status** runs a backup now - of every app, or just one - and shows it
live, **History** lists, downloads and deletes backups, and **Restore** restores them.

Each app's log from each run is kept, for as long as retention keeps backups
(its latest run is always kept). **View log** on an app's Overview tile shows
its latest run; in History, each backup has its own, and runs that left no
backup on that destination are listed greyed with the reason.
