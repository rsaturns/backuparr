"""Durable stop / snapshot / replace / start transaction for one Plex container."""
import fcntl
import json
import logging
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import threading
import time
import xml.etree.ElementTree as ET

import requests

from plex_restore_agent.archive import stage_database
from plex_restore_agent.docker import AgentError

DATABASE = "com.plexapp.plugins.library.db"
BLOBS = "com.plexapp.plugins.library.blobs.db"
DATABASE_FILES = tuple(name + suffix for name in (DATABASE, BLOBS) for suffix in ("", "-wal", "-shm"))
TERMINAL = {"complete", "failed", "rolled_back", "recovery_failed"}
SETTLED = TERMINAL - {"recovery_failed"}
logger = logging.getLogger(__name__)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as out:
        os.chmod(temporary, 0o600)
        json.dump(value, out)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def real_directory(path):
    path = Path(path).absolute()
    if not path.is_dir() or path.resolve() != path:
        raise AgentError("Agent directories must exist and must not contain symlinks")
    return path


def regular_file(path, *, required=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if required:
            raise AgentError("Plex library database is missing; initialize the server first") from None
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise AgentError("Refusing a symlink, hard link or non-regular database file")
    return info


def mount_source(container, path):
    """Resolve a path through the longest matching Docker volume/bind mount."""
    path = PurePosixPath(path)
    if not path.is_absolute() or ".." in path.parts:
        raise AgentError("Container database directory must be an absolute path without '..'")
    mounts = sorted(container.get("Mounts", []), key=lambda m: len(m["Destination"]), reverse=True)
    for mount in mounts:
        destination = PurePosixPath(mount["Destination"])
        if destination != path and destination.is_relative_to(path):
            raise AgentError("Nested mounts inside the database/state directory are not supported")
        if path.is_relative_to(destination):
            if mount.get("Type") not in ("bind", "volume") or not mount.get("RW"):
                raise AgentError("Plex databases must be on a writable bind mount or named volume")
            return str(PurePosixPath(mount["Source"]) / path.relative_to(destination))
    raise AgentError("Database directory is not covered by a Docker bind mount or named volume")


class RestoreManager:
    def __init__(self, docker, *, container, agent_container, database_dir,
                 container_database_dir, state_dir, plex_url, max_bytes, health_timeout=180):
        self.docker = docker
        self.container = container
        self.agent_container = agent_container
        self.database_dir = real_directory(database_dir)
        self.container_database_dir = container_database_dir
        self.state_dir = real_directory(state_dir)
        if self.state_dir.is_relative_to(self.database_dir) or self.database_dir.is_relative_to(self.state_dir):
            raise AgentError("Agent state and Plex database directories must be separate")
        self.plex_url = plex_url.rstrip("/")
        self.max_bytes = max_bytes
        self.health_timeout = health_timeout
        self.lock = threading.Lock()
        self.active_job = None
        self.journal_failed = False
        self.file_locks = []
        # Also prevents two differently configured agents from editing one DB.
        for folder in (self.state_dir, self.database_dir):
            fd = os.open(folder / ".backuparr-restore.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                self.close()
                raise AgentError("Another Plex restore agent owns this state/database directory") from None
            self.file_locks.append(fd)

    def close(self):
        for fd in self.file_locks:
            os.close(fd)
        self.file_locks.clear()

    def read_job(self, job_id):
        path = self.state_dir / job_id / "job.json"
        try:
            job = json.loads(path.read_text())
            if not isinstance(job, dict) or job.get("id") != job_id or not isinstance(job.get("phase"), str):
                raise ValueError("Invalid job journal")
            return job
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            self.journal_failed = True
            raise AgentError("Cannot read restore journal; repair agent storage before retrying recovery") from None

    def save(self, job, phase=None, **fields):
        saved = dict(job, **fields)
        if phase:
            saved["phase"] = phase
        try:
            write_json(self.state_dir / job["id"] / "job.json", saved)
        except OSError:
            # The failure marker may be impossible to persist as well. Keep
            # this latch until startup recovery has reconciled the journal.
            self.journal_failed = True
            raise
        job.update(saved)

    def fail_job(self, job, phase, error):
        try:
            self.save(job, phase, error=error)
        except OSError:
            logger.error("Cannot record restore failure; new restores are blocked. Repair storage and restart the agent.")

    def validate_target(self, target_id=None):
        target = self.docker.inspect(target_id or self.container)
        agent = self.docker.inspect(self.agent_container)
        labels = target.get("Config", {}).get("Labels") or {}
        if target["Id"] == agent["Id"] or labels.get("io.backuparr.plex-restore") != "true":
            raise AgentError("Target must be a separate container labelled io.backuparr.plex-restore=true")
        if "com.docker.swarm.service.id" in labels or target.get("HostConfig", {}).get("AutoRemove"):
            raise AgentError("Swarm tasks and auto-remove containers are not supported")
        target_source = mount_source(target, self.container_database_dir)
        if target_source != mount_source(agent, str(self.database_dir)):
            raise AgentError("Agent database mount does not match the selected Plex container's database directory")
        state_source = PurePosixPath(mount_source(agent, str(self.state_dir)))
        if state_source.is_relative_to(target_source) or PurePosixPath(target_source).is_relative_to(state_source):
            raise AgentError("Agent state must use separate persistent storage from the database directory")
        for name in DATABASE_FILES:
            regular_file(self.database_dir / name, required=(name == DATABASE))
        return target

    def plex_identity(self, token=None, *, check_library=False):
        headers = {"X-Plex-Token": token} if token else {}
        try:
            with requests.get(self.plex_url + "/identity", headers=headers,
                              timeout=(5, 10), allow_redirects=False) as response:
                if response.status_code != 200:
                    raise AgentError("Plex identity check failed; configure a direct Plex URL in the agent")
                root = ET.fromstring(response.content)
            identity = {"version": root.get("version"), "machine_identifier": root.get("machineIdentifier")}
            if root.tag != "MediaContainer" or not all(identity.values()):
                raise AgentError("Agent URL did not return a Plex server identity")
            # The mounted Preferences.xml ties the HTTP endpoint to this volume.
            prefs = self.database_dir.parent.parent / "Preferences.xml"
            regular_file(prefs, required=True)
            preferences = ET.parse(prefs).getroot()
            # PMS advertises the processed ID in /identity; the native UUID is
            # a different value on current Linux images.
            configured_id = (preferences.get("ProcessedMachineIdentifier")
                             or preferences.get("MachineIdentifier"))
            if configured_id != identity["machine_identifier"]:
                raise AgentError("Agent Plex URL and mounted data directory belong to different servers")
            if check_library:
                with requests.get(self.plex_url + "/library/sections", headers=headers,
                                  timeout=(5, 10), allow_redirects=False) as response:
                    if response.status_code != 200 or ET.fromstring(response.content).tag != "MediaContainer":
                        raise AgentError("Plex library API is not ready or the Plex token was refused")
            return identity
        except (requests.RequestException, ET.ParseError, OSError):
            raise AgentError("Could not verify Plex readiness and the mounted server identity") from None

    def status(self):
        target = self.validate_target()
        return {"api_version": 1, "container": target.get("Name", "").lstrip("/"),
                "running": bool(target["State"].get("Running")),
                "server": self.plex_identity(), "scope": "library_database"}

    def wait_healthy(self, job, token=None):
        deadline = time.monotonic() + self.health_timeout
        while True:
            try:
                target = self.docker.inspect(job["container_id"])
                if target["State"].get("Running") and self.plex_identity(
                        token, check_library=bool(token)) == job["server"]:
                    return
            except AgentError:
                pass
            if time.monotonic() >= deadline:
                raise AgentError("Plex did not become ready after restart")
            time.sleep(2)

    def copy_file(self, source, destination, metadata=None):
        """Files are fsynced before any journal entry can depend on them."""
        regular_file(source, required=True)
        with source.open("rb") as src, destination.open("xb") as out:
            os.chmod(destination, 0o600)
            shutil.copyfileobj(src, out, length=1024 * 1024)
            if metadata:
                os.fchown(out.fileno(), metadata["uid"], metadata["gid"])
                os.fchmod(out.fileno(), metadata["mode"])
            out.flush()
            os.fsync(out.fileno())

    def snapshot(self, job):
        directory = self.state_dir / job["id"] / "rollback"
        directory.mkdir(mode=0o700, exist_ok=True)
        files = {}
        for name in DATABASE_FILES:
            self.docker.assert_stopped(job["container_id"])
            info = regular_file(self.database_dir / name, required=(name == DATABASE))
            if info:
                self.copy_file(self.database_dir / name, directory / name)
                files[name] = {"uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode)}
        sync_directory(directory)
        self.save(job, "snapshot_ready", original_files=files)

    def replace(self, job, source, name, metadata):
        self.docker.assert_stopped(job["container_id"])
        target = self.database_dir / name
        regular_file(target)
        temporary = self.database_dir / (".backuparr-" + job["id"] + "-" + name)
        # A previous crash may have left an incomplete temporary copy.
        regular_file(temporary)
        temporary.unlink(missing_ok=True)
        self.copy_file(source, temporary, metadata)
        self.docker.assert_stopped(job["container_id"])
        os.replace(temporary, target)
        sync_directory(self.database_dir)

    def remove(self, job, name):
        self.docker.assert_stopped(job["container_id"])
        path = self.database_dir / name
        regular_file(path)
        path.unlink(missing_ok=True)
        sync_directory(self.database_dir)

    def rollback(self, job, token=None, cause=None):
        self.validate_target(job["container_id"])
        # A failed write may have filled the disk with an incomplete staging file.
        self.cleanup_staging(job)
        self.save(job, "rolling_back", **({"cause": cause} if cause else {}))
        self.docker.restart_policy(job["container_id"], {"Name": "no", "MaximumRetryCount": 0})
        self.docker.stop(job["container_id"])
        # Before snapshot_ready no database file has been changed.
        if "original_files" in job:
            directory = self.state_dir / job["id"] / "rollback"
            for name in DATABASE_FILES:
                if name in job["original_files"]:
                    self.replace(job, directory / name, name, job["original_files"][name])
                else:
                    self.remove(job, name)
        self.docker.start(job["container_id"])
        self.wait_healthy(job, token)
        self.docker.restart_policy(job["container_id"], job["restart_policy"])
        self.save(job, "rolled_back", error="Restore failed or was interrupted; the original databases were recovered and Plex restarted")

    def cleanup(self, job):
        for name in ("upload.zip", "incoming.db", "databases.zip"):
            (self.state_dir / job["id"] / name).unlink(missing_ok=True)
        self.cleanup_staging(job)

    def cleanup_staging(self, job):
        for name in DATABASE_FILES:
            path = self.database_dir / (".backuparr-" + job["id"] + "-" + name)
            regular_file(path)
            path.unlink(missing_ok=True)

    def execute(self, job, token, expected_identity):
        """Caller owns the operation lock; no request can change the target."""
        self.active_job = job["id"]
        try:
            self.save(job, "validating")
            directory = self.state_dir / job["id"]
            identity = stage_database(directory / "upload.zip", directory, self.max_bytes)
            target = self.validate_target()
            state = target["State"]
            if not state.get("Running") or state.get("Paused") or state.get("Restarting"):
                raise AgentError("Plex must be running normally before a restore can be verified")
            current = self.plex_identity(token, check_library=True)
            if current["machine_identifier"] != expected_identity:
                raise AgentError("Backuparr and the restore agent point to different Plex servers")
            if identity["machine_identifier"] != current["machine_identifier"]:
                raise AgentError("Backup belongs to another Plex server; automatic migration is not supported")
            if identity["version"] != current["version"]:
                raise AgentError("Plex version differs from the backup; use the same Plex version for automatic restore")
            # Reserve headroom before stopping Plex. Copies still fail safely if
            # another process consumes the disk between this check and snapshot.
            sizes = [(self.database_dir / name).stat().st_size
                     for name in DATABASE_FILES if (self.database_dir / name).exists()]
            old_size = sum(sizes)
            replacement_size = max([*sizes, (directory / "incoming.db").stat().st_size])
            shared_disk = directory.stat().st_dev == self.database_dir.stat().st_dev
            for folder, needed in ((directory, old_size + (replacement_size if shared_disk else 0)),
                                   (self.database_dir, replacement_size)):
                if shutil.disk_usage(folder).free < needed + 64 * 1024 * 1024:
                    raise AgentError("Not enough disk space for the restore and rollback copy")
            self.save(job, "stopping", container_id=target["Id"], server=current,
                      restart_policy=target["HostConfig"]["RestartPolicy"])
            # A Docker daemon restart must not start Plex halfway through writes.
            self.docker.restart_policy(target["Id"], {"Name": "no", "MaximumRetryCount": 0})
            self.docker.stop(target["Id"])
            self.snapshot(job)
            self.save(job, "applying")
            self.replace(job, directory / "incoming.db", DATABASE, job["original_files"][DATABASE])
            for suffix in ("-wal", "-shm"):
                self.remove(job, DATABASE + suffix)
            self.save(job, "starting")
            self.docker.start(target["Id"])
            self.wait_healthy(job, token)
            self.docker.restart_policy(target["Id"], job["restart_policy"])
            self.save(job, "complete", message="Plex library database restored; Plex is ready. API settings and artwork were not applied.")
        except Exception as exc:
            error = str(exc) if isinstance(exc, AgentError) else "Restore failed; check agent storage and Docker access"
            if job.get("container_id"):
                self.recover_job(job, token, error)
            else:
                self.fail_job(job, "failed", error)
        finally:
            try:
                if job.get("phase") in SETTLED:
                    self.cleanup(job)
            finally:
                self.active_job = None
                self.lock.release()

    def recover_job(self, job, token=None, cause=None):
        try:
            # Record diagnostics before rollback, so the durable rolled_back
            # result is the last write. Nothing may downgrade that result.
            self.rollback(job, token, cause)
        except Exception:
            self.fail_job(job, "recovery_failed", (
                "Automatic recovery could not finish. Do not start another restore. "
                "Keep the agent state volume and recover the rollback files using the agent documentation."
            ))

    def recover(self):
        """Run before accepting requests; retry an incomplete rollback after reboot."""
        with self.lock:
            self.journal_failed = False
            for job in self.jobs():
                if job["phase"] not in SETTLED:
                    if job.get("container_id"):
                        self.recover_job(job)
                    else:
                        self.fail_job(job, "failed", "Upload/validation was interrupted before any Plex changes")
                    if job["phase"] in TERMINAL:
                        self.cleanup(job)

    def jobs(self):
        # iterdir propagates I/O errors instead of silently skipping journals.
        for directory in sorted(self.state_dir.iterdir()):
            if stat.S_ISDIR(directory.stat().st_mode):
                job = self.read_job(directory.name)
                if job is not None:
                    yield job

    def recovery_blocked(self):
        if self.journal_failed:
            return True
        try:
            return any(job["phase"] == "recovery_failed" or
                       (job["phase"] not in SETTLED and job["id"] != self.active_job)
                       for job in self.jobs())
        except (OSError, AgentError):
            self.journal_failed = True
            return True
