---
title: Configuration reference
nav_order: 10
---

# Configuration reference

`config.json`, edited in the web UI or by hand:

| Field | Purpose |
|---|---|
| `apps.<name>.enabled/url/api_key` | Per app, as on the Settings tab |
| `apps.<radarr\|sonarr\|lidarr\|whisparr>-<suffix>` | An extra instance (e.g. `radarr-4k`): the same fields plus `name` |
| `apps.bazarr.username/password` | Only if Bazarr's web auth is `Basic` |
| `apps.nzbget.username/password` | NZBGet's Control username and password |
| `apps.jellyfin.backup_dir` | Jellyfin's mounted backups folder - required |
| `apps.emby.data_dir` | Emby's mounted data folder (its `/config`) - required |
| `apps.plex.data_dir` | Plex's mounted `Plex Media Server` folder - required |
| `apps.seerr.data_dir` | Seerr's mounted config folder (its `/app/config`) - required |
| `apps.lazylibrarian.data_dir` | LazyLibrarian's mounted data folder (its `/config`) - required |
| `bazarr_backup_dir` | Bazarr's mounted config/backup folder, for restores |
| `request_headers` | [Service authentication headers](settings.md#service-authentication-headers): `[{"name": "Proxy-Authorization", "value": "Basic …", "apps": ["radarr"]}]`; omit `apps` for every app |
| `destinations.local.enabled/path` | Local storage; the path defaults to `/config/backuparr/backups` |
| `destinations.gdrive.enabled/client_id/client_secret/developer_key` | Google Drive OAuth client and API key, from the Setup guide |
| `destinations.gdrive.refresh_token/folder_id/folder_name` | Set by Connect/Choose folder - don't edit |
| `destinations.onedrive.enabled`, `destinations.dropbox.enabled` | Whether the destination is used |
| `destinations.onedrive.token/drive_id/drive_type/item_id`, `destinations.dropbox.token` | Set by the Connect buttons - don't edit |
| `retention_days` | Delete backups older than this, per app and destination, always keeping each app's newest (default 7) |
| `cron_schedule` | 5-field cron schedule (default `0 3 * * *`) |
| `notify_url` | Optional summary after every run - see [Notifications](notifications.md) |

## The backup record

`manifest.json`, beside `config.json`, lists each backup Backuparr made: the
destinations holding it, the version of the app it came from, Backuparr's
version, its size and its SHA-256. Backuparr keeps it up to date - don't edit
it. Backups made before 2.5.0 are added in the background after the update:
each one is read once. Local storage is read first, and a cloud copy of a
backup already read there isn't downloaded; any other backup on Google Drive,
OneDrive or Dropbox is downloaded one time. This pauses while a backup or
restore runs, and later startups only download backups that aren't recorded
yet. On a slow or metered connection, set
[`RCLONE_BWLIMIT`](environment-variables.md). Bazarr, SABnzbd and Tdarr
backups made before 2.5.0 don't hold a version, so theirs is unknown.
