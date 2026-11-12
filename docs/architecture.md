---
title: Architecture
nav_order: 14
---

# Architecture

Backuparr is one container. It reaches each app the way the app itself
offers a backup, and sends the result through [rclone](https://rclone.org/)
to the destinations you enable.

![The apps you host, in a box per category - Media Management, Downloaders, Media Servers, Network, Security, Home Automation and Server, where Backuparr's own backup sits too - each feed Backuparr; Backuparr uploads each backup via rclone to Local Storage, Google Drive, Microsoft OneDrive and Dropbox](https://raw.githubusercontent.com/rsaturns/backuparr/main/webui/static/architecture-diagram.png)

## How a backup runs

On its schedule, or when you click **Run backup now**, Backuparr backs up each
enabled app in turn:

1. **Makes the app's own backup.** Most apps make it through their API or web
   UI; a few are copied from their folder, mounted into Backuparr. See
   [Apps](apps/index.md) for each one.
2. **Checks it** before anything is uploaded: the archive must be whole and
   hold what that app's backup holds. See
   [The check before upload](backups.md#the-check-before-upload).
3. **Encrypts it** with your [encryption key](security.md#encrypted-backups),
   unless you turned that off for the app.
4. **Uploads it** with rclone to every destination you enabled, as
   `<app>/<app>_<date>_<time>.<ext>`, and adds it to the
   [backup record](backups.md#the-backup-record): its version, size and
   checksum.
5. **Prunes** backups older than the [retention](backups.md#retention) -
   only files with a name Backuparr gave them.

A full run ends with [Backuparr's own backup](security.md#backuparrs-own-backup).
Each run has a log on **Run & Status**, and the Overview shows every app's
latest backup.

A **restore** runs the other way: Backuparr fetches the backup, checks it
against the backup record and the app's version, decrypts it and hands it
back to the app. Apps Backuparr can't restore get a download and their own
steps instead. See [Restoring](restoring.md).

Backups and restores never overlap: one waits for the other.
