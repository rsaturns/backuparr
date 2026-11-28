"""Cloudflare R2 signs requests with the fixed auto region."""

PROVIDER = {
    "label": "Cloudflare R2",
    "backend": "Cloudflare",
    "icon": "cloudflare.svg",
    "endpoint": "https://<account-id>.r2.cloudflarestorage.com",
    "default_region": "auto",
}
