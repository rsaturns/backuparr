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

1. Create a bucket and an access key pair scoped to that bucket. The key needs
   permission to list the bucket and read, write and delete objects under the
   backup prefix. See [rclone's S3 permissions guide](https://rclone.org/s3/#s3-permissions)
   for an AWS policy example.
2. In **Settings**, turn on **Cloud Storage** and choose a storage provider.
   Enter the bucket name and the provider-specific settings below. For a
   service without a preset, choose **Other S3-compatible** and enter its S3
   endpoint and signing region. Backuparr converts Backblaze B2 bucket names to
   lowercase for S3 requests.
3. Enter the access key ID and secret access key. Set a relative path prefix
   such as `server1/backups`, or leave it blank to use `Backuparr`. You can also
   paste `/server1/backups/`. Backuparr removes the surrounding slashes.
4. Click **Test** to check that Backuparr can list the bucket prefix, then click
   **Save settings**. The secret access key field shows dots when a key is saved.
   Leave it blank to keep the saved key or enter a replacement.

| Provider | Endpoint URL | Region |
|---|---|---|
| AWS S3 | Optional S3 API URL. Leave blank for the AWS regional endpoint. | Bucket or signing region (defaults to `us-east-1`) |
| Backblaze B2 | Uses the regional endpoint, defaulting to `https://s3.us-west-004.backblazeb2.com` | Uses the region in the endpoint if available, otherwise `us-west-004` |
| Cloudflare R2 | Account's S3 endpoint, for example `https://<account-id>.r2.cloudflarestorage.com` | Automatically uses `auto` |
| Wasabi | Uses the regional endpoint, defaulting to `https://s3.wasabisys.com` | Uses the region in the endpoint if available, otherwise `us-east-1` |
| MinIO | S3 API address, for example `https://minio.example.com:9000` | Server region (defaults to `us-east-1`) |
| Other S3-compatible | S3 API address | Signing region (defaults to `us-east-1`) |

**AWS S3** uses AWS settings for its own endpoints and rclone's generic S3
mode for custom endpoints on other hosts. Enter the S3 API address, including
`https://` (or `http://` for a local service that uses plain HTTP). Enter the
access keys for the endpoint whenever it changes. For Backblaze B2 or Wasabi
buckets outside the default region, enter the bucket's region or regional S3
endpoint. Check the region and endpoint when using a custom or alias URL.

Backups appear in **History** and **Restore**. The Overview shows the space
Backuparr's backups use. Free space is unknown for S3-compatible destinations.
After a bucket or prefix change, select the old settings again to see its
existing backups.

Retention removes current objects. Versioned buckets can keep and charge for
older versions after those objects disappear from History. For Backblaze B2,
review its [lifecycle rules](https://www.backblaze.com/docs/cloud-storage-lifecycle-rules).
For example, **Keep only the last version of the file** expires hidden versions.
