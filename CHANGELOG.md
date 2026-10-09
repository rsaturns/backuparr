# Changelog

All notable changes to this project are documented in this file. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Plex native database backups preserving all users' local watched/progress data, plus API server/library settings, metadata and artwork, with manual restore guidance.
- A **Get Plex token** button signs in through Plex's own website and fills the token for a server you own, with a server selector when needed.
- An optional Plex restore agent restores the library database through Backuparr's Restore tab. It works with the existing Plex Docker image, validates the container/data mount and backup version, preserves rollback copies and recovers interrupted restores. Includes compose setup and explicit restore limitations.

### Fixed

- Plex database exports named `databaseBackup.db`, including names with an appended UUID, are now accepted alongside the on-disk library filename. Restore instructions explain how to rename the downloaded snapshot.
- Missing Plex artwork (HTTP 404) is recorded in the backup manifest instead of discarding the database backup. Other download failures still fail the backup.
- Plex's own same-origin redirects are followed without forwarding the token to other hosts, ports or protocols. External artwork redirects are recorded as unavailable; other redirect failures identify the requested endpoint.

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
