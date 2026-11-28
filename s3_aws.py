"""AWS regional endpoints are resolved by rclone unless an endpoint is supplied."""

PROVIDER = {
    "label": "AWS S3",
    "backend": "AWS",
    "icon": "amazon-s3.svg",
    "endpoint": "AWS regional endpoint",
    "endpoint_optional": True,
    "region": True,
    "default_region": "us-east-1",
    "allow_http": True,
}
