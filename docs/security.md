---
title: Login and security
nav_order: 6
---

# Login and security

## Login

The admin account from the setup screen is required by default.

**Authentication at a reverse proxy:** if your proxy (e.g. Authelia) already
protects every request, set `BACKUPARR_DISABLE_AUTH=true` and recreate the
container. The UI and API then need no local login, and the setup, login,
logout and reset endpoints return `403`. **Block direct access to port 8990**:
anyone who reaches it has full access, stored credentials included.

**Forgot the password?** Click **Reset Backuparr** on the login screen. After
two confirmations it wipes local state - every app's API key, every cloud
connection, the admin account and Backuparr's backups in Local storage (other
files in that folder are left alone) - and returns to the setup screen. Backups
already in Google Drive, OneDrive or Dropbox are untouched.

The reset works without logging in, gated only by a confirmation phrase that's
public in this repo. Other websites can't trigger it (or any other change):
Backuparr only accepts changes sent as JSON from its own page. Still, **don't
expose port 8990 to an untrusted network** - put it behind a reverse proxy
with its own login and TLS.

## Encryption at rest

Every app's API key, Bazarr's and NZBGet's passwords, service authentication
header values, Google Drive's client secret, API key and refresh token,
OneDrive's and Dropbox's tokens, and the Notify URL are encrypted in
`config.json` with `secrets.key`, generated on first run (or set
`BACKUPARR_SECRETS_KEY` to keep it off the volume). `rclone.conf` is encrypted
with rclone's own config encryption, using a generated password in
`rclone.pass` (or `RCLONE_CONFIG_PASS`). It's always on.

If the key changes (say, a mistyped `BACKUPARR_SECRETS_KEY`), the stored secrets
show up blank but aren't lost: saving settings keeps them encrypted as they
were, and they work again once the original key is back.

Backup archives themselves aren't encrypted, and some hold app credentials
(see the [app notes](apps/index.md)), so protect every destination.

