# Changelog

All notable changes to this project are documented in this file. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versions before `0.9.0-beta` were never tagged in git or published as
GitHub Releases - they're reconstructed here from commit history purely
for readability, not presented as formal releases. Version headers below
are dated to when the `VERSION` file was set to that value, not to a
release event.

## [Unreleased]

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
