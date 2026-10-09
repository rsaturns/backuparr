<img src="webui/static/logo.png" alt="Backuparr logo" width="160">

# Backuparr

**Backup your Arrs.**

> **AI disclosure:** This project was built with substantial assistance
> from Claude (Anthropic) - code, documentation, and commit history
> included. Reviewed and maintained by a human.

Scheduled config/database backups for Radarr, Sonarr, Prowlarr, Profilarr,
Bazarr, Tdarr, SABnzbd, Tautulli, and Plex (Seerr coming soon), sent to every
destination you enable: Local storage, Google Drive, OneDrive, and Dropbox.
Apps, URLs/API keys, destinations, schedule, retention,
and restores are all configured and triggered from the web UI, not env vars.

Every app is backed up through **its own HTTP API**, never by reading its
config volume directly.

To recover after a host failure, re-create the containers from your compose
file, then restore each app from its latest backup (see
[Restoring after a disaster](#restoring-after-a-disaster)).

See [CHANGELOG.md](CHANGELOG.md) for release history.

## Table of contents

- [Backuparr](#backuparr)
  - [Table of contents](#table-of-contents)
  - [Architecture](#architecture)
  - [Why not reuse an existing tool?](#why-not-reuse-an-existing-tool)
  - [Per-app backup method (read this before deploying)](#per-app-backup-method-read-this-before-deploying)
    - [Plex backup note](#plex-backup-note)
    - [Profilarr backup note](#profilarr-backup-note)
    - [Tautulli backup note](#tautulli-backup-note)
    - [Tautulli restore note](#tautulli-restore-note)
    - [Bazarr auth note](#bazarr-auth-note)
    - [Tdarr auth note](#tdarr-auth-note)
  - [Environment variables](#environment-variables)
    - [Advanced: file locations](#advanced-file-locations)
  - [Destinations](#destinations)
    - [Local storage](#local-storage)
    - [Google Drive](#google-drive)
    - [OneDrive](#onedrive)
    - [Dropbox](#dropbox)
  - [Deploying](#deploying)
    - [Pull the published image (recommended)](#pull-the-published-image-recommended)
    - [Or build from source](#or-build-from-source)
    - [Prowlarr discovery](#prowlarr-discovery)
    - [Login](#login)
    - [Encryption at rest](#encryption-at-rest)
  - [Restoring after a disaster](#restoring-after-a-disaster)
  - [Notifications](#notifications)
    - [Discord](#discord)
    - [Slack](#slack)
    - [Telegram](#telegram)
    - [Gotify (self-hosted)](#gotify-self-hosted)
    - [ntfy.sh, Healthchecks.io, or anything else](#ntfysh-healthchecksio-or-anything-else)
  - [Configuration reference](#configuration-reference)
  - [Credits](#credits)
  - [License](#license)

## Architecture

<img src="webui/static/architecture-diagram.svg" alt="Radarr, Sonarr, Prowlarr, Profilarr, Bazarr, Tdarr, SABnzbd, and Tautulli each feed Backuparr over their own HTTP API; Backuparr uploads each backup via rclone to Local storage, Google Drive, Microsoft OneDrive, and Dropbox" width="100%">

## Why not reuse an existing tool?

[Zerka30/servarr-backup](https://github.com/Zerka30/servarr-backup) does
this for Radarr/Sonarr/Prowlarr via S3. Backuparr extends the same idea
(trigger the app's own backup API, download it, upload it) to Profilarr,
Bazarr, Tdarr, and Tautulli, and uses a pick-your-destinations model built
on [rclone](https://rclone.org/): Local out of the box, Google Drive via an
in-app "Connect" button, and OneDrive and Dropbox via a one-time
`rclone authorize` paste, with no need to run rclone's interactive config wizard yourself.

## Per-app backup method (read this before deploying)

| App | Method | Notes |
|---|---|---|
| Radarr / Sonarr / Prowlarr | `POST .../system/backup` to trigger, download the result, `DELETE` it server-side | The apps' official manual-backup zip. Restore is a multipart upload to `.../system/backup/restore/upload` - fully automated, no filesystem access. |
| Profilarr | `POST /api/v1/backups` to trigger (async job, polled via `GET /api/v1/jobs/{id}`), download the newest result, `DELETE` it server-side | Backup only - Profilarr's restore has no public API; see the [Profilarr backup note](#profilarr-backup-note). |
| Bazarr | `POST /api/system/backups` to trigger, poll `GET` until the new file appears, download it, `DELETE` it server-side | The download route (`/system/backup/download/<file>`) is gated by Bazarr's web-auth setting, **not** the API key - see the [Bazarr auth note](#bazarr-auth-note). Bazarr has no upload-restore endpoint, so restore writes the file into Bazarr's backup folder, then an API call triggers the restore and restart. |
| Tdarr | `POST /api/v2/cruddb` with `mode: getAll` for every internal DB collection (library settings, flows, global settings, node registrations, staged/output/statistics) | Fully API-driven both ways. Restore does `removeAll` then re-`insert`s each document one at a time (no bulk-insert mode) - destructive, asks for confirmation. |
| SABnzbd | `GET /sabnzbd/api?mode=get_config` to back up; `mode=set_config` per key to restore | SABnzbd's API returns every password field (e.g. a Usenet server password) as `**********`, with no way to get the real value. Restore recreates each Usenet server and every plain `misc`-style setting via the API, and asks for each server's real password (fields left out of the API call are untouched, so a skipped password isn't overwritten with a blank). Categories, RSS feeds, and sorters aren't auto-restored. |
| Tautulli | `GET /api/v2?cmd=download_database` and `cmd=download_config` - each streams a fresh copy directly, no trigger/poll step | The database comes back with Plex tokens nulled out; the config is only lightly sanitized - see the [Tautulli backup note](#tautulli-backup-note). Restore uploads each separately via `cmd=import_database` and `cmd=import_config` (multipart) - see the [Tautulli restore note](#tautulli-restore-note). |
| Plex | Native database export plus API server/library settings, metadata and artwork | Includes every user's local watch/progress state. Optional image-independent [restore agent](docs/plex-restore-agent.md) for database recovery. See [Plex backup note](#plex-backup-note) for scope. |
| Seerr | *(none)* | Not implemented - Seerr has no backup/restore API. Shown on the Settings tab as "Coming soon". |

### Plex backup note

Enter the Plex server URL (for example `http://plex:32400`), then click
**Get Plex token**. Sign in with the server owner's account in the Plex window
and authorize Backuparr. The token is filled automatically; if you own multiple
servers, select the one matching your URL. If popups are blocked, use the
**Open Plex sign-in** link. Test the connection, then **Save settings**.
Your Plex password and two-factor code are entered only on Plex's website.
This sign-in needs internet access to plex.tv; it does not require a publicly
reachable Backuparr URL. Cancelling or editing the Plex credentials discards
the pending result, and acquiring a token never saves settings automatically.

You can also paste the server owner's **Plex token** (`X-Plex-Token`) manually. See Plex's guide to
[finding your token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).
It is stored encrypted in Backuparr's `api_key` field and sent in an HTTP
header. A domain/reverse proxy is fine. Same-origin redirects are followed with
a bounded hop count; redirects to another host, port or protocol never receive
the token. A reverse proxy must permit the Plex API paths used here,
including `/identity`, `/:/prefs`, `/diagnostics/databases` and `/library/...`, with Plex
handling token authentication.

The ZIP contains:

- `databases.zip`: Plex's **unchanged native Download Database archive**.
  This preserves library metadata and local user/account mappings, including
  **watched/unwatched state, playback progress and ratings for every user**
  in `com.plexapp.plugins.library.db` (`metadata_item_settings`). It is not
  limited to the token owner's watch state. Playlist/collection data stored
  in the database is retained too.
- `server-preferences.xml`, `library-preferences/`, `library-sections.xml`:
  server and per-library settings exposed through Plex's API.
- `metadata/`: paginated library metadata by media type, including series,
  seasons, episodes, music and collections where the library supports them.
- `artwork/`: library thumbnails, backgrounds and banners served by Plex,
  with a JSON index mapping files to source item URLs and MIME types.
- `manifest.json`, `RESTORE.txt`: source server/version, scope, missing artwork and recovery notes.

**Missing:** the native `Preferences.xml` (or platform registry equivalent),
media and subtitle files, caches, codecs, plugin binaries/private data and
cloud-only Plex account information not present on this server. API settings
are recovery references, not a native preferences file. External artwork URLs
are recorded in the manifest but are not fetched with the server token.
Artwork and metadata are not a byte-for-byte copy of Plex's data directory.
Protect the archive: exported settings/databases can contain credentials.

Large libraries/artwork can take time and space. API exports run sequentially
and report each phase in the live run log, including metadata counts and
periodic download progress. Waiting for Plex to prepare its database snapshot
is reported separately from downloading it. API settings/artwork reads
are not atomic with the native database snapshot; avoid scans and settings
changes during a backup. Artwork that Plex lists but cannot serve (HTTP 404), or
redirects outside the configured origin, is
recorded in `manifest.json` under `unavailable_artwork`; it does not discard
the database backup. Broken pagination and other failed downloads fail the backup.

**Automatic database restore:** deploy the optional [Plex restore agent](docs/plex-restore-agent.md),
enter its URL and separate token in Settings, then use Backuparr's **Restore**
tab. The agent works beside your existing Plex Docker image. It verifies the
target, stops the container, preserves rollback copies, replaces the library
database and restarts Plex. This requires a new backup carrying its source
server/version and the same server and exact Plex version at restore time.
API settings and artwork still require the manual steps below.

**Restore manually:** download the archive from History and extract
`databases.zip`. Plex's API normally names the library snapshot
`databaseBackup.db`, sometimes with a UUID appended (for example
`databaseBackup.db3fa58294-9e85-4bd7-ac6d-8da54a567d7e`);
rename it to `com.plexapp.plugins.library.db` before
restoring it. If it already has the latter name, keep it unchanged.
Stop Plex, keep a copy of the target database directory,
and follow Plex's [database restore procedure](https://support.plex.tv/articles/202485658-restore-a-database-backed-up-via-scheduled-tasks/)
to replace the matching library database files (including the blobs database
when supplied). Remove stale `-wal`/`-shm` companions as the guide directs,
retain file permissions and restart Plex. Use the same Plex version first.
This restores the library, playlists/collections and locally stored user watch
state/account mappings together; reconnect the same Plex users to see their
state. Cloud-only history and newly assigned account identities are not covered.

For server/library settings, open the corresponding settings screens and reapply
the exported XML `Setting` values, omitting read-only/token/identity fields.
The API equivalents are `PUT /:/prefs` and
`PUT /library/sections/{sectionId}/prefs` with the reviewed setting IDs/values
as parameters. Use the current library IDs after migration. Do not rename
`server-preferences.xml` to native `Preferences.xml`.

Database recovery restores metadata values; artwork bytes need a separate step
because the native export does not contain the metadata folders. Match each
`artwork/index.json` source item to the restored library (or path/GUID in
`metadata/` if IDs changed), open its **Edit > Poster/Background** screen and
upload the corresponding exported file. Missing artwork can also be refreshed
from its provider. No media folders need mounting into Backuparr for backup;
manual database recovery itself requires access to Plex's data directory.
Backuparr itself needs no Docker socket or Plex filesystem mounts.

For selective migration of watched state and ratings, follow Plex's
[Move Viewstate/Ratings guide](https://support.plex.tv/articles/201154527-move-viewstate-ratings-from-one-install-to-another/),
including account/media matching when those differ. See also Plex's
[Download Database documentation](https://support.plex.tv/articles/226836308-help/).

### Profilarr backup note

Profilarr sanitizes the backup before it leaves the server, stripping arr
instance URLs/API keys, sync configs, notification webhook URLs/tokens, user
accounts and sessions, personal access tokens for linked databases, and
AI/TMDB API keys. After a restore, re-add those by hand.

Restore isn't automated: Profilarr's restore is a browser-session-only form
outside its `/api/v1` REST API, and only stages a pending restore that is
applied at the next container restart. Profilarr therefore doesn't appear on
the **Restore** tab. To restore by hand:

1. Download the backup from Backuparr's **History** tab.
2. Upload it in Profilarr's own **Settings > Backups** and restore it there.
3. Restart the Profilarr container and re-add whatever was stripped above.

### Tautulli backup note

`download_database` nulls out Plex user/server tokens server-side.
`download_config` only strips `PMS_TOKEN`/`JWT_SECRET` - **the Tautulli API
key and any notification agent credentials in config.ini (webhook URLs,
tokens, etc.) are in the backup in plain text.**

Backuparr's own `config.json` secrets are encrypted (see [Encryption at
rest](#encryption-at-rest)), but treat every destination holding Tautulli
backups as holding a live Tautulli API key, and rotate it in Tautulli's
Settings if a destination is ever compromised.

### Tautulli restore note

Tautulli's `import_config` API call only **stages** the uploaded config.
Applying it takes a second request to a web route (`/restart_import_config`)
that Tautulli protects with its login, and Backuparr makes that request for
you right after the upload:

- **No login set up in Tautulli** (Settings > Web Interface): the import
  starts and Tautulli restarts by itself.
- **Login enabled:** an API key can't use that route, so the config stays
  staged and nothing changes yet. The Restore tab (and the log) says so and
  shows the URL: log in to Tautulli, open
  `<your Tautulli URL>/restart_import_config`, and the import runs and
  Tautulli restarts. The staged import is held in Tautulli's memory: if
  Tautulli restarts before you open that URL, its page still says "Importing a
  Config" but nothing is applied, so run the restore again.

The database can't be restored over the API at all (an upstream Tautulli bug),
so Backuparr skips it and says so; import `tautulli.db` by hand from Tautulli's
Settings > Import & Backup > Import Database.

### Bazarr auth note

Bazarr's `/system/backup/download/...` route follows **Settings > General >
Security**, not your API key:
- `None` - works out of the box.
- `Basic` - fill in the "Basic auth username/password" fields on Bazarr's
  card.
- `Forms` - not supported for automated download; switch to `None` or
  `Basic`, or authenticate at your reverse proxy instead.

### Tdarr auth note

If Tdarr's server settings have an API auth token enabled, put it in the
optional API key field on Tdarr's card; it's sent as `Authorization: Bearer
<key>`. Use **Test connection** to confirm it works for your version, and
leave it blank if Tdarr has no auth (the default).

## Environment variables

App-level settings (apps, URLs/API keys, destinations, schedule, retention)
live in `config.json` and are edited in the web UI (see [Configuration
reference](#configuration-reference)). The deployment-level settings below
are set in your compose file's `environment:` block.

| Env var | Default | Purpose |
|---|---|---|
| `WEBUI_HOST` | `0.0.0.0` | Address the web UI listens on. With `network_mode: host`, set `127.0.0.1` to allow connections only through the host's loopback interface. |
| `WEBUI_PORT` | `8990` | Port the web UI listens on inside the container |
| `PUID` / `PGID` | `1000` / `1000` | User/group the container runs as instead of root. Match your host user (`id`) so files on the config volume are owned by you; re-applied on every start |
| `TZ` | UTC | Timezone: sets the local time the cron schedule fires at, and the timestamps in `backup.log` |
| `UMASK` | `077` | Permissions of newly created files: the default makes backups readable only by the container user (files `0600`, directories `0700`). Set `022` if something else on the host reads the backup folder as a different user. Files written earlier keep their old permissions. |
| `LOG_LEVEL` | `INFO` | Python logging level for backup/restore runs, e.g. `DEBUG` when troubleshooting |
| `BACKUPARR_SECRETS_KEY` | *(auto-generated)* | Overrides the generated `secrets.key` that encrypts `config.json`'s secrets (see [Encryption at rest](#encryption-at-rest)); set it to keep the key off the volume, e.g. a Docker secret |
| `RCLONE_CONFIG_PASS` | *(auto-generated)* | Overrides the auto-generated password used to encrypt `rclone.conf` |
| `BACKUPARR_DISABLE_AUTH` | `false` | Set to `true` (or `1`/`yes`) to turn off local login when an authenticating reverse proxy (e.g. Authelia) protects the whole UI and API. Anything else keeps login required. See [Login](#login). |
| `BACKUPARR_FORCE_HTTPS` | `false` | Set to `true` behind your own TLS-terminating reverse proxy: marks the session cookie `Secure` and trusts the proxy's `X-Forwarded-Proto`/`X-Forwarded-For` headers, so Google Drive OAuth redirect URIs are `https://` and login lockout sees real client IPs. Leave unset for plain HTTP on a LAN, or login will silently fail. |
| `BACKUPARR_ALLOW_CROSS_HOST_REDIRECTS` | `false` | Set to `true` (or `1`/`yes`) only if an app's configured URL redirects to a *different* host and you can't update the URL to its final address. By default Backuparr refuses such redirects so your API key is never sent to another host; same-host redirects (including http→https) always work. |

For loopback-only access with Docker host networking, set `network_mode: host`
on the service and `WEBUI_HOST: "127.0.0.1"` in its environment. Remove the
`ports:` section, since host networking does not use port publishing. A reverse
proxy running on the host (or also using host networking) can then reach
Backuparr at `http://127.0.0.1:8990`.

With Docker bridge networking, keep the default `WEBUI_HOST`. To restrict the
published port to the host, use `ports: ["127.0.0.1:8990:8990"]` instead; binding
the app to loopback inside a bridge container prevents published-port access.

### Advanced: file locations

Only needed if you move files out of the single `/config/backuparr` mount;
by default every path below is inside it.

| Env var | Default | What it is |
|---|---|---|
| `BACKUPARR_CONFIG` | `/config/backuparr/config.json` | The main config file |
| `RCLONE_CONFIG` | `/config/backuparr/rclone.conf` | rclone's own remotes config |
| `RCLONE_CONFIG_PASS_FILE` | `/config/backuparr/rclone.pass` | Generated `rclone.conf` encryption password (ignored if `RCLONE_CONFIG_PASS` is set) |
| `BACKUPARR_SECRETS_KEY_PATH` | `/config/backuparr/secrets.key` | Generated config-encryption key (ignored if `BACKUPARR_SECRETS_KEY` is set) |
| `BACKUPARR_SECRET_KEY_PATH` | `/config/backuparr/secret_key` | Flask's session-signing key - **not** `secrets.key` above, which encrypts `config.json`'s secrets |
| `BACKUPARR_AUTH` | `/config/backuparr/auth.json` | Admin username and password hash from the setup screen |
| `BACKUPARR_LOG_DIR` | `/var/log/backuparr` | Where `backup.log` (shown on the Run & Status tab) is written |

## Destinations

Enable one or more destinations on the **Settings** tab; every run uploads
to all of them.

### Local storage

No setup needed. Backups go to `/config/backuparr/backups`, on the same
volume as `config.json`, so they survive container recreation. Download or
delete them from the **History** tab. To use a different mounted volume
(e.g. a NAS share), set a custom path on the Local card.

### Google Drive

Connected entirely from the web UI - no `rclone config` or files to copy.
On the Google Drive card in **Settings**:

1. Click **Setup guide**. It walks through creating a Google Cloud project,
   enabling the Drive API, creating an OAuth client and an API key, and shows
   the exact redirect URI to register. It also has you publish the OAuth app
   (Audience tab > **Publish App**) - don't skip this: Google expires
   refresh tokens after 7 days for apps left in Testing status, which breaks
   the connection weekly until you reconnect.
2. Paste the Client ID, Client Secret, and API key into the three fields,
   then **Save settings**. The API key is separate from the OAuth client; the
   **Choose folder** picker needs it, and loads blank without it.
3. Click **Connect Google Drive** and approve the consent screen. Backuparr
   requests only the `drive.file` scope (files/folders it created or you
   picked, not your whole Drive). Google shows a "Google hasn't verified this
   app" warning because your app is published but unverified; that's
   expected - click **Advanced**, then **Go to (your app name) (unsafe)**.
4. Click **Choose folder** to pick (or create) the Drive folder for backups.

### OneDrive

Uses rclone's built-in Microsoft app, so no Azure account or app
registration is needed. Personal Microsoft accounts only, not work/school
(Microsoft 365).

1. On any computer with a browser (not necessarily where Backuparr runs),
   [download rclone](https://rclone.org/downloads/) (a single binary) and run
   `rclone authorize onedrive`.
2. Sign in with your personal Microsoft account at the link it opens or
   prints, and approve access.
3. Copy the block rclone prints after "Paste the following into your remote
   machine --->", paste it into the OneDrive card in **Settings**, and click
   **Connect OneDrive**.

There's no folder picker: backups go to a dedicated `Apps/Backuparr` folder
in your OneDrive, created on first connect.

### Dropbox

Uses rclone's built-in Dropbox app, so no Dropbox developer account or app
registration is needed.

1. On any computer with a browser (not necessarily where Backuparr runs),
   [download rclone](https://rclone.org/downloads/) (a single binary) and run
   `rclone authorize dropbox`.
2. Sign in to Dropbox at the link it opens or prints, and approve access.
3. Copy the block rclone prints after "Paste the following into your remote
   machine --->", paste it into the Dropbox card in **Settings**, and click
   **Connect Dropbox**.

There's no folder picker: backups go to a `Backuparr` folder in your
Dropbox, created on the first backup.

## Deploying

Add a `backuparr` service to your **existing** compose file (the one that
defines radarr/sonarr/etc.) so it shares that network and can reach the other
containers by name.

### Pull the published image (recommended)

```yaml
services:
  backuparr:
    image: rsaturns/backuparr:latest
    container_name: backuparr
    restart: unless-stopped
    environment:
      - TZ=America/Los_Angeles
      - PUID=1000
      - PGID=1000
      # Optional - see Environment variables above.
      #- LOG_LEVEL=INFO
      #- BACKUPARR_FORCE_HTTPS=true
      #- BACKUPARR_DISABLE_AUTH=true
      #- BACKUPARR_SECRETS_KEY=
      #- RCLONE_CONFIG_PASS=
      # Changing this also means updating the ports: mapping below.
      #- WEBUI_PORT=8990
    ports:
      - "8990:8990"
    volumes:
      # config.json, rclone.conf, and local-destination backups.
      - /share/Container/backuparr:/config/backuparr
      # Optional: needed only to restore Bazarr - path to its config/backup folder.
      #- /share/Container/bazarr/backup:/mnt/bazarr-backup
```

```sh
docker compose up -d
```

Or without Compose:

```sh
docker run -d --name backuparr --restart unless-stopped \
  -e TZ=America/Los_Angeles -e PUID=1000 -e PGID=1000 \
  -p 8990:8990 \
  -v /share/Container/backuparr:/config/backuparr \
  rsaturns/backuparr:latest
```

Published for `linux/amd64` and `linux/arm64`. `latest` tracks the most
recent tagged release, not every commit to `main`. To control upgrades, pin a
version (e.g. `rsaturns/backuparr:1.1.0-beta`); see [Docker
Hub](https://hub.docker.com/r/rsaturns/backuparr) for tags.

### Or build from source

Clone this repo and use its `docker-compose.yml` (or swap the `image:` line
above for `build: .`):

```sh
docker compose up -d --build backuparr
```

Then open `http://<host>:8990`. The first visit is a one-time setup screen
to create an admin username and password; after that, every visit requires
login. On the **Settings** tab:

1. For each app you want backed up: switch it on, enter its URL (container
   name and internal port on the same Compose network, e.g.
   `http://radarr:7878`, or LAN IP:port for anything on host networking like
   Tdarr) and API key (from the app's Settings > General), and click **Test
   connection** before saving. Using Prowlarr? Set it up first (it leads the
   list) and click **Discover services** to fill in Radarr, Sonarr and
   SABnzbd - see [Prowlarr discovery](#prowlarr-discovery).
2. Enable at least one destination. Local needs nothing further; see
   [Destinations](#destinations) for Google Drive, OneDrive and Dropbox.
3. Set retention and a schedule (Daily/Weekly/Every few hours with a time
   picker, or **Advanced** for a raw cron expression).
4. **Save settings.**

Settings are written to `config.json` on the config volume, so they survive
container recreation, and schedule changes apply on save with no restart.

Use the **Run & Status** tab to run a backup now and watch it live,
**History** to see (and download or delete) each destination's backups per
app, and **Restore** for [disaster recovery](#restoring-after-a-disaster).

### Prowlarr discovery

Optional - everything can be configured by hand. On the Settings tab, switch on
the Prowlarr card at the top of the list, enter its URL and API key, and click
**Discover services**. Backuparr fills in the URLs and API keys of the
Radarr, Sonarr and SABnzbd instances configured in Prowlarr, and lets you
choose when there are several.

- Only empty fields are filled, and nothing is enabled or saved until you do
  so. **Test connection** never runs discovery.
- Prowlarr masks its apps' API keys, so Backuparr reads them from a temporary
  Prowlarr backup and deletes it afterwards (existing backups are never
  touched). Results stay in memory for ten minutes.
- The URLs are the ones Prowlarr uses; run **Test connection** to confirm
  Backuparr can reach them.
- On PostgreSQL, or if a login proxy blocks `/backup/`, URLs are still filled
  in and you enter the missing keys by hand. Backuparr never asks for web-login
  credentials.
- Discovery refuses any redirect away from Prowlarr's exact URL, so use its
  final address.
- Bazarr, Profilarr, Tdarr, Tautulli and Seerr aren't stored in Prowlarr and
  are always configured manually.

### Login

#### Authentication at the reverse proxy

Local login is on unless you opt out. If your reverse proxy already
authenticates every request (e.g. Authelia), set
`BACKUPARR_DISABLE_AUTH=true` and recreate the container:

```yaml
services:
  backuparr:
    environment:
      BACKUPARR_DISABLE_AUTH: "true"
```

With this set, the UI and API work without a local account or login,
including on a fresh install. `/login` and `/setup` redirect to the main UI,
the logout button is hidden, and `/api/setup`, `/api/login`, `/api/logout`
and `/api/reset` return `403`. Existing credentials and backups are kept;
remove the setting (or set it to `false`) and recreate the container to
require login again.

**Protect the entire UI and API at the reverse proxy and block direct access
to port 8990.** Anyone who can reach Backuparr directly in this mode has full
access, including backup/restore and stored credentials. Backuparr does not
read proxy user headers.

#### Local account and password recovery

The admin account from the setup screen is required by default. Forgot the
password? Click **Reset Backuparr** on the login screen. After you confirm a
warning and type a confirmation phrase, it wipes local state (every app's API
key, every cloud destination's connection, the admin account, and local backup
files) and returns to a fresh install and the setup screen. Anything already
uploaded to Google Drive, OneDrive or Dropbox is untouched.

**With local authentication enabled, the reset endpoint is reachable without
logging in**, gated only by a fixed confirmation phrase visible in this
project's source (`webui/static/login.js`), not a per-install secret. That's
fine on a trusted LAN behind your firewall, but **don't expose port 8990 to an
untrusted network** without your own login in front of it at the reverse
proxy.

Login only protects the app itself. If it's reachable beyond your LAN, put it
behind a reverse proxy for TLS, as you likely do for Radarr/Sonarr.

### Encryption at rest

Every app's API key, Bazarr's basic-auth password, Google Drive's client
secret/API key/refresh token, OneDrive's and Dropbox's tokens, and the Notify URL (which can
embed a Telegram bot token or webhook secret) are encrypted in `config.json`.
The key is `secrets.key`, generated on first run; set `BACKUPARR_SECRETS_KEY`
to keep it off the volume entirely (e.g. a Docker secret).

`rclone.conf`, which mirrors the Google Drive/OneDrive/Dropbox secrets for rclone, is
encrypted with rclone's built-in config encryption, using a generated password
in `rclone.pass` (override with `RCLONE_CONFIG_PASS`).

Encryption is always on; there's nothing to enable.

## Restoring after a disaster

First re-create the app containers from your compose file (their config
volumes will be empty). Then on the **Restore** tab, pick the source, the app,
and a backup (newest first), and confirm. Like a backup run, a restore shows
live progress and a log.

Every restorable app also has a **"Restore to a different target"** checkbox
to override that app's URL/API key (and extra fields, e.g. Bazarr's Basic auth
username/password) for this restore only - nothing is written to Settings.
Use it to restore onto a rebuilt or renamed instance, or to try a restore on a
throwaway copy first.

- **Radarr/Sonarr/Prowlarr** - fully automated; the app restarts itself.
- **Profilarr** - not available here; see the [Profilarr backup
  note](#profilarr-backup-note) for the manual steps.
- **Bazarr** - needs the local path to its config/backup folder, mounted into
  the Backuparr container (see the commented-out volume in
  `docker-compose.yml`). Fill in "Bazarr backup folder" unless
  `bazarr_backup_dir` is already set in Settings.
- **Tdarr** - destructive (wipes each DB collection before repopulating); the
  UI warns you before you confirm.
- **SABnzbd** - choosing a backup loads its Usenet server list and shows which
  servers need a password (SABnzbd's API never returns the real value; see the
  table above). Type them in, or leave any blank and set that server's
  password in SABnzbd afterward.
- **Tautulli** - restores the config (the database must be imported by hand);
  Tautulli restarts once the config is applied. With a login enabled in
  Tautulli the config is only staged and the result tells you how to finish -
  see the [Tautulli restore note](#tautulli-restore-note).

## Notifications

Set **Notify URL** on the Settings tab (`notify_url` in config.json) to get
a summary POSTed after every run: one line per app (✅/❌), under a 🎉 header
if everything succeeded or a ⚠️ header if anything failed. Backuparr
recognizes common webhook URL shapes and sends the right kind of request
automatically.

### Discord

1. In the target channel: **Edit Channel > Integrations > Webhooks > New
   Webhook**, then **Copy Webhook URL**.
2. Paste that straight into **Notify URL**.

### Slack

1. Create an [incoming
   webhook](https://api.slack.com/messaging/webhooks) for your
   workspace and channel.
2. Paste the resulting `https://hooks.slack.com/services/...` URL into
   **Notify URL**.

### Telegram

1. Message [@BotFather](https://t.me/BotFather) to create a bot and get its
   token.
2. Get your chat ID: message the bot once, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and read
   `chat.id` (for a group, add the bot to the group first and use the group's
   negative chat ID).
3. Set **Notify URL** to:
   ```
   https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>
   ```

### Gotify (self-hosted)

1. In Gotify, create an application under **Apps** and copy its token.
2. Set **Notify URL** to:
   ```
   https://<your-gotify-host>/message?token=<APP_TOKEN>
   ```

### ntfy.sh, Healthchecks.io, or anything else

Any URL that doesn't match the shapes above gets the message as a plain-text
`POST` body, which is [ntfy.sh](https://ntfy.sh)'s native format. Set
**Notify URL** to `https://ntfy.sh/your-topic-name` (a self-hosted ntfy server
works the same way). This also works with a
[healthchecks.io](https://healthchecks.io)-style ping URL, an Uptime Kuma push
URL, or any webhook logger that accepts raw text.

## Configuration reference

These fields live in `config.json`, edited via the web UI or by hand:

| Field | Purpose |
|---|---|
| `apps.<name>.enabled/url/api_key` | Per app, as shown in the Settings tab |
| `apps.bazarr.username/password` | Only if Bazarr's web auth is set to `Basic` |
| `destinations.local.enabled/path` | Local storage - path defaults to `/config/backuparr/backups` if blank |
| `destinations.gdrive.enabled/client_id/client_secret/developer_key` | Google Drive OAuth client + API key, set via the Setup guide |
| `destinations.gdrive.refresh_token/folder_id/folder_name` | Set automatically by the Connect/Choose folder buttons - don't hand-edit |
| `destinations.onedrive.enabled` | Whether the destination is active |
| `destinations.onedrive.token/drive_id/drive_type/item_id` | Set automatically by pasting a token from `rclone authorize onedrive` - don't hand-edit |
| `destinations.dropbox.enabled` | Whether the destination is active |
| `destinations.dropbox.token` | Set automatically by pasting a token from `rclone authorize dropbox` - don't hand-edit |
| `retention_days` | Delete backups older than this, per app per destination (default 7) |
| `cron_schedule` | Standard 5-field cron syntax (default `0 3 * * *`) |
| `notify_url` | Optional: POST a summary here after every run - see [Notifications](#notifications) above |
| `bazarr_backup_dir` | Local path to Bazarr's config/backup folder, for restores |

Deployment-level settings are env vars, not `config.json`; see [Environment
variables](#environment-variables).

## Credits

App icons (`webui/static/icons/`) are from the [selfh.st icon
collection](https://selfh.st/icons/) ([selfhst/icons on
GitHub](https://github.com/selfhst/icons)), licensed
[CC BY 4.0](https://github.com/selfhst/icons/blob/main/LICENSE).

## License

[GNU AGPLv3](LICENSE) - if you run a modified version as a network service,
its source must be available to its users too.
