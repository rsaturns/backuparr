---
title: Destinations
nav_order: 5
---

# Destinations

Enable one or more on the **Settings** tab; every run uploads to all of them.
Each backup is [checked before it's uploaded](backups.md#the-check-before-upload).
Each card can keep backups for its own number of days - see
[Retention](backups.md#retention).

## Local Storage

No setup. Backups go to `/config/backuparr/backups`, on the same volume as
`config.json`. Set a custom path on the Local card to use another mounted
volume, such as a NAS share. Backuparr only ever deletes its own archives
there (`<app>/<app>_<date>_<time>.zip`), so the folder can hold other files.

A USB disk that spins down to save power is fine: before using it, Backuparr
reads a small `.backuparr-wake` file it keeps there and waits up to 60 seconds
for the disk to wake. The log shows "waiting for storage to be ready...", and
so do the Overview, History and Restore tabs, Test and Reset while they wait.
If it still fails, check `dmesg` on the host: a USB disk that resets or
disconnects as it wakes is a cable, power or enclosure problem.

## Google Drive

Set up from the Google Drive card, with no rclone config:

1. Click **Setup guide** and follow it: it creates a Google Cloud project, the
   Drive API, an OAuth client and an API key, and shows the redirect URI to
   register. **Publish the OAuth app** as it says - apps left in Testing lose
   their connection every 7 days.
2. Paste the Client ID, Client Secret and API key, then **Save settings**. The
   **Choose folder** picker needs the API key. The API key and Client Secret
   inputs are masked on screen; the Client ID remains visible.
3. Click **Connect Google Drive** and approve. Backuparr only asks for
   `drive.file` (files it created or you picked). Google warns the app is
   unverified; click **Advanced**, then **Go to (your app name) (unsafe)**.
4. Click **Choose folder** to pick or create the backup folder.

## OneDrive

Uses rclone's built-in Microsoft app - no Azure registration. Personal
Microsoft accounts only, not work/school (Microsoft 365).

1. On any computer with a browser, [download rclone](https://rclone.org/downloads/)
   and run `rclone authorize onedrive`.
2. Sign in and approve access.
3. Paste the block rclone prints after "Paste the following into your remote
   machine --->" into the OneDrive card and click **Connect OneDrive**.

Backups go to an `Apps/Backuparr` folder.

## Dropbox

Uses rclone's built-in Dropbox app - no developer account.

1. On any computer with a browser, [download rclone](https://rclone.org/downloads/)
   and run `rclone authorize dropbox`.
2. Sign in and approve access.
3. Paste the block rclone prints after "Paste the following into your remote
   machine --->" into the Dropbox card and click **Connect Dropbox**.

Backups go to a `Backuparr` folder.
