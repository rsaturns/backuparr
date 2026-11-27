"""Back up a self-hosted GitLab (Linux package and Docker installs).

GitLab has no backup API. It makes its own backup archive on the server
with ``gitlab-backup create`` (Docker: ``docker exec -t <container>
gitlab-backup create``), usually from cron with ``CRON=1``, plus a config
tar with ``gitlab-ctl backup-etc``. Backuparr takes the newest complete
archive and the newest config tar from GitLab's backups folder, which is
mounted read-only into this container.

What GitLab's docs and source say (read 2026-11-25, docs for GitLab 19.6):
- No backup API.
- One ``<backup-id>_gitlab_backup.tar`` holds the database, repositories
  and LFS, uploads, CI/CD artifacts and job logs, packages and container
  registry, wikis, Pages, Terraform state and snippets. Not included: data
  in object storage, Redis, server and file hooks. The configuration and
  secrets are not in it either: "The backup Rake task GitLab provides does
  not store your configuration files."
- The backup ID is ``<timestamp>_<YYYY_MM_DD>_<version>-<edition>``, for
  example ``1493107454_2018_04_25_10.6.4-ce_gitlab_backup.tar``. A custom
  ``BACKUP=`` name drops the version from the file name.
- ``backup_information.yml`` inside the archive records the version and any
  skipped parts (``SKIP=``).
- Archives are ``git:git``, mode ``0600``, by default; setting
  ``gitlab_rails['backup_archive_permissions'] = 0644`` changes that. Old
  archives are pruned by ``backup_keep_time``.
- "At the very minimum" ``/etc/gitlab/gitlab-secrets.json`` (the database
  encryption key: without it, 2FA and CI/CD variables are lost) and
  ``/etc/gitlab/gitlab.rb``; for Docker, the config volume
  (``$GITLAB_HOME/config``). ``gitlab-ctl backup-etc`` makes
  ``gitlab_config_<timestamp>_<date>.tar`` of ``/etc/gitlab``, readable only
  by root; ``--backup-path`` puts it elsewhere.
- Restore: only to "exactly the same version and type (CE/EE)", on a
  working install, with ``puma`` and ``sidekiq`` stopped, then
  ``gitlab-backup restore BACKUP=<backup-id>``, with the secrets file put
  back.
- Version over the API: ``GET /api/v4/metadata`` (or the older
  ``/api/v4/version``) needs a login. A personal access token with only
  ``read_user`` is enough.

Each run takes the newest complete ``*_gitlab_backup.tar`` and the newest
``config/gitlab_config_*.tar``. An archive changed in the last few minutes
is skipped for the one before it (it is still being written), and staging
files (``db/``, ``*.tar.gz``, ``backup_information.yml`` in the folder) are
not archives. The backup holds the two tars, in the same layout as the
backups folder, so the restore is GitLab's own steps. The run log warns
when the newest archive is more than about 2 days old, when
``backup_information.yml`` lists skipped parts, or when there is no config
tar. The version for the manifest comes from the backup ID, else from
``backup_information.yml``. The token goes only to the same host
(``SameHostSession``, as it is sent in a custom header). The backup is
always encrypted: the config tar holds the database encryption key.

Not covered: Helm chart and Operator installs, self-compiled installs, data
in object storage, the Linux package's SSH host key (``/etc/ssh``), and
GitLab.com.
"""

import logging
import os
import re
import shutil
import tarfile
import time
import zipfile

import requests
from apps import mounted
from apps.base import AppError
from apps.servarr import SameHostSession

logger = logging.getLogger(f"backuparr.{__name__}")


class GitLabError(AppError):
    """A GitLab backup error, with a message the user can act on."""


