---
title: Home
nav_order: 1
description: Scheduled backups and web-UI restores for your *arr stack and media servers.
permalink: /
---

# Backuparr

**Everything you host, backed up.**

Scheduled config/database backups for Radarr, Sonarr, Lidarr, Whisparr,
Prowlarr, Profilarr, Bazarr, Tdarr, SABnzbd, NZBGet, Tautulli, Jellyfin, Emby,
Plex, Seerr, Immich, LazyLibrarian, Pi-hole, Vaultwarden, OPNsense, and pfSense,
sent to every destination you enable: Local storage, Google Drive, OneDrive, and
Dropbox. Everything - apps, destinations,
schedule, retention and restores - is set up in the web UI, not env vars.

Most apps are backed up through their own API. Jellyfin, Emby, Plex, Seerr,
LazyLibrarian and Vaultwarden also need a folder mounted into Backuparr - see
[Apps](apps/index.md).

[Get started](getting-started.md){: .btn .btn-primary }
[View on GitHub](https://github.com/rsaturns/backuparr){: .btn }

![Overview tab: enabled services, destinations, schedule and retention, a line saying all 23 backups succeeded, and each app's last backup](screenshots/overview.png)

## Where to find things

- [Features](features.md) - everything Backuparr does, in one list.
- [Getting started](getting-started.md) - install with
  [Docker](getting-started/docker.md), on [Unraid](getting-started/unraid.md)
  or [from source](getting-started/build-from-source.md), the
  [first run](getting-started/first-run.md) and [updating](getting-started/updating.md).
- [Apps](apps/index.md) - how each app is backed up and restored, with notes
  for the apps that need extra setup.
- [Destinations](destinations.md) - Local Storage, Google Drive, OneDrive and
  Dropbox.
- [Backups and history](backups.md) - the schedule, retention, the check
  before upload, run logs and the History tab.
- [Restoring](restoring.md) - restoring an app, the version check, and
  restoring Backuparr itself.
- [Settings](settings.md) - Prowlarr discovery, more than one instance of an
  app, and authentication headers for apps behind a reverse proxy.
- [Login and security](security.md) - the admin login, password recovery, your
  encryption key and Backuparr's own backup.
- [Notifications](notifications.md) - Discord, Slack, Telegram, Gotify, ntfy and
  more.
- [Environment variables](environment-variables.md) and the
  [Configuration reference](configuration.md).

See the [changelog](https://github.com/rsaturns/backuparr/blob/main/CHANGELOG.md)
for release history.

> **AI disclosure:** This project was built with substantial assistance
> from Claude (Anthropic) - code, documentation, and commit history
> included. Reviewed and maintained by a human.
