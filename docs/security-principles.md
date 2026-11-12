---
title: Security principles
nav_order: 2.5
---

# Security principles

## A backup is a copy of everything

A backup of an app is everything it takes to bring that app back: its
settings, its database, and the API keys, passwords and certificates it holds.
For a firewall, a password manager or a home automation server, that's some of
the most sensitive data you have. A backup tool collects all of it in one
place, and to do that it needs a key or a login for every app.

So the question to ask of a backup tool isn't only "will my backups work?",
but also "who else could read them, and what could they do with what it
holds?". This page answers that for Backuparr: what it protects, how, and what
it leaves to you.

## The principles

- **Your data stays yours.** Backups go only to the destinations you enable:
  a folder you mount, or your own Google Drive, OneDrive or Dropbox. There's no
  Backuparr account, cloud service or telemetry.
- **Encrypted before it leaves.** Backups are encrypted with a key only your
  install holds, so a destination stores files it can't read.
- **Secure by default.** The safe setting is the one you get without asking:
  a login, encryption on, HTTPS, private files. Turning something off is your
  choice, and the docs say what it costs.
- **Least privilege.** Where an app lets you limit what a key or login can
  do, the docs say the least its backup needs, and Backuparr asks for no more.
- **Nothing skipped to make it work.** A certificate that can't be checked is
  trusted by its fingerprint, never by turning the check off. A connection
  that isn't what Backuparr expects stops, and says why.
- **No secrets in what you see.** Logs, error messages and notifications are
  written to leave out API keys, passwords and tokens, and the app addresses
  that could carry them.
- **Written down.** The archive format, the risks Backuparr accepts and how to
  report a problem are all public.

## Your credentials

Backuparr needs each app's API key or login, and a token for each cloud
destination. They're kept like this:

- **Encrypted in `config.json`** with your install's encryption key
  (`secrets.key`, made on first start). Set `BACKUPARR_SECRETS_KEY` to keep
  the key off the volume, so a copy of the volume alone can't read them. See
  [Encryption at rest](security.md#encryption-at-rest).
- **rclone's settings** (`rclone.conf`, the cloud tokens) are encrypted with
  rclone's own config encryption.
- **Files are private.** Backuparr drops from root to your `PUID` before it
  starts, and its files are readable by that user only.
- **The key is shown once.** After setup Backuparr shows your encryption key
  until you confirm you've saved it, and never again. It's never logged, and
  it isn't in Backuparr's own backup. See
  [Your encryption key](security.md#your-encryption-key).
- **Your admin password** is stored only as an Argon2id hash.

## Your backups

- **AES-256-GCM.** Each backup is encrypted in chunks with a key of its own,
  derived from your encryption key and a random salt. A changed, cut or
  extended file fails to decrypt rather than restoring something else.
- **On by default for every app**, and always on - with no switch - for apps
  whose backups hold secrets that must never sit readable anywhere: Pi-hole,
  Bitwarden, Vaultwarden, OPNsense, pfSense, UniFi, Home Assistant, Unraid and
  Backuparr's own backup.
- **Checked before upload.** Each backup is checked to be whole before it's
  encrypted, and the encrypted file is checked to decrypt back to it before
  it's uploaded. See [The check before upload](backups.md#the-check-before-upload).
- **Openable without Backuparr.** The image's `decrypt` command opens a backup
  with your key, and the [format](security.md#the-format) is documented for
  anyone who wants their own tool. Your backups don't depend on Backuparr
  still running.
- **Downloads are decrypted** for you, with a warning: the file on your
  computer is then readable by anyone with access to it.
- **Retention only deletes what Backuparr made**: files in its own naming
  pattern, never the newest backup of an app.

## Talking to your apps

- **Least privilege.** Where an app has roles, its page in [Apps](apps/index.md)
  says the smallest one that backs it up: an OPNsense user with two
  privileges, an Immich API key with only the permissions its backup and
  restore use.
- **Pinned certificates.** Firewalls and other appliances often use a
  self-signed certificate. Test connection shows it, with its SHA-256
  fingerprint to compare with the app's own; **Trust** pins it. From then on
  only that certificate is accepted, checked before a byte of the request is
  sent. A different one stops backups until you trust it.
- **No redirects to another host** while a key or login is being sent.
- **Restores are checked first**: against the backup's recorded checksum and
  the app's version. A backup is unpacked with guards against paths that
  point outside its folder.

## The web UI

- **A login by default**, with a lockout after repeated wrong passwords.
  Changing the password signs out every other browser.
- **HTTPS on its own port** (8991), with a self-signed certificate Backuparr
  makes, or your own. **HTTPS only** closes plain HTTP. See
  [HTTPS](security.md#https).
- **Hardened pages.** A strict Content Security Policy with no inline
  scripts, no framing, and session cookies that scripts can't read.
  State-changing requests must be JSON, so another website can't make them.
  API answers are never cached.
- **Home Assistant's token** opens only two read-only status addresses, and
  Backuparr keeps only its hash. See [Home Assistant](home-assistant.md).

## What leaves your network

- Your backups, encrypted, to the cloud destinations you enable, and the
  sign-ins those destinations need.
- Your notifications, to the Notify URL you set: each app's name and, when it
  failed, why.
- If you click **Get Plex token**, the request for that token, to plex.tv
  (your Plex password goes only to Plex's website).
- From your browser, not the server: the footer's check for a new release
  (GitHub's API) and, when you pick a Google Drive folder, Google's picker.

Nothing else: no telemetry, no usage data, no account.

## What's left to you

Some protection only you can provide:

- **Save your encryption key** somewhere safe, like a password manager.
  Without it, encrypted backups can't be opened - by anyone, Backuparr
  included.
- **Don't expose Backuparr to the internet.** Keep it on your network, or put
  it behind a reverse proxy with its own login.
- **Protect where your backups land**, and the computers you download them to.
- **Keep it updated.** Fixes go into the newest release only.

A few trade-offs are deliberate, for a single trusted admin: the
forgot-password reset works without a login, `BACKUPARR_DISABLE_AUTH` trusts
your proxy completely, and the key sits on the same volume unless you move it.
[SECURITY.md](https://github.com/rsaturns/backuparr/blob/main/SECURITY.md)
lists each one and why.

## How the code is checked

- **Open source** under the [AGPLv3](https://github.com/rsaturns/backuparr/blob/main/LICENSE):
  anyone can read what Backuparr does with their data.
- **Every change** runs the test suite (about 2,000 tests), a smoke test of
  the built image and GitHub's CodeQL security analysis before it's merged.
- **Dependencies** are kept up to date with Dependabot.
- **Built with AI, reviewed by a human.** Backuparr was written with
  substantial help from Claude (Anthropic); every change is reviewed and
  tested by its maintainer.

## Reporting a vulnerability

Please don't open a public issue. Use GitHub's
[private vulnerability reporting](https://github.com/rsaturns/backuparr/security/advisories/new)
instead, so a fix can be ready before the details are public. See
[SECURITY.md](https://github.com/rsaturns/backuparr/blob/main/SECURITY.md).
