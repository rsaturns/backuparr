"""Persistent config for Backuparr, shared by backup.py, restore.py, and
the web UI. Stored as a single JSON file on a volume so it survives
container recreation, and so the web UI and the cron-triggered backup run
are always looking at the same settings.
"""
import copy
import json
import os
import tempfile

CONFIG_PATH = os.environ.get("BACKUPARR_CONFIG", "/config/backuparr/config.json")

APP_NAMES = ["radarr", "sonarr", "prowlarr", "bazarr", "tdarr", "sabnzbd"]

DEFAULT_APP = {"enabled": False, "url": "", "api_key": "", "username": "", "password": ""}

# Drives the web UI's forms generically instead of hardcoding per-app
# knowledge in JS.
APP_META = [
    {"id": "radarr", "label": "Radarr", "icon": "radarr.svg", "key_required": True, "url_placeholder": "http://radarr:7878", "extra_fields": []},
    {"id": "sonarr", "label": "Sonarr", "icon": "sonarr.svg", "key_required": True, "url_placeholder": "http://sonarr:8989", "extra_fields": []},
    {"id": "prowlarr", "label": "Prowlarr", "icon": "prowlarr.svg", "key_required": True, "url_placeholder": "http://prowlarr:9696", "extra_fields": []},
    {
        "id": "bazarr",
        "label": "Bazarr",
        "icon": "bazarr.svg",
        "key_required": True,
        "url_placeholder": "http://bazarr:6767",
        "extra_fields": [
            {"name": "username", "label": "Basic auth username", "type": "text",
             "help": "Only if Settings > General > Security is set to Basic"},
            {"name": "password", "label": "Basic auth password", "type": "password",
             "help": "Only if Settings > General > Security is set to Basic"},
        ],
    },
    {
        "id": "tdarr",
        "label": "Tdarr",
        "icon": "tdarr.png",
        "key_required": False,
        "url_placeholder": "http://192.168.1.10:8266",
        "extra_fields": [],
        "key_help": "Only if Tdarr's API auth token is enabled",
    },
    {"id": "sabnzbd", "label": "SABnzbd", "icon": "sabnzbd.svg", "key_required": True, "url_placeholder": "http://sabnzbd:8080", "extra_fields": []},
]

DEST_NAMES = ["local", "gdrive", "dropbox", "onedrive"]

# Where local-destination backups land by default if the user hasn't set a
# custom path - a subdirectory of the same volume config.json already lives
# on, so it survives container recreation with no extra mount needed.
DEFAULT_LOCAL_DIR = "/config/backuparr/backups"

DEFAULT_DEST = {
    "local": {"enabled": True, "path": ""},
    "gdrive": {
        "enabled": False,
        "client_id": "",
        "client_secret": "",
        "refresh_token": "",
        "folder_id": "",
        "folder_name": "",
    },
    "dropbox": {"enabled": False},
    "onedrive": {"enabled": False},
}

# Fields the generic POST /api/config can write per destination. Notably
# excludes gdrive's refresh_token/folder_id/folder_name - those are only
# ever set by the dedicated OAuth/folder-picker routes in webui/app.py, so
# a POST to the general settings form can't forge a connected state or
# silently detach the picked folder.
DEST_EDITABLE_FIELDS = {
    "local": {"enabled", "path"},
    "gdrive": {"enabled", "client_id", "client_secret"},
    "dropbox": {"enabled"},
    "onedrive": {"enabled"},
}

# Drives the Settings > Destinations card generically. "coming_soon" ones
# render disabled with a badge instead of a working toggle - the roadmap is
# visible in the UI without us having built (or registered OAuth apps for)
# the integration yet.
DESTINATION_META = [
    {
        "id": "local",
        "label": "Local storage",
        "status": "available",
        "description": "Written straight to this container's own volume - nothing to connect. Download any backup from the History tab.",
        "setup_help": {
            "title": "Local storage",
            "intro": "No setup needed - this works out of the box.",
            "steps": [
                "Backups are written to /config/backuparr/backups on the volume already mounted for this container (see docker-compose.yml).",
                "Optionally set a custom path below if you'd rather use a different mounted volume - e.g. a NAS share mounted into this container.",
            ],
            "links": [],
        },
    },
    {
        "id": "gdrive",
        "label": "Google Drive",
        "icon": "google-drive.svg",
        "status": "available",
        "description": "Backed up to a folder in your Google Drive. Click-through OAuth - no rclone config, no terminal.",
        "setup_help": {
            "title": "Connect Google Drive",
            "intro": "Google requires every app to have its own registered OAuth client - a one-time, ~5 minute setup in Google Cloud Console.",
            "steps": [
                {
                    "text": "Create a Google Cloud project (or pick an existing one) - billing is not required for this.",
                    "link": {"label": "Create a project", "url": "https://console.cloud.google.com/projectcreate"},
                },
                {
                    "text": "Enable the Google Drive API for that project.",
                    "link": {"label": "Enable the Google Drive API", "url": "https://console.cloud.google.com/apis/library/drive.googleapis.com"},
                },
                {
                    "text": "Set up the Google Auth Platform: click \"Get started\", fill in an app name and your support email under App Information, then choose External under Audience (this just means anyone with a Google account can be added as a tester - it does not make the app public).",
                    "link": {"label": "Google Auth Platform - Branding", "url": "https://console.cloud.google.com/auth/branding"},
                },
                {
                    "text": "On the Audience tab, add your own Google account (and anyone else who should have access) under Test users - this is what keeps the app private with no Google review needed.",
                    "link": {"label": "Google Auth Platform - Audience", "url": "https://console.cloud.google.com/auth/audience"},
                },
                {
                    "text": "On the Data access tab, click \"Add or remove scopes\", filter by API = \"Google Drive API\", check the row matching the permission below (not .../auth/drive, which is full access), then click \"Update\" at the bottom of the panel:",
                    "checklist": [
                        "API: Google Drive API",
                        "Scope: .../auth/drive.file",
                        "Description: \"See, edit, create, and delete only the specific Google Drive files you use with this app\"",
                    ],
                    "link": {"label": "Google Auth Platform - Data access", "url": "https://console.cloud.google.com/auth/scopes"},
                },
                {
                    "text": "On the Clients tab, click Create Client, choose Web application, and add the redirect URI shown below to Authorized redirect URIs, exactly as shown.",
                    "link": {"label": "Google Auth Platform - Clients", "url": "https://console.cloud.google.com/auth/clients"},
                },
                {
                    "text": "Click Create. Google only shows the Client Secret once, at this moment - copy both the Client ID and Client Secret now, paste them into the fields below, save, then click \"Connect Google Drive\".",
                    "link": None,
                },
            ],
            "links": [
                {"label": "Google Auth Platform - Clients", "url": "https://console.cloud.google.com/auth/clients"},
                {"label": "Google's guide to creating OAuth credentials", "url": "https://developers.google.com/identity/protocols/oauth2/web-server#creatingcred"},
            ],
        },
    },
    {
        "id": "dropbox",
        "label": "Dropbox",
        "icon": "dropbox.svg",
        "status": "coming_soon",
        "description": "Coming soon.",
        "setup_help": None,
    },
    {
        "id": "onedrive",
        "label": "Microsoft OneDrive",
        "icon": "microsoft-onedrive.svg",
        "status": "coming_soon",
        "description": "Coming soon.",
        "setup_help": None,
    },
]

