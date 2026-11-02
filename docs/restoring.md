---
title: Restoring after a disaster
nav_order: 7
---

# Restoring after a disaster

Re-create the app containers from your compose file, then on the **Restore**
tab pick the destination, the app and a backup, and confirm. The restore shows
live progress and a log.

**Restore to a different target** overrides the app's URL, key and other
fields for that restore only, without changing Settings - for a rebuilt or
renamed instance, or a trial restore on a throwaway copy.

- **Radarr / Sonarr / Prowlarr** - automatic; the app restarts.
- **Profilarr** - manual, see the [Profilarr note](apps/profilarr.md).
- **Bazarr** - needs Bazarr's config/backup folder mounted into Backuparr;
  enter it under "Bazarr backup folder" unless it's set in Settings.
- **Tdarr** - wipes each database collection before refilling it; you're
  asked to confirm.
- **SABnzbd** - asks for each Usenet server's password (leave any blank and
  set it in SABnzbd later).
- **NZBGet** - automatic; NZBGet reloads. See the [NZBGet note](apps/nzbget.md).
- **Tautulli** - config only, see the [Tautulli note](apps/tautulli.md).
- **Jellyfin** - automatic; Jellyfin restarts. See the [Jellyfin note](apps/jellyfin.md) before restoring onto a fresh install.
- **Emby, Plex, Seerr** - stop the app first, restore, then start it again. See
  the [Emby](apps/emby.md), [Plex](apps/plex.md) and [Seerr](apps/seerr.md) notes.
