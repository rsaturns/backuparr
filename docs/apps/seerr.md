---
title: Seerr
parent: Apps
nav_order: 11
---

# Seerr

Seerr has no backup API, so Backuparr reads **Seerr's config folder** (its
`/app/config`):

```yaml
    volumes:
      - /share/Container/seerr/config:/mnt/seerr:ro   # drop :ro to restore from the web UI
```

Then fill in the card: the URL, the API key from **Settings > General**, and
**Data folder** = `/mnt/seerr`. Seerr only accepts its API key once its setup
is finished.

- **What's in the backup:** what [Seerr's backup
  guide](https://docs.seerr.dev/using-seerr/backups/) lists - the database
  (users, requests, issues, blocklist) and `settings.json` (every setting,
  including the media server, Radarr/Sonarr and notification credentials, and
  Seerr's API key). The image cache and logs are left out.
- **Only SQLite is supported** (Seerr's default). With PostgreSQL, use
  `pg_dump` as Seerr's guide describes.
- **Restore needs Seerr stopped** (`docker stop seerr`); Backuparr refuses while
  it's running. It replaces the database and `settings.json`, then you start
  Seerr. The backup must come from the same Seerr version. Run Backuparr as `1000:1000` (Seerr's user in the official image).
- **Rebuilding:** start the new Seerr once (no need to finish setup), stop it,
  then restore. Its API key comes back too, so keep the original key in
  Settings (unless Seerr's `API_KEY` environment variable sets it).
- **To restore by hand:** stop Seerr, put `db/db.sqlite3` in its `db` folder
  (deleting any `db.sqlite3-wal` and `db.sqlite3-shm` there) and
  `settings.json` in its config folder, then start Seerr.
