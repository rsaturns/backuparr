# Changelog

All notable changes to this project are documented in this file. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Security

- The settings the web UI loads included the Google Drive, OneDrive and
  Dropbox sign-in tokens, which never need to leave the server - anyone with a
  logged-in browser session could read them. The UI now only learns whether
  each one is connected.

- Profilarr, Bazarr, Emby, Seerr and SABnzbd sent their API key to whatever
  host a redirect pointed at, such as a reverse proxy's login page on another
  domain. Like Radarr, Sonarr and Prowlarr, they now refuse redirects to a
  different host unless `BACKUPARR_ALLOW_CROSS_HOST_REDIRECTS` is set.

- Backuparr passed the Google Drive client secret and refresh token to rclone
  on its command line every time it opened History, Restore or ran a backup,
  where any user on the Docker host could see them with `ps`. It now does so
  only when the Google Drive settings change.

- The Overview showed its error message as HTML, the one place the web UI
  didn't treat text from the server as plain text. It now does.

- The CI and release workflows run third-party GitHub Actions pinned to exact
  commits instead of version tags that can be moved, so a compromised action
  can't slip into the image build or the Docker Hub push.

### Fixed

- A username with accented or other non-ASCII letters (such as `Tím`) could be
  set at first-run setup but then never log in - every login failed with a
  server error, leaving only Reset. Setup and login also answered a username
  or password sent as a number with a server error instead of rejecting it.

- A new Radarr/Sonarr instance could be given the id, and so the backup
  folder, of one that had been removed. Its backups then mixed with the
  removed server's in History, Restore and retention, and an older file could
  restore the other server's data. Backuparr now remembers removed instances'
  ids and never reuses them. The second instance with the same name also gets
  `-2` instead of `-3`.

- Prowlarr discovery wrote what it found for an extra Radarr/Sonarr instance -
  its API key, and with **Use this service** its URL too - into the first
  instance's card, leaving that one with the wrong server or key.

- With the first Radarr/Sonarr card empty, Prowlarr discovery could fill it
  with a server another instance's card already had, so both backed up the
  same server. The first card is now only offered servers no other card holds.

- Removing a Radarr/Sonarr instance that a request header was limited to
  silently took it out of that header's apps - narrowing the header, or
  leaving it with no apps and a confusing save error. Settings now says which
  header to change first.

- Emby and Seerr backups failed, or read the wrong file, when the mounted data
  folder's path contained `#`, `?` or `%`.

- A Bazarr backup whose download was damaged on the way (a member failing its
  checksum) was kept as good and Bazarr's own copy deleted. Backuparr now
  downloads it again until it reads back intact.

- Restoring NZBGet trimmed spaces from the start and end of setting values. A
  value with a line break in it, which the backup can't hold, now fails the
  backup with the setting's name instead of being split into two lines.

- Jellyfin's **Test** could fail with an unexpected error instead of "check
  the volume mount" when the first backup Jellyfin listed had no path.

- Reset failed and deleted nothing when the local backup folder couldn't be
  read, such as a stale network mount, leaving no way back in after a
  forgotten password. It now resets anyway and logs which backups it couldn't
  remove.

- A failed backup of an extra instance was reported as "radarr-4k: radarr:
  unauthorized ...", which on the Overview read as if the first Radarr had
  failed. It now reads "radarr-4k: unauthorized ...".

- While a backup or restore ran, a slow status reply could let the progress
  view's requests pile up and show an older state after a newer one, and a
  failed request (the container restarting) went unreported. The view now asks
  again only after each answer, and shows and retries failures.

- A Tdarr restore cut off partway, for example by a network drop, failed with
  a bare connection error. It now says which collection was left partly
  restored and how far it got, and that running the restore again finishes it.

### Changed

- Opening History or Restore, downloading or deleting a backup, and every
  backup run now start one rclone process to check the cloud connections
  instead of three to five, so those pages load faster.

