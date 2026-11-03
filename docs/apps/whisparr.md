---
title: Whisparr
parent: Apps
nav_order: 10
---

# Whisparr

Whisparr v2 and v3 are both supported, with a URL and API key like Radarr and
Sonarr. To back up both, add one as an extra instance with **Add another
Whisparr**.

- **v3 restores:** Whisparr v3 names its database `whisparr3.db` in its
  backups, but its own restore only accepts `whisparr.db`. It restores the
  settings, reports success and leaves the database as it was - from
  Whisparr's own **Restore** too. Backuparr uploads v3 backups with the
  database renamed to `whisparr.db`, so the whole backup is restored.
- **hotio image:** after Whisparr restarts itself, as it does after a
  restore, the container keeps trying to start a second copy and logs
  `address already in use` every few seconds. Whisparr keeps working; restart
  the container to stop it.
