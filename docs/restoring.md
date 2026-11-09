---
title: Restoring
nav_order: 7
---

# Restoring

## Restoring an app

1. Rebuilding a server? Re-create the app's container from your compose file
   first, and install the version the backup came from.
2. On the **Restore** tab, pick the source (a destination), the app and a
   backup file.
3. Backuparr checks the backup against the installed app (below). Once it
   passes, confirm the restore.

The restore shows live progress and a log.

![Restore tab: Radarr and a backup picked, with the check saying the backup is from the installed Radarr version]({{ '/screenshots/restore.png' | relative_url }})

**Restore to a different target** overrides the app's URL, key and other
fields for that restore only, without changing Settings. Use it for a rebuilt
or renamed instance, or for a trial restore on a throwaway copy.

## The version check

A backup is only restored onto the **exact** app version it came from, so a
restore never leaves an app half-migrated.

- When you pick a backup, Backuparr asks the running app for its version. If
  the versions differ, the restore is refused, and the message names the
  version to install. Install it, restore, then update the app as usual.
- LazyLibrarian has no release numbers, so the message names the commit that
  its Docker image tag starts with.
- Bazarr, SABnzbd and Tdarr backups made before 2.5.0 don't record a version.
  They restore with a warning.
- Emby, Plex, Seerr and LazyLibrarian must be stopped to restore. Pick the
  backup while the app is still running, then stop it and restore within
  6 hours.
- Each download is also checked against the checksum recorded when the backup
  was made.
- An [encrypted backup](security.md#encrypted-backups) is decrypted after
  that check. One made with another encryption key asks for that key on the
  Restore tab: enter it and restore again.

## What each restore does

| App | Restore |
|---|---|
| Radarr, Sonarr, Lidarr, Whisparr, Prowlarr | Automatic; the app restarts |
| Profilarr | By hand - see the [Profilarr note](apps/profilarr.md) |
| Bazarr | Needs Bazarr's config/backup folder mounted into Backuparr. Enter it under "Bazarr backup folder" unless it's set in Settings |
| Tdarr | Wipes each database collection before refilling it; you're asked to confirm |
| SABnzbd | Asks for each Usenet server's password. Leave any blank and set it in SABnzbd later |
| NZBGet | Automatic; NZBGet reloads. See the [NZBGet note](apps/nzbget.md) |
| Tautulli | Config only - see the [Tautulli note](apps/tautulli.md) |
| Jellyfin | Automatic; Jellyfin restarts. Read the [Jellyfin note](apps/jellyfin.md) before restoring onto a new install |
| Emby, Plex, Seerr, LazyLibrarian | Stop the app, restore, then start it again. See the [Emby](apps/emby.md), [Plex](apps/plex.md), [Seerr](apps/seerr.md) and [LazyLibrarian](apps/lazylibrarian.md) notes |

## Restoring Backuparr itself

Backuparr's own backup brings back its settings, destinations, cloud
connections and login. It's `backuparr/backuparr_<date>_<time>.enc` on each
destination, and you can also download it from History. You need the
[encryption key](security.md#your-encryption-key) you saved.

![Setup screen of a new install: Restore Backuparr, with the backup file and the encryption key]({{ '/screenshots/setup-restore.png' | relative_url }})

- **On a new install:** the setup screen asks "Do you wish to restore your
  Backuparr configuration?" Choose **Yes, restore**, pick the file and enter
  the key. Then sign in with your old username and password. This is only
  offered until an account exists.
- **On a running install:** use **Restore Backuparr** at the bottom of the
  Settings tab. It needs the file, the key, the admin password and a
  confirmation. It replaces the settings and login, and signs everyone out.

Good to know:

- The restored key becomes this install's key. If `BACKUPARR_SECRETS_KEY` is
  set to a different key, set it to the restored key (or remove it) and
  recreate the container. Until then, the restored passwords, API keys and
  cloud logins read as blank.
- A backup from a newer Backuparr isn't restored; update Backuparr first.
- A wrong key or a damaged file is refused before anything changes.

Then restore your apps from the Restore tab as usual.
