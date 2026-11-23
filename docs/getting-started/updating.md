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
`rsaturns/backuparr:3.0.0`) only changes when you edit it.

Updating keeps your settings, login and backups. A backup Backuparr makes of
itself can only be restored on the same or a newer version, so don't downgrade
and then restore one.

<span id="updating-to-300-beta"></span>

## Updating to 3.0.0

3.0 changes a lot, so read this before you update. A container that follows
`latest` gets 3.0.0 the next time it updates. The
[changelog](https://github.com/rsaturns/backuparr/blob/{{ site.gh_edit_branch }}/CHANGELOG.md)
lists everything that's new.

**On a 3.0 beta?** Set the tag to `latest` or `3.0.0`: a beta's tag doesn't
move. Nothing else changes - your settings, login and backups stay.

### Before you update

- **Find your encryption key.** From 3.0 every backup is encrypted with it,
  and restoring one on another install needs it. See
  [Your encryption key](../security.md#your-encryption-key).

### Your container

- **Publish port 8991** for HTTPS: add `"8991:8991"` to the container's ports
  (on Unraid, add a port with container port 8991). Port 8990 keeps working;
  the [Home Assistant integration](../home-assistant.md) needs 8991. See
  [HTTPS](../security.md#https).
- **With `BACKUPARR_DISABLE_AUTH`**, block 8991 the same way you block 8990
  (host networking or a firewall).
- **Behind a reverse proxy**, nothing changes: keep `BACKUPARR_FORCE_HTTPS` and
  `BACKUPARR_TRUSTED_PROXY`. To switch on HTTPS only, point the proxy at 8991.

### After you update

- **Sign in again** once: 3.0 keeps sessions on the server, so sessions from
  2.5.x end.
- **Backups are encrypted** with your encryption key from the next run,
  including apps you set up before; Backuparr says so once. Turn it off for
  an app on its card in Settings. See
  [Encrypted backups](../security.md#encrypted-backups).
- **Backups made before stay as they are** and still restore.
- **Backuparr backs itself up** in the background on the first start of 3.0
  (if its own backup is on and a destination is set up). 3.0 can't restore
  Backuparr's own 2.5.x backups: restore one on 2.5.7 first, then update.
  They stay until retention removes them. See
  [Restoring Backuparr itself](../restoring.md#restoring-backuparr-itself).
- **Deleting on Google Drive and OneDrive is permanent:** backups that
  retention or History removes no longer go to the trash or recycle bin,
  where they still counted against your storage.
- **Hand-typed schedules:** 3.0's cron update refuses a few hand-typed
  forms, such as `1#7` or non-ASCII digits; the Overview and Run & Status
  then say the schedule isn't valid until you fix it in Settings. A step that
  starts at a field's maximum, such as `59/15` or `6/1`, now means just that
  value, not "every": use `*/15` for every 15 minutes. The schedule picker
  never writes these.

## Updating to 2.5.0

- **Your encryption key** is shown once, and Backuparr waits until you confirm
  you've saved it. See [Your encryption key](../security.md#your-encryption-key).
- **Existing backups are recorded in the background**, and cloud backups are
  downloaded once to do it. See [The backup record](../backups.md#the-backup-record).
- **Backuparr backs itself up** at the end of each full run, starting with the
  next one. See [Backuparr's own backup](../security.md#backuparrs-own-backup).
- **Restores need the same app version** as the backup. See
  [The version check](../restoring.md#the-version-check).
