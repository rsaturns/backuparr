"""Persistent config for Backuparr - a single JSON file on a volume,
shared by backup.py and the web UI."""
import copy
import json
import logging
import os
import re

import atomic_file
import secrets_crypto
from apps.http_headers import RequestHeadersError, validate_request_headers

log = logging.getLogger("backuparr.config_store")

CONFIG_PATH = os.environ.get("BACKUPARR_CONFIG", "/config/backuparr/config.json")


class ConfigError(RuntimeError):
    """config.json exists but can't be used (hand-edited into invalid JSON,
    truncated by a full disk, ...). The message says what to do about it."""


APP_NAMES = ["radarr", "sonarr", "lidarr", "whisparr", "prowlarr", "profilarr", "bazarr", "tdarr", "sabnzbd", "nzbget", "tautulli", "jellyfin", "emby", "plex", "seerr", "lazylibrarian"]

DEFAULT_APP = {"enabled": False, "url": "", "api_key": "", "username": "", "password": ""}

# Apps that can run more than once - e.g. a 4K Radarr beside the HD one. The
# first instance keeps the bare app id, so its config, remote folder and
# archive names never change; each extra one is "<app>-<suffix>" (radarr-4k),
# an id fixed when it's added because it names the instance's backup folder.
# Only its display name can change later.
MULTI_INSTANCE_APPS = ("radarr", "sonarr", "lidarr", "whisparr")
MAX_EXTRA_INSTANCES = 10
_EXTRA_INSTANCE_ID = re.compile(rf"({'|'.join(MULTI_INSTANCE_APPS)})-([a-z0-9]+(?:-[a-z0-9]+)*)")
_MAX_INSTANCE_ID_LENGTH = 40
# Apps Prowlarr discovery can fill in: its Applications, then its download
# clients. Each one's Prowlarr implementation, lowercased, is its app id.
DISCOVERABLE_APPS = ("radarr", "sonarr", "lidarr", "whisparr", "lazylibrarian", "sabnzbd", "nzbget")
# An extra instance also has a display name, e.g. "Radarr 4K".
DEFAULT_INSTANCE = {**DEFAULT_APP, "name": ""}

