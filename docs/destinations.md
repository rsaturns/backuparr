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
there (`<app>/<app>_<date>_<time>.<ext>`), so the folder can hold other files.

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
   **Choose folder** picker needs the API key.
3. Click **Connect Google Drive** and approve. Backuparr only asks for
   `drive.file` (files it created or you picked). Google warns the app is
   unverified; click **Advanced**, then **Go to (your app name) (unsafe)**.
4. Click **Choose folder** to pick or create the backup folder.

Good to know:

- Publishing the app doesn't need Google's review, as `drive.file` isn't a
  sensitive scope.
- The API key only needs the Google Picker API, which **Choose folder**
  uses. Leave its **Application restrictions** at **None**: restricted to
  websites, the picker can fail with "The API developer key is invalid". The
  API restriction already limits what the key can do.

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

## Cloud Storage

Back up to any S3-compatible object storage - AWS S3, Backblaze B2, Cloudflare R2, Wasabi, MinIO, or any other S3-compatible service - through rclone's `s3` backend. The card takes an access key ID and a secret access key, which Backuparr encrypts at rest; there is no OAuth sign-in.

1. On the Cloud Storage card, pick the provider, then fill in the endpoint URL and region. A field left blank uses the provider's default: AWS uses its own endpoint with region `us-east-1`; Wasabi uses `https://s3.wasabisys.com` with region `us-east-1`; Backblaze B2 uses region `us-west-000` and endpoint `https://s3.us-west-000.backblazeb2.com` (change both for another B2 region); Cloudflare R2 uses region `auto` and needs your account's endpoint (`https://<account-id>.r2.cloudflarestorage.com`); MinIO and other S3-compatible services need an endpoint URL.
2. Enter the bucket, and a path prefix if the backups should live under a folder other than `Backuparr`.
3. Enter the access key ID and secret access key, then **Save settings**. For AWS, make them in IAM; for Cloudflare R2, make an API token with Object Read & Write on the bucket.
4. Click **Test** to check the bucket is reachable.

Backups go to the `Backuparr` folder in the bucket (or the path prefix you set).

Good to know:

- The S3 backend never answers `rclone about`, so the Overview's Storage tile shows the free space as unknown, not as an error.
- Backblaze B2 keeps old versions of overwritten files by default, so deleting backups there can hide them without freeing space. Set a lifecycle rule on the bucket, or use rclone's native `b2` backend with a hard delete.
- The secret access key is never shown again after saving and never appears in error messages.
