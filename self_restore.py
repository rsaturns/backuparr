"""Restores Backuparr from its own encrypted backup (#111), on the setup
page of a fresh install or from Settings.

Everything is checked before anything changes: the archive must open with
the key given (the encryption is authenticated, so a wrong key or a
damaged file fails here), hold only Backuparr's files, come from this or an older
Backuparr, and every file must parse. Then the restored key becomes this
install's key - first, so a restore cut short can simply be run again -
followed by the config, the login, the manifest, the run logs and the
rclone remotes. The caller then replaces the session key, which signs
everyone out."""
import json
import logging
import os
import re

from cryptography.fernet import Fernet

import atomic_file
import auth_store
import config_store
import destination_util
import manifest
import rclone_util
import runlog
import secrets_crypto
import tls
import self_backup
from apps.base import AppError
from webui.version import VERSION

log = logging.getLogger("backuparr.self_restore")


class SelfRestoreError(AppError):
    pass


def _version(text):
    """(major, minor, patch) of a Backuparr version; a pre-release suffix
    (3.0.0-beta) compares as its release, so a beta still refuses a backup
    from a newer Backuparr."""
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.]+)?", str(text or ""))
    return tuple(int(part) for part in match.groups()) if match else None


def _json(members, name, kind):
    try:
        value = json.loads(members[name])
    except (ValueError, UnicodeDecodeError):
        raise SelfRestoreError(f"backuparr: {name} in the backup is damaged") from None
    if not isinstance(value, kind):
        raise SelfRestoreError(f"backuparr: {name} in the backup isn't what Backuparr writes")
    return value


def prepare(data, key):
    """The archive's files, checked; raises SelfRestoreError saying why not."""
    key = (key or "").strip()
    try:
        Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise SelfRestoreError("backuparr: that isn't a Backuparr encryption key - it's 44 characters ending in =") from None
    try:
        members = self_backup.open_archive(data, key)
    except self_backup.SelfBackupError as exc:
        raise SelfRestoreError(str(exc)) from None

    meta = _json(members, self_backup.META, dict)
    if not isinstance(meta.get("format"), int) or meta["format"] > self_backup.FORMAT:
        raise SelfRestoreError("backuparr: this backup was made by a newer Backuparr - update Backuparr first, then restore")
    made_by, running = _version(meta.get("backuparr_version")), _version(VERSION)
    if made_by and running and made_by > running:
        raise SelfRestoreError(f"backuparr: this backup is from Backuparr {meta['backuparr_version']} and this is "
                               f"{VERSION}. Update Backuparr to {meta['backuparr_version']} first, then restore.")

    config = _json(members, "config.json", dict)
    try:
        config_store._merge_defaults(config)
    except Exception:
        raise SelfRestoreError("backuparr: config.json in the backup isn't what Backuparr writes") from None
    if "auth.json" not in members:
        raise SelfRestoreError("backuparr: the backup has no login (auth.json)")
    auth = _json(members, "auth.json", dict)
    if not isinstance(auth.get("username"), str) or not isinstance(auth.get("password_hash"), str):
        raise SelfRestoreError("backuparr: the login in the backup is incomplete")
    for name in ("manifest.json", "run_logs.json"):
        if name in members:
            _json(members, name, dict)
    remotes = _json(members, self_backup.REMOTES, dict) if self_backup.REMOTES in members else {}
    if not all(isinstance(fields, dict) and isinstance(fields.get("type"), str) for fields in remotes.values()):
        raise SelfRestoreError(f"backuparr: {self_backup.REMOTES} in the backup isn't what Backuparr writes")
    return members


def _write_or_remove(path, data):
    if data is None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    else:
        atomic_file.write_private(path, data)


def _current_https_only():
    try:
        with open(config_store.CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f).get("https_only") is True
    except (OSError, ValueError, AttributeError):
        return False


def apply(members, key):
    """Writes a prepare()d archive in place of this install's state. Returns
    warnings for the user."""
    key = key.strip()
    warnings = []
    atomic_file.write_private(secrets_crypto.KEY_PATH, key)
    secrets_crypto.forget_key()
    env_key = os.environ.get("BACKUPARR_SECRETS_KEY")
    if env_key and env_key.strip() != key:
        warnings.append("BACKUPARR_SECRETS_KEY is set to a different key. Set it to the restored key (or remove it) "
                        "and recreate the container - until then the restored passwords, API keys and cloud logins "
                        "can't be read.")

    manifest.clear()  # also stops a backfill still running from writing over it
    # HTTPS only belongs to this install, like its ports: carried over to a
    # server that hasn't published the HTTPS port, it would lock you out (#177).
    # Read and written as plain JSON: the secrets key already changed above.
    restored = json.loads(members["config.json"])
    with config_store.CONFIG_LOCK:  # no Settings save in between can bring the old one back
        restored["https_only"] = _current_https_only()
        atomic_file.write_private(config_store.CONFIG_PATH, json.dumps(restored, indent=2))
    config_store.forget_secrets_key_acknowledged()
    atomic_file.write_private(auth_store.AUTH_PATH, members["auth.json"])
    _write_or_remove(manifest.path(), members.get("manifest.json"))
    _write_or_remove(runlog.path(), members.get("run_logs.json"))

    remotes = json.loads(members.get(self_backup.REMOTES, b"{}"))
    try:
        for name in rclone_util.config_dump():
            rclone_util.config_delete(name)
        for name, fields in remotes.items():
            fields = dict(fields)
            kind = fields.pop("type")
            # Only the remotes Backuparr keeps, as the type each one is: some
            # rclone remotes can run a command (sftp's ssh), and nothing else
            # in rclone.conf is Backuparr's to bring back.
            if destination_util.OWN_REMOTES.get(name) != kind:
                log.warning("backuparr: left out the rclone remote %r (%s) from the backup - it isn't one of "
                            "Backuparr's own", name, kind)
                continue
            rclone_util.config_set(name, kind, fields, force=True)
    except rclone_util.RcloneError as exc:
        warnings.append(f"The cloud connections couldn't all be restored ({exc.detail}); "
                        "the next backup sets them up again from the restored settings.")
    key_pem, cert_pem = (members.get(name) for name in tls.BACKUP_NAMES)
    if key_pem and cert_pem:
        tls.restore_files(key_pem, cert_pem)
    log.info("backuparr: restored its configuration from its own backup")
    return warnings
