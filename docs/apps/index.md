---
title: Apps
nav_order: 4
has_children: true
has_toc: false
---

# Apps

How each app is backed up and restored. Each app's page has its setup and
the details.

## Media Management

| App | Backed up | Restored |
|---|---|---|
| [Radarr, Sonarr, Lidarr and Prowlarr](servarr.md) | Their own backup, over the API | From the web UI |
| [Whisparr](whisparr.md) (v2 and v3) | Its own backup, over the API | From the web UI |
| [Profilarr](profilarr.md) | Its own backup, over the API | By hand |
| [Bazarr](bazarr.md) | Its own backup, over the API | From the web UI, with its backup folder mounted |
| [Tdarr](tdarr.md) (2 and 3) | Its database, over the API | From the web UI |
| [LazyLibrarian](lazylibrarian.md) | Its own backup, from its mounted folder | From the web UI, while it's stopped |

If Radarr, Sonarr, Lidarr, Whisparr or Prowlarr asks every address to log
in, it needs its login on the card or **Disabled for Local Addresses** - see
[the note](servarr.md).

## Downloaders

| App | Backed up | Restored |
|---|---|---|
| [SABnzbd](sabnzbd.md) | Its settings, without passwords, over the API | From the web UI |
| [NZBGet](nzbget.md) | Its settings, over the API | From the web UI |

## Media Servers

| App | Backed up | Restored |
|---|---|---|
| [Plex](plex.md) | Its database over the API, its settings from its mounted folder | From the web UI, while it's stopped |
| [Jellyfin](jellyfin.md) (10.11+) | Its own backup, from its mounted folder | From the web UI |
| [Emby](emby.md) | Its databases and settings, from its mounted folder | From the web UI, while it's stopped |
| [Tautulli](tautulli.md) | Its database and config, over the API | The config from the web UI, the database by hand |
| [Seerr](seerr.md) | Its database and settings, from its mounted folder | From the web UI, while it's stopped |
| [Immich](immich.md) (2.5.0+) | Its database, not photos, over the API | From the web UI |

## Network

| App | Backed up | Restored |
|---|---|---|
| [Pi-hole](pihole.md) (v6+) | Teleporter, over the API | From the web UI |
| [OPNsense](opnsense.md) (24.7+) | `config.xml`, over the API | By hand |
| [pfSense](pfsense.md) (CE and Plus) | `config.xml`, from its web UI | By hand |
| [UniFi](unifi.md) | Its own backup, from its web UI | By hand |

## Security

| App | Backed up | Restored |
|---|---|---|
| [Bitwarden](bitwarden.md) (self-hosted) | Its data and database backup, from its mounted folder | By hand |
| [Vaultwarden](vaultwarden.md) | Its data, from its mounted folder | From the web UI, while it's stopped |

## Home Automation

| App | Backed up | Restored |
|---|---|---|
| [Home Assistant](homeassistant.md) (2025.5+) | Its own backup, over its API | From the web UI |

## Server

| App | Backed up | Restored |
|---|---|---|
| [Unraid](unraid.md) | Its boot device, from its web UI | By hand |
| [Synology DSM](synology.md) (7 and 6.2) | Its settings, not your data, from its web UI | By hand |
| [QNAP QuTS hero](qnap.md) (h5.2+) | Its settings, not your data, from its web UI | By hand |

## Good to know

- Backups of Pi-hole, Bitwarden, Vaultwarden, OPNsense, pfSense, UniFi, Home
  Assistant, Unraid, Synology DSM and QNAP QuTS hero are always encrypted -
  see [Encrypted backups](../security.md#encrypted-backups).
- [What each restore does](../restoring.md#what-each-restore-does) lists what
  happens when each app is restored.
- For the apps with a mounted folder, read-only is enough for backups;
  restoring from the web UI needs it read-write, with Backuparr running as the
  app's `PUID`/`PGID` (or otherwise able to write the app's files).
- Running more than one instance of an app? See
  [More than one instance](../settings.md#more-than-one-instance).