DEFAULTS = {
    "retention_days": 14,
    "cron_schedule": "0 3 * * *",
    "notify_url": "",
    "bazarr_backup_dir": "",
    "apps": {name: dict(DEFAULT_APP) for name in APP_NAMES},
    "destinations": {name: dict(DEFAULT_DEST[name]) for name in DEST_NAMES},
}

# Legacy per-app env vars from before the web UI existed, used only to seed
# config.json the first time this runs against an existing deployment.
_LEGACY_ENV_MAP = {
    "radarr": ("RADARR_URL", "RADARR_API_KEY"),
    "sonarr": ("SONARR_URL", "SONARR_API_KEY"),
    "prowlarr": ("PROWLARR_URL", "PROWLARR_API_KEY"),
    "bazarr": ("BAZARR_URL", "BAZARR_API_KEY"),
    "tdarr": ("TDARR_URL", "TDARR_API_KEY"),
    "sabnzbd": ("SABNZBD_URL", "SABNZBD_API_KEY"),
}


def _seed_from_legacy_env():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["retention_days"] = int(os.environ.get("RETENTION_DAYS", 14))
    cfg["cron_schedule"] = os.environ.get("CRON_SCHEDULE", DEFAULTS["cron_schedule"])
    cfg["notify_url"] = os.environ.get("NOTIFY_URL", "")

    enabled_names = {a.strip() for a in os.environ.get("APPS", "").split(",") if a.strip()}
    for name, (url_var, key_var) in _LEGACY_ENV_MAP.items():
        url = os.environ.get(url_var, "")
        if url:
            cfg["apps"][name]["url"] = url
            cfg["apps"][name]["api_key"] = os.environ.get(key_var, "")
            cfg["apps"][name]["enabled"] = name in enabled_names
    cfg["apps"]["bazarr"]["username"] = os.environ.get("BAZARR_USERNAME", "")
    cfg["apps"]["bazarr"]["password"] = os.environ.get("BAZARR_PASSWORD", "")
    return cfg


def load_config():
    if not os.path.exists(CONFIG_PATH):
        cfg = _seed_from_legacy_env()
        save_config(cfg)
        return cfg

    with open(CONFIG_PATH) as f:
        data = json.load(f)

    merged = copy.deepcopy(DEFAULTS)
    for key, value in data.items():
        if key not in ("apps", "destinations"):
            merged[key] = value
    for name in APP_NAMES:
        merged["apps"][name].update(data.get("apps", {}).get(name, {}))
    for name in DEST_NAMES:
        merged["destinations"][name].update(data.get("destinations", {}).get(name, {}))
    return merged


def save_config(cfg):
    config_dir = os.path.dirname(CONFIG_PATH)
    os.makedirs(config_dir, exist_ok=True)
    # Unique tmp filename, not a fixed "<path>.tmp" - avoids two concurrent
    # saves colliding on the same tmp path (see gdrive_oauth.sync_rclone_remote
    # for a case where that raced in practice).
    fd, tmp_path = tempfile.mkstemp(dir=config_dir, prefix=".config.json.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, CONFIG_PATH)
    except BaseException:
        os.remove(tmp_path)
        raise


def enabled_apps(cfg):
    return [name for name in APP_NAMES if cfg["apps"].get(name, {}).get("enabled")]


def key_required(name):
    return next((m["key_required"] for m in APP_META if m["id"] == name), True)


def enabled_destinations(cfg):
    """Only ones with status "available" can ever be enabled - a coming_soon
    id can't be flipped on client-side, but guard here too since config.json
    can be hand-edited."""
    available = {m["id"] for m in DESTINATION_META if m["status"] == "available"}
    return [
        name for name in DEST_NAMES
        if name in available and cfg["destinations"].get(name, {}).get("enabled")
    ]


def destination_meta(name):
    return next((m for m in DESTINATION_META if m["id"] == name), None)
