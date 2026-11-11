---
title: Architecture
nav_order: 14
---

# Architecture

![Radarr, Sonarr, Lidarr, Whisparr, Prowlarr, Profilarr, Bazarr, Tdarr, SABnzbd, NZBGet, Tautulli, Jellyfin, Emby, Plex, Seerr, LazyLibrarian, Pi-hole, Vaultwarden, and OPNsense each feed Backuparr; Backuparr uploads each backup via rclone to Local Storage, Google Drive, Microsoft OneDrive, and Dropbox](https://raw.githubusercontent.com/rsaturns/backuparr/main/webui/static/architecture-diagram.svg)

## Why not reuse an existing tool?

[Zerka30/servarr-backup](https://github.com/Zerka30/servarr-backup) does this
for Radarr/Sonarr/Prowlarr to S3. Backuparr covers more apps and sends backups
through [rclone](https://rclone.org/) to Local Storage, Google Drive (connected
in the UI), and OneDrive or Dropbox (a one-time `rclone authorize` paste) -
no rclone config wizard needed.
