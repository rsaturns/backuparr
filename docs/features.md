---
title: Features
nav_order: 2
---

# Features

## Apps

- Backs up 16 apps: Radarr, Sonarr, Lidarr, Whisparr (v2 and v3), Prowlarr,
  Profilarr, Bazarr, Tdarr (2 and 3), SABnzbd, NZBGet, Tautulli, Jellyfin,
  Emby, Plex, Seerr and LazyLibrarian. See [Apps](apps/index.md).
- Most apps are backed up through their own API. Jellyfin, Emby, Plex, Seerr
  and LazyLibrarian also need a folder mounted into Backuparr.
- Up to 10 extra instances each of Radarr, Sonarr, Lidarr and Whisparr, such as
  a 4K Radarr. See [More than one instance](settings.md#more-than-one-instance).
- **Prowlarr discovery** fills in the apps Prowlarr already knows, API keys
  included. See [Prowlarr discovery](settings.md#prowlarr-discovery).
- **Test connection** on every app card.
- Signs in to Radarr, Sonarr, Lidarr, Whisparr and Prowlarr with their own
  login when they ask for one. See [the note](apps/servarr.md).
- Works with apps behind an authenticating reverse proxy. See
  [Service authentication headers](settings.md#service-authentication-headers).

## Destinations

- Local storage, Google Drive, OneDrive and Dropbox. Every run uploads to each
  one you enable. See [Destinations](destinations.md).
- Cloud connections are made in the web UI, with no rclone config.
- An optional bandwidth cap for uploads and downloads (`RCLONE_BWLIMIT`).

## Backups

- Scheduled backups (daily, weekly, every few hours or any cron schedule), and
  **Run backup now** for every app or just one.
- Each backup is checked before it's uploaded. A damaged one isn't uploaded,
  and the app shows as failed with the reason.
- Retention by age, set once or per destination, always keeping each app's
  newest backup.
- A live log while a backup runs, and each app's log from every run, kept with
  its backups.
- **History** lists, downloads and deletes backups on each destination, with
  the app version each backup came from.
- A summary after every run to Discord, Slack, Telegram, Gotify, ntfy or any
  webhook. See [Notifications](notifications.md).

See [Backups and history](backups.md).

## Restores

- Restore from the web UI, with live progress and a log. Every app can be
  restored this way except Profilarr and Tautulli's database, which are
  restored by hand.
- A backup is only restored onto the exact app version it came from. When the
  versions differ, the message names the version to install.
- Each download is checked against the checksum recorded when the backup was
  made.
- **Restore to a different target**: a one-off URL and key, for a rebuilt
  server or a trial restore.

See [Restoring](restoring.md).

## Backuparr's own backup

- At the end of each full run, Backuparr backs up its own settings, cloud
  connections, login, backup record and run logs to every destination,
  encrypted with your encryption key.
- On a new install, restore it from the setup screen. On a running install,
  restore it from Settings.

See [Restoring Backuparr itself](restoring.md#restoring-backuparr-itself).

## Security

- An admin login, a lockout after repeated wrong passwords, and a password
  change from the user menu.
- A forgotten password can be replaced with your encryption key, keeping your
  settings. See [Forgot your password?](security.md#forgot-your-password).
- Backups are encrypted with your encryption key before upload; turn it off
  per app, except for apps that are always encrypted. See
  [Encrypted backups](security.md#encrypted-backups).
- API keys, passwords and cloud tokens are encrypted at rest.
- Local login can be turned off behind an authenticating reverse proxy.

See [Login and security](security.md).

## Install

- A Docker image for `linux/amd64` and `linux/arm64`, on Docker Hub and GitHub
  Container Registry.
- An Unraid template in Community Applications.

See [Getting started](getting-started.md).
