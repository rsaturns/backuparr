"""Cloud Storage as a Backuparr destination, backed by rclone's s3 backend.

One card, one rclone remote (REMOTE_NAME): the card's provider picks which
S3-compatible storage it talks to - AWS S3, Backblaze B2, Cloudflare R2,
Wasabi, MinIO or any other S3-compatible endpoint - and the provider's
endpoint and region fill in when the card leaves them blank. rclone's s3
backend has no Backblaze provider, so B2 runs as "Other" with B2's S3
endpoint. The secret access key is the credential: it is encrypted at
rest (see config_store) and redacted from rclone's errors (see
rclone_util.SENSITIVE_FIELDS).
"""

import rclone_util

REMOTE_NAME = "backuparr-s3"
RCLONE_TYPE = "s3"
TOKEN_FIELD = "secret_access_key"
BACKUP_FOLDER = "Backuparr"


class S3Error(RuntimeError):
    pass


# The providers the Cloud Storage card offers, in card order. "endpoint"
# and "region" are the provider's defaults, used when the card leaves the
# field blank; "endpoint_required" means the provider has no default
# endpoint, so the card must supply one.
PROVIDERS = {
    "aws": {
        "label": "AWS S3",
        "rclone_provider": "AWS",
        "endpoint": "",
        "region": "us-east-1",
    },
    "backblaze": {
        "label": "Backblaze B2",
        # rclone's s3 backend has no Backblaze provider: B2 speaks the S3
        # API, so it runs as "Other" with B2's S3 endpoint.
        "rclone_provider": "Other",
        "endpoint": "https://s3.us-west-000.backblazeb2.com",
        "region": "us-west-000",
    },
    "cloudflare": {
        "label": "Cloudflare R2",
        "rclone_provider": "Cloudflare",
        "endpoint": "",
        "region": "auto",
        "endpoint_required": True,
    },
    "wasabi": {
        "label": "Wasabi",
        "rclone_provider": "Wasabi",
        "endpoint": "https://s3.wasabisys.com",
        "region": "us-east-1",
    },
    "minio": {
        "label": "MinIO",
        "rclone_provider": "Minio",
        "endpoint": "",
        "region": "",
        "endpoint_required": True,
    },
    "other": {
        "label": "Other S3-compatible",
        "rclone_provider": "Other",
        "endpoint": "",
        "region": "",
        "endpoint_required": True,
    },
}


def _path_prefix(dest_cfg):
    """The folder inside the bucket, without leading or trailing slashes."""
    return (dest_cfg.get("path_prefix") or BACKUP_FOLDER).strip().strip("/") or BACKUP_FOLDER


def enable_error(dest_cfg):
    """The first reason this destination can't be enabled, or None."""
    provider = PROVIDERS.get(dest_cfg.get("provider") or "")
    if provider is None:
        return "Cloud Storage: choose a storage provider to enable it"
    if provider.get("endpoint_required") and not (dest_cfg.get("endpoint") or "").strip():
        return "Cloud Storage: an endpoint URL is required for this provider"
    if not (dest_cfg.get("bucket") or "").strip():
        return "Cloud Storage: a bucket is required to enable it"
    if not (dest_cfg.get("access_key_id") or "").strip():
        return "Cloud Storage: an access key ID is required to enable it"
    if not dest_cfg.get("secret_access_key"):
        return "Cloud Storage: a secret access key is required to enable it"
    return None


def _configured(dest_cfg):
    """Whether the destination holds everything it needs to connect."""
    return enable_error(dest_cfg) is None


def remote_root(dest_cfg):
    """The rclone path backups go to: the bucket and the path prefix."""
    error = enable_error(dest_cfg)
    if error is not None:
        raise S3Error(error)
    bucket = (dest_cfg.get("bucket") or "").strip()
    return f"{REMOTE_NAME}:{bucket}/{_path_prefix(dest_cfg)}"


def backup_target(dest_cfg):
    """A sentence naming where backups go, for the Test connection message."""
    provider = PROVIDERS[dest_cfg.get("provider") or ""]
    bucket = (dest_cfg.get("bucket") or "").strip()
    return f"the {_path_prefix(dest_cfg)} folder in bucket {bucket} ({provider['label']})"


def _remote_fields(dest_cfg):
    """The rclone fields for this destination, with the provider's
    endpoint and region filled in when the card leaves them blank."""
    provider = PROVIDERS[dest_cfg.get("provider") or ""]
    return {
        "provider": provider["rclone_provider"],
        "endpoint": (dest_cfg.get("endpoint") or "").strip() or provider["endpoint"],
        "region": (dest_cfg.get("region") or "").strip() or provider["region"],
        "access_key_id": (dest_cfg.get("access_key_id") or "").strip(),
        "secret_access_key": dest_cfg.get("secret_access_key") or "",
    }


def _remote_matches(remote, fields):
    """Whether rclone.conf's remote already holds these settings."""
    if not remote or remote.get("type") != RCLONE_TYPE:
        return False
    return all(remote.get(key, "") == value for key, value in fields.items())


def sync_rclone_remote(dest_cfg, remotes=None):
    """Writes (or removes) the REMOTE_NAME remote in rclone.conf to match
    destinations.s3. A full rewrite (force=True) - safe since access keys
    don't rotate on use - but only when something changed, since the
    secret goes on rclone's command line.
    `remotes` is a config_dump() the caller already has."""
    remote = rclone_util.connected_remote(REMOTE_NAME, dest_cfg.get("secret_access_key"), remotes)
    if not _configured(dest_cfg):
        return
    fields = _remote_fields(dest_cfg)
    if _remote_matches(remote, fields):
        return
    rclone_util.config_set(
        REMOTE_NAME, RCLONE_TYPE, {key: value for key, value in fields.items() if value}, force=True)
