"""MinIO may be hosted locally and defaults to the us-east-1 region."""

PROVIDER = {
    "label": "MinIO",
    "backend": "Minio",
    "icon": "minio.svg",
    "icon_dark": "minio-dark.svg",
    "endpoint": "https://minio.example.com:9000",
    "region": True,
    "default_region": "us-east-1",
    "allow_http": True,
}
