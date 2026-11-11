"""Backuparr's own backup (#110): everything needed to bring Backuparr back
after losing the server, encrypted with its existing secrets key.

The archive is a zip made in memory, encrypted with archive_crypto like
an encrypted app backup (authenticated, so a wrong key or a damaged or
altered file is refused). Backups made before 3.0 (one Fernet token) are
recognised but no longer open: restore them with 2.5.6 (#162). It holds:

- config.json as stored (its secret fields stay encrypted with the same key)
- auth.json: the admin username and password hash
- manifest.json and run_logs.json
- rclone-remotes.json: the rclone remotes, so a restore needs no second
  password (rclone.conf itself is encrypted with rclone.pass)
- backuparr-meta.json: Backuparr's version, when it was made, the format
- tls-key.pem and tls-cert.pem: its own HTTPS key and certificate (#177),
  so a restored install keeps them and Home Assistant's pin still matches
  (never your own certificate's files from BACKUPARR_TLS_CERT/_KEY)

Left out: the secrets key itself (it unlocks the archive), the session key
(a restore signs everyone out) and rclone.pass (not needed).

Saved as backuparr/backuparr_<timestamp>.enc on every destination, at the
end of each full run, so the manifest and run logs include that run."""
import io
import json
import logging
import os
import tempfile
import zipfile
from datetime import datetime, timezone

import archive_crypto
import auth_store
import config_store
import destination_util
import manifest
import rclone_util
import runlog
import tls
from apps.base import AppError
from webui.version import VERSION

log = logging.getLogger("backuparr.self_backup")

FOLDER = destination_util.SELF_BACKUP_FOLDER
SUFFIX = ".enc"
FORMAT = 1
META = "backuparr-meta.json"
REMOTES = "rclone-remotes.json"
# The only files an archive may hold, and how big they may be unzipped -
# a restore reads them into memory.
MEMBERS = {"config.json", "auth.json", "manifest.json", "run_logs.json", REMOTES, META, *tls.BACKUP_NAMES}
MAX_UNZIPPED = 256 * 1024 * 1024


class SelfBackupError(AppError):
    pass


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def build():
    """The encrypted archive, as bytes."""
    files = {
        "config.json": _read(config_store.CONFIG_PATH),
        "auth.json": _read(auth_store.AUTH_PATH),
        "manifest.json": _read(manifest.path()),
        "run_logs.json": _read(runlog.path()),
        REMOTES: json.dumps(rclone_util.config_dump(), indent=1).encode("utf-8"),
        META: json.dumps({"format": FORMAT, "backuparr_version": VERSION,
                          "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}).encode("utf-8"),
        **tls.backup_files(),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            if data is not None:
                z.writestr(name, data)
    return archive_crypto.encrypt_bytes(buffer.getvalue())


def open_archive(data, key=None):
    """{name: bytes} from an encrypted archive, with this install's key or
    the one given. Raises SelfBackupError, saying why, if it can't be."""
    if archive_crypto.is_pre_3_self_backup(data):
        raise SelfBackupError(f"backuparr: {archive_crypto.PRE_3_SELF_BACKUP}")
    try:
        plain = archive_crypto.decrypt_bytes(data, key)
    except (archive_crypto.ArchiveCryptoError, ValueError, TypeError):
        raise SelfBackupError("backuparr: this isn't a Backuparr backup made with this key, or it's damaged") from None
    try:
        with zipfile.ZipFile(io.BytesIO(plain)) as z:
            infos = z.infolist()
            unexpected = sorted({i.filename for i in infos} - MEMBERS)
            if unexpected:
                raise SelfBackupError(f"backuparr: the backup holds an unexpected file ({unexpected[0]})")
            if sum(i.file_size for i in infos) > MAX_UNZIPPED:
                raise SelfBackupError("backuparr: the backup is too large to be one of Backuparr's")
            if z.testzip() is not None:
                raise SelfBackupError("backuparr: the backup is damaged")
            members = {i.filename: z.read(i) for i in infos}
    except zipfile.BadZipFile:
        raise SelfBackupError("backuparr: the backup is damaged") from None
    for needed in (META, "config.json"):
        if needed not in members:
            raise SelfBackupError(f"backuparr: the backup has no {needed}")
    try:
        json.loads(members[META])
        json.loads(members["config.json"])
    except ValueError:
        raise SelfBackupError("backuparr: the backup is damaged") from None
    return members


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run(dest_roots, timestamp):
    """Makes the archive, checks it opens and uploads it to every destination.
    Returns None, or what went wrong for the run's failed list. Its log is
    kept like an app's, so the Overview and History show it."""
    record = {"id": f"{FOLDER}_{timestamp}", "app": FOLDER, "started": _utc_now(), "finished": None, "file": None,
              "ok": False, "error": None, "app_version": VERSION, "destinations": {d: None for d in dest_roots}}
    capture = runlog.Capture().start()
    try:
        problem = _run(dest_roots, timestamp, record)
    finally:
        capture.stop()
    record.update(ok=problem is None, error=problem, finished=_utc_now(), lines=capture.result())
    runlog.save(record)
    return problem


def _run(dest_roots, timestamp, record):
    name = f"{FOLDER}_{timestamp}{SUFFIX}"
    log.info("=== %s ===", FOLDER)
    log.info("backuparr: backing up its own configuration...")
    try:
        data = build()
        open_archive(data)  # the check before upload: it must open with the key
    except (OSError, rclone_util.RcloneError, SelfBackupError) as exc:
        message = getattr(exc, "detail", None) or str(exc).removeprefix("backuparr: ")
        log.error("backuparr: couldn't make its own backup: %s", message)
        return message
    record["file"] = name
    failures, uploaded = [], []
    with tempfile.TemporaryDirectory(prefix="backuparr-self-") as tmp:
        path = os.path.join(tmp, name)
        with open(path, "wb") as f:
            f.write(data)
        for dest_id, root in dest_roots.items():
            try:
                destination_util.wait_for_storage(dest_id, root)
                rclone_util.copyto(path, f"{root}/{FOLDER}/{name}")
                log.info("backuparr: uploaded its own backup to %s (%d bytes)", dest_id, len(data))
                uploaded.append(dest_id)
                record["destinations"][dest_id] = {"ok": True, "error": None}
            except (rclone_util.RcloneError, destination_util.DestinationError) as exc:
                detail = exc.detail
                log.error("backuparr: upload of its own backup to %s failed: %s", dest_id, detail)
                failures.append(f"{dest_id}: {detail}")
                record["destinations"][dest_id] = {"ok": False, "error": detail}
        if uploaded:
            try:
                manifest.record(FOLDER, name, uploaded, app_version=VERSION, sha256=manifest.file_sha256(path),
                                size=len(data), backuparr_version=VERSION)
            except OSError as exc:
                log.warning("backuparr: couldn't write the backup record: %s", exc.strerror or type(exc).__name__)
    return f"failed on {'; '.join(failures)}" if failures else None
