"""Generic S3-compatible storage, including self-hosted services."""

PROVIDER = {
    "label": "Other S3-compatible",
    "backend": "Other",
    "icon": "cloud-storage.svg",
    "endpoint": "https://s3.example.com",
    "region": True,
    "default_region": "us-east-1",
    "allow_http": True,
}
