---
title: NZBGet
parent: Apps
nav_order: 8
---

# NZBGet

## Setting it up

NZBGet has no API key. On NZBGet's card, enter its URL - for example
`http://nzbget:6789` - and the **Control** username and password from
**Settings > Security** (defaults `nzbget`/`tegbzn6789`); the restricted and
add-only users can't read every setting, so they're refused. Leave both blank
only if NZBGet has no password. Click **Test connection**.

## Backups and restores

- **What's in the backup:** every setting in `nzbget.conf` - news servers with
  their passwords, categories, paths, RSS feeds, scheduler tasks, extension
  settings and NZBGet's own logins. The file uses NZBGet's **Backup settings**
  format, so NZBGet's own **Restore settings** can load it too (see NZBGet's
  [Backup and Restore Settings](https://nzbget.com/documentation/backup-and-restore-settings/)). The queue,
  history and extension scripts aren't included.
- **Restore** replaces every setting and reloads NZBGet; the queue and history
  are kept. Paths come back as they were, so keep NZBGet's volume mounts the
  same. The backup's username and password come back too - update the card if
  they differ.
- **Emoji and other characters outside Unicode's basic range** can't pass
  through NZBGet's API - NZBGet itself turns each one into `.` (its own Backup
  settings does the same). Accented and non-Latin text is fine.
- **Reverse proxy:** NZBGet's login uses the `Authorization` header, so a proxy
  credential needs another header name.