# Drives the web UI's forms generically. restore_warning is shown on the
# Restore tab before an app's restore.
APP_META = [
    {"id": "radarr", "label": "Radarr", "icon": "radarr.svg", "key_required": True, "url_placeholder": "http://radarr:7878", "extra_fields": [], "multi_instance": True, "max_extra_instances": MAX_EXTRA_INSTANCES},
    {"id": "sonarr", "label": "Sonarr", "icon": "sonarr.svg", "key_required": True, "url_placeholder": "http://sonarr:8989", "extra_fields": [], "multi_instance": True, "max_extra_instances": MAX_EXTRA_INSTANCES},
    {"id": "lidarr", "label": "Lidarr", "icon": "lidarr.svg", "key_required": True, "url_placeholder": "http://lidarr:8686", "extra_fields": [], "multi_instance": True, "max_extra_instances": MAX_EXTRA_INSTANCES},
    {"id": "whisparr", "label": "Whisparr", "icon": "whisparr.svg", "key_required": True, "url_placeholder": "http://whisparr:6969", "extra_fields": [], "multi_instance": True, "max_extra_instances": MAX_EXTRA_INSTANCES},
    {"id": "prowlarr", "label": "Prowlarr", "icon": "prowlarr.svg", "key_required": True, "url_placeholder": "http://prowlarr:9696", "extra_fields": []},
    {
        "id": "profilarr",
        "label": "Profilarr",
        "icon": "profilarr.svg",
        "key_required": True,
        "url_placeholder": "http://profilarr:6868",
        "extra_fields": [],
        # Profilarr's restore has no public API - see docs/apps/profilarr.md.
        "restore_supported": False,
    },
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
        "key_help": "Tools > API Keys - needed when Tdarr's authentication is on, the default from Tdarr 3",
        "restore_warning": {"lead": "Destructive:", "text": "this wipes each Tdarr database collection before repopulating it from the backup.", "danger": True},
    },
    {"id": "sabnzbd", "label": "SABnzbd", "icon": "sabnzbd.svg", "key_required": True, "url_placeholder": "http://sabnzbd:8080", "extra_fields": []},
    {
        "id": "nzbget",
        "label": "NZBGet",
        "icon": "nzbget.svg",
        # NZBGet has no API key: its JSON-RPC API takes the username and
        # password of the Control user - see apps/nzbget.py.
        "key_required": False,
        "no_api_key": True,
        "url_placeholder": "http://nzbget:6789",
        "extra_fields": [
            {"name": "username", "label": "Username", "type": "text",
             "help": "Settings > Security > ControlUsername"},
            {"name": "password", "label": "Password", "type": "password",
             "help": "Settings > Security > ControlPassword"},
        ],
        "restore_warning": {"lead": "Replaces NZBGet's settings", "text": "with the backup's - news servers and their passwords, categories, paths, extension settings, and the username and password. NZBGet reloads to apply them; the queue and history are kept. If the backup's username or password differ from the ones in Settings, update them there afterwards."},
    },
    {"id": "tautulli", "label": "Tautulli", "icon": "tautulli.svg", "key_required": True, "url_placeholder": "http://tautulli:8181", "extra_fields": []},
    {
        "id": "jellyfin",
        "label": "Jellyfin",
        "icon": "jellyfin.svg",
        "key_required": True,
        "url_placeholder": "http://jellyfin:8096",
        "key_help": "Dashboard > API Keys",
        # Jellyfin's API can't download a backup, so its backups folder is
        # mounted into this container - see apps/jellyfin.py.
        "extra_fields": [
            {"name": "backup_dir", "label": "Backups folder", "type": "text", "required": True,
             "placeholder": "/mnt/jellyfin-backups",
             "help": "Jellyfin's backups folder as mounted here; read-write to restore"},
        ],
        "restore_warning": {"lead": "Replaces Jellyfin's database and settings", "text": "with the backup's - users, watch history, libraries and API keys. Anything added since is lost. Jellyfin restarts to apply it."},
    },
    {
        "id": "emby",
        "label": "Emby",
        "icon": "emby.svg",
        "key_required": True,
        "url_placeholder": "http://emby:8096",
        "key_help": "Settings > Advanced > API Keys",
        # Emby has no backup API, so its data folder is mounted into this
        # container - see apps/emby.py.
        "extra_fields": [
            {"name": "data_dir", "label": "Data folder", "type": "text", "required": True,
             "placeholder": "/mnt/emby",
             "help": "Emby's /config folder as mounted here; read-write to restore"},
        ],
        "restore_warning": {"lead": "Stop Emby first", "text": "- the restore is refused while it's running. It replaces Emby's databases and settings with the backup's - users, watch history, playlists, collections and API keys. Anything added since is lost. Start Emby again afterwards."},
    },
    {
        "id": "plex",
        "label": "Plex",
        "icon": "plex.svg",
        "key_required": True,
        "key_label": "Plex token",
        "url_placeholder": "http://plex:32400",
        # The database comes from Plex's API; the server's settings and
        # identity (Preferences.xml) from its mounted data folder - see
        # apps/plex.py.
        "extra_fields": [
            {"name": "data_dir", "label": "Data folder", "type": "text", "required": True,
             "placeholder": "/mnt/plex",
             "help": "Plex's \"Plex Media Server\" folder as mounted here; read-write to restore"},
        ],
        "restore_warning": {"lead": "Stop Plex first", "text": "- the restore is refused while it's running. It replaces Plex's library database and settings with the backup's - libraries, playlists, collections, every user's watched state, and the server's identity, so a rebuilt server comes back as the same server with its shared users. Anything added since is lost. Never run the original server at the same time. Start Plex again afterwards."},
    },
    {
        "id": "seerr",
        "label": "Seerr",
        "icon": "seerr.svg",
        "key_required": True,
        "url_placeholder": "http://seerr:5055",
        "key_help": "Settings > General > API Key",
        # Seerr has no backup API, so its config folder is mounted into this
        # container - see apps/seerr.py.
        "extra_fields": [
            {"name": "data_dir", "label": "Data folder", "type": "text", "required": True,
             "placeholder": "/mnt/seerr",
             "help": "Seerr's /app/config folder as mounted here; read-write to restore"},
        ],
        "restore_warning": {"lead": "Stop Seerr first", "text": "- the restore is refused while it's running. It replaces Seerr's database and settings with the backup's - users, requests, issues, the media server and Radarr/Sonarr connections, notifications and the API key. Anything added since is lost. Start Seerr again afterwards."},
    },
    {
        "id": "lazylibrarian",
        "label": "LazyLibrarian",
        "icon": "lazylibrarian.png",
        "key_required": True,
        "url_placeholder": "http://lazylibrarian:5299",
        "key_help": "Config > Interface > API > API Key",
        # LazyLibrarian's backup API only says where it wrote the file, so its
        # data folder is mounted into this container - see apps/lazylibrarian.py.
        "extra_fields": [
            {"name": "data_dir", "label": "Data folder", "type": "text", "required": True,
             "placeholder": "/mnt/lazylibrarian",
             "help": "LazyLibrarian's /config folder as mounted here; read-write to restore"},
        ],
        "restore_warning": {"lead": "Stop LazyLibrarian first", "text": "- the restore is refused while it's running. It replaces LazyLibrarian's database and settings with the backup's, its API key included. Anything added since is lost. Start LazyLibrarian again afterwards."},
    },
]
for _meta in APP_META:
    _meta["discoverable"] = _meta["id"] in DISCOVERABLE_APPS

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
    "dropbox": {"enabled": False, "token": ""},
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
                    "text": "On the Audience tab, add your own Google account (and anyone else who should have access) under Test users - this keeps the app private with no Google review needed. Leave Publishing status as Testing for now; the next step changes that.",
                    "link": {"label": "Google Auth Platform - Audience", "url": "https://console.cloud.google.com/auth/audience"},
                },
                {
                    "text": "Still on the Audience tab, click \"Publish App\" under Publishing status and confirm. This is the important step: while a project stays in Testing, Google auto-expires every refresh token 7 days after it's issued, so Backuparr's Google Drive connection (and every other Testing-status app) would silently die and need reconnecting weekly. Publishing removes that 7-day limit. It does not require Google's review/verification process here, since drive.file is a non-sensitive scope and this app stays well under Google's 100-user threshold for requiring it - clicking Publish App just flips the status immediately.",
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
                    "text": "Leave Application restrictions set to None. Google's own docs say restricting by Websites works if you also allow https://docs.google.com/* (the picker itself runs in an iframe hosted there) - but in practice this can still fail with a cryptic \"The API developer key is invalid\" error, since the picker's internal request doesn't always carry a referrer your browser will send. The API restriction above already limits this key to the Picker API, so an unrestricted key here is a reasonable tradeoff.",
                    "link": None,
                },
                {
                    "text": "Copy the API key, then paste the Client ID, Client Secret, and API key into the fields below, save, then click \"Connect Google Drive\". Since the app is published but unverified, Google shows a \"Google hasn't verified this app\" warning on the consent screen - that's expected for your own app; click Advanced, then \"Go to (your app name) (unsafe)\" to continue.",
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
        "description": "Backed up to a Backuparr folder in your Dropbox. Uses rclone's own built-in Dropbox app - no Dropbox developer account, no app registration.",
        "setup_help": {
            "title": "Connect Dropbox",
            "intro": "Uses rclone's own built-in Dropbox app, so there's no app to register - just run one command on any computer with a browser (it doesn't need to be this server).",
            "steps": [
                {
                    "text": "Download rclone (a single binary, no install needed) on any computer with a web browser - your own laptop is fine, it doesn't have to be wherever Backuparr runs.",
                    "link": {"label": "Download rclone", "url": "https://rclone.org/downloads/"},
                },
                {
                    "text": "Open a terminal there and run:",
                    "code": "rclone authorize dropbox",
                    "link": None,
                },
                {
                    "text": "It prints a link - open it in your browser, sign in to Dropbox, and approve access.",
                    "link": None,
                },
                {
                    "text": "Back in the terminal, rclone prints a block starting with \"Paste the following into your remote machine --->\". Copy that whole block (or just the line in the middle - either works) and paste it below, then click \"Connect Dropbox\".",
                    "link": None,
                },
                {
                    "text": "Backups are written to a folder named Backuparr in your Dropbox, created on the first backup.",
                    "link": None,
                },
            ],
            "links": [
                {"label": "rclone downloads", "url": "https://rclone.org/downloads/"},
                {"label": "rclone's Dropbox docs", "url": "https://rclone.org/dropbox/"},
                {"label": "rclone's remote setup docs", "url": "https://rclone.org/remote_setup/"},
            ],
        },
    },
]

