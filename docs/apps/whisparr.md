---
title: Whisparr
parent: Apps
nav_order: 20
---

# Whisparr

Whisparr v2 and v3 are both supported. To back up both, add the second with
**Add another Whisparr**. Like Radarr and Sonarr, if it asks every address to
log in, it needs its login on the card or **Disabled for Local Addresses** -
see the [Radarr, Sonarr and Lidarr note](servarr.md).

- **v3 restores:** Whisparr v3's own restore skips the database in its backups
  (they hold `whisparr3.db`, but it only accepts `whisparr.db`) and still
  reports success. Backuparr renames the file when it uploads the backup, so
  the database is restored too.
- **hotio image:** after Whisparr restarts, such as after a restore, the
  container logs `address already in use` every few seconds. Whisparr keeps
  working; restart the container to stop it.