# The newest backup archive, and the newest config tar, are picked from the
# mounted backups folder. An archive changed in the last few minutes is
# still being written, so it is skipped for the one before it.
_ARCHIVE = re.compile(r"^(.+)_gitlab_backup\.tar$")
_CONFIG = re.compile(r"^gitlab_config_.+\.tar$")
_CONFIG_DIR = "config"
_BACKUP_INFO = "backup_information.yml"
# A backup ID is <timestamp>_<YYYY_MM_DD>_<version>-<edition>; the version
# (with the edition, for example 10.6.4-ce) is the last part. A custom
# BACKUP= name has no version, so the version then comes from
# backup_information.yml.
_BACKUP_ID = re.compile(r"^\d+_\d{4}_\d{2}_\d{2}_(.+)$")
_STALE_SECONDS = 2 * 24 * 3600
_RECENT_SECONDS = 5 * 60
_MAX_INFO = 1024 * 1024
_VERSION = re.compile(r"^gitlab_version:\s*[\"']?([^\"'\s]+)[\"']?\s*$", re.MULTILINE)


def _days(seconds):
    """Whole days in seconds, for a log line."""
    return int(seconds // 86400)


def _age(path):
    """Seconds since path was last changed."""
    return time.time() - os.path.getmtime(path)


def archive_version(filename):
    """The version in a backup archive's file name, or None.

    The backup ID is <timestamp>_<YYYY_MM_DD>_<version>-<edition>; a custom
    BACKUP= name carries no version.
    """
    match = _ARCHIVE.match(filename)
    if not match:
        return None
    version = _BACKUP_ID.match(match.group(1))
    return version.group(1) if version else None


def _parse_backup_information(text):
    """The (version, skipped parts) in a backup_information.yml.

    The file is small YAML; this reads only the two keys Backuparr needs,
    so it does not need a YAML library. A missing key comes back as None,
    or an empty list.
    """
    version = _VERSION.search(text)
    skipped = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("skipped:"):
            continue
        rest = stripped.partition(":")[2].strip()
        if rest == "[]":
            break
        if rest:
            # An inline list: skipped: [db, uploads], or a single value.
            for part in rest.strip("[]").split(","):
                part = part.strip().strip("\"'")
                if part:
                    skipped.append(part)
            break
        # A block list: the parts follow as "- <part>" lines.
        for following in lines[index + 1:]:
            item = following.strip()
            if item.startswith("- "):
                skipped.append(item[2:].strip().strip("\"'"))
            elif item == "":
                continue
            else:
                break
        break
    return (version.group(1) if version else None, skipped)


def _read_backup_information(archive_path):
    """The (version, skipped parts) in an archive's backup_information.yml.

    Returns (None, []) when the file is missing or unreadable; the caller
    warns, it does not fail the backup.
    """
    try:
        with tarfile.open(archive_path, mode="r|*") as tar:
            for member in tar:
                if member.name == _BACKUP_INFO:
                    info = tar.extractfile(member)
                    if info is None:
                        return (None, [])
                    text = info.read(_MAX_INFO).decode("utf-8", "replace")
                    return _parse_backup_information(text)
            return (None, [])
    except (OSError, tarfile.TarError, EOFError):
        return (None, [])


def _is_tar(path):
    """Whether path opens as a tar archive."""
    try:
        return tarfile.is_tarfile(path)
    except OSError:
        return False


def check_backup(path):
    """archive_check's check: raises GitLabError unless path is a sound
    GitLab backup (a zip holding a backup archive and a config tar)."""
    if not zipfile.is_zipfile(path):
        raise GitLabError("gitlab: it isn't a zip archive")
    with zipfile.ZipFile(path) as archive:
        bad = archive.testzip()
        if bad:
            raise GitLabError(f"gitlab: {bad} in it is damaged")
        names = [info.filename for info in archive.infolist() if not info.is_dir()]
        archives = [name for name in names if _ARCHIVE.match(os.path.basename(name))]
        configs = [
            name
            for name in names
            if name.startswith(_CONFIG_DIR + "/")
            and _CONFIG.match(os.path.basename(name))
        ]
        if not archives:
            raise GitLabError("gitlab: no *_gitlab_backup.tar in it")
        if not configs:
            raise GitLabError("gitlab: no config/gitlab_config_*.tar in it")
        for name in archives + configs:
            with archive.open(name) as member:
                try:
                    with tarfile.open(fileobj=member, mode="r|*") as tar:
                        if next(tar, None) is None:
                            raise GitLabError(f"gitlab: {name} in it is empty")
                except tarfile.TarError as exc:
                    raise GitLabError(
                        f"gitlab: {name} in it is damaged ({exc})"
                    ) from None


class GitLabApp:
    """One self-hosted GitLab, backed up from its mounted backups folder."""

    def __init__(self, url, api_key, data_dir=None, timeout=30):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.data_dir = data_dir or None
        self.timeout = timeout
        self.session = SameHostSession(self.url)
        self.session.headers.update(
            {"PRIVATE-TOKEN": api_key, "Accept": "application/json"}
        )

    def _get(self, path):
        """A GET that raises a GitLabError the user can act on."""
        try:
            response = self.session.get(f"{self.url}{path}", timeout=self.timeout)
        except requests.RequestException as exc:
            raise GitLabError(f"gitlab: couldn't reach {path} ({exc})") from None
        if response.status_code == 401:
            raise GitLabError(
                "gitlab: unauthorized - check the API token "
                "(it needs only read_user)"
            )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise GitLabError(f"gitlab: {path} failed ({exc})") from None
        return response

    def _live_version(self):
        """The running GitLab's version, from /api/v4/metadata."""
        response = self._get("/api/v4/metadata")
        try:
            data = response.json()
        except ValueError:
            raise GitLabError(
                "gitlab: /api/v4/metadata didn't return JSON"
            ) from None
        version = data.get("version") if isinstance(data, dict) else None
        if not version:
            raise GitLabError("gitlab: /api/v4/metadata didn't return a version")
        return str(version)

    def _require_folder(self):
        """The mounted backups folder, or a GitLabError saying how to set it."""
        mounted.require_dir(
            self.data_dir,
            GitLabError,
            "gitlab",
            "backups folder",
            "GitLab's backups folder",
            "mount GitLab's backups folder (/var/opt/gitlab/backups, or "
            "$GITLAB_HOME/data/backups in Docker) read-only",
        )
        return self.data_dir

    def _newest_archive(self):
        """The newest complete backup archive: (name, path, backup id).

        An archive changed in the last few minutes is still being written,
        so it is skipped for the one before it. Staging files (db/,
        *.tar.gz, backup_information.yml in the folder) are not archives.
        """
        folder = self._require_folder()
        try:
            names = os.listdir(folder)
        except OSError as exc:
            raise GitLabError(f"gitlab: couldn't list {folder} ({exc})") from None
        candidates = []
        for name in names:
            path = os.path.join(folder, name)
            if _ARCHIVE.match(name) and os.path.isfile(path):
                candidates.append((os.path.getmtime(path), name, path))
        candidates.sort(reverse=True)
        for mtime, name, path in candidates:
            if time.time() - mtime < _RECENT_SECONDS:
                logger.warning(
                    "gitlab: skipping %s - it changed %d seconds ago, "
                    "so it is still being written",
                    name,
                    int(time.time() - mtime),
                )
                continue
            if not _is_tar(path):
                logger.warning("gitlab: skipping %s - it isn't a tar archive", name)
                continue
            return (name, path, _ARCHIVE.match(name).group(1))
        raise GitLabError(
            "gitlab: no complete *_gitlab_backup.tar in the backups folder - "
            "GitLab makes one with gitlab-backup create"
        )

    def _newest_config(self):
        """The newest config tar: (name, path), or (None, None)."""
        folder = self._require_folder()
        config_dir = os.path.join(folder, _CONFIG_DIR)
        if not os.path.isdir(config_dir):
            return (None, None)
        try:
            names = os.listdir(config_dir)
        except OSError as exc:
            raise GitLabError(
                f"gitlab: couldn't list {config_dir} ({exc})"
            ) from None
        candidates = []
        for name in names:
            path = os.path.join(config_dir, name)
            if _CONFIG.match(name) and os.path.isfile(path) and _is_tar(path):
                candidates.append((os.path.getmtime(path), name, path))
        if not candidates:
            return (None, None)
        candidates.sort(reverse=True)
        return (candidates[0][1], candidates[0][2])

    def _newest(self):
        """The newest backup: (archive name, archive path, config name,
        config path, version, skipped parts).

        The version comes from the backup ID, else from
        backup_information.yml.
        """
        archive_name, archive_path, _backup_id = self._newest_archive()
        config_name, config_path = self._newest_config()
        version = archive_version(archive_name)
        info_version, skipped = _read_backup_information(archive_path)
        if version is None:
            version = info_version
        return (
            archive_name,
            archive_path,
            config_name,
            config_path,
            version,
            skipped,
        )

    def app_version(self):
        """The newest backup's version: from its backup ID, else from
        backup_information.yml."""
        _archive_name, _archive_path, _config_name, _config_path, version, _skipped = (
            self._newest()
        )
        if not version:
            raise GitLabError(
                "gitlab: couldn't read the version from the newest backup - "
                "its file name has no version and backup_information.yml "
                "doesn't list one"
            )
        return version

    def test_connection(self):
        """The live version, compared with the newest archive."""
        live = self._live_version()
        archive_name, archive_path, config_name, _config_path, version, skipped = (
            self._newest()
        )
        if config_name is None:
            logger.warning(
                "gitlab: no config tar in %s - run gitlab-ctl backup-etc",
                os.path.join(self.data_dir, _CONFIG_DIR),
            )
        if skipped:
            logger.warning(
                "gitlab: the newest backup skipped %s", ", ".join(skipped)
            )
        age = _age(archive_path)
        if age > _STALE_SECONDS:
            logger.warning("gitlab: the newest backup is %d days old", _days(age))
        if version and version != live:
            logger.warning(
                "gitlab: the newest backup is from %s, but GitLab is %s - "
                "a restore needs the backup's version",
                version,
                live,
            )
        return f"gitlab {live} reachable, newest backup {archive_name}" + (
            f" ({version})" if version else ""
        )

    def _copy_in(self, src, dst):
        """Copy src to dst, refusing a link that points outside the
        backups folder."""
        real_src = os.path.realpath(src)
        real_root = os.path.realpath(self.data_dir)
        try:
            inside = os.path.commonpath([real_src, real_root]) == real_root
        except ValueError:
            inside = False
        if not inside:
            raise GitLabError(
                f"gitlab: {src} points outside the backups folder"
            )
        if not os.path.isfile(real_src):
            raise GitLabError(f"gitlab: {src} isn't a file")
        shutil.copyfile(real_src, dst)

    def backup(self, dest_dir):
        """Copy the newest backup archive and config tar into dest_dir.

        Returns dest_dir (a folder); run_backup zips it. The two tars keep
        the backups folder's layout, so the restore is GitLab's own steps.
        """
        archive_name, archive_path, config_name, config_path, version, skipped = (
            self._newest()
        )
        os.makedirs(dest_dir, exist_ok=True)
        self._copy_in(archive_path, os.path.join(dest_dir, archive_name))
        if config_name is not None:
            config_dest = os.path.join(dest_dir, _CONFIG_DIR)
            os.makedirs(config_dest, exist_ok=True)
            self._copy_in(config_path, os.path.join(config_dest, config_name))
        else:
            logger.warning(
                "gitlab: no config tar - run gitlab-ctl backup-etc so the "
                "secrets and settings are kept"
            )
        if skipped:
            logger.warning(
                "gitlab: the newest backup skipped %s", ", ".join(skipped)
            )
        age = _age(archive_path)
        if age > _STALE_SECONDS:
            logger.warning("gitlab: the newest backup is %d days old", _days(age))
        logger.info(
            "gitlab: backing up %s%s",
            archive_name,
            f" and {config_name}" if config_name else "",
        )
        if version:
            logger.info("gitlab: newest backup is version %s", version)
        return dest_dir
