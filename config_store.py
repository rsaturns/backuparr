"""Persistent config for Backuparr, shared by backup.py, restore.py, and
the web UI. Stored as a single JSON file on a volume so it survives
container recreation, and so the web UI and the cron-triggered backup run
are always looking at the same settings.
"""
import copy
import json
import os

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

DEFAULTS = {
    "rclone_remote": "",
    "retention_days": 14,
    "cron_schedule": "0 3 * * *",
    "notify_url": "",
    "bazarr_backup_dir": "",
    "apps": {name: dict(DEFAULT_APP) for name in APP_NAMES},
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
    cfg["rclone_remote"] = os.environ.get("RCLONE_REMOTE", "")
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
        if key != "apps":
            merged[key] = value
    for name in APP_NAMES:
        merged["apps"][name].update(data.get("apps", {}).get(name, {}))
    return merged


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, CONFIG_PATH)


def enabled_apps(cfg):
    return [name for name in APP_NAMES if cfg["apps"].get(name, {}).get("enabled")]


def key_required(name):
    return next((m["key_required"] for m in APP_META if m["id"] == name), True)
