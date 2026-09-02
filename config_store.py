"""Persistent config for Backuparr - a single JSON file on a volume,
shared by backup.py, restore.py, and the web UI."""
import copy
import json
import os
import tempfile

import secrets_crypto

CONFIG_PATH = os.environ.get("BACKUPARR_CONFIG", "/config/backuparr/config.json")

APP_NAMES = ["radarr", "sonarr", "prowlarr", "profilarr", "bazarr", "tdarr", "sabnzbd", "tautulli", "seerr"]

DEFAULT_APP = {"enabled": False, "url": "", "api_key": "", "username": "", "password": ""}

# Drives the web UI's forms generically. "coming_soon" apps render
# disabled with a badge, same as DESTINATION_META below.
APP_META = [
    {"id": "radarr", "label": "Radarr", "icon": "radarr.svg", "status": "available", "key_required": True, "url_placeholder": "http://radarr:7878", "extra_fields": []},
    {"id": "sonarr", "label": "Sonarr", "icon": "sonarr.svg", "status": "available", "key_required": True, "url_placeholder": "http://sonarr:8989", "extra_fields": []},
    {"id": "prowlarr", "label": "Prowlarr", "icon": "prowlarr.svg", "status": "available", "key_required": True, "url_placeholder": "http://prowlarr:9696", "extra_fields": []},
    {
        "id": "profilarr",
        "label": "Profilarr",
        "icon": "profilarr.svg",
        "status": "available",
        "key_required": True,
        "url_placeholder": "http://profilarr:6868",
        "extra_fields": [],
        # Profilarr's restore has no public API - see README.
        "restore_supported": False,
    },
    {
        "id": "bazarr",
        "label": "Bazarr",
        "icon": "bazarr.svg",
        "status": "available",
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
        "status": "available",
        "key_required": False,
        "url_placeholder": "http://192.168.1.10:8266",
        "extra_fields": [],
        "key_help": "Only if Tdarr's API auth token is enabled",
    },
    {"id": "sabnzbd", "label": "SABnzbd", "icon": "sabnzbd.svg", "status": "available", "key_required": True, "url_placeholder": "http://sabnzbd:8080", "extra_fields": []},
    {"id": "tautulli", "label": "Tautulli", "icon": "tautulli.svg", "status": "available", "key_required": True, "url_placeholder": "http://tautulli:8181", "extra_fields": []},
    {
        "id": "seerr",
        "label": "Seerr",
        "icon": "seerr.svg",
        "status": "coming_soon",
        # No backup/restore API exists - card shown for visibility only.
        "key_required": True,
        "url_placeholder": "",
        "extra_fields": [],
        "restore_supported": False,
    },
]

DEST_NAMES = ["local", "gdrive", "onedrive", "dropbox"]

# Default local-destination backup path - same volume as config.json.
DEFAULT_LOCAL_DIR = "/config/backuparr/backups"

DEFAULT_DEST = {
    "local": {"enabled": True, "path": ""},
    "gdrive": {
        "enabled": False,
        "client_id": "",
        "client_secret": "",
        "developer_key": "",
        "refresh_token": "",
        "folder_id": "",
        "folder_name": "",
    },
    "dropbox": {"enabled": False},
    "onedrive": {
        "enabled": False,
        "token": "",
        "drive_id": "",
        "drive_type": "",
        "item_id": "",
    },
}

# Fields POST /api/config can write per destination - excludes OAuth
# state (tokens, folder IDs), which only the dedicated connect routes set.
DEST_EDITABLE_FIELDS = {
    "local": {"enabled", "path"},
    "gdrive": {"enabled", "client_id", "client_secret", "developer_key"},
    "dropbox": {"enabled"},
    "onedrive": {"enabled"},
}

