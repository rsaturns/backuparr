---
title: Apps
nav_order: 3
has_children: true
has_toc: false
---

# Apps

| App | Backup | Restore |
|---|---|---|
| Radarr / Sonarr / Lidarr / Prowlarr | The app's own backup zip (`POST /system/backup`), downloaded and then deleted on the server | Uploaded to `/system/backup/restore/upload`; the app restarts itself |
| Profilarr | The app's own backup (`POST /api/v1/backups`), downloaded and then deleted on the server | Manual only - see the [Profilarr note](profilarr.md) |
| Bazarr | The app's own backup (`POST /api/system/backups`), downloaded and then deleted on the server | Written into Bazarr's mounted backup folder, then restored over the API. Downloads depend on Bazarr's web auth - see the [Bazarr note](bazarr.md) |
| Tdarr | Every internal database collection, read over `/api/v2/cruddb` | Destructive: each collection is wiped, then refilled - see the [Tdarr note](tdarr.md) |
| SABnzbd | `get_config` over the API. SABnzbd masks every password as `**********` | Settings and Usenet servers over `set_config`; you're asked for each server's password. Categories, RSS feeds and sorters aren't restored |
| NZBGet | Every setting, real passwords included, over JSON-RPC `loadconfig` | `saveconfig`, then NZBGet reloads - see the [NZBGet note](nzbget.md) |
| Tautulli | `download_database` and `download_config` | Config only, via `import_config` - see the [Tautulli note](tautulli.md) |
| Jellyfin (10.11+) | Jellyfin's own backup (`POST /Backup/Create`), picked up from its mounted backups folder | Over `POST /Backup/Restore`; Jellyfin restarts - see the [Jellyfin note](jellyfin.md) |
| Emby | Settings, plugins, lists and databases from its mounted data folder, while Emby runs | Written back while Emby is stopped - see the [Emby note](emby.md) |
| Plex | Plex's database export (`GET /diagnostics/databases`) plus `Preferences.xml` from its mounted data folder | Written back while Plex is stopped; the server keeps its identity - see the [Plex note](plex.md) |
| Seerr | Database and `settings.json` from its mounted config folder, while Seerr runs | Written back while Seerr is stopped - see the [Seerr note](seerr.md) |

For the apps with a mounted folder, read-only is enough for backups; restoring
from the web UI needs it read-write, with Backuparr running as the app's
`PUID`/`PGID` (or otherwise able to write the app's files).

Running more than one instance of an app? See
[More than one instance](../settings.md#more-than-one-instance).
