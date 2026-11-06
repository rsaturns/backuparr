---
title: Backups and history
nav_order: 6
---

# Backups and history

## Schedule and Run now

Set the schedule under **Settings → Schedule & notifications**: daily, weekly,
every few hours, or a 5-field cron schedule. Changes apply without a restart.

**Run & Status → Run backup now** backs up every app, or the one you pick, and
shows the run live. A backup and a restore never run at the same time.

![Run & Status tab: a backup of all apps in progress, with its live log]({{ '/screenshots/run.png' | relative_url }})

## Retention

**Default retention (days)** deletes each app's backups older than that,
separately on each destination. Each app's newest backup is always kept, so a
run of failed backups never removes the last good one. Backuparr only deletes
its own archives (`<app>/<app>_<date>_<time>.zip`), so a destination folder can
hold other files.

To keep backups longer or shorter on one destination, set **Retention (days)**
on its card under Settings > Destinations - for example 28 days on Local
storage and 7 on Google Drive. Left blank, a destination uses the default. Once
a destination has its own, the Overview shows each destination's retention.

## The check before upload

Each backup is checked before it's uploaded:

- The archive opens and reads back intact.
- Its SQLite databases pass a quick check.
- The files the app's restore needs are in it.

A backup that fails isn't uploaded, and the app shows as failed with the
reason.

## Run logs

Each app's log from each run is kept for as long as retention keeps that
run's backups (the longest retention, when destinations differ), and its
latest run is always kept.

- **View log** on an app's Overview tile shows its latest run.
- On the History tab, each backup has its own log. A run that left no backup
  on that destination is listed greyed out as **Backup failed**; its log
  says why.

![An app's run log: when it ran, the app version, the file it made and where it was uploaded, then the log lines]({{ '/screenshots/run-log.png' | relative_url }})

## History

Pick a destination to list each app's backups there, with their size, date and
the app version each one came from. You can download or delete any of them, or
open its log. Backuparr's own backups are listed under "Backuparr's own
configuration".

![History tab: Radarr's backups with their version, size and date, View log, download and delete buttons, and a failed run greyed out as Backup failed]({{ '/screenshots/history.png' | relative_url }})

## The backup record

`manifest.json`, beside `config.json`, records each backup Backuparr made:

- the destinations holding it
- the app version it came from, and Backuparr's version
- its size and SHA-256 checksum

Restores use it to check the app version and the download. Backuparr keeps it
up to date; don't edit it.

**Backups made before 2.5.0** are added in the background after updating, each
read once:

- Local storage is read first. A cloud copy of a backup already read there
  isn't downloaded.
- Any other backup on Google Drive, OneDrive or Dropbox is downloaded once.
- This pauses while a backup or restore runs. Later startups only read backups
  that aren't recorded yet.
- On a slow or metered connection, set
  [`RCLONE_BWLIMIT`](environment-variables.md).
- Bazarr, SABnzbd and Tdarr backups from before 2.5.0 don't hold a version,
  so theirs stays unknown.
