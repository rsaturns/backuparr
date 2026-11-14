---
title: Configuration reference
nav_order: 13
---

# Configuration reference

`config.json`, edited in the web UI or by hand. Fields marked "don't edit"
are set by Backuparr.

## Apps

Each app's settings are under `apps.<id>`: the app's id (`radarr`, `pihole`)
or an extra instance's (`radarr-4k`).

| Field | Purpose |
|---|---|
| `enabled`, `url`, `api_key` | As on the Settings tab |
| `encrypt` | Encrypt its backups (default `true`); ignored for an app that's always encrypted - see [Encrypted backups](security.md#encrypted-backups) |
| `name` | An extra instance's name. Radarr, Sonarr, Lidarr, Whisparr, Pi-hole, OPNsense, pfSense, UniFi, Immich, Home Assistant, Unraid, Synology DSM and QNAP QuTS hero can have [extra instances](settings.md#more-than-one-instance), with the same fields as the first |
| `username`, `password` | A login: Radarr's, Sonarr's, Lidarr's, Whisparr's and Prowlarr's own, only if it asks every address to log in ([note](apps/servarr.md)); Bazarr's, only if its web auth is `Basic`; NZBGet's Control user; the pfSense, UniFi, Synology DSM and QNAP QuTS hero account; Unraid's root `password`. Passwords are encrypted at rest |
| `trusted_cert` | The self-signed certificate trusted on the card, for OPNsense, pfSense, UniFi, Unraid, Synology DSM, QNAP QuTS hero, Home Assistant and Bitwarden: `{"sha256", "subject", "not_after"}`; set by **Trust** |

These are under `apps.<id>` too, for one app:

| Field | Purpose |
|---|---|
| `jellyfin.backup_dir` | Jellyfin's mounted backups folder - required |
| `emby.data_dir` | Emby's mounted data folder (its `/config`) - required |
| `plex.data_dir` | Plex's mounted `Plex Media Server` folder - required |
| `jellyfin.metadata`, `emby.metadata`, `plex.metadata` | Include metadata: `true` adds the app's metadata (posters and artwork) to its backups; default `false` |
| `seerr.data_dir` | Seerr's mounted config folder (its `/app/config`) - required |
| `lazylibrarian.data_dir` | LazyLibrarian's mounted data folder (its `/config`) - required |
| `bitwarden.data_dir` | Bitwarden's mounted `bwdata` folder (standard install) or lite's mounted backups folder - required |
| `vaultwarden.data_dir` | Vaultwarden's mounted data folder (its `/data`) - required |
| `opnsense.api_secret` | The secret of OPNsense's API key (encrypted at rest) - required |
| `unifi.backup` | On UniFi OS: `everything` (UniFi OS's own backup, the default; needs a Super Admin) or `network` (UniFi Network's) |
| `unifi.history` | Statistics kept in a UniFi Network backup: `settings` (the default), `7`, `30`, `90`, `365` (days) or `all` |
| `unifi.install_type` | `unifi-os` or `standalone`, set by **Test connection** - don't edit |
| `synology.device_token`, `qnap.device_token` | The remembered sign-in after a 2FA code (encrypted at rest), set by **Test connection** - don't edit |
| `homeassistant.history` | Back up Home Assistant's history (default `true`) |
| `homeassistant.media`, `homeassistant.share`, `homeassistant.local_apps` | Back up the Media, Share and Local apps folders, on HA OS and Supervised (default `false`) |
| `homeassistant.apps` | Apps to back up, on HA OS and Supervised: `none` (the default), `all` or `chosen` |
| `homeassistant.chosen_apps` | The apps picked when `apps` is `chosen`: `[{"slug", "name"}]` |
| `homeassistant.install_type` | `core` or `supervisor`, set by **Test connection** - don't edit |

## Destinations

Each destination's settings are under `destinations.<id>`: `local`,
`gdrive`, `onedrive` or `dropbox`.

| Field | Purpose |
|---|---|
| `enabled` | Whether the destination is used |
| `retention_days` | This destination's own retention in days; `null` (the default) uses the default retention |
| `local.path` | Local Storage's folder; defaults to `/config/backuparr/backups` |
| `gdrive.client_id`, `gdrive.client_secret`, `gdrive.developer_key` | Google Drive's OAuth client and API key, from the Setup guide |
| `gdrive.refresh_token`, `gdrive.folder_id`, `gdrive.folder_name` | Set by **Connect Google Drive** and **Choose folder** - don't edit |
| `onedrive.token`, `onedrive.drive_id`, `onedrive.drive_type`, `onedrive.item_id` | Set by **Connect OneDrive** - don't edit |
| `dropbox.token` | Set by **Connect Dropbox** - don't edit |

## Schedule and other settings

| Field | Purpose |
|---|---|
| `retention_days` | The default retention: delete backups older than this, per app and destination, always keeping each app's newest (default 7) |
| `cron_schedule` | 5-field cron schedule (default `0 3 * * *`) |
| `notify_url` | Optional summary after every run - see [Notifications](notifications.md) |
| `request_headers` | [Service authentication headers](settings.md#service-authentication-headers): `[{"name": "Proxy-Authorization", "value": "Basic …", "apps": ["radarr"]}]`; omit `apps` for every app |
| `bazarr_backup_dir` | Bazarr's mounted config/backup folder, for restores |

## Backuparr's own card

| Field | Purpose |
|---|---|
| `self_backup` | Back up Backuparr's own configuration, encrypted, at the end of each full run (default `true`) - see [Backuparr's own backup](security.md#backuparrs-own-backup) |
| `https_only` | Plain HTTP closed, HTTPS only (default `false`); `BACKUPARR_HTTPS_ONLY` overrides it - see [HTTPS](security.md#https) |
| `ha_enabled`, `ha_token_hash` | The [Home Assistant](home-assistant.md) integration's switch and its token's SHA-256 - don't edit |

## Set by Backuparr

| Field | Purpose |
|---|---|
| `secrets_key_acknowledged` | Set once you've confirmed saving your encryption key - don't edit |
| `encryption_notice_acknowledged` | Set once the "Backups Are Now Encrypted" notice has been seen - don't edit |
| `upgrade_self_backup_tried`, `upgrade_self_backup_made` | Set once the first start of 3.0 has made Backuparr's own backup, and whether it worked - don't edit |
| `removed_instances` | The ids of removed extra instances: their backups are kept, so an id isn't used again - don't edit |

## Other files

Beside `config.json`, Backuparr keeps `manifest.json`
([the backup record](backups.md#the-backup-record)) and `run_logs.json`
([run logs](backups.md#run-logs)). Don't edit them.
