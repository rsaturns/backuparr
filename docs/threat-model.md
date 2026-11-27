---
title: Threat model
nav_order: 2.6
---

# Threat model

[Security principles](security-principles.md) says how Backuparr protects
your backups and credentials. This page looks at it from the other side:
what an attacker would want, where they could come from, what stops them,
and what's left. It covers Backuparr 3.0.1 and is reviewed when a change
adds a way in, such as a new app, destination or API route.

## What's worth attacking

| What | Where it lives | Why it matters |
|---|---|---|
| Your apps' API keys and logins | `config.json`, encrypted | Control of every app Backuparr backs up |
| Cloud tokens | `rclone.conf`, encrypted | Your backups in the cloud, and for OneDrive and Dropbox more of the account |
| The encryption key | `secrets.key`, or `BACKUPARR_SECRETS_KEY` | Opens the stored credentials and every encrypted backup |
| Backups | Your destinations | A full copy of each app: password vaults, firewall configs, your smart home |
| The admin login | `auth.json` (an Argon2id hash) and server-side sessions | Everything above, through the web UI |
| Apps' mounted folders | Mounted into the container, read-write for restores | A restore writes there |

## Who it defends against

- **Someone on your network** who can reach Backuparr's ports.
- **Someone between Backuparr and an app or cloud**, reading or changing
  traffic.
- **A compromised app**: its answers, the backups it hands over, the files in
  its mounted folder.
- **Someone who gets your backups**: the cloud provider, a stolen cloud
  account, a copied disk.
- **Someone with a copy of Backuparr's config volume.**
- **A compromised dependency**, GitHub Action or base image.

Trusted: you, the admin, and the host Backuparr runs on. Anyone with root on
the host, or with the config volume and the encryption key together, can read
everything; that's outside what Backuparr can defend.

## Ways in

- **The web UI**, on port 8990 (HTTP) and 8991 (HTTPS). Without signing in,
  only these answer: sign-in, first-run setup (only until an account exists),
  the forgot-password reset, static files, and the Home Assistant status
  (with its own token). Everything else needs a session.
- **Uploads**: Backuparr's own backup, restored from the setup screen or
  Settings.
- **Connections out**: to your apps (with their keys or logins), to your cloud
  destinations (through rclone), and to the Notify URL.
- **Files**: backups read from and restored into apps' mounted folders.
- **The build**: GitHub Actions, Docker Hub and GitHub Container Registry.

## Threats and what stops them

### The web UI

- **Guessing or stealing the admin login.** The password is an Argon2id hash,
  repeated wrong passwords lock the address out, and only two password checks
  run at once. Sessions live on the server, so signing out ends a copied
  cookie, and cookies can't be read by scripts. *Left:* on plain HTTP (port
  8990) the password crosses your network unencrypted; use HTTPS, or turn on
  **HTTPS only**.
- **Another website acting through your browser.** State-changing requests
  must be JSON, which a cross-site page can't send; the pages have a strict
  Content Security Policy with no inline scripts, and can't be framed.
- **The forgot-password reset**, which works without signing in. It wipes
  Backuparr's own state (settings, login, backups in Local storage) but not
  your cloud backups, and only from a JSON request, so not from another
  site. *Left:* someone on your network can use it to wipe Backuparr; this
  is an accepted trade-off for a single admin (see SECURITY.md).
- **The Home Assistant token.** It opens only two read-only status addresses,
  and Backuparr keeps only its hash.

### Your apps

- **Reading or changing traffic to an app.** Certificates are verified; a
  self-signed one is pinned by its fingerprint and checked before a byte of
  the request is sent. Keys and logins go only to the app's own address:
  Backuparr refuses redirects to another host, and **Restore to a different
  target** needs that target's own key. *Left:* an app you connect over plain
  HTTP receives its key unencrypted across your network.
- **A compromised app.** Each backup is checked to be whole before it's
  uploaded. A restore is checked against the backup's recorded checksum and
  the app's version, unpacked with guards against paths outside its folder,
  and stopped if a symbolic link in the app's folder would send a write
  elsewhere (GHSA-r2gf-f7v8-j7g2). Error messages never repeat an app's
  address or key.

- **The Reactive Resume key.** It is Read only, sent in the `x-api-key` header, and goes only to the same host.
The backup holds resumes, job applications and contacts - personal data - so it is always encrypted.

### Your backups

- **Reading backups where they're stored.** Each backup is encrypted with
  AES-256-GCM, with a key derived from your encryption key and a salt of its
  own; a changed or cut file fails to decrypt. Apps whose backups hold
  secrets are always encrypted. Google Drive access is limited to files
  Backuparr made or you picked. *Left:* OneDrive and Dropbox connect through
  rclone's built-in apps, whose tokens reach more than Backuparr's folder,
  so treat those tokens like a password; a backup you turn encryption off
  for, or download, is readable.
- **A forged backup of Backuparr itself.** Restoring one needs your
  install's encryption key, and a backup from a newer Backuparr is refused.
- **Losing the key.** Without it no one can open your backups, Backuparr
  included. It's shown once, until you confirm you've saved it.

### The config volume

- **A copy of the volume.** Credentials and cloud tokens are encrypted.
  *Left:* by default the key sits on the same volume, so a copy of the whole
  volume opens them; set `BACKUPARR_SECRETS_KEY` to keep it apart.
- **Watching the container's processes.** When a cloud destination is
  connected or changed, its token is briefly in rclone's command line.
  *Left:* accepted; see SECURITY.md.
- **`BACKUPARR_DISABLE_AUTH`** turns the login off for a reverse proxy that
  signs you in. *Left:* Backuparr then trusts anything that reaches it, so
  the container must be reachable only through that proxy.

### The build

- **A compromised dependency, Action or base image.** Every Python package is
  pinned with its hash and the base image by its digest; a new version waits
  a week before it's proposed; Actions are pinned to commits. Every change
  runs the tests, CodeQL and a Trivy scan, and high or critical findings block
  it. Releases are built from scratch, the job that holds the registry
  credentials runs only after those checks, and each image carries signed
  provenance and an SBOM. See
  [How the code is checked](security-principles.md#how-the-code-is-checked).

## Not covered

- Root on the host, or the config volume together with the key.
- The security of the apps themselves and of your cloud accounts.
- Backuparr exposed directly to the internet: keep it on your network or
  behind a reverse proxy with its own login.
- Denial of service from inside your network, beyond the limits on password
  checks and uploads before sign-in.
