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
`rsaturns/backuparr:3.0.0-beta`) only changes when you edit it.

Updating keeps your settings, login and backups. A backup Backuparr makes of
itself can only be restored on the same or a newer version, so don't downgrade
and then restore one.

## Updating to 3.0.0-beta

- **HTTPS on port 8991:** add `"8991:8991"` to the container's ports (on
  Unraid, add a port with container port 8991). Port 8990 keeps working. See
  [HTTPS](../security.md#https).
- **Backups are encrypted** with your encryption key from the next run,
  including apps you set up before; Backuparr says so once. Turn it off for
  an app on its card in Settings. See
  [Encrypted backups](../security.md#encrypted-backups).
- **Backups made before stay as they are** and still restore.
- **Backuparr backs itself up** in the background on the first start of 3.0
  (if its own backup is on and a destination is set up). Its backups from
  before 3.0 can't be restored any more: restore one with Backuparr 2.5.6,
  then update. They stay until retention removes them. See
  [Restoring Backuparr itself](../restoring.md#restoring-backuparr-itself).

## Updating to 2.5.0

- **Your encryption key** is shown once, and Backuparr waits until you confirm
  you've saved it. See [Your encryption key](../security.md#your-encryption-key).
- **Existing backups are recorded in the background**, and cloud backups are
  downloaded once to do it. See [The backup record](../backups.md#the-backup-record).
- **Backuparr backs itself up** at the end of each full run, starting with the
  next one. See [Backuparr's own backup](../security.md#backuparrs-own-backup).
- **Restores need the same app version** as the backup. See
  [The version check](../restoring.md#the-version-check).