- While a backup runs, the progress view's status checks no longer re-read the
  whole log file each time (up to 1 MB every 1.5 seconds), and the run's log
  they send is capped at its newest 500 lines.

## [1.9.0-beta] - 2026-10-29

### Added

- More than one Radarr or Sonarr (#46), such as a 4K Radarr beside the HD one
  or an anime Sonarr. **Add another Radarr/Sonarr** in Settings adds a named
  instance with its own settings, backups, History, Restore and authentication
  headers; Prowlarr discovery can add the ones it finds. Existing settings and
  backups are unchanged.

## [1.8.14-beta] - 2026-10-29

### Security

- Another website could make a visitor's browser reset Backuparr - deleting
  its settings, keys, login and local backups - because the reset needs no
  login and the API accepted plain-text request bodies. Every change to
  Backuparr (settings, backups, restores, reset, first-run setup) now has to
  be sent as JSON, which other sites can't do without permission.
- With `BACKUPARR_FORCE_HTTPS`, Backuparr took a client's address from
  `X-Forwarded-For` whoever sent it, so the login lockout could be dodged by
  connecting to the port directly. It now trusts that header only from the
  proxy named in the new `BACKUPARR_TRUSTED_PROXY` setting.
- The CI workflow runs with read-only repository access.

### Fixed

- Retention and Reset only delete Backuparr's own archives
  (`<app>/<app>_<date>_<time>.<ext>`). Before, retention deleted every old file
  in the destination folder and Reset deleted the whole Local storage folder,
  which could remove unrelated files when Local storage pointed at a shared
  folder.
- Retention always keeps each app's newest backup, so an app that keeps
  failing or was switched off doesn't lose its last good backup.
- NZBGet: restoring settings with accented or non-Latin characters (paths,
  category names, passwords) no longer corrupts them.
- NZBGet: if NZBGet doesn't confirm the reload after a restore, the restore now
  says to restart NZBGet instead of reporting that it's reloading.
- NZBGet: a setting containing an unusual Unicode line separator no longer
  breaks a restore.
- If the encryption key changes, saving settings no longer erases the stored
  API keys, passwords and tokens: they're kept encrypted until the original key
  is back.
- A Tdarr restore checks every collection in the backup before wiping
  anything, so a damaged backup can't leave Tdarr half restored.
- Tautulli and Profilarr backups fail when the app returns an error instead of
  the file, rather than saving the error as a backup.
- A backup and a restore can no longer run at the same time; a scheduled backup
  during a restore is skipped and logged.
- Bazarr's download error no longer points at environment variables that don't
  exist; it says to fill in the Basic auth fields on Bazarr's card.

## [1.8.0-beta] - 2026-10-29

### Added

- NZBGet backup and restore. NZBGet has no API key, so its card takes the
  Control username and password (the password is encrypted at rest like API
  keys). A backup holds every NZBGet setting exactly as `nzbget.conf` stores
  it - news servers with their real passwords, categories, paths, extension
  settings - in the format of NZBGet's own **Backup settings**, so it can also
  be restored through NZBGet's web UI. Restoring replaces NZBGet's settings
  with the backup's and reloads NZBGet; the queue and history are kept.
  Test connection refuses NZBGet's restricted and add-only users, which can't
  read every setting.

### Changed

- The README and the architecture diagram no longer say that app config files
  are never read directly: the media servers and Seerr are backed up from
  their mounted folders.
- The README is shorter and easier to scan: a Quick start with the complete
  compose example and first-run steps now comes right after the introduction,
  the per-app table lists each app's backup and restore, the per-app notes drop
  repeated explanations, and discovery, service authentication headers, login
  and encryption are grouped under Settings and security. It also now says
  that backup archives aren't encrypted.

## [1.7.0-beta] - 2026-10-29

### Added

- Seerr backup and restore. Seerr has no backup API, so its config folder (its
  `/app/config`) is mounted into Backuparr and set as the new **Data folder**
  field; a read-only mount is enough for backups. A backup holds Seerr's
  database - users, requests, issues, the blocklist - copied with SQLite's
  online backup so Seerr keeps running, and `settings.json` with every setting
  and Seerr's API key. Restoring writes both back while Seerr is stopped, and
  refuses a backup from a newer Seerr. Only Seerr's default SQLite database is
  supported, not PostgreSQL.

## [1.6.1-beta] - 2026-10-29

### Fixed

- Plex servers that require secure connections can be backed up: Plex's
  certificate is issued for its `plex.direct` address, which many routers
  refuse to resolve, so Backuparr now connects to the address that name spells
  out itself, still verifying the certificate. **Get Plex token** fills in the
  server's local `plex.direct` address along with the token.
- Plex connection failures now say why, like every other app's: a hostname
  that doesn't resolve, a refused connection, a timeout or a TLS error, instead
  of "could not reach /:/prefs". An `https://` address with an IP explains to
  use the server's `plex.direct` address. The README's Plex note explains that
  Plex usually runs with host networking, so its URL needs the host's address
  rather than the container name.

## [1.6.0-beta] - 2026-10-28

### Added

- Plex backup and restore. A backup holds Plex's own database export, taken
  through its API with the server owner's Plex token (libraries, playlists,
  collections and every user's watched state, progress and ratings), and
  `Preferences.xml` with the server's settings and identity, read from Plex's
  data folder, which is mounted into Backuparr and set as the new **Data
  folder** field. A restore writes both back while Plex is stopped, so a
  rebuilt server comes back as the same server and shared users keep their
  access. A **Get Plex token** button signs in on Plex's website and fills in
  the token (#48). Thanks to @vladimir-aubrecht for the contribution (#55).

## [1.5.0-beta] - 2026-10-28

### Added

- Emby backup and restore. Emby has no backup API, so its data folder (its
  `/config`) is mounted into Backuparr and set as the new **Data folder**
  field; a read-only mount is enough for backups. A backup holds Emby's
  databases (users, watch history, playlists, collections and libraries),
  settings and plugins, without metadata images or cache, and is taken while
  Emby runs. A restore writes them back while Emby is stopped (#49).

## [1.4.0-beta] - 2026-10-28

### Added

- Jellyfin backup and restore (Jellyfin 10.11 or newer), through Jellyfin's own
  backup API: the database (users, watch history, playlists, libraries and
  settings) and config files, without metadata or trickplay images. Jellyfin's
  API can't download a backup, so its backups folder is mounted into Backuparr
  and set as the new **Backups folder** field; a restore copies the archive back
  there and Jellyfin restarts to apply it (#47).

## [1.3.0-beta] - 2026-10-28

### Added

- Service authentication headers in Settings for apps behind an authenticating
  reverse proxy, with encrypted values and a Basic authentication helper for
  standard or custom headers. Each header goes to all apps or only the apps you
  choose. Applied to app connection tests, discovery, backups and restores while
  retaining app API credentials. Cross-origin redirects are blocked for apps
  that receive these headers. Thanks to @vladimir-aubrecht for the
  contribution (#56).

### Changed

- The image now runs on Python 3.15 (`python:3.15-alpine`), and CI runs the
  test suite on Python 3.15 to match. The pytest floor is raised to 9.2.0.

### Fixed

- SABnzbd restore kept only the first entry of list settings such as
  `host_whitelist`, so a restore could lock out access through a reverse proxy.
  List settings are now restored in full.
- Bazarr restore behind a reverse proxy reported a failure (HTTP 502) although
  the restore succeeded. Backuparr also now checks that Bazarr is reachable
  before it starts a restore.

## [1.2.1-beta] - 2026-10-07

### Changed

- Setup and environment problems are now logged as one readable line instead
  of a Python traceback, and the same wording appears in the web UI and
  notifications. Covered: a hostname that doesn't resolve, a refused or
  timed-out connection, a TLS error, a rejected API key or other HTTP error
  (404, 5xx), a URL that answers with a web page instead of the app's API, a
  full disk, a read-only or unwritable folder, a corrupt backup file and an
  unreadable `config.json`. The messages name the host or file, never the
  full URL, so API keys can't end up in the log. Unexpected errors still log
  their full traceback.
- Destination failures from rclone show the cause on its own, with a hint for
  the usual ones: a folder Backuparr can't write to, a full or read-only
  destination, an expired or revoked connection, or a hostname that won't
  resolve.
- Saving settings to a volume that is full or read-only, or opening Backuparr
  with a broken `config.json`, now says what is wrong instead of returning a
  bare server error. The scheduler reports a broken config once instead of
  every 20 seconds.

### Fixed

- Restoring Tautulli's config reported success but never applied it: Tautulli's
  `import_config` call only stages the file, and starting the import is a
  separate request that Backuparr wasn't making. It now makes it. If Tautulli
  has a login enabled, that request can't be made with an API key, so the
  restore says the config is staged and shows the URL to open after logging in
  to Tautulli. See the README's Tautulli restore note.
- Failures reported by the apps themselves no longer repeat the app name
  ("sonarr: sonarr: unauthorized - check the API key").
- A backup work folder that can't be created now fails only that app, instead
  of ending the whole run.

## [1.2.0-beta] - 2026-10-06

### Added

- Dropbox is now a backup destination. Connect it from Settings by pasting
  the token `rclone authorize dropbox` prints (rclone's built-in Dropbox app,
  so no Dropbox app registration); backups go to a `Backuparr` folder in your
  Dropbox. Its token is encrypted at rest like the other destinations'.

## [1.1.1-beta] - 2026-10-06

### Added

- A Backuparr logo button in the bottom-right corner of the web UI opens a
  small menu with **Report a bug** and **Request a feature**, which go
  straight to the matching GitHub issue templates.

## [1.1.0-beta] - 2026-10-06

### Added

- `WEBUI_HOST` sets the address the web UI listens on (default `0.0.0.0`,
  unchanged). With host networking, `127.0.0.1` restricts access to the
  host's loopback interface.
- Optional Prowlarr discovery: a **Discover services** button on the Prowlarr
  card (listed first, followed by Radarr, Sonarr and SABnzbd) fills in the URLs
  and API keys of the Radarr, Sonarr and SABnzbd instances configured in
  Prowlarr. Keys are read from a temporary Prowlarr backup that is deleted
  afterwards. Manual setup is unchanged.

### Security

- Radarr, Sonarr and Prowlarr requests no longer follow a redirect to a
  different host. Requests keeps a custom `X-Api-Key` header across
  redirects, so such a redirect could send the API key (or a backup
  upload) to another host. Redirects to the same host, including an
  http-to-https upgrade or a port change, still work; a downgrade from
  https to http does not. If your app URL currently relies on a cross-host
  redirect, update it to its final address, or set
  `BACKUPARR_ALLOW_CROSS_HOST_REDIRECTS=true` to keep the old behaviour.
- API responses (which include your API keys and backups) are now sent with
  `Cache-Control: no-store`, and every page carries a Content-Security-Policy
  that only allows scripts from Backuparr itself and Google's Drive picker.
- New backup files are private: mode `0600` in `0700` directories, instead
  of readable by every user on the host, since they hold API keys and
  credentials. Files already written are not changed. If something else on
  the host reads your backups as a different user, set `UMASK=022` to keep
  the previous behaviour.

### Changed

- Backup downloads from Radarr, Sonarr and Prowlarr are staged privately
  and checked to be a real ZIP archive before being kept, so a login page
  or a partial download is never stored as a backup. A download that is
  redirected to the app's web login now fails with an actionable message.
- The Docker healthcheck now probes the configured `WEBUI_HOST` (wildcard
  addresses map to loopback, IPv6 included) and connects directly, ignoring
  any outbound HTTP proxy.

### Removed

- The `restore.py` command-line script. Restores are done from the web UI's
  **Restore** tab, which covers every app the script did.

### Fixed

- A failed Tautulli connection or error response wrote its API key into the
  logs: the key is sent in the URL, and the error (and its traceback)
  repeated that URL in `backup.log` and the container output. Tautulli
  errors no longer include it. If an earlier failure may have logged your key,
  rotate it in Tautulli and clear old logs.
- Saving settings or starting a restore with a malformed request body
  (for example `apps` or the restore `override` sent as a list) returned a
  server error and a stack trace in the log; they now return a 400. A
  boolean `retention_days` is rejected, and the restore backup list refuses
  unknown app names (it accepted `..`, which listed the folder above the
  backups).
- History download and delete, and restore, accepted the file names `.` and
  `..`. Downloading `..` made the server copy every app's backups into its
  temporary folder before failing. Names starting with a dot, or ending in a
  newline, are now rejected.
- Profilarr backups were stored as `profilarr_<timestamp>.zip` although
  they are Profilarr's own `.tar.gz`, which Profilarr refuses to import
  under any other name. They are now kept as
  `profilarr_<timestamp>.tar.gz`; earlier backups keep their old names.

## [1.0.7-beta] - 2026-10-04

### Added

- `BACKUPARR_DISABLE_AUTH=true` skips local account setup and login for
  deployments protected by an authenticating reverse proxy such as
  Authelia. Local authentication is enabled by default. In this mode,
  local authentication and forgot-password reset APIs are disabled, and
  the logout button is hidden. Existing admin credentials are preserved.
  A startup warning is logged whenever auth is disabled. Thanks to
  @vladimir-aubrecht for the contribution (#34).

## [1.0.6-beta] - 2026-09-14

### Fixed

- A Google Drive upload failure (e.g. an expired/revoked OAuth token) on
  the Overview page showed as an unreadable wall of text: rclone logs one
  timestamped line per retry attempt (3 by default), each repeating the
  same cause and wrapping a long, unbroken Google API request URL that
  overflowed its app card into the next one. rclone failures are now
  collapsed to their single most useful line, with any embedded request
  URL replaced by `<url>` - the Overview page's failure text also wraps
  within its card as a defense-in-depth measure against future long
  messages.

### Docs

- The Google Drive Setup guide (in-app and README) now has you publish
  the OAuth app right after adding yourself as a test user, and explains
  why: a Google Cloud project left in Testing status has Google
  auto-expire its refresh tokens 7 days after they're issued, which was
  silently breaking the Google Drive connection on a weekly cycle until
  manually reconnected. No Google verification review is needed for
  this, since `drive.file` is a non-sensitive scope and usage stays well
  under the 100-user threshold that would require it.

## [1.0.5-beta] - 2026-09-02

### Security

- The notification `notify_url` setting is now encrypted at rest in
  `config.json`, matching every other credential-bearing field - it can
  embed a Telegram bot token or a Discord/Slack/Gotify webhook secret,
  which were previously stored in plaintext. An existing plaintext value
  is migrated to encrypted automatically on next load, same as the
  original app API key migration.

## [1.0.4-beta] - 2026-09-02

### Fixed

- Corrected the Google Drive Setup guide: no longer suggests restricting
  the API key to your own website (+ `docs.google.com`) - confirmed live
  that this can still fail with a cryptic "The API developer key is
  invalid" error even set up exactly as Google's docs describe, since
  the picker's internal request doesn't reliably carry a referrer.
  Recommends leaving Application restrictions unset instead, relying on
  the existing API restriction (Picker API only) to scope the key.

## [1.0.3-beta] - 2026-09-02

### Fixed

- Fixed Google Drive OAuth still failing with `redirect_uri_mismatch`
  behind a reverse proxy even after 1.0.2-beta's fix. That fix taught
  Flask (via `ProxyFix`) to trust a proxy's `X-Forwarded-Proto` header,
  but missed a layer underneath it: waitress, the actual production
  server, silently discards incoming `X-Forwarded-Proto`/`-For`/`-Host`
  headers unless it's explicitly told which proxy to trust - so Flask
  never saw the header at all. `BACKUPARR_FORCE_HTTPS` now also passes
  `--trusted-proxy`/`--trusted-proxy-headers` to waitress, so the header
  actually reaches the app.

### Changed

- The Docker Hub publish workflow now runs only on a version tag push
  (a formal release), not on every merge to `main` - `:latest` and the
  versioned tag now both update together on release, instead of
  `:latest` tracking every merge independently.

## [1.0.2-beta] - 2026-09-02

### Fixed

- Corrected the Google Drive Setup guide: Google Cloud now requires
  enabling the Google Picker API (separately from the Drive API) and
  requires an API key to have at least one API restriction - the guide
  previously said to leave API restrictions unset, which is no longer
  an option in Google Cloud Console. Also clarified that the
  `drive.file` scope shows up under "Your non-sensitive scopes" in the
  Data access panel, and moved the redirect URI copy box to sit inline
  under the step that asks for it instead of after the whole guide.
- Fixed Google Drive OAuth failing with `redirect_uri_mismatch` when
  Backuparr runs behind a TLS-terminating reverse proxy. The redirect
  URI was built from the raw request Backuparr receives (`http://`,
  since the proxy talks to it in plain HTTP), not the `https://` URL
  the browser and Google actually used. `BACKUPARR_FORCE_HTTPS` now
  also makes Backuparr trust that proxy's `X-Forwarded-Proto` (and
  `X-Forwarded-For`, fixing login-lockout tracking seeing the proxy's
  IP instead of the real client's) via Werkzeug's `ProxyFix`.

## [1.0.1-beta] - 2026-09-02

### Fixed

- Fixed the Google Drive "Choose folder" picker showing a blank white
  dialog instead of a folder list. Google's Picker API requires an API
  key (`setDeveloperKey`) and a Cloud project ID (`setAppId`) in
  addition to the OAuth token already in use - without them the picker
  fails silently inside its own iframe. Added an API key field to the
  Google Drive destination (encrypted at rest, like the client secret)
  and updated the in-app Setup guide and README with the extra step.

## [1.0.0-beta] - 2026-09-01

### Added

- Added a GitHub Actions CI/CD pipeline: every PR is gated on the test
  suite passing and a clean multi-arch (`linux/amd64`/`linux/arm64`)
  Docker build; merges to `main` and version tags automatically publish
  to Docker Hub, with tag pushes also creating a GitHub Release with
  notes pulled from this changelog.
- Added a pytest suite (24 tests) covering the security-critical code
  paths: the path-traversal filename allowlist, zip-slip containment,
  secrets encrypt/decrypt round-trip, config load/save round-trip, and
  rclone secret redaction.
- Added Dependabot configuration for pip, Docker, and GitHub Actions
  updates, and enabled its automated security-fix PRs.
- Published the project to Docker Hub (`rsaturns/backuparr`, both
  `linux/amd64` and `linux/arm64`) with a description, overview, and
  category, and documented deploying the published image in the README.
- Added SECURITY.md (private vulnerability reporting) and a CODEOWNERS
  file.

### Changed

- rclone now installs from its own official GitHub release
  (checksum-verified against its published `SHA256SUMS`) instead of
  Alpine's `apk` package, which lagged upstream and carried 53 CVEs
  baked into its bundled Go dependencies - cut the full image's
  vulnerability count from 57 to 15.
- Bumped the Docker base image from `python:3.12-alpine` to
  `python:3.14-alpine`, every Python dependency floor (croniter,
  cryptography, waitress, argon2-cffi, requests) past known CVEs, and
  every pinned GitHub Action past GitHub's Node 20 deprecation.

### Security

- Made the GitHub repository public, protected by a repository ruleset
  on `main`: pull requests required, one approval from a CODEOWNERS
  reviewer, the CI check must pass and be up to date with `main`, and
  no force-pushes or branch deletion.
- Enabled secret scanning and push protection.

## [0.9.0-beta] - 2026-09-01

### Added

- Added a footer version check against GitHub's latest release, showing
  an "Update Available" indicator when a newer version is out.
- Added a README table of contents linking every section, and completed
  the Environment variables reference to cover every env var the code
  actually reads (adding `TZ`, `LOG_LEVEL`, and five previously-
  undocumented file-location overrides), plus an update to the Restoring
  section for recent restore-tab changes.

### Changed

- Updated the tagline from "Secure your Arrs" to "Backup your Arrs" to
  more accurately describe the tool.
- Deduplicated repeated logic across the codebase: a shared parameterized
  helper for `enabled_apps()`/`enabled_destinations()`, shared lock/status/
  thread machinery for backup and restore runs, a shared JS pattern for
  test buttons and status polling, a single app-name-to-restore-action
  dispatch used by both the CLI and the web UI, and consolidated repeated
  Bazarr/Tautulli/SABnzbd explanatory comments and constants.
- Fixed a redundant per-app retention loop (now one `rclone` delete call
  per destination, which also fixes disabled apps' old backups never
  being pruned) and a couple of other minor inefficiencies (an
  unnecessary `rclone config dump` call, copying instead of moving a
  backup file about to be deleted anyway).

### Fixed

- Fixed the Overview page's empty-state message wrapping oddly in a wide
  window, and tightened the setup screen's intro text into two shorter
  paragraphs.

### Security

- Stopped secrets and API keys from leaking through error messages and
  cookies: redacted client secrets/tokens/passwords out of rclone error
  text surfaced in the UI and logs, sanitized Bazarr/Profilarr backup
  filenames before using them in a local path join, stopped Tautulli's
  raw HTTP error (which embedded the API key in its URL) from escaping,
  added an opt-in `BACKUPARR_FORCE_HTTPS` env var for secure session
  cookies, and bumped the Flask/waitress version floors past known CVEs.

## [0.1.0-alpha] - 2026-08-24

### Added

- Initial release: scheduled config/database backups for Radarr, Sonarr,
  Bazarr, Prowlarr, Tdarr, and SABnzbd via each app's own API, uploaded to
  Google Drive via rclone, configured entirely from a Flask + waitress
  web UI with a CLI restore fallback for disaster recovery.
- Added Profilarr (backup-only - Profilarr sanitizes secrets out of its
  own backups and has no restore API) and Tautulli (backup + restore) as
  backed-up services, each with an architecture-diagram entry and a
  README note on its own quirks.
- Added a multi-destination backend, replacing the single rclone remote:
  Local storage and Google Drive (in-app OAuth, `drive.file` scope) at
  first, then OneDrive (via rclone's own built-in OAuth app, personal
  accounts only, after Microsoft's app-registration restrictions ruled
  out a Backuparr-hosted Azure flow) - each with its own Settings card,
  setup guide modal, and Google Picker / `rclone authorize` connection
  flow. Dropbox and a Seerr app card are shown as "coming soon"
  placeholders for visibility.
- Added an Overview landing page (enabled services, destinations,
  schedule, retention, and a per-app status card with a download-latest
  link), a light/dark theme toggle, an unsaved-changes guard on Settings,
  and delete/filter/download actions on History rows.
- Redesigned Settings into collapsible per-app cards with icons and a
  single sticky save toolbar; added a Daily/Weekly/Every-N-hours schedule
  picker, with raw cron syntax kept behind an "Advanced" toggle.
- Added a version footer, backed by a new top-level `VERSION` file.
- Added a live progress bar, spinner, and cancel button to backup runs,
  plus a restore-target override and live per-step progress on the
  Restore tab.
- Added notification support for Discord, Slack, Telegram, and Gotify
  webhook shapes in `notify_url` (alongside the existing ntfy.sh/generic
  case), a Test button for it, and per-app checklist-formatted
  run-completion messages.
- Added an AGPL-3.0 LICENSE, a README architecture diagram, and bug
  report / feature request issue templates.
- Added a Dockerfile `HEALTHCHECK` and a `.dockerignore`.

### Changed

- Rebranded the project from arr-backup to Backuparr across the UI,
  Docker assets, and internal env vars/volume paths.
- Removed the `WEBUI_USERNAME`/`WEBUI_PASSWORD` Basic Auth and
  `RUN_ON_START` env vars, both superseded by the new login system and
  the Run tab's manual trigger.
- Lowered the default backup retention from 14 to 7 days.
- Sped up Overview/History loading by listing each destination with one
  recursive `rclone lsjson` call instead of one call per app.
- Replaced raw `requests` exception text with clean, human-readable
  connection-failure messages across Test Connection, backup runs, and
  (in a later pass) the remaining restore and OAuth routes.
- Polished the UI: increased the base font size and scaled the whole
  stylesheet proportionally, aligned and bolded the Overview stat labels,
  centered the Destinations icon row, and fixed Run/History cards
  shrinking to their own content width instead of filling the page.
- Trimmed verbose, narrative-style code comments and README rationale
  down to concise reference content ahead of public posting.

### Fixed

- Fixed `rclone_util.lsf()` returning a 500 instead of an empty list for
  apps with no backups yet.
- Fixed scheduled backups never actually running - `crond` was started in
  a way that left it as a permanent zombie process - then replaced
  cron-based scheduling entirely with an in-process scheduler thread
  after finding cron also wouldn't hot-reload a schedule changed at
  runtime.
- Fixed Zip-Slip protection in restore extraction that had silently never
  worked (`zipfile.extractall` has no `filter=` parameter, unlike
  `tarfile`), breaking every Tdarr/SABnzbd/Tautulli restore until
  replaced with manual member-path validation.
- Fixed a batch of restore bugs found while exercising each connector
  end-to-end: Radarr/Sonarr/Prowlarr restores silently never applying
  (the required app restart was never triggered), SABnzbd restoring
  against the wrong API path and self-locking-out by overwriting its own
  API key mid-restore, Tautulli's database-import restore being
  unreachable due to an upstream Tautulli API bug (now handled as
  best-effort), and Bazarr restore failing on an incompatible filename, a
  truncated-download race, and a false failure on its
  restart-drops-the-connection response.
- Fixed a logging bug where per-app connector warnings never reached the
  Run & Status log or `backup.log`.
- Fixed assorted stale README claims and setup-screen instructions that
  had drifted from the current code (OneDrive's actual connection flow,
  the old "delete auth.json" recovery hint, encryption-at-rest coverage),
  and a broken link in the OneDrive setup guide.

### Security

- Required login by default via a first-run admin setup screen and
  session-based auth, replacing opt-in HTTP Basic Auth; later switched
  password hashing from scrypt to Argon2id, and replaced the "delete
  auth.json" recovery instructions with an in-app Reset flow that clears
  all local secrets and backups.
- Encrypted secrets at rest: app API keys and other secrets in
  `config.json` via Fernet, and `rclone.conf`'s mirrored OAuth tokens/
  client secrets via rclone's own built-in config encryption.
- Fixed a path-traversal vulnerability in restore file selection and a
  Zip-Slip vulnerability in zip extraction.
- Added login rate limiting (exponential backoff after repeated failures)
  and basic security headers (`X-Frame-Options`, `X-Content-Type-Options`,
  `Referrer-Policy`).
- Switched the container to run as a non-root user, resolved via
  `PUID`/`PGID` at startup.
- Documented that `/api/reset` is intentionally reachable without a
  session (it's the account-recovery path), and removed dead code
  surfaced during the security review.
