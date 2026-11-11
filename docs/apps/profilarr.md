---
title: Profilarr
parent: Apps
nav_order: 12
---

# Profilarr

Profilarr strips secrets from its backups: Arr instances (their URLs, API keys
and sync settings), notification services, accounts and sessions, API keys,
linked database credentials, and AI and TMDB keys. Re-add them after a
restore.

## Restoring

Profilarr's restore is a button on its own **Settings > Backups** page,
applied when it restarts, with no API, so Backuparr can't restore it itself.
Pick Profilarr and a backup on Backuparr's **Restore** tab: instead of
**Restore** it shows a **Download** button and these steps.

![Restore tab with Profilarr picked: a Download button, the steps to restore it in Profilarr, and a link to this page]({{ '/screenshots/restore-manual.png' | relative_url }})

1. **Download** the backup. An encrypted backup downloads decrypted, as a
   `.tar.gz`; anyone with access to that computer can read it, so delete it
   when you're done.
2. In Profilarr, open **Settings > Backups**. The restore replaces all of
   Profilarr's data and can't be undone once it restarts, so click **Create
   Backup** first if you might need what's there.
3. Click **Upload Backup** and pick the downloaded file (Profilarr takes only
   `.tar.gz` files, up to 1 GB).
4. Click **Restore** on its row, then restart Profilarr, for example
   `docker restart profilarr`. Until then a banner names the backup and lets
   you cancel.
5. If you sign in with a password, Profilarr sends you to its setup page to
   create an account. Then add back the secrets listed above.

Profilarr's own guide, [Backups](https://profilarr.com/installation/backups),
covers the rest, including moving to a new machine. Settings in your compose
file (such as `ORIGIN`, `AUTH` and the `OIDC_` variables) aren't in any
backup, so keep that file too. The same backup can also be downloaded from
**History**.
