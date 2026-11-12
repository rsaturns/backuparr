---
title: Tautulli
parent: Apps
nav_order: 17
---

# Tautulli

- **Backup:** `download_database` removes Plex tokens, but `download_config`
  only removes `PMS_TOKEN`/`JWT_SECRET`. **The Tautulli API key and any
  notification credentials are in the backup in plain text** - rotate the key
  if a destination is ever compromised.
- **Restore** uploads the config, then asks Tautulli to apply it. With no login
  set up in Tautulli, it applies and Tautulli restarts. With a login enabled,
  the config is only staged: log in to Tautulli and open
  `<your Tautulli URL>/restart_import_config` (the Restore tab shows it). If
  Tautulli restarts first, the staged config is lost; restore again.
- **The database can't be restored over the API** (an upstream Tautulli bug):
  import `tautulli.db` by hand in Tautulli's **Settings > Import & Backup >
  Import Database**, as Tautulli's FAQ describes for
  [moving or reinstalling Tautulli](https://docs.tautulli.com/support/frequently-asked-questions#general-q11).