# Drives the Settings > Destinations card generically.
DESTINATION_META = [
    {
        "id": "local",
        "label": "Local storage",
        "icon": "local.svg",
        "status": "available",
        "description": "Written straight to this container's own volume - nothing to connect. Download any backup from the History tab.",
        "setup_help": {
            "title": "Local storage",
            "intro": "No setup needed - this works out of the box.",
            "steps": [
                {
                    "text": "Backups are written here by default, on the volume already mounted for this container (see docker-compose.yml):",
                    "code": "/config/backuparr/backups",
                    "link": None,
                },
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
                    "text": "Also enable the Google Picker API - it's what the \"Choose folder\" button uses, separately from the Drive API above, and it must be enabled before you can restrict an API key to it in a later step.",
                    "link": {"label": "Enable the Google Picker API", "url": "https://console.cloud.google.com/apis/library/picker.googleapis.com"},
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
                    "text": "On the Data access tab, click \"Add or remove scopes\", filter by API = \"Google Drive API\", and check the row for this scope (not .../auth/drive, which is full access) - its description reads \"See, edit, create, and delete only the specific Google Drive files you use with this app\". It's classified as a non-sensitive scope, so it'll show up under \"Your non-sensitive scopes\" in the panel, not Sensitive or Restricted. Then click \"Update\" at the bottom of the panel:",
                    "code": "https://www.googleapis.com/auth/drive.file",
                    "link": {"label": "Google Auth Platform - Data access", "url": "https://console.cloud.google.com/auth/scopes"},
                },
                {
                    "text": "On the Clients tab, click Create Client, choose Web application, and add the redirect URI shown below to Authorized redirect URIs, exactly as shown.",
                    "redirect_uri": True,
                    "link": {"label": "Google Auth Platform - Clients", "url": "https://console.cloud.google.com/auth/clients"},
                },
                {
                    "text": "Click Create. Google only shows the Client Secret once, at this moment - copy both the Client ID and Client Secret now.",
                    "link": None,
                },
                {
                    "text": "Also create an API key - the \"Choose folder\" button needs one even though you're already signed in, since Google's folder-picker widget calls the Drive API separately from the OAuth token. Click Create Credentials > API key.",
                    "link": {"label": "Credentials", "url": "https://console.cloud.google.com/apis/credentials"},
                },
                {
                    "text": "Google Cloud now requires picking at least one API restriction for a new key - under API restrictions, choose \"Restrict key\" and select Google Picker API (only - the picker's calls are attributed to this API, not Drive API, so Drive API doesn't need to be added here).",
                    "link": None,
                },
                {
                    "text": "Optionally also restrict the key to your own site under Application restrictions > Websites - but if you do, also add https://docs.google.com/* to the allowed list. The folder picker itself runs in an iframe hosted on docs.google.com, not your domain, so leaving that one out reproduces the same broken picker this step exists to avoid.",
                    "link": None,
                },
                {
                    "text": "Copy the API key, then paste the Client ID, Client Secret, and API key into the fields below, save, then click \"Connect Google Drive\".",
                    "link": None,
                },
            ],
            "links": [
                {"label": "Google Auth Platform - Clients", "url": "https://console.cloud.google.com/auth/clients"},
                {"label": "Google's guide to creating OAuth credentials", "url": "https://developers.google.com/identity/protocols/oauth2/web-server#creatingcred"},
                {"label": "Credentials - API keys", "url": "https://console.cloud.google.com/apis/credentials"},
            ],
        },
    },
    {
        "id": "onedrive",
        "label": "Microsoft OneDrive",
        "icon": "microsoft-onedrive.svg",
        "status": "available",
        "description": "Backed up to a dedicated app folder in your personal OneDrive. Uses rclone's own built-in Microsoft app - no Azure account, no app registration. Personal Microsoft accounts only, not work/school (Microsoft 365) accounts.",
        "setup_help": {
            "title": "Connect OneDrive",
            "intro": "Uses rclone's own built-in Microsoft app, so there's no Azure account or app registration to set up - just run one command on any computer with a browser (it doesn't need to be this server).",
            "steps": [
                {
                    "text": "Download rclone (a single binary, no install needed) on any computer with a web browser - your own laptop is fine, it doesn't have to be wherever Backuparr runs.",
                    "link": {"label": "Download rclone", "url": "https://rclone.org/downloads/"},
                },
                {
                    "text": "Open a terminal there and run:",
                    "code": "rclone authorize onedrive",
                    "link": None,
                },
                {
                    "text": "It prints a link - open it in your browser, sign in with your personal Microsoft account, and approve access.",
                    "link": None,
                },
                {
                    "text": "Back in the terminal, rclone prints a block starting with \"Paste the following into your remote machine --->\". Copy that whole block (or just the line in the middle - either works) and paste it below, then click \"Connect OneDrive\".",
                    "link": None,
                },
            ],
            "links": [
                {"label": "rclone downloads", "url": "https://rclone.org/downloads/"},
                {"label": "rclone's remote setup docs", "url": "https://rclone.org/remote_setup/"},
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
]

DEFAULTS = {
    "retention_days": 7,
    "cron_schedule": "0 3 * * *",
    "notify_url": "",
    "bazarr_backup_dir": "",
    "apps": {name: dict(DEFAULT_APP) for name in APP_NAMES},
    "destinations": {name: dict(DEFAULT_DEST[name]) for name in DEST_NAMES},
}

def _secret_fields(cfg):
    """(container_dict, key) for every value encrypted at rest - an
    explicit allowlist, not "encrypt everything"."""
    fields = [(cfg["apps"][name], "api_key") for name in APP_NAMES]
    fields.append((cfg["apps"]["bazarr"], "password"))
    fields.append((cfg["destinations"]["gdrive"], "client_secret"))
    fields.append((cfg["destinations"]["gdrive"], "developer_key"))
    fields.append((cfg["destinations"]["gdrive"], "refresh_token"))
    fields.append((cfg["destinations"]["onedrive"], "token"))
    return fields


def load_config():
    if not os.path.exists(CONFIG_PATH):
        cfg = copy.deepcopy(DEFAULTS)
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

    # Decrypt in place; a legacy plaintext value gets re-saved encrypted.
    needs_migration = False
    for container, key in _secret_fields(merged):
        raw = container.get(key, "")
        if raw and not raw.startswith(secrets_crypto.PREFIX):
            needs_migration = True
        container[key] = secrets_crypto.decrypt(raw)

    if needs_migration:
        save_config(merged)

    return merged


def save_config(cfg):
    to_write = copy.deepcopy(cfg)
    for container, key in _secret_fields(to_write):
        container[key] = secrets_crypto.encrypt(container.get(key, ""))

    config_dir = os.path.dirname(CONFIG_PATH)
    os.makedirs(config_dir, exist_ok=True)
    # Unique tmp filename avoids concurrent saves colliding.
    fd, tmp_path = tempfile.mkstemp(dir=config_dir, prefix=".config.json.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(to_write, f, indent=2)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, CONFIG_PATH)
    except BaseException:
        os.remove(tmp_path)
        raise


def _enabled(names, meta, cfg_section):
    """Only "available" entries can be enabled - guards against a
    hand-edited config.json flipping on a coming_soon entry."""
    available = {m["id"] for m in meta if m["status"] == "available"}
    return [name for name in names if name in available and cfg_section.get(name, {}).get("enabled")]


def enabled_apps(cfg):
    return _enabled(APP_NAMES, APP_META, cfg["apps"])


def key_required(name):
    return next((m["key_required"] for m in APP_META if m["id"] == name), True)


def restore_supported(name):
    return next((m.get("restore_supported", True) for m in APP_META if m["id"] == name), True)


def app_meta(name):
    return next((m for m in APP_META if m["id"] == name), None)


def enabled_destinations(cfg):
    return _enabled(DEST_NAMES, DESTINATION_META, cfg["destinations"])


def destination_meta(name):
    return next((m for m in DESTINATION_META if m["id"] == name), None)
