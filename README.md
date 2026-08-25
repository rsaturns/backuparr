<img src="webui/static/logo.png" alt="Backuparr logo" width="160">

# Backuparr

**Secure your Arrs.**

Scheduled config/database backups for Radarr, Sonarr, Bazarr, Prowlarr,
Tdarr, and Sabnzbd, sent to whichever destinations you enable - Local
storage (zero setup, download straight from the History tab), Google Drive
(click-through OAuth, no config files to hand-copy), and OneDrive (one-time
`rclone authorize` paste, no Azure account needed) today, with Dropbox
planned. Enable more than one and every backup gets a copy on each of
them. If the host dies, the recovery path is: re-create the
containers from your compose file, then restore each app's config from its
latest backup. Everything - which apps to back up, their URLs/API keys,
which destinations to use, the schedule, retention, and restores - is
configured and triggered from a web UI, not env vars.

Every app is backed up through **its own HTTP API** - this tool never reads
an app's config volume directly. Each app has an official (or at least
supported) way to export/import its own state, and using that instead of
copying files means every backup is internally consistent (no risk of
grabbing a database mid-write) and restores go through the app's own
validated restore path instead of a raw file overwrite.

## Why not reuse an existing tool?

[Zerka30/servarr-backup](https://github.com/Zerka30/servarr-backup) does
this for Radarr/Sonarr/Prowlarr via S3. This tool extends the same idea
(trigger the app's own backup API, download it, upload it) to Bazarr and
Tdarr, which have their own equivalent mechanisms, and moves the storage
side to a pick-your-destinations model built on [rclone](https://rclone.org/)
under the hood - Local out of the box, Google Drive via an in-app
"Connect" button, and OneDrive via a one-time `rclone authorize` paste,
instead of running rclone's full interactive config wizard yourself.

## Per-app backup method (read this before deploying)

| App | Method | Notes |
|---|---|---|
| Radarr / Sonarr / Prowlarr | `POST .../system/backup` to trigger, download the result, `DELETE` it server-side | Same official backup zip the apps use for manual backups. Restore is a genuine multipart upload to `.../system/backup/restore/upload` - fully automated, no filesystem access. |
| Bazarr | `POST /api/system/backups` to trigger, poll `GET` until the new file appears, download it, `DELETE` it server-side | The download route (`/system/backup/download/<file>`) is gated by Bazarr's own web-auth setting, **not** the API key - see below. Restore needs one local file write into Bazarr's own backup folder (no upload-restore endpoint exists), then an API call triggers the actual restore + restart. |
| Tdarr | `POST /api/v2/cruddb` with `mode: getAll` for every internal DB collection (library settings, flows, global settings, node registrations, staged/output/statistics) | Fully API-driven both ways. Restore does `removeAll` then re-`insert`s each document per collection - destructive, asks for confirmation. |
| Sabnzbd | `GET /sabnzbd/api?mode=get_config` to back up; `mode=set_config` per key to restore | SABnzbd's API hardcodes every password field (most importantly your Usenet server password) to `**********` on the way out - there's no API mode that returns the real value. Restore still automates everything else: it recreates each Usenet server (host/port/username/connections/ssl/priority/...) and every plain `misc`-style setting via the API, and interactively prompts you for each server's real password before sending it (verified against SABnzbd's own source - an existing server's fields not included in the API call are left untouched, so a skipped password doesn't get overwritten with a blank one). Categories, RSS feeds, and sorters use their own special-cased API shapes that aren't reverse-engineered here, so those aren't auto-restored. |

### Bazarr auth note

Bazarr's `/system/backup/download/...` route checks **Settings > General >
Security**, not your API key:
- `None` - works out of the box.
- `Basic` - fill in the "Basic auth username/password" fields on Bazarr's
  card in the web UI.
- `Forms` - not supported for automated download; switch to `None` or
  `Basic`, or put auth at your reverse proxy instead.

### Tdarr auth note

If you've enabled an API auth token in Tdarr's server settings, put it in
the (optional) API key field on Tdarr's card - it's sent as `Authorization:
Bearer <key>`. This header format isn't formally documented by Tdarr, so
verify it actually works for your version with the "Test connection"
button; leave it blank if Tdarr has no auth configured (the default).

## Destinations

Every backup is sent to whichever destinations you enable on the
**Settings** tab - enable more than one and each run uploads to all of
them.

### Local storage

Works with zero setup. Backups land in `/config/backuparr/backups` inside
the container, which is on the same `./data` volume as `config.json`, so
they survive container recreation without any extra mount. Download or
delete any of them straight from the **History** tab. Set a custom path on
the Local card in Settings if you'd rather point it at a different mounted
volume (e.g. a NAS share).

### Google Drive

Connected entirely from the web UI - no `rclone config`, no files to copy
onto the host. There's one unavoidable one-time step: Google requires every
app to have its own registered OAuth client (~5 minutes, all in a browser).
On the Google Drive card in **Settings**:

1. Click **Setup guide** - it walks through creating a Google Cloud project,
   enabling the Drive API, and creating an OAuth client, and shows the exact
   redirect URI to register (computed from whatever host/port you're
   reaching Backuparr on).
2. Paste the Client ID and Client Secret it gives you into the two fields,
   then **Save settings**.
3. Click **Connect Google Drive** and approve the consent screen - Backuparr
   only ever requests the `drive.file` scope, so it can only see files/
   folders it created or that you explicitly pick, not your whole Drive.
4. Click **Choose folder** to pick (or create) the Drive folder backups
   should go in, via Google's own folder picker.

Behind the scenes this generates an rclone remote from the OAuth token (the
same shape `rclone config`'s own Drive wizard would produce) and keeps it in
sync automatically - rclone still does the actual upload/download/list/
delete work, you just never have to touch its config file.

### OneDrive

Uses rclone's own built-in Microsoft app instead of a Backuparr-hosted OAuth
flow - Microsoft has required every *new* app registration to live in a
directory since June 2024, which most personal Microsoft accounts (and not
everyone qualifies for the free ways to get one) don't have. rclone's app
predates that requirement, so there's no Azure account or app registration
needed at all. Scoped to **personal** Microsoft accounts only, not
work/school (Microsoft 365) ones.

1. On any computer with a web browser - your own laptop is fine, it doesn't
   need to be wherever Backuparr runs - [download rclone](https://rclone.org/downloads/)
   (a single binary, no install) and run `rclone authorize onedrive`.
2. It opens/prints a link - sign in with your personal Microsoft account and
   approve access.
3. Copy the block rclone prints starting with "Paste the following into your
   remote machine --->" and paste it into the OneDrive card in **Settings**,
   then click **Connect OneDrive**.

There's no folder picker: Backuparr requests the `Files.ReadWrite.AppFolder`
scope, which Microsoft Graph resolves to a single dedicated app folder
(`Apps/Backuparr` in your OneDrive) created automatically on first connect -
narrower than requesting access to your whole OneDrive, at the cost of not
being able to choose a different folder.

### Dropbox

Shown on the Settings tab as "Coming soon" - not wired up yet.

## Deploying

Add the `backuparr` service block from `docker-compose.yml` into your
**existing** compose file (the one that already defines radarr/sonarr/etc.)
so it shares that stack's network and can reach the other containers by
name.

```sh
docker compose up -d --build backuparr
```

Then open `http://<host>:8990` - the first visit is a one-time setup screen
to create an admin username/password (stored as a salted hash, not
plaintext); every visit after that requires logging in. On the
**Settings** tab:

1. For each app you want backed up: flip it on, fill in its URL (container
   name + internal port if it's on the same Compose network, e.g.
   `http://radarr:7878` - or a LAN IP:port for anything on host networking,
   like Tdarr) and API key (from that app's Settings > General), then hit
   **Test connection** to confirm it's right before saving.
2. Enable at least one destination - Local needs nothing further; see
   [Destinations](#destinations) above for connecting Google Drive or
   OneDrive.
3. Set retention and a backup schedule (Daily/Weekly/Every few hours, with
   a time picker) - or expand **Advanced** to enter a raw cron expression
   instead.
4. **Save settings.**

Everything is written to `config.json` on the `./data` volume, so it
survives container recreation - and the cron schedule inside the container
picks up changes automatically the next time you save, no restart needed.

Use the **Run & Status** tab to trigger a backup immediately and watch it
happen live, **History** to see what's on each destination per app (and
download or delete any backup from there), and **Restore** for disaster
recovery (see below).

### Login

The setup screen's admin account is required by default - nothing to
configure, the first visit walks you through creating it. Forgot the
password? Click **Reset Backuparr** on the login screen - after confirming
a warning (it explains exactly what this deletes: every app's API key,
both destinations' connections, this admin account, and local backup
files - anything already uploaded to Google Drive/OneDrive is untouched)
and typing a confirmation phrase, it wipes local state back to a fresh
install and shows the setup screen again. There's no lighter-weight
recovery on purpose - see [Encryption at rest](#encryption-at-rest) below
for why.

**The reset endpoint is intentionally reachable without logging in** (a
locked-out admin has no session to present), gated only by that typed
confirmation phrase - which is fixed and visible in this project's source
(`webui/static/login.js`), not a per-install secret. Anyone who can reach
the web UI's network address can trigger it, no credentials required. This
is fine on a trusted LAN behind your own firewall (the normal deployment
model this README assumes throughout), but **do not expose port 8990 to
an untrusted network** (the open internet, a shared/guest network, etc.)
without putting a login of your own in front of it at the reverse-proxy
layer - the same TLS-terminating proxy recommended just below should also
gate access to this port entirely, not only encrypt it.

This only authenticates the app itself - if it's reachable beyond your own
LAN, put it behind your own reverse proxy for TLS the same way you likely
already do for Radarr/Sonarr, since a login page doesn't encrypt
credentials in transit over plain HTTP on its own.

### Encryption at rest

Every app's API key, Bazarr's basic-auth password, Google Drive's client
secret/refresh token, and OneDrive's token are encrypted in `config.json` -
not just the admin login. The key lives in its own file, `secrets.key`,
auto-generated on first run; override it with the `BACKUPARR_SECRETS_KEY`
env var to keep the key off the volume entirely (a Docker secret, for
instance) rather than trusting the auto-generated file next to everything
it protects.

`rclone.conf` - which mirrors the same Google Drive/OneDrive secrets for
rclone's own use - is encrypted too, using rclone's own built-in config
encryption rather than reinventing it. Same pattern: an auto-generated
password in its own file, `rclone.pass`, overridable with
`RCLONE_CONFIG_PASS` directly.

Neither key can be tied to your login password: backups run unattended on
a schedule, with nobody logged in to unlock anything, so both have to be
available on their own regardless of session state. That's also why a
forgotten-password reset can't be a quiet, no-consequence action (see
[Login](#login) above) - anyone who could reset the login without
consequence would, by definition, still have everything the encryption is
meant to protect sitting right there decrypted.

Encryption is on unconditionally - nothing to enable.

## Restoring after a disaster

Re-create the app containers from your compose file first (config volumes
will be empty/fresh), then use the **Restore** tab: pick the source,
pick the app, pick a backup (newest first), and confirm.

- **Radarr/Sonarr/Prowlarr** - fully automated, the app restarts itself.
- **Bazarr** - needs a local path to its own config/backup folder; fill in
  the override field if you didn't already set `bazarr_backup_dir` in
  Settings (this needs that path mounted into the backuparr container,
  see the commented-out volume in `docker-compose.yml`).
- **Tdarr** - destructive (wipes each DB collection before repopulating) -
  the UI warns about this before you confirm.
- **Sabnzbd** - click "Load Usenet servers" to see which ones need a
  password (SABnzbd's API never returns the real value - see the table
  above), type them in, then restore. Leave any blank to set that server's
  password manually in SABnzbd's Settings afterward instead.

The same operations are available from the CLI inside the container, e.g.
for scripting:

```sh
docker compose exec -it backuparr python3 restore.py radarr
docker compose exec -it backuparr python3 restore.py sabnzbd   # -it matters here, for the password prompts
```

If only one destination is enabled it's picked automatically; with more
than one, pass `--destination local`, `--destination gdrive`, or
`--destination onedrive`. `restore.py
--help` documents the rest (`--file <name>` for a specific backup, `--yes`
to skip confirmation/password prompts).

## Configuration reference

Everything below lives in `config.json` (edited via the web UI, or by hand
if you'd rather):

| Field | Purpose |
|---|---|
| `apps.<name>.enabled/url/api_key` | Per app, as shown in the Settings tab |
| `apps.bazarr.username/password` | Only if Bazarr's web auth is set to `Basic` |
| `destinations.local.enabled/path` | Local storage - path defaults to `/config/backuparr/backups` if blank |
| `destinations.gdrive.enabled/client_id/client_secret` | Google Drive OAuth client, set via the Setup guide |
| `destinations.gdrive.refresh_token/folder_id/folder_name` | Set automatically by the Connect/Choose folder buttons - don't hand-edit |
| `destinations.onedrive.enabled` | Whether the destination is active |
| `destinations.onedrive.token/drive_id/drive_type/item_id` | Set automatically by pasting a token from `rclone authorize onedrive` - don't hand-edit |
| `retention_days` | Delete backups older than this, per app per destination (default 7) |
| `cron_schedule` | Standard 5-field cron syntax (default `0 3 * * *`) |
| `notify_url` | Optional: POST a plain-text summary here after every run (e.g. an ntfy.sh topic) |
| `bazarr_backup_dir` | Local path to Bazarr's config/backup folder, for restores |

A few things are still env vars, since they're deployment-level rather
than app-level (set in `docker-compose.yml`):

| Env var | Purpose |
|---|---|
| `WEBUI_PORT` | Default `8990` |
| `BACKUPARR_SECRETS_KEY` | Optional: overrides the auto-generated `secrets.key` used to encrypt config.json's secrets (see [Encryption at rest](#encryption-at-rest)) |
| `RCLONE_CONFIG_PASS` | Optional: overrides the auto-generated `rclone.pass` used to encrypt rclone.conf |

## Credits

App icons (`webui/static/icons/`) are from the [selfh.st icon
collection](https://selfh.st/icons/) ([selfhst/icons on
GitHub](https://github.com/selfhst/icons)), licensed
[CC BY 4.0](https://github.com/selfhst/icons/blob/main/LICENSE).