DEFAULTS = {
    "request_headers": [],
    "retention_days": 7,
    "cron_schedule": "0 3 * * *",
    "notify_url": "",
    "bazarr_backup_dir": "",
    # Ids of removed extra instances. Their backups are kept, so an id is
    # never given to another instance.
    "removed_instances": [],
    # Back up Backuparr's own configuration at the end of each full run (#110).
    "self_backup": True,
    # Whether the user has confirmed saving the encryption key; until then
    # the web UI shows it and allows nothing else. Set by Backuparr only.
    "secrets_key_acknowledged": False,
    "apps": {name: dict(DEFAULT_APP) for name in APP_NAMES},
    "destinations": {name: dict(DEFAULT_DEST[name]) for name in DEST_NAMES},
}

_key_acknowledged = False


def secrets_key_acknowledged():
    """Checked on every API request, so it reads only this one field and
    remembers a yes."""
    global _key_acknowledged
    if not _key_acknowledged:
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                _key_acknowledged = json.load(f).get("secrets_key_acknowledged") is True
        except (OSError, ValueError, AttributeError):
            return False
    return _key_acknowledged


def holds_settings():
    """True once Backuparr holds stored secrets, or its key was confirmed
    saved. A lost login is then only replaced with the encryption key (#123):
    without it, deleting auth.json would hand those secrets to whoever sets
    up next. Read from the file itself, not the cached flag; an unreadable
    config.json counts as holding settings."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if secrets_crypto.PREFIX in raw:
        return True
    try:
        return json.loads(raw).get("secrets_key_acknowledged") is True
    except (ValueError, AttributeError):
        return True


def acknowledge_secrets_key():
    global _key_acknowledged
    cfg = load_config()
    cfg["secrets_key_acknowledged"] = True
    save_config(cfg)
    _key_acknowledged = True


def forget_secrets_key_acknowledged():
    """Reset: the next install's key has to be saved again."""
    global _key_acknowledged
    _key_acknowledged = False


