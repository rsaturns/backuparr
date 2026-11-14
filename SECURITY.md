# Security Policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, use GitHub's [private vulnerability reporting](https://github.com/rsaturns/backuparr/security/advisories/new) (Security tab → Report a vulnerability). This opens a private conversation with the maintainer so a fix can be prepared before details are public.

Include what you'd include in a bug report: affected version/commit, steps to reproduce, and the impact.

## Known, accepted risks

A few things are intentionally not "fixed" - they're documented trade-offs for this project's single-trusted-admin threat model, not oversights:

- With local authentication enabled, `POST /api/reset` (the forgot-password recovery flow) is reachable without authentication, gated only by a confirmation phrase that's public in this repo. It wipes Backuparr's own state - API keys, cloud connections, the admin account and Backuparr's backups in Local storage - but not backups in cloud destinations. Other websites can't trigger it, since state-changing API calls must be sent as JSON. It is disabled when `BACKUPARR_DISABLE_AUTH=true`. See [Login](https://rsaturns.github.io/backuparr/security/#login) in the docs.
- `BACKUPARR_DISABLE_AUTH=true` grants full access to the UI and operational API without a local account. Use it only when an authenticating reverse proxy protects all routes and direct access to the container is blocked; Backuparr does not verify proxy user headers.
- Backup archives are encrypted with the same key as the secrets below, unless encryption is turned off for an app; an unencrypted archive can contain app credentials (see the [app notes](https://rsaturns.github.io/backuparr/apps/) in the docs). A backup downloaded from the web UI is decrypted, so protect the computer it goes to. See [Encrypted backups](https://rsaturns.github.io/backuparr/security/#encrypted-backups) in the docs.
- Secrets in `config.json` are encrypted with `secrets.key`, which by default sits on the same volume - anyone with the whole volume can decrypt them. Set `BACKUPARR_SECRETS_KEY` to keep the key off it. See [Encryption at rest](https://rsaturns.github.io/backuparr/security/#encryption-at-rest) in the docs.
- When a Google Drive, OneDrive or Dropbox remote is first written to `rclone.conf` or its settings change, its OAuth token (and Google Drive's client secret) is passed to `rclone` as a command-line argument, briefly visible to anything that can inspect the container's process list (`/proc`, `ps`, `docker top`).

If you believe any of these has a worse impact than described, or you've found something else, please still report it privately first.

## Supported versions

Backuparr moves quickly - only the newest release (the `latest` image) receives fixes. There's no long-term support branch.
