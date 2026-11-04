---
title: Restoring after a disaster
nav_order: 7
---

# Restoring after a disaster

Re-create the app containers from your compose file, then on the **Restore**
tab pick the destination, the app and a backup, and confirm. The restore shows
live progress and a log.

**The app version must match.** When you pick a backup, Backuparr compares
the app version it came from with the installed one, while the app is still
running. They must be exactly the same: if they aren't, the restore is refused
and the message names the version to install. Install that version, restore,
then update the app as usual. For LazyLibrarian, which has no release numbers,
it names the commit its Docker image tag starts with. Bazarr, SABnzbd and Tdarr
backups made before 2.5.0 don't record a version, so they restore with a
warning. For Emby, Plex, Seerr and LazyLibrarian, pick the backup while the app
is running, then stop it and restore within 6 hours. Each download is also
checked against the checksum recorded when the backup was made.

**Restore to a different target** overrides the app's URL, key and other
fields for that restore only, without changing Settings - for a rebuilt or
renamed instance, or a trial restore on a throwaway copy.

- **Radarr / Sonarr / Lidarr / Whisparr / Prowlarr** - automatic; the app restarts.
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
- **Emby, Plex, Seerr, LazyLibrarian** - stop the app first, restore, then start
  it again. See the [Emby](apps/emby.md), [Plex](apps/plex.md),
  [Seerr](apps/seerr.md) and [LazyLibrarian](apps/lazylibrarian.md) notes.