def instance_type(instance_id):
    """The app an instance id runs ("radarr" for radarr-4k), or None if
    it isn't a well-formed instance id."""
    if instance_id in APP_NAMES:
        return instance_id
    if not isinstance(instance_id, str) or len(instance_id) > _MAX_INSTANCE_ID_LENGTH:
        return None
    match = _EXTRA_INSTANCE_ID.fullmatch(instance_id)
    return match.group(1) if match else None


def instance_ids(cfg):
    """Every configured instance id, each app's extra instances right after
    its first one."""
    extras = {}
    for instance_id in cfg["apps"]:
        if instance_id not in APP_NAMES and instance_type(instance_id):
            extras.setdefault(instance_type(instance_id), []).append(instance_id)
    return [i for name in APP_NAMES for i in [name, *sorted(extras.get(name, []))]]


def _secret_fields(cfg):
    """(ref, container_dict, key) for every value encrypted at rest - an
    explicit allowlist, not "encrypt everything". `ref` names the field the
    same way across saves, so adding or removing an instance never lines up
    one field's stored value with another."""
    fields = []
    for instance_id in instance_ids(cfg):
        app = cfg["apps"][instance_id]
        fields.append((("apps", instance_id, "api_key"), app, "api_key"))
        if instance_type(instance_id) in ("bazarr", "nzbget"):
            fields.append((("apps", instance_id, "password"), app, "password"))
    for dest, key in (("gdrive", "client_secret"), ("gdrive", "developer_key"), ("gdrive", "refresh_token"),
                      ("onedrive", "token"), ("dropbox", "token")):
        fields.append((("destinations", dest, key), cfg["destinations"][dest], key))
    fields.append((("notify_url",), cfg, "notify_url"))
    rows = cfg.get("request_headers", [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("value"), str) for row in rows):
        raise ConfigError("request_headers must be a list of name/value pairs")
    # A header row is the same field only at the same position with the same name.
    fields.extend((("request_headers", index, row.get("name")), row, "value") for index, row in enumerate(rows))
    return fields


