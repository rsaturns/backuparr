---
title: Environment variables
nav_order: 9
---

# Environment variables

App settings live in `config.json` and are edited in the web UI (see the
[Configuration reference](configuration.md)). These are set in your
compose file:

| Env var | Default | Purpose |
|---|---|---|
| `WEBUI_HOST` | `0.0.0.0` | Address the web UI listens on (see below) |
| `WEBUI_PORT` | `8990` | Port the web UI listens on inside the container |
| `PUID` / `PGID` | `1000` / `1000` | User/group the container runs as. Match your host user (`id`) so files on the config volume are owned by you |
| `TZ` | UTC | Timezone for the schedule and `backup.log` timestamps |
| `UMASK` | `077` | Permissions of new files: by default only the container user can read backups (`0600`/`0700`). Set `022` if another user on the host reads the backup folder |
| `LOG_LEVEL` | `INFO` | Logging level for backup/restore runs, e.g. `DEBUG` |
| `BACKUPARR_SECRETS_KEY` | *(generated)* | Replaces the generated `secrets.key` (see [Encryption at rest](security.md#encryption-at-rest)), e.g. from a Docker secret |
| `RCLONE_CONFIG_PASS` | *(generated)* | Replaces the generated password that encrypts `rclone.conf` |
| `BACKUPARR_DISABLE_AUTH` | `false` | `true` turns off local login behind an authenticating reverse proxy - see [Login](security.md#login) |
| `BACKUPARR_FORCE_HTTPS` | `false` | Set `true` behind your own TLS reverse proxy: secure session cookie, and trusts `X-Forwarded-Proto` (needed for Google Drive's `https://` redirect URI). Leave unset on plain HTTP, or login fails |
| `BACKUPARR_TRUSTED_PROXY` | *(none)* | With `BACKUPARR_FORCE_HTTPS`: your proxy's IP address, so the login lockout sees each client's own address from `X-Forwarded-For`. Without it, failed logins all count against the proxy's address |
| `BACKUPARR_ALLOW_CROSS_HOST_REDIRECTS` | `false` | `true` only if an app's URL redirects to a *different* host and you can't use the final address. By default such redirects are refused so credentials never go to another host |

To listen on loopback only: with bridge networking, publish
`ports: ["127.0.0.1:8990:8990"]` and leave `WEBUI_HOST` alone (setting it to
`127.0.0.1` there blocks the published port). With `network_mode: host`, set
`WEBUI_HOST: "127.0.0.1"` and remove `ports:`.

## Advanced: file locations

Only needed to move files out of the `/config/backuparr` mount, where they all
live by default.

| Env var | Default | What it is |
|---|---|---|
| `BACKUPARR_CONFIG` | `/config/backuparr/config.json` | The main config file |
| `RCLONE_CONFIG` | `/config/backuparr/rclone.conf` | rclone's remotes config |
| `RCLONE_CONFIG_PASS_FILE` | `/config/backuparr/rclone.pass` | Generated `rclone.conf` password (ignored if `RCLONE_CONFIG_PASS` is set) |
| `BACKUPARR_SECRETS_KEY_PATH` | `/config/backuparr/secrets.key` | Generated config-encryption key (ignored if `BACKUPARR_SECRETS_KEY` is set) |
| `BACKUPARR_SECRET_KEY_PATH` | `/config/backuparr/secret_key` | Session-signing key - not the same as `secrets.key` |
| `BACKUPARR_AUTH` | `/config/backuparr/auth.json` | Admin username and password hash |
| `BACKUPARR_LOG_DIR` | `/var/log/backuparr` | Where `backup.log` (shown on Run & Status) is written |
