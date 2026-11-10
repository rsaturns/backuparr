---
title: Profilarr
parent: Apps
nav_order: 8
---

# Profilarr

Profilarr strips secrets from its backups: arr URLs and API keys, sync
configs, notification webhooks and tokens, user accounts and sessions, linked
database tokens, and AI/TMDB keys. Re-add them after a restore.

Profilarr's restore has no API, so it isn't on the **Restore** tab. To restore
by hand: download the backup from Backuparr's **History** tab, upload and
restore it in Profilarr's **Settings > Backups**, then restart Profilarr.