def _merge_defaults(data):
    merged = copy.deepcopy(DEFAULTS)
    for key, value in data.items():
        if key not in ("apps", "destinations"):
            merged[key] = value
    apps = data.get("apps", {})
    for name in APP_NAMES:
        merged["apps"][name].update(apps.get(name, {}))
    for instance_id, app_data in apps.items():
        if instance_id not in APP_NAMES and instance_type(instance_id):
            merged["apps"][instance_id] = {**DEFAULT_INSTANCE, **app_data}
    for name in DEST_NAMES:
        merged["destinations"][name].update(data.get("destinations", {}).get(name, {}))
    return merged


def _stored_secret_fields():
    """The secret fields as config.json holds them now (still encrypted),
    by _secret_fields() ref, or {} if it can't be read."""
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {ref: (container, key) for ref, container, key in _secret_fields(_merge_defaults(data))}
    except (OSError, ValueError, ConfigError, AttributeError, TypeError):
        return {}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        cfg = copy.deepcopy(DEFAULTS)
        save_config(cfg)
        return cfg

    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{CONFIG_PATH} isn't valid JSON ({exc.msg}, line {exc.lineno}) - fix it, or delete the file to start over with defaults"
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{CONFIG_PATH} should hold a JSON object - fix it, or delete the file to start over with defaults")

    merged = _merge_defaults(data)

    # Decrypt in place; a legacy plaintext value gets re-saved encrypted.
    needs_migration = False
    for _ref, container, key in _secret_fields(merged):
        raw = container.get(key, "")
        if raw and not raw.startswith(secrets_crypto.PREFIX):
            needs_migration = True
        container[key] = secrets_crypto.decrypt(raw)

    try:
        validate_request_headers(merged["request_headers"], instance_ids(merged))
    except RequestHeadersError as exc:
        raise ConfigError(str(exc)) from None
    if needs_migration:
        save_config(merged)

    return merged


def save_config(cfg):
    try:
        validate_request_headers(cfg.get("request_headers", []), instance_ids(cfg))
    except RequestHeadersError as exc:
        raise ConfigError(str(exc)) from None
    to_write = copy.deepcopy(cfg)
    stored = _stored_secret_fields()
    kept = 0
    for ref, container, key in _secret_fields(to_write):
        value = container.get(key, "")
        old = stored.get(ref)
        # A secret the current key can't decrypt loads as "" - saving that
        # would erase it for good. Keep it encrypted as it is, unless a new
        # value was entered (or a header row was replaced).
        if not value and old is not None and secrets_crypto.undecryptable(old[0].get(old[1], "")):
            container[key] = old[0][old[1]]
            kept += 1
            continue
        container[key] = secrets_crypto.encrypt(value)
    if kept:
        log.warning("kept %d stored secret(s) that the current encryption key can't decrypt - "
                    "restore the original key (secrets.key or BACKUPARR_SECRETS_KEY) to use them again", kept)

    atomic_file.write_private(CONFIG_PATH, json.dumps(to_write, indent=2))


def enabled_apps(cfg):
    """Enabled instance ids, e.g. ["radarr", "radarr-4k", "sonarr"]."""
    return [name for name in instance_ids(cfg) if cfg["apps"][name].get("enabled")]


# These take an app or an instance id: an instance shares its app's metadata.
def key_required(name):
    return next((m["key_required"] for m in APP_META if m["id"] == instance_type(name)), True)


def restore_supported(name):
    return next((m.get("restore_supported", True) for m in APP_META if m["id"] == instance_type(name)), True)


def app_meta(name):
    return next((m for m in APP_META if m["id"] == instance_type(name)), None)


def enabled_destinations(cfg):
    return [name for name in DEST_NAMES if cfg["destinations"][name].get("enabled")]

