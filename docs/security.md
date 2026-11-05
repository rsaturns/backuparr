---
title: Login and security
nav_order: 6
---

# Login and security

## Login

The admin account from the setup screen is required by default. To change its
password, click your username at the top right and choose **Change password**:
enter the current password and the new one twice (at least 8 characters).
Every other signed-in browser is signed out, and wrong current passwords count
towards the login lockout.

**Authentication at a reverse proxy:** if your proxy (e.g. Authelia) already
protects every request, set `BACKUPARR_DISABLE_AUTH=true` and recreate the
container. The UI and API then need no local login, and the setup, login,
logout, password change and reset endpoints return `403`. **Block direct access to port 8990**:
anyone who reaches it has full access, stored credentials included.

## Forgot your password?

There's no email recovery. There are two ways back in.

**Keep your settings:** delete `auth.json` from Backuparr's config folder
(`/config/backuparr`), then open Backuparr. The setup screen asks for a new
username and password, and your [encryption key](#your-encryption-key).

- Your settings, cloud connections, backups and run logs are kept.
- Every other signed-in browser is signed out.
- Wrong keys count towards the login lockout.
- Lost the key as well? It's in `secrets.key` in the same folder, unless you
  set `BACKUPARR_SECRETS_KEY`.

**Start over:** click **Reset Backuparr** on the login screen. After two
confirmations it wipes local state and returns to the setup screen of a new
install:

- every app's settings and API key, and every cloud connection
- the admin account
- Backuparr's backups in Local storage, its own backups included (other files
  in that folder are left alone)
- the backup record and run logs

Backups already in Google Drive, OneDrive or Dropbox are untouched.

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

## Your encryption key

The key that encrypts those secrets also encrypts Backuparr's own backup
(below). After the admin account is created, and once after updating to 2.5.0,
Backuparr shows the key and does nothing else until you confirm you've saved
it. It's never shown again, so save it in a password manager: without it,
Backuparr's own backup can't be opened and its stored secrets can't be read.
If you set `BACKUPARR_SECRETS_KEY`, that's the key shown.

## Backuparr's own backup

At the end of each full run (not a single app's), Backuparr backs up its own
configuration to every destination as `backuparr/backuparr_<date>_<time>.enc`,
kept like the app backups: its settings and destinations, the cloud
connections, the admin login, the backup record and the run logs. It's
encrypted with your encryption key and can't be read or changed without it.
The key itself, the session key and `rclone.pass` aren't in it. Turn it off on
the Settings tab. History lists these backups under "Backuparr's own
configuration" for download and delete. See
[Restoring Backuparr itself](restoring.md#restoring-backuparr-itself).

