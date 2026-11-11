---
title: Configuration reference
nav_order: 13
---

# Configuration reference

`config.json`, edited in the web UI or by hand:

| Field | Purpose |
|---|---|
| `apps.<name>.enabled/url/api_key` | Per app, as on the Settings tab |
| `apps.<name>.encrypt` | Encrypt its backups (default `true`); ignored for an app that's always encrypted - see [Encrypted backups](security.md#encrypted-backups) |
| `apps.<radarr\|sonarr\|lidarr\|whisparr>-<suffix>` | An extra instance (e.g. `radarr-4k`): the same fields plus `name` |
| `apps.<radarr\|sonarr\|lidarr\|whisparr\|prowlarr>.username/password` | The app's own login, only if it asks every address to log in ([note](apps/servarr.md)) |
| `apps.bazarr.username/password` | Only if Bazarr's web auth is `Basic` |
| `apps.nzbget.username/password` | NZBGet's Control username and password |
| `apps.jellyfin.backup_dir` | Jellyfin's mounted backups folder - required |
| `apps.emby.data_dir` | Emby's mounted data folder (its `/config`) - required |
| `apps.plex.data_dir` | Plex's mounted `Plex Media Server` folder - required |
| `apps.seerr.data_dir` | Seerr's mounted config folder (its `/app/config`) - required |
| `apps.lazylibrarian.data_dir` | LazyLibrarian's mounted data folder (its `/config`) - required |
| `apps.vaultwarden.data_dir` | Vaultwarden's mounted data folder (its `/data`) - required |
| `apps.opnsense.api_secret` | The secret of OPNsense's API key (encrypted at rest) - required |
| `apps.pfsense.username/password` | The pfSense user's login (the password encrypted at rest) - required |
| `apps.<opnsense\|pfsense>.trusted_cert` | The self-signed certificate trusted on the card: `{"sha256", "subject", "not_after"}`; set by **Trust** |
| `bazarr_backup_dir` | Bazarr's mounted config/backup folder, for restores |
| `request_headers` | [Service authentication headers](settings.md#service-authentication-headers): `[{"name": "Proxy-Authorization", "value": "Basic …", "apps": ["radarr"]}]`; omit `apps` for every app |
| `destinations.local.enabled/path` | Local Storage; the path defaults to `/config/backuparr/backups` |
| `destinations.gdrive.enabled/client_id/client_secret/developer_key` | Google Drive OAuth client and API key, from the Setup guide |
| `destinations.gdrive.refresh_token/folder_id/folder_name` | Set by Connect/Choose folder - don't edit |
| `destinations.onedrive.enabled`, `destinations.dropbox.enabled` | Whether the destination is used |
| `destinations.onedrive.token/drive_id/drive_type/item_id`, `destinations.dropbox.token` | Set by the Connect buttons - don't edit |
| `destinations.<id>.retention_days` | This destination's own retention in days; `null` (the default) uses `retention_days` |
| `retention_days` | Delete backups older than this, per app and destination, always keeping each app's newest (default 7) |
| `cron_schedule` | 5-field cron schedule (default `0 3 * * *`) |
| `notify_url` | Optional summary after every run - see [Notifications](notifications.md) |
| `ha_enabled`, `ha_token_hash` | The [Home Assistant](home-assistant.md) integration's switch and its token's SHA-256; set only by the Backuparr card |
| `https_only` | Plain HTTP closed, HTTPS only (default `false`); set only by the Backuparr card, and `BACKUPARR_HTTPS_ONLY` overrides it - see [HTTPS](security.md#https) |
| `self_backup` | Back up Backuparr's own configuration, encrypted, at the end of each full run (default `true`; the switch on Settings' Backuparr card) - see [Backuparr's own backup](security.md#backuparrs-own-backup) |
| `secrets_key_acknowledged` | Set once you've confirmed saving your encryption key - don't edit |
| `encryption_notice_acknowledged` | Set once the "Backups Are Now Encrypted" notice has been seen - don't edit |
| `upgrade_self_backup_tried`, `upgrade_self_backup_made` | Set once the first start of 3.0 has made Backuparr's own backup, and whether it worked - don't edit |

## Other files

Beside `config.json`, Backuparr keeps `manifest.json`
([the backup record](backups.md#the-backup-record)) and `run_logs.json`
([run logs](backups.md#run-logs)). Don't edit them.
