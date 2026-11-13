---
title: Jellyfin
parent: Apps
nav_order: 6
---

# Jellyfin

## Setting it up

Jellyfin creates backups through its API but can't download or upload them, so
mount **Jellyfin's backups folder** into Backuparr. It is
`/config/data/backups` in the official image and `/config/data/data/backups` in
linuxserver's:

```yaml
    volumes:
      # official image, with Jellyfin's /config at /share/Container/jellyfin/config
      - /share/Container/jellyfin/config/data/backups:/mnt/jellyfin-backups
      # or linuxserver image, with Jellyfin's /config at /share/Container/jellyfin
      # - /share/Container/jellyfin/data/data/backups:/mnt/jellyfin-backups
```

Then fill in the card: the URL, an API key from **Dashboard > API Keys** (a
user's sign-in token can't manage backups), and **Backups folder** =
`/mnt/jellyfin-backups`. If the path is wrong, **Test connection** names the
folder Jellyfin actually uses.

## Backups and restores

- **What's in the backup:** Jellyfin's database (users, watched state,
  playlists, collections, libraries, settings and API keys) and config files.
  Images and subtitles are left out; a library scan brings them back. The
  archive holds password hashes and API keys.
- **Include metadata** on the card adds Jellyfin's metadata folder: posters,
  artwork and people images, artwork you set by hand, and saved metadata. It
  uses the Metadata option of Jellyfin's own backup, and takes the default
  metadata folder too when you set a custom metadata path. It's off by
  default: most metadata downloads again, and it can make each backup many
  times larger (retention keeps that many copies). Jellyfin writes the
  archive to its backups folder first, so that folder needs room for it.
  Trickplay images and extracted subtitles stay out; Jellyfin rebuilds them.
  Jellyfin 10.11.7 or newer is needed: older versions restored metadata to
  the wrong folder
  ([jellyfin#16425](https://github.com/jellyfin/jellyfin/pull/16425)), so on
  them Backuparr leaves it out and Test connection says so.
- **Mount the folder read-write**: Backuparr removes Jellyfin's copy of each
  archive after taking it, and a restore writes into the folder. Read-only,
  backups still work but restores don't.
- **Restore** replaces Jellyfin's database and settings, and its metadata if
  the backup has it, then Jellyfin restarts to apply them, as in Jellyfin's guide,
  [Restore from a Built-in Backup](https://jellyfin.org/docs/general/administration/backup-and-restore/#restore-from-a-built-in-backup). The archive stays in Jellyfin's backups folder; delete it
  there when you're done. API keys come back too: after rebuilding Jellyfin, enter the
  new install's key under **Restore to a different target**, not in Settings -
  the key from the backup works again afterwards.
- **Reverse proxy:** Jellyfin takes its API key only in the `Authorization`
  header, so a proxy credential needs another header name.
