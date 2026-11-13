---
title: Emby
parent: Apps
nav_order: 3
---

# Emby

## Setting it up

Emby has no backup API, so Backuparr reads **Emby's data folder** (its
`/config`, in both the official and linuxserver images):

```yaml
    volumes:
      - /share/Container/emby/config:/mnt/emby:ro   # drop :ro to restore from the web UI
```

Then fill in the card: the URL, an API key from **Settings > Advanced > API
Keys**, and **Data folder** = `/mnt/emby`. **Test connection** also checks that
the folder belongs to this server.

## Backups and restores

- **What's in the backup:** the databases (users, watched state, favourites,
  playlists, collections, libraries, API keys), server and user settings,
  plugins, and the playlist and collection folders - copied safely while Emby
  runs. Images, cache, logs and transcodes are left out. The archive holds
  password hashes and API keys.
- **Restore needs Emby stopped** (`docker stop emby`); Backuparr refuses while
  it's running. It replaces everything above, then you start Emby. The backup
  must come from the same Emby version. On a new install, start Emby once (the wizard
  can be skipped) and stop it before restoring. API keys come back too, so keep
  the original key in Settings.
- **To restore by hand:** stop Emby, extract the archive into its `/config`
  folder, delete any `data/*.db-wal` and `data/*.db-shm` files, and start Emby.
  Emby's own guide to the same files: [How to Backup Manually](https://emby.media/support/articles/Backup-Manual-Backups.html).
