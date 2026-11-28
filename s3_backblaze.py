"""Backblaze B2 uses rclone's generic S3 provider."""

PROVIDER = {
    "label": "Backblaze B2",
    "backend": "Other",
    "icon": "backblaze.svg",
    "endpoint": "https://s3.us-west-004.backblazeb2.com",
    "endpoint_optional": True,
    "regional_host": "backblazeb2.com",
    "region": True,
    "default_region": "us-west-004",
}
