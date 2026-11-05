---
title: Updating
parent: Getting started
nav_order: 5
---

# Updating

With Compose:

```sh
docker compose pull backuparr && docker compose up -d backuparr
```

On Unraid, click **Apply update** on the Docker tab. A pinned tag (such as
`rsaturns/backuparr:2.5.0`) only changes when you edit it.

Updating keeps your settings, login and backups. A backup Backuparr makes of
itself can only be restored on the same or a newer version, so don't downgrade
and then restore one.

## Updating to 2.5.0

- **Your encryption key** is shown once, and Backuparr waits until you confirm
  you've saved it. See [Your encryption key](../security.md#your-encryption-key).
- **Existing backups are recorded in the background**, and cloud backups are
  downloaded once to do it. See [The backup record](../backups.md#the-backup-record).
- **Backuparr backs itself up** at the end of each full run, starting with the
  next one. See [Backuparr's own backup](../security.md#backuparrs-own-backup).
- **Restores need the same app version** as the backup. See
  [The version check](../restoring.md#the-version-check).
