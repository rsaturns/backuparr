# Backuparr

**Secure your Arrs.**

Scheduled config/database backups for Radarr, Sonarr, Bazarr, Prowlarr,
Tdarr, and Sabnzbd, uploaded to Google Drive. If the host dies, the recovery
path is: re-create the containers from your compose file, then restore each
app's config from its latest backup. Everything - which apps to back up,
their URLs/API keys, the schedule, retention, and restores - is configured
and triggered from a web UI, not env vars.

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
Tdarr, which have their own equivalent mechanisms, and uploads to Google
Drive via [rclone](https://rclone.org/) instead of S3.

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

## One-time setup: rclone + Google Drive

This step needs a browser, so it has to be done interactively by you - it
can't be scripted headlessly.

1. Install rclone somewhere with a browser available (your laptop is fine,
   it doesn't have to be the NAS): https://rclone.org/downloads/
2. Run `rclone config` and create a new remote:
   - name: `gdrive`
   - type: `drive` (Google Drive)
   - client_id / client_secret: leave blank to use rclone's own (fine for
     personal use), or supply your own OAuth app for higher API quota
   - scope: `drive` (full access) or `drive.file` (only files rclone
     creates - more restrictive, recommended)
   - leave root_folder_id blank unless you want to scope it to a specific
     Drive folder
   - "Use auto config?" - say yes if you're on the machine with the
     browser; if you're configuring this directly on a headless NAS over
     SSH, say no and follow the `rclone authorize "drive"` prompt instead
     (run that command on your laptop, paste the resulting token back into
     the NAS session)
3. This produces `~/.config/rclone/rclone.conf`. Copy it into this project
   directory as `rclone.conf` (same directory as `docker-compose.yml`).
   It contains an OAuth refresh token - treat it like a credential, don't
   commit it (already covered by `.gitignore`).
4. Sanity check: `rclone lsd gdrive:` should list your Drive's folders.

rclone refreshes the token on its own, so this is a one-time step.

## Deploying

Add the `backuparr` service block from `docker-compose.yml` into your
**existing** compose file (the one that already defines radarr/sonarr/etc.)
so it shares that stack's network and can reach the other containers by
name.

```sh
docker compose up -d --build backuparr
```

Then open `http://<host>:8990` and, on the **Settings** tab:

1. For each app you want backed up: flip it on, fill in its URL (container
   name + internal port if it's on the same Compose network, e.g.
   `http://radarr:7878` - or a LAN IP:port for anything on host networking,
   like Tdarr) and API key (from that app's Settings > General), then hit
   **Test connection** to confirm it's right before saving.
2. Fill in your Google Drive remote (`gdrive:backuparr`, from the rclone
   setup above) and hit **Test**.
3. Set retention and a cron schedule (presets provided for common ones).
4. **Save settings.**

Everything is written to `config.json` on the `./data` volume, so it
survives container recreation - and the cron schedule inside the container
picks up changes automatically the next time you save, no restart needed.

Use the **Run & Status** tab to trigger a backup immediately and watch it
happen live, **History** to see what's actually in Drive per app, and
**Restore** for disaster recovery (see below).

If this is reachable beyond your own LAN, set `WEBUI_USERNAME`/
`WEBUI_PASSWORD` in `docker-compose.yml` first - the UI holds every app's
API key and can trigger destructive restores, and is unauthenticated by
default.

## Restoring after a disaster

Re-create the app containers from your compose file first (config volumes
will be empty/fresh), then use the **Restore** tab: pick the app, pick a
backup (newest first), and confirm.

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

`restore.py --help` documents the flags (`--file <name>` for a specific
backup, `--yes` to skip confirmation/password prompts).

## Configuration reference

Everything below lives in `config.json` (edited via the web UI, or by hand
if you'd rather):

| Field | Purpose |
|---|---|
| `apps.<name>.enabled/url/api_key` | Per app, as shown in the Settings tab |
| `apps.bazarr.username/password` | Only if Bazarr's web auth is set to `Basic` |
| `rclone_remote` | e.g. `gdrive:backuparr` |
| `retention_days` | Delete remote backups older than this, per app (default 14) |
| `cron_schedule` | Standard 5-field cron syntax (default `0 3 * * *`) |
| `notify_url` | Optional: POST a plain-text summary here after every run (e.g. an ntfy.sh topic) |
| `bazarr_backup_dir` | Local path to Bazarr's config/backup folder, for restores |

A few things are still env vars, since they're deployment-level rather than
app-level (set in `docker-compose.yml`):

| Env var | Purpose |
|---|---|
| `RUN_ON_START` | `true` to run one backup immediately on container start |
| `WEBUI_USERNAME`, `WEBUI_PASSWORD` | Basic auth for the web UI - recommended if it's reachable beyond your LAN |
| `WEBUI_PORT` | Default `8990` |

If you're upgrading from a pre-web-UI deployment that used env vars like
`RADARR_URL`/`APPS`/`RCLONE_REMOTE`, the first startup migrates them into
`config.json` automatically - check the Settings tab looks right, then feel
free to remove those env vars from `docker-compose.yml`.

## Credits

App icons (`webui/static/icons/`) are from the [selfh.st icon
collection](https://selfh.st/icons/) ([selfhst/icons on
GitHub](https://github.com/selfhst/icons)), licensed
[CC BY 4.0](https://github.com/selfhst/icons/blob/main/LICENSE).
