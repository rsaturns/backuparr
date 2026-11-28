"""Cloud Storage's shared rclone S3 driver."""
import hashlib
import json
import re
from urllib.parse import urlsplit

import rclone_util
import s3_aws
import s3_backblaze
import s3_cloudflare
import s3_minio
import s3_other
import s3_wasabi


PROVIDERS = {
    "aws": s3_aws.PROVIDER,
    "backblaze": s3_backblaze.PROVIDER,
    "cloudflare": s3_cloudflare.PROVIDER,
    "wasabi": s3_wasabi.PROVIDER,
    "minio": s3_minio.PROVIDER,
    "other": s3_other.PROVIDER,
}
SETTING_FIELDS = ("provider", "endpoint", "region", "bucket", "prefix", "access_key_id", "secret_access_key")
CONNECTION_FIELDS = ("provider", "endpoint", "access_key_id")
RCLONE_TYPE = "s3"
DEFAULT_PREFIX = "Backuparr"
_REMOTE = re.compile(r"backuparr-s3-[0-9a-f]{24}")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
_REGION = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_AWS_ENDPOINT = re.compile(r"(?:^|\.)s3(?:[.-][a-z0-9-]+)*\.amazonaws\.com(?:\.cn)?$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class S3StorageError(RuntimeError):
    pass


def _relative_prefix(prefix):
    # Strip pasted delimiters while preserving spaces in existing relative keys.
    trimmed = prefix.strip(" ")
    return trimmed.strip(" /") if trimmed.startswith("/") or trimmed.endswith("/") else prefix


def merge_settings(saved, edits):
    """Normalize pasted names and require a fresh secret when the connection changes."""
    merged = {**saved, **edits}
    if "secret_access_key" not in edits and (
        any(key in edits and edits[key] != saved.get(key) for key in CONNECTION_FIELDS)
        or _endpoint(merged) != _endpoint(saved)
    ):
        merged["secret_access_key"] = ""
    if "bucket" in merged:
        merged["bucket"] = _bucket_name(merged)
    if isinstance(edits.get("prefix"), str):
        merged["prefix"] = _relative_prefix(edits["prefix"])
    return merged


def _valid_endpoint(value, allow_http):
    if not value.isascii() or any(c.isspace() for c in value) or any(c in value for c in "\\?#"):
        return False
    try:
        url = urlsplit(value)
        return (url.scheme in (("http", "https") if allow_http else ("https",))
                and url.hostname and url.username is None and url.path in ("", "/")
                and (url.port is None or url.port > 0))
    except ValueError:
        return False


def _endpoint(cfg):
    provider = PROVIDERS.get(cfg.get("provider"), {})
    endpoint = cfg.get("endpoint", "") if "endpoint" in provider else ""
    if not endpoint and "regional_host" in provider:
        region = cfg.get("region") or provider["default_region"]
        if region == provider["default_region"]:
            return provider["endpoint"]
        return f'https://s3.{region}.{provider["regional_host"]}'
    return endpoint


def _region(cfg, endpoint):
    provider = PROVIDERS[cfg["provider"]]
    if "region" in provider and cfg.get("region"):
        return cfg["region"]
    if endpoint and "regional_host" in provider:
        host = urlsplit(endpoint).hostname
        match = re.fullmatch(rf"s3\.({_REGION.pattern})\.{re.escape(provider['regional_host'])}", host)
        if match:
            return match[1]
    return provider.get("default_region", "")


def _bucket_name(cfg):
    bucket = cfg.get("bucket", "")
    if cfg.get("provider") == "backblaze" and bucket.isascii():
        return bucket.lower()
    return bucket


def validation_error(cfg, required=True):
    for key in SETTING_FIELDS:
        value = cfg.get(key, "")
        if not isinstance(value, str) or _CONTROL.search(value):
            return f"Cloud Storage: {key} must be single-line text"
    provider = PROVIDERS.get(cfg.get("provider"))
    if not provider:
        if required or cfg.get("provider"):
            return "Cloud Storage: choose a supported storage provider"
        return None
    endpoint = cfg.get("endpoint")
    if "endpoint" in provider:
        if endpoint and not _valid_endpoint(endpoint, provider.get("allow_http")):
            return "Cloud Storage: enter the S3 API endpoint URL without credentials, a path, or a query"
        if required and not endpoint and not provider.get("endpoint_optional"):
            return "Cloud Storage: the endpoint URL is required for this provider"
    region = cfg.get("region")
    if "region" in provider:
        if region and not _REGION.fullmatch(region):
            return "Cloud Storage: enter a valid region"
    bucket = _bucket_name(cfg)
    if (bucket or required) and not (bucket and _BUCKET.fullmatch(bucket) and ".." not in bucket):
        return "Cloud Storage: enter an S3 bucket name (3-63 lowercase letters, digits, dots or hyphens)"
    prefix = _relative_prefix(cfg.get("prefix", ""))
    if prefix and (any(part in ("", ".", "..") for part in prefix.split("/")) or any(c in prefix for c in "\\:")):
        return "Cloud Storage: enter a relative path prefix with non-empty folder names"
    for key, label in (("access_key_id", "access key ID"), ("secret_access_key", "secret access key")):
        value = cfg.get(key, "")
        if (required and not value) or any(c.isspace() for c in value):
            return f"Cloud Storage: enter a valid {label}"
    return None


def _options(cfg):
    error = validation_error(cfg)
    if error:
        raise S3StorageError(error)
    provider = PROVIDERS[cfg["provider"]]
    endpoint = _endpoint(cfg)
    backend = provider["backend"]
    if cfg["provider"] == "aws" and endpoint and not _AWS_ENDPOINT.search(urlsplit(endpoint).hostname):
        # Compatible servers may need path-style requests and lack AWS-specific features.
        backend = s3_other.PROVIDER["backend"]
    return {
        "provider": backend,
        "endpoint": endpoint,
        "region": _region(cfg, endpoint),
        "access_key_id": cfg["access_key_id"],
        "secret_access_key": cfg["secret_access_key"],
        "env_auth": "false",
        "no_check_bucket": "true",  # use an existing bucket with bucket-scoped credentials
    }


def _remote_name(cfg, options):
    # A saved provider or endpoint change must never send an operation still
    # holding the previous root to the new storage. Keys can rotate in place.
    target = [cfg["provider"], options["endpoint"], options["region"], _bucket_name(cfg)]
    digest = hashlib.sha256(json.dumps(target).encode()).hexdigest()[:24]
    return f"backuparr-s3-{digest}"


def managed_remote(name):
    return _REMOTE.fullmatch(name) is not None


def backup_target(cfg):
    prefix = _relative_prefix(cfg.get("prefix") or "") or DEFAULT_PREFIX
    return f"{_bucket_name(cfg)}/{prefix}"


def remote_root(cfg):
    return f"{_remote_name(cfg, _options(cfg))}:{backup_target(cfg)}"


def sync_rclone_remote(cfg, remotes):
    """Sync the saved target. Old roots fail closed after a target change."""
    name = None
    if cfg.get("enabled") and not validation_error(cfg):
        fields = _options(cfg)
        name = _remote_name(cfg, fields)
        remote = remotes.get(name, {})
        if remote.get("type") != RCLONE_TYPE or any(str(remote.get(k, "")) != v for k, v in fields.items()):
            rclone_util.config_set(name, RCLONE_TYPE, fields, force=True)
    for old in remotes:
        if old != name and managed_remote(old):
            rclone_util.config_delete(old)


def test_connection(cfg):
    fields = _options(cfg)
    root = f"backuparr_s3_test:{backup_target(cfg)}"
    rclone_util.check_bucket(root, fields)
